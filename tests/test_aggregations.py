"""terms, multi_terms, stats and histogram — result-shaping only.

`_post` is stubbed at the seam. Under test: bucket-list to flat-dict conversion,
the fielddata warning, the empty-aggregation partition, and the aggregation
specs that get built.
"""

import pytest

from lib.client import MAX_TERMS_SIZE

from .conftest import terms_response

# ── terms ─────────────────────────────────────────────────────────────────────


def test_terms_converts_buckets_to_a_value_count_map(client, stub):
    stub(terms_response("top_values", [("web01", 120), ("web02", 45)]))
    assert client.terms("idx", "agent.name") == {"web01": 120, "web02": 45}


def test_terms_preserves_the_descending_order_opensearch_returned(client, stub):
    stub(terms_response("top_values", [("a", 9), ("b", 5), ("c", 1)]))
    assert list(client.terms("idx", "agent.name")) == ["a", "b", "c"]


def test_terms_empty_buckets_yields_empty_dict(client, stub):
    stub(terms_response("top_values", []))
    assert client.terms("idx", "agent.name") == {}


def test_terms_missing_aggregations_envelope_yields_empty_dict(client, stub):
    stub({})
    assert client.terms("idx", "agent.name") == {}


def test_terms_numeric_bucket_keys_stay_numeric(client, stub):
    """Keys are used verbatim, so a numeric field yields int keys — which JSON
    serialisation will render as strings anyway, but Python callers see ints."""
    stub(terms_response("top_values", [(10, 3), (7, 1)]))
    assert client.terms("idx", "rule.level") == {10: 3, 7: 1}


def test_terms_builds_a_size_zero_search_with_a_terms_agg(client, stub):
    rec = stub(terms_response("top_values", []))
    client.terms("idx", "agent.name", size=25, from_ts="2024-01-01")
    assert rec.path == "/idx/_search"
    assert rec.body["size"] == 0
    assert rec.body["aggs"] == {
        "top_values": {"terms": {"field": "agent.name", "size": 25}}
    }


def test_terms_default_size_is_fifty(client, stub):
    rec = stub(terms_response("top_values", []))
    client.terms("idx", "agent.name")
    assert rec.body["aggs"]["top_values"]["terms"]["size"] == 50


def test_terms_adds_the_fielddata_warning_for_a_text_looking_field(client, stub):
    stub(terms_response("top_values", [("kernel panic", 2)]))
    result = client.terms("idx", "rule.description")
    assert "rule.description.keyword" in result["_warning"]
    assert "fielddata" in result["_warning"]


def test_terms_omits_the_warning_for_a_keyword_looking_field(client, stub):
    stub(terms_response("top_values", [("web01", 2)]))
    assert "_warning" not in client.terms("idx", "agent.name")


def test_terms_size_is_capped_and_says_so(client, stub):
    """The guard used to cover only the cheap operations. Search was capped at 200 and
    histograms at 2000 buckets, but a terms agg — the single most reliable way to OOM
    a coordinating node, because the whole bucket set lands in cluster heap — accepted
    any `size` at all."""
    rec = stub(terms_response("top_values", []))
    out = client.terms("idx", "agent.name", size=100_000)
    assert rec.body["aggs"]["top_values"]["terms"]["size"] == MAX_TERMS_SIZE
    assert "size capped at 1,000" in out["_warning"]
    assert "OPENSEARCH_MAX_TERMS_SIZE" in out["_warning"]


def test_terms_size_at_the_cap_is_not_warned_about(client, stub):
    """On point for the cap boundary: nothing was clamped, so no warning."""
    rec = stub(terms_response("top_values", []))
    out = client.terms("idx", "agent.name", size=MAX_TERMS_SIZE)
    assert rec.body["aggs"]["top_values"]["terms"]["size"] == MAX_TERMS_SIZE
    assert "_warning" not in out


def test_terms_warning_key_overwrites_a_bucket_literally_named_warning(client, stub):
    """Recorded collision: `_warning` shares the namespace with bucket keys, so a
    document value of "_warning" is silently replaced by the warning text."""
    stub(terms_response("top_values", [("_warning", 7)]))
    result = client.terms("idx", "rule.description")
    assert result["_warning"].startswith("Field 'rule.description' looks like")
    assert 7 not in result.values()


# ── multi_terms ───────────────────────────────────────────────────────────────


def test_multi_terms_returns_one_map_per_aggregation_id(client, stub):
    stub(
        {
            "aggregations": {
                "hosts": {"buckets": [{"key": "web01", "doc_count": 3}]},
                "ips": {"buckets": [{"key": "1.2.3.4", "doc_count": 9}]},
            }
        }
    )
    result = client.multi_terms(
        "idx",
        [{"id": "hosts", "field": "agent.name"}, {"id": "ips", "field": "data.srcip"}],
    )
    assert result == {"hosts": {"web01": 3}, "ips": {"1.2.3.4": 9}}


def test_multi_terms_builds_one_agg_per_entry_with_per_entry_size(client, stub):
    rec = stub({"aggregations": {}})
    client.multi_terms(
        "idx",
        [
            {"id": "hosts", "field": "agent.name", "size": 5},
            {"id": "ips", "field": "data.srcip"},
        ],
    )
    assert rec.body["aggs"] == {
        "hosts": {"terms": {"field": "agent.name", "size": 5}},
        "ips": {"terms": {"field": "data.srcip", "size": 50}},
    }
    assert rec.body["size"] == 0


def test_multi_terms_makes_exactly_one_request(client, stub):
    """The whole point of the tool: N frequency analyses, one round trip."""
    rec = stub({"aggregations": {}})
    client.multi_terms(
        "idx",
        [{"id": f"a{i}", "field": f"f{i}"} for i in range(5)],
    )
    assert rec.call_count == 1


def test_multi_terms_id_with_no_matching_response_agg_becomes_empty_map(client, stub):
    stub({"aggregations": {"hosts": {"buckets": [{"key": "w", "doc_count": 1}]}}})
    result = client.multi_terms(
        "idx",
        [{"id": "hosts", "field": "agent.name"}, {"id": "missing", "field": "x"}],
    )
    assert result == {"hosts": {"w": 1}, "missing": {}}


def test_multi_terms_empty_buckets_yields_empty_map(client, stub):
    stub({"aggregations": {"hosts": {"buckets": []}}})
    assert client.multi_terms("idx", [{"id": "hosts", "field": "agent.name"}]) == {
        "hosts": {}
    }


@pytest.mark.parametrize("empty", [[], None])
def test_multi_terms_rejects_empty_aggregations(client, stub, empty):
    stub({"aggregations": {}})
    with pytest.raises(ValueError, match="aggregations list must not be empty"):
        client.multi_terms("idx", empty)


def test_multi_terms_rejects_empty_input_before_any_request(client, stub):
    """The precondition must fail fast — no round trip on invalid input."""
    rec = stub({"aggregations": {}})
    with pytest.raises(ValueError):
        client.multi_terms("idx", [])
    assert rec.call_count == 0


def test_multi_terms_warns_once_per_text_looking_field(client, stub):
    stub({"aggregations": {}})
    result = client.multi_terms(
        "idx",
        [
            {"id": "a", "field": "rule.description"},
            {"id": "b", "field": "agent.name"},
            {"id": "c", "field": "data.message"},
        ],
    )
    assert result["_warning"].count(" | ") == 1
    assert "rule.description.keyword" in result["_warning"]
    assert "data.message.keyword" in result["_warning"]
    assert "agent.name" not in result["_warning"]


def test_multi_terms_omits_the_warning_when_all_fields_look_like_keywords(
    client, stub
):
    stub({"aggregations": {}})
    result = client.multi_terms(
        "idx",
        [{"id": "a", "field": "agent.name"}, {"id": "b", "field": "data.srcip"}],
    )
    assert "_warning" not in result


def test_multi_terms_duplicate_ids_are_rejected(client, stub):
    """`aggs` was built by a dict comprehension keyed on `id`, so two specs sharing an
    id silently produced one aggregation (last field wins) and one output key. The
    agent asked two questions, got one answer, received no warning, and had no way to
    tell which field the numbers belonged to — the id is the only label tying a result
    back to its field."""
    stub({"aggregations": {"dup": {"buckets": [{"key": "x", "doc_count": 1}]}}})
    with pytest.raises(ValueError, match="Duplicate aggregation id"):
        client.multi_terms(
            "idx",
            [{"id": "dup", "field": "agent.name"}, {"id": "dup", "field": "data.srcip"}],
        )


@pytest.mark.parametrize("reserved", ["_warning", "_x", "_"])
def test_multi_terms_rejects_ids_starting_with_underscore(client, stub, reserved):
    """The result dict holds aggregation results and metadata in one namespace, and
    the id is caller-supplied — so an agent passing `id="_warning"` got the warning
    string back *instead of* the aggregation it asked for. Verified reachable before
    this guard existed.

    Reserving the prefix closes it without changing the response shape. Restructuring
    the output of `terms`, `multi_terms` and `discover_fields` would have broken every
    consumer in order to handle a name a caller can simply be told not to use.
    """
    stub({"aggregations": {}})
    with pytest.raises(ValueError, match="reserved for response metadata"):
        client.multi_terms("idx", [{"id": reserved, "field": "agent.name"}])


def test_multi_terms_allows_an_underscore_elsewhere_in_the_id():
    """Only the leading underscore is reserved. `agent_name` is a perfectly ordinary
    id and must not be caught by an over-broad rule."""
    from tests.conftest import make_client

    c = make_client()
    c._resolve_backend = lambda: None
    c._post = lambda p, body=None, params=None: {
        "aggregations": {"agent_name": {"buckets": [{"key": "a", "doc_count": 1}]}}
    }
    assert c.multi_terms("idx", [{"id": "agent_name", "field": "agent.name"}]) == {
        "agent_name": {"a": 1}
    }


# ── stats ─────────────────────────────────────────────────────────────────────

FULL_STATS = {
    "count": 10,
    "min": 1.0,
    "max": 15.0,
    "avg": 7.5,
    "sum": 75.0,
    "std_deviation": 3.2,
    "variance": 10.24,
    "sum_of_squares": 700.0,
}


def test_stats_selects_the_six_documented_metrics(client, stub):
    stub({"aggregations": {"field_stats": FULL_STATS}})
    assert client.stats("idx", "rule.level") == {
        "count": 10,
        "min": 1.0,
        "max": 15.0,
        "avg": 7.5,
        "sum": 75.0,
        "std_deviation": 3.2,
    }


def test_stats_drops_variance_and_sum_of_squares(client, stub):
    stub({"aggregations": {"field_stats": FULL_STATS}})
    result = client.stats("idx", "rule.level")
    assert "variance" not in result
    assert "sum_of_squares" not in result


def test_stats_builds_an_extended_stats_agg(client, stub):
    rec = stub({"aggregations": {"field_stats": FULL_STATS}})
    client.stats("idx", "rule.level", from_ts="2024-01-01")
    assert rec.body["size"] == 0
    assert rec.body["aggs"] == {
        "field_stats": {"extended_stats": {"field": "rule.level"}}
    }


def test_stats_missing_aggregation_yields_none_metrics(client, stub):
    """With no aggregation envelope at all, every metric is unknown.

    `count` is genuinely 0 — no documents were counted — but reporting `min: 0` would
    claim a measurement that was never made, and the caller could not distinguish it
    from a real minimum of zero. `None` is the honest answer; see
    test_stats_null_metrics_are_returned_as_none_not_zero for the same contract on the
    zero-document path."""
    stub({})
    assert client.stats("idx", "rule.level") == {
        "count": 0,
        "min": None,
        "max": None,
        "avg": None,
        "sum": None,
        "std_deviation": None,
    }


def test_stats_null_metrics_are_returned_as_none_not_zero(client, stub):
    """NEW BUG (recorded, not fixed here): on a query matching zero documents,
    OpenSearch's extended_stats returns the keys *present* with JSON null. The
    `.get(key, 0)` defaults therefore never fire — a present-but-null key
    returns None — so the caller receives min/max/avg/std_deviation = None
    despite the code reading as though it guarantees numeric zeros. Any consumer
    doing arithmetic or formatting on these hits a TypeError. Fix: use
    `st.get(key) or 0`, or `0 if st.get(key) is None else st[key]`."""
    stub(
        {
            "aggregations": {
                "field_stats": {
                    "count": 0,
                    "min": None,
                    "max": None,
                    "avg": None,
                    "sum": 0.0,
                    "std_deviation": None,
                }
            }
        }
    )
    result = client.stats("idx", "rule.level")
    assert result["count"] == 0
    assert result["min"] is None
    assert result["max"] is None
    assert result["avg"] is None
    assert result["std_deviation"] is None


def test_stats_rewrites_a_bad_request_into_a_non_numeric_field_message(client):
    """The only error-translation branch that is pure enough to unit test: it
    keys off the message _raise_for_status already produced."""

    def boom(path, body=None, params=None):
        raise RuntimeError("Bad request: POST /idx/_search. Check your query syntax.")

    client._post = boom
    with pytest.raises(RuntimeError, match="is not numeric or does not support stats"):
        client.stats("idx", "agent.name")


def test_stats_reraises_unrelated_runtime_errors_unchanged(client):
    def boom(path, body=None, params=None):
        raise RuntimeError("Permission denied: POST /idx/_search.")

    client._post = boom
    with pytest.raises(RuntimeError, match=r"^Permission denied") as exc:
        client.stats("idx", "rule.level")
    assert "not numeric" not in str(exc.value)


# ── histogram ─────────────────────────────────────────────────────────────────

BOUNDED = ("2024-01-01T00:00:00", "2024-01-01T03:00:00")


def bucket(key_as_string, doc_count, key=0):
    return {"key": key, "key_as_string": key_as_string, "doc_count": doc_count}


def test_histogram_maps_key_as_string_to_doc_count(client, stub):
    stub(
        {
            "aggregations": {
                "over_time": {
                    "buckets": [
                        bucket("2024-01-01T00:00:00.000Z", 5),
                        bucket("2024-01-01T01:00:00.000Z", 0),
                        bucket("2024-01-01T02:00:00.000Z", 12),
                    ]
                }
            }
        }
    )
    result = client.histogram("idx", *BOUNDED, interval="1h")
    assert result["results"] == {
        "2024-01-01T00:00:00.000Z": 5,
        "2024-01-01T01:00:00.000Z": 0,
        "2024-01-01T02:00:00.000Z": 12,
    }


def test_histogram_echoes_the_requested_interval(client, stub):
    stub({"aggregations": {"over_time": {"buckets": [bucket("t", 1)]}}})
    assert client.histogram("idx", *BOUNDED, interval="1h")["interval_used"] == "1h"


def test_histogram_empty_buckets_yields_empty_results_map(client, stub):
    stub({"aggregations": {"over_time": {"buckets": []}}})
    assert client.histogram("idx", *BOUNDED, interval="1h") == {
        "interval_used": "1h",
        "results": {},
    }


def test_histogram_missing_aggregations_envelope_yields_empty_results_map(
    client, stub
):
    stub({})
    assert client.histogram("idx", *BOUNDED, interval="1h")["results"] == {}


def test_histogram_falls_back_to_the_raw_epoch_key_when_no_key_as_string(
    client, stub
):
    stub({"aggregations": {"over_time": {"buckets": [{"key": 1704067200000,
                                                     "doc_count": 4}]}}})
    result = client.histogram("idx", *BOUNDED, interval="1h")
    assert result["results"] == {"1704067200000": 4}


def test_histogram_bucket_without_a_key_uses_key_as_string(client, stub):
    """`b.get("key_as_string", str(b["key"]))` evaluated its default eagerly, so
    `b["key"]` was indexed on every bucket even when `key_as_string` was present — and
    a bucket carrying only the label raised KeyError instead of using the label sitting
    right there."""
    stub({"aggregations": {"over_time": {"buckets": [{"key_as_string": "t",
                                                     "doc_count": 1}]}}})
    assert client.histogram("idx", *BOUNDED, interval="1h")["results"] == {"t": 1}


def test_histogram_fixed_interval_spec_includes_extended_bounds_and_min_doc_count(
    client, stub
):
    rec = stub({"aggregations": {"over_time": {"buckets": []}}})
    client.histogram("idx", *BOUNDED, interval="15m")
    assert rec.body["size"] == 0
    assert rec.body["aggs"]["over_time"] == {
        "date_histogram": {
            "field": "@timestamp",
            "fixed_interval": "15m",
            "min_doc_count": 0,
            "extended_bounds": {"min": BOUNDED[0], "max": BOUNDED[1]},
        }
    }


def test_histogram_auto_uses_auto_date_histogram_with_fifty_buckets(client, stub):
    rec = stub({"aggregations": {"over_time": {"buckets": []}}})
    client.histogram("idx", *BOUNDED, interval="auto")
    assert rec.body["aggs"]["over_time"] == {
        "auto_date_histogram": {"field": "@timestamp", "buckets": 50}
    }


def test_histogram_auto_reports_the_interval_opensearch_actually_chose(client, stub):
    stub(
        {
            "aggregations": {
                "over_time": {"interval": "1h", "buckets": [bucket("t", 1)]}
            }
        }
    )
    assert client.histogram("idx", *BOUNDED, interval="auto")["interval_used"] == "1h"


def test_histogram_auto_with_no_buckets_reports_auto(client, stub):
    """The resolution branch is guarded on `buckets` being non-empty, so an empty
    auto histogram keeps the literal "auto" rather than claiming an interval."""
    stub({"aggregations": {"over_time": {"interval": "1h", "buckets": []}}})
    assert client.histogram("idx", *BOUNDED, interval="auto")["interval_used"] == "auto"


def test_histogram_auto_without_an_interval_in_the_response_reports_auto(
    client, stub
):
    stub({"aggregations": {"over_time": {"buckets": [bucket("t", 1)]}}})
    assert client.histogram("idx", *BOUNDED, interval="auto")["interval_used"] == "auto"


def test_histogram_validates_before_dispatching(client, stub):
    """The bucket guard must run before the request, or the cluster does the work
    the guard exists to prevent."""
    rec = stub({"aggregations": {"over_time": {"buckets": []}}})
    with pytest.raises(ValueError, match="Invalid interval"):
        client.histogram("idx", *BOUNDED, interval="5x")
    assert rec.call_count == 0


def test_histogram_uses_a_custom_ts_field_in_both_the_agg_and_the_filter(
    client, stub
):
    rec = stub({"aggregations": {"over_time": {"buckets": []}}})
    client.histogram("idx", *BOUNDED, ts_field="data.timestamp", interval="1h")
    assert rec.body["aggs"]["over_time"]["date_histogram"]["field"] == "data.timestamp"
    assert "data.timestamp" in rec.body["query"]["bool"]["filter"][0]["range"]
