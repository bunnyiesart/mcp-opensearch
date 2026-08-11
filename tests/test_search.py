"""search_string, count and discover_fields — result-shaping only.

`_post` is stubbed at the seam, so nothing here touches HTTP. What is under
test is purely the translation from an OpenSearch response envelope to the
compact dict the MCP tool returns, plus the request body that was built.

Boundaries (step 4):
  * search_string limit: `capped = min(limit, max_search_limit)`, warn when
    `capped < limit`  <=>  `limit > max_search_limit`.
      on point  = max_search_limit      -> no warning
      off point = max_search_limit + 1  -> warning
  * search_string offset: `max(offset, 0)`. on point 0, off point -1.
  * discover_fields sample_size: capped at MAX_SAMPLE_SIZE (100).
      on point 100 -> no warning, off point 101 -> warning
"""

import pytest

from lib.client import _NO_TIME_RANGE_WARNING, MAX_SAMPLE_SIZE, MAX_SEARCH_LIMIT

from .conftest import make_client, search_response

BOUNDED = {"from_ts": "2024-01-01", "to_ts": "2024-01-02"}


# ── search_string: total normalisation ────────────────────────────────────────


def test_total_dict_shape_is_unwrapped_to_its_value(client, stub):
    stub(search_response(hits=[{"a": 1}], total=4321))
    assert client.search_string("idx", **BOUNDED)["total"] == 4321


def test_total_int_shape_is_passed_through(client, stub):
    """Pre-7.0 / some proxies return a flat integer instead of {"value": N}."""
    stub(search_response(hits=[{"a": 1}], total=7, total_as_int=True))
    assert client.search_string("idx", **BOUNDED)["total"] == 7


def test_total_missing_entirely_defaults_to_zero(client, stub):
    stub({"hits": {"hits": []}})
    assert client.search_string("idx", **BOUNDED)["total"] == 0


def test_total_dict_without_a_value_key_defaults_to_zero(client, stub):
    stub({"hits": {"total": {"relation": "gte"}, "hits": []}})
    assert client.search_string("idx", **BOUNDED)["total"] == 0


def test_hits_envelope_missing_entirely_yields_empty_result(client, stub):
    stub({})
    assert client.search_string("idx", **BOUNDED) == {"total": 0, "ids": [], "hits": []}


# ── search_string: hit extraction ─────────────────────────────────────────────


def test_hits_are_reduced_to_their_source_documents(client, stub):
    stub(search_response(hits=[{"a": 1}, {"b": 2}]))
    result = client.search_string("idx", **BOUNDED)
    assert result["hits"] == [{"a": 1}, {"b": 2}]


def test_hit_without_source_becomes_an_empty_dict_not_a_crash(client, stub):
    """Happens with `_source: false` or stored-fields-only responses."""
    stub({"hits": {"total": {"value": 1}, "hits": [{"_id": "x"}]}})
    assert client.search_string("idx", **BOUNDED)["hits"] == [{}]


def test_metadata_such_as_id_and_score_is_dropped(client, stub):
    stub({"hits": {"total": {"value": 1}, "hits": [{"_id": "x", "_score": 1.0,
                                                   "_source": {"a": 1}}]}})
    assert client.search_string("idx", **BOUNDED)["hits"] == [{"a": 1}]


# ── search_string: request body ───────────────────────────────────────────────


def test_search_targets_the_index_search_endpoint(client, stub):
    rec = stub(search_response())
    client.search_string("wazuh-alerts-*", **BOUNDED)
    assert rec.path == "/wazuh-alerts-*/_search"


def test_sort_defaults_to_the_timestamp_field_descending(client, stub):
    """`unmapped_type` is what stops the sort from 400ing on an index that lacks the
    field — which is the whole reason the documented 403 fallback chain used to be
    circular: `discover_fields` sorts on `@timestamp` by default, so it failed on
    exactly the indices whose mapping the agent could not read."""
    rec = stub(search_response())
    client.search_string("idx", ts_field="@timestamp", **BOUNDED)
    assert rec.body["sort"] == [
        {"@timestamp": {"order": "desc", "unmapped_type": "date"}}
    ]


def test_explicit_sort_field_gets_a_keyword_unmapped_type(client, stub):
    """The unmapped type tracks the field being sorted on, not a constant: sorting on
    the timestamp field declares `date`, anything else declares `keyword`. Declaring
    `date` for `rule.level` would make the degraded case fail differently."""
    rec = stub(search_response())
    client.search_string("idx", sort_field="rule.level", sort_dir="asc", **BOUNDED)
    assert rec.body["sort"] == [
        {"rule.level": {"order": "asc", "unmapped_type": "keyword"}}
    ]


def test_source_fields_are_forwarded_as_source_filtering(client, stub):
    rec = stub(search_response())
    client.search_string("idx", source_fields=["agent.name", "rule.id"], **BOUNDED)
    assert rec.body["_source"] == ["agent.name", "rule.id"]


def test_source_key_is_omitted_when_no_source_fields_are_requested(client, stub):
    rec = stub(search_response())
    client.search_string("idx", **BOUNDED)
    assert "_source" not in rec.body


@pytest.mark.parametrize("empty", [None, []])
def test_falsy_source_fields_are_treated_as_absent(client, stub, empty):
    rec = stub(search_response())
    client.search_string("idx", source_fields=empty, **BOUNDED)
    assert "_source" not in rec.body


# ── search_string: the limit boundary ─────────────────────────────────────────


def test_limit_on_point_at_the_cap_is_not_capped_and_warns_nothing(client, stub):
    rec = stub(search_response())
    result = client.search_string("idx", limit=MAX_SEARCH_LIMIT, **BOUNDED)
    assert rec.body["size"] == MAX_SEARCH_LIMIT
    assert "warning" not in result


def test_limit_off_point_one_over_the_cap_is_capped_and_warns(client, stub):
    rec = stub(search_response())
    over = MAX_SEARCH_LIMIT + 1
    result = client.search_string("idx", limit=over, **BOUNDED)
    assert rec.body["size"] == MAX_SEARCH_LIMIT
    assert f"limit capped at {MAX_SEARCH_LIMIT} (requested {over})" in result["warning"]
    assert "paginate" in result["warning"]


def test_limit_boundary_follows_the_instance_override_not_the_module_constant():
    """A client configured with a lower cap must enforce *its* cap."""
    from .conftest import SeamRecorder

    small = make_client(max_search_limit=5)
    rec = SeamRecorder([search_response()])
    small._post = rec
    assert "warning" not in small.search_string("idx", limit=5, **BOUNDED)
    assert "capped at 5" in small.search_string("idx", limit=6, **BOUNDED)["warning"]
    assert rec.body["size"] == 5


def test_limit_below_the_cap_is_passed_through_untouched(client, stub):
    rec = stub(search_response())
    client.search_string("idx", limit=10, **BOUNDED)
    assert rec.body["size"] == 10


def test_negative_limit_is_clamped_to_zero(client, stub):
    """`offset` was clamped with max(offset, 0) but `limit` was only ever *upper*-
    bounded by min(), so a negative size went to the cluster verbatim and came back
    as a 400 that `_raise_for_status` rendered as the misleading "check your query
    syntax". `size: 0` is a legal request meaning "no documents", which is the closest
    honest reading of a negative one."""
    rec = stub(search_response())
    client.search_string("idx", limit=-5, **BOUNDED)
    assert rec.body["size"] == 0


# ── search_string: the offset boundary ────────────────────────────────────────


def test_offset_on_point_zero(client, stub):
    rec = stub(search_response())
    client.search_string("idx", offset=0, **BOUNDED)
    assert rec.body["from"] == 0


def test_offset_off_point_negative_one_is_clamped_to_zero(client, stub):
    rec = stub(search_response())
    client.search_string("idx", offset=-1, **BOUNDED)
    assert rec.body["from"] == 0


def test_positive_offset_is_forwarded_for_pagination(client, stub):
    rec = stub(search_response())
    client.search_string("idx", offset=200, **BOUNDED)
    assert rec.body["from"] == 200


# ── search_string: the no-time-range warning ──────────────────────────────────


def test_no_time_range_produces_the_full_scan_warning(client, stub):
    stub(search_response())
    assert client.search_string("idx")["warning"] == _NO_TIME_RANGE_WARNING


@pytest.mark.parametrize(
    "bounds",
    [{"from_ts": "2024-01-01"}, {"to_ts": "2024-01-02"}, BOUNDED],
)
def test_any_single_bound_suppresses_the_full_scan_warning(client, stub, bounds):
    stub(search_response())
    assert "warning" not in client.search_string("idx", **bounds)


def test_both_warnings_are_joined_with_a_pipe(client, stub):
    stub(search_response())
    warning = client.search_string("idx", limit=MAX_SEARCH_LIMIT + 1)["warning"]
    assert " | " in warning
    assert "limit capped" in warning
    assert _NO_TIME_RANGE_WARNING in warning


def test_result_key_order_places_hits_last(client, stub):
    """The tool serialises this dict straight to the model; keeping `total` and
    `warning` ahead of the (potentially long) `hits` array means the caller sees
    them before the payload is truncated."""
    stub(search_response(hits=[{"a": 1}]))
    assert list(client.search_string("idx").keys()) == [
        "total",
        "warning",
        "ids",
        "hits",
    ]


# ── count ─────────────────────────────────────────────────────────────────────


def test_count_extracts_the_count_field(client, stub):
    rec = stub({"count": 42, "_shards": {"total": 1}})
    result = client.count("idx", **BOUNDED)
    assert result == {"count": 42}
    assert rec.path == "/idx/_count"


def test_count_missing_field_defaults_to_zero(client, stub):
    stub({})
    assert client.count("idx", **BOUNDED) == {"count": 0}


def test_count_body_carries_only_the_query(client, stub):
    rec = stub({"count": 0})
    client.count("idx", query_string="rule.level:>=10", **BOUNDED)
    assert set(rec.body) == {"query"}
    assert rec.body["query"]["bool"]["must"]["query_string"]["query"] == "rule.level:>=10"


def test_count_warns_when_unbounded(client, stub):
    stub({"count": 5})
    assert client.count("idx") == {"count": 5, "warning": _NO_TIME_RANGE_WARNING}


@pytest.mark.parametrize(
    "bounds",
    [{"from_ts": "2024-01-01"}, {"to_ts": "2024-01-02"}, BOUNDED],
)
def test_count_does_not_warn_when_bounded(client, stub, bounds):
    stub({"count": 5})
    assert "warning" not in client.count("idx", **bounds)


# ── discover_fields ───────────────────────────────────────────────────────────


def test_discover_fields_flattens_and_sorts_field_names(client, stub):
    stub(search_response(hits=[{"z": 1, "agent": {"name": "web01"}}]))
    result = client.discover_fields("idx", **BOUNDED)
    assert list(result) == ["agent.name", "z"]
    assert result == {"agent.name": "str", "z": "int"}


def test_discover_fields_unions_fields_across_all_sampled_hits(client, stub):
    stub(search_response(hits=[{"a": 1}, {"b": "x"}, {"a": 2, "c": None}]))
    assert client.discover_fields("idx", **BOUNDED) == {
        "a": "int",
        "b": "str",
        "c": "NoneType",
    }


def test_discover_fields_last_hit_wins_on_conflicting_types(client, stub):
    """A field that is an int in one document and a str in another is reported
    with whichever type the *last* sampled hit had — the disagreement is not
    surfaced. Recorded: sampling cannot detect mapping conflicts."""
    stub(search_response(hits=[{"f": 1}, {"f": "one"}]))
    assert client.discover_fields("idx", **BOUNDED) == {"f": "str"}


def test_discover_fields_no_hits_yields_empty_dict(client, stub):
    stub(search_response(hits=[]))
    assert client.discover_fields("idx", **BOUNDED) == {}


def test_discover_fields_sample_size_on_point_at_the_cap_does_not_warn(client, stub):
    rec = stub(search_response(hits=[{"a": 1}]))
    result = client.discover_fields("idx", sample_size=MAX_SAMPLE_SIZE, **BOUNDED)
    assert rec.body["size"] == MAX_SAMPLE_SIZE
    assert "_warning" not in result


def test_discover_fields_sample_size_off_point_one_over_the_cap_warns(client, stub):
    rec = stub(search_response(hits=[{"a": 1}]))
    over = MAX_SAMPLE_SIZE + 1
    result = client.discover_fields("idx", sample_size=over, **BOUNDED)
    assert rec.body["size"] == MAX_SAMPLE_SIZE
    assert result["_warning"] == (
        f"sample_size capped at {MAX_SAMPLE_SIZE} (requested {over}). "
        f"Maximum is {MAX_SAMPLE_SIZE}."
    )


def test_discover_fields_default_sample_size_is_ten(client, stub):
    rec = stub(search_response())
    client.discover_fields("idx", **BOUNDED)
    assert rec.body["size"] == 10


def test_discover_fields_forwards_query_and_time_range(client, stub):
    rec = stub(search_response())
    client.discover_fields("idx", query_string="agent.name:web01", **BOUNDED)
    q = rec.body["query"]
    assert q["bool"]["must"]["query_string"]["query"] == "agent.name:web01"
    assert q["bool"]["filter"] == [
        {"range": {"@timestamp": {"gte": "2024-01-01", "lte": "2024-01-02"}}}
    ]


def test_discover_fields_propagates_the_no_time_range_warning(client, stub):
    """`discover_fields` consumed `search_string`'s return value but read only `hits`,
    discarding the full-index-scan warning it had just produced — so unbounded field
    discovery scanned all history in silence while every sibling tool warned. The
    `_warning` key it already used for the sample-size cap has to carry this too, so
    the two are joined rather than one displacing the other."""
    stub(search_response(hits=[{"a": 1}]))
    result = client.discover_fields("idx")  # deliberately unbounded
    assert result["a"] == "int"
    assert _NO_TIME_RANGE_WARNING in result["_warning"]


def test_discover_fields_warning_key_collides_with_a_real_field_named_warning(
    client, stub
):
    """Recorded collision: the synthetic `_warning` entry shares the namespace
    with discovered field names, so a document containing a literal `_warning`
    field is overwritten when the cap also fires."""
    stub(search_response(hits=[{"_warning": "from the document"}]))
    result = client.discover_fields("idx", sample_size=MAX_SAMPLE_SIZE + 1, **BOUNDED)
    assert result["_warning"].startswith("sample_size capped")


# ── hits.total.relation: exact count vs lower bound ───────────────────────────


def test_truncated_total_is_reported_as_a_lower_bound(client, stub):
    """`hits.total.relation == "gte"` means OpenSearch stopped counting.

    Found by running against a live cluster, not by any unit test: a wildcard search
    reported `total: 10000` where `opensearch_count` returned 33,645,389 — a 3,364x
    under-report on the question "how many events match?". OpenSearch stops counting
    at `track_total_hits` (10,000 by default) and says so via `relation`, which the
    code discarded, turning a floor into an apparent exact count.

    Every stub in this suite emitted `relation: "eq"`, which is exactly why the defect
    was invisible here — the fixtures only ever described the case that worked.
    """
    stub({"hits": {"total": {"value": 10000, "relation": "gte"}, "hits": []}})
    out = client.search_string("idx", **BOUNDED)
    assert out["total"] == 10000
    assert "LOWER BOUND" in out["warning"]
    assert "opensearch_count" in out["warning"]


def test_exact_total_carries_no_lower_bound_warning(client, stub):
    """The other side of the boundary: `relation: "eq"` is an exact count, and
    warning about it would train the caller to ignore the warning."""
    stub({"hits": {"total": {"value": 42, "relation": "eq"}, "hits": []}})
    out = client.search_string("idx", **BOUNDED)
    assert out["total"] == 42
    assert "warning" not in out


def test_missing_relation_is_treated_as_exact(client, stub):
    """Absent `relation` means an older OpenSearch that did not truncate. Assuming
    the pessimistic case would warn on every response from such a cluster."""
    stub({"hits": {"total": {"value": 7}, "hits": []}})
    assert "warning" not in client.search_string("idx", **BOUNDED)


def test_flat_integer_total_is_treated_as_exact(client, stub):
    """Pre-7.0 clusters send a bare integer, which has no truncation concept."""
    stub(search_response(hits=[{"a": 1}], total=5, total_as_int=True))
    out = client.search_string("idx", **BOUNDED)
    assert out["total"] == 5
    assert "warning" not in out
