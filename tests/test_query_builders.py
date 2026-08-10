"""_with_time_range and _qs_query — pure query-dict construction.

Spec (step 1):
  _with_time_range(query, from_ts, to_ts, ts_field)
      Wrap `query` in a bool/must + range filter. With no bounds at all, return
      `query` untouched (or match_all when `query` is falsy).
  _qs_query(query_string, from_ts, to_ts, ts_field)
      Build a query_string clause (defaulting to "*") and hand it to the above.

Partitions across the two bound inputs (step 3, axis 2 — combinations):
  neither / from only / to only / both.
Crossed with the `query` partition: a real dict / None / an empty dict.
"""

import pytest

# ── _with_time_range: the four bound combinations ─────────────────────────────

BASE = {"term": {"agent.name": "web01"}}


def test_with_time_range_neither_bound_returns_the_query_unwrapped(client):
    """No bool wrapper at all — the caller's query is passed through as-is."""
    assert client._with_time_range(BASE, None, None, "@timestamp") == BASE


def test_with_time_range_from_only_emits_gte_and_no_lte(client):
    result = client._with_time_range(BASE, "2024-01-01", None, "@timestamp")
    assert result == {
        "bool": {
            "must": BASE,
            "filter": [{"range": {"@timestamp": {"gte": "2024-01-01"}}}],
        }
    }


def test_with_time_range_to_only_emits_lte_and_no_gte(client):
    result = client._with_time_range(BASE, None, "2024-01-02", "@timestamp")
    assert result == {
        "bool": {
            "must": BASE,
            "filter": [{"range": {"@timestamp": {"lte": "2024-01-02"}}}],
        }
    }


def test_with_time_range_both_bounds_emit_gte_and_lte_in_one_clause(client):
    result = client._with_time_range(BASE, "2024-01-01", "2024-01-02", "@timestamp")
    assert result["bool"]["filter"] == [
        {"range": {"@timestamp": {"gte": "2024-01-01", "lte": "2024-01-02"}}}
    ]


def test_with_time_range_uses_the_supplied_ts_field(client):
    result = client._with_time_range(BASE, "2024-01-01", None, "data.timestamp")
    assert "data.timestamp" in result["bool"]["filter"][0]["range"]


# ── _with_time_range: the falsy-query partition ───────────────────────────────


@pytest.mark.parametrize("empty_query", [None, {}])
def test_with_time_range_falsy_query_becomes_match_all_when_bounded(
    client, empty_query
):
    result = client._with_time_range(empty_query, "2024-01-01", None, "@timestamp")
    assert result["bool"]["must"] == {"match_all": {}}


@pytest.mark.parametrize("empty_query", [None, {}])
def test_with_time_range_falsy_query_becomes_match_all_when_unbounded(
    client, empty_query
):
    """Boundary between the two return paths: unbounded still substitutes
    match_all, so the function never returns None or {}."""
    assert client._with_time_range(empty_query, None, None, "@timestamp") == {
        "match_all": {}
    }


@pytest.mark.parametrize(
    ("from_ts", "to_ts"),
    [("", None), (None, ""), ("", "")],
)
def test_with_time_range_empty_string_bounds_count_as_absent(client, from_ts, to_ts):
    """`if not from_ts` treats "" like None, so an empty string does not create a
    half-open range with an empty bound value."""
    assert client._with_time_range(BASE, from_ts, to_ts, "@timestamp") == BASE


def test_with_time_range_does_not_mutate_the_caller_s_query(client):
    original = {"term": {"a": 1}}
    snapshot = {"term": {"a": 1}}
    client._with_time_range(original, "2024-01-01", "2024-01-02", "@timestamp")
    assert original == snapshot


# ── _qs_query ─────────────────────────────────────────────────────────────────


def test_qs_query_unbounded_is_a_bare_query_string_clause(client):
    assert client._qs_query("agent.name:web01", None, None, "@timestamp") == {
        "query_string": {"query": "agent.name:web01", "analyze_wildcard": True}
    }


@pytest.mark.parametrize("falsy", [None, ""])
def test_qs_query_falsy_query_string_defaults_to_match_everything(client, falsy):
    assert client._qs_query(falsy, None, None, "@timestamp")["query_string"][
        "query"
    ] == "*"


def test_qs_query_always_sets_analyze_wildcard(client):
    """Leading-wildcard searches depend on this; a regression would silently
    change result sets rather than error."""
    q = client._qs_query("*evil*", "2024-01-01", "2024-01-02", "@timestamp")
    assert q["bool"]["must"]["query_string"]["analyze_wildcard"] is True


def test_qs_query_from_only(client):
    q = client._qs_query("*", "2024-01-01", None, "@timestamp")
    assert q["bool"]["filter"] == [{"range": {"@timestamp": {"gte": "2024-01-01"}}}]


def test_qs_query_to_only(client):
    q = client._qs_query("*", None, "2024-01-02", "@timestamp")
    assert q["bool"]["filter"] == [{"range": {"@timestamp": {"lte": "2024-01-02"}}}]


def test_qs_query_both(client):
    q = client._qs_query("*", "2024-01-01", "2024-01-02", "ts")
    assert q == {
        "bool": {
            "must": {"query_string": {"query": "*", "analyze_wildcard": True}},
            "filter": [{"range": {"ts": {"gte": "2024-01-01", "lte": "2024-01-02"}}}],
        }
    }


def test_qs_query_does_not_escape_or_validate_the_query_string(client):
    """Recorded contract: the Lucene string is passed through verbatim. Callers
    that interpolate untrusted values must escape them themselves — see
    test_timeline.py for where that obligation is only half met."""
    q = client._qs_query('a:"un"balanced', None, None, "@timestamp")
    assert q["query_string"]["query"] == 'a:"un"balanced'
