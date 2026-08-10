"""Alerting and Anomaly Detection reads — result-shaping only.

Covers list_monitors, get_alerts, list_detectors and get_anomaly_results. Both
seams are stubbed: get_alerts is the only one of the four that goes through
`_get`.
"""

import pytest

from lib.client import MAX_SEARCH_LIMIT

# ── list_monitors ─────────────────────────────────────────────────────────────


def test_list_monitors_summarises_the_nested_monitor_object(client, stub):
    stub(
        {
            "hits": {
                "hits": [
                    {
                        "_id": "mon-1",
                        "_source": {
                            "monitor": {
                                "name": "Brute force",
                                "enabled": True,
                                "monitor_type": "query_level_monitor",
                                "schedule": {"period": {"interval": 5, "unit": "MINUTES"}},
                                "inputs": ["ignored"],
                            }
                        },
                    }
                ]
            }
        }
    )
    assert client.list_monitors() == [
        {
            "id": "mon-1",
            "name": "Brute force",
            "enabled": True,
            "type": "query_level_monitor",
            "schedule": {"period": {"interval": 5, "unit": "MINUTES"}},
        }
    ]


def test_list_monitors_falls_back_to_a_flat_source(client, stub):
    """Some Alerting versions return the monitor fields at the top level of
    `_source` instead of nested under a `monitor` key."""
    stub(
        {
            "hits": {
                "hits": [
                    {
                        "_id": "mon-2",
                        "_source": {
                            "name": "Flat",
                            "enabled": False,
                            "monitor_type": "bucket_level_monitor",
                        },
                    }
                ]
            }
        }
    )
    assert client.list_monitors() == [
        {
            "id": "mon-2",
            "name": "Flat",
            "enabled": False,
            "type": "bucket_level_monitor",
            "schedule": None,
        }
    ]


def test_list_monitors_missing_fields_become_none(client, stub):
    stub({"hits": {"hits": [{"_id": "mon-3", "_source": {}}]}})
    assert client.list_monitors() == [
        {"id": "mon-3", "name": None, "enabled": None, "type": None, "schedule": None}
    ]


def test_list_monitors_no_hits_yields_empty_list(client, stub):
    stub({"hits": {"hits": []}})
    assert client.list_monitors() == []


def test_list_monitors_missing_hits_envelope_yields_empty_list(client, stub):
    stub({})
    assert client.list_monitors() == []


def test_list_monitors_posts_a_match_all_to_the_alerting_search_endpoint(client, stub):
    rec = stub({"hits": {"hits": []}})
    client.list_monitors(size=7)
    assert rec.path == "/_plugins/_alerting/monitors/_search"
    assert rec.body == {"size": 7, "query": {"match_all": {}}}


def test_list_monitors_default_size_is_fifty(client, stub):
    rec = stub({"hits": {"hits": []}})
    client.list_monitors()
    assert rec.body["size"] == 50


# ── get_alerts ────────────────────────────────────────────────────────────────

ALERT = {
    "alert_id": "alert-1",
    "monitor_name": "Brute force",
    "trigger_name": "more than 5",
    "state": "ACTIVE",
    "severity": "1",
    "start_time": "2024-01-01T00:00:00Z",
    "last_notification_time": "2024-01-01T00:05:00Z",
    "acknowledged_time": None,
    "error_message": "dropped by the projection",
}


def test_get_alerts_projects_the_documented_fields(client, stub):
    stub({"totalAlerts": 1, "alerts": [ALERT]}, on="_get")
    result = client.get_alerts()
    assert result["total"] == 1
    assert result["alerts"] == [
        {
            "id": "alert-1",
            "monitor_name": "Brute force",
            "trigger_name": "more than 5",
            "state": "ACTIVE",
            "severity": "1",
            "start_time": "2024-01-01T00:00:00Z",
            "last_notification_time": "2024-01-01T00:05:00Z",
            "acknowledged_time": None,
        }
    ]


def test_get_alerts_id_falls_back_from_alert_id_to_id(client, stub):
    stub({"alerts": [{"alert_id": "a"}, {"id": "b"}, {}]}, on="_get")
    assert [a["id"] for a in client.get_alerts()["alerts"]] == ["a", "b", None]


def test_get_alerts_total_falls_back_to_the_alert_count(client, stub):
    """`totalAlerts` is absent on some versions; the length is then reported, so
    the total silently becomes "as many as fit in `size`" rather than the true
    count. Recorded — a caller cannot distinguish the two cases."""
    stub({"alerts": [{"id": "a"}, {"id": "b"}]}, on="_get")
    assert client.get_alerts()["total"] == 2


def test_get_alerts_empty_response_yields_zero_and_empty_list(client, stub):
    stub({}, on="_get")
    assert client.get_alerts() == {"total": 0, "alerts": []}


def test_get_alerts_base_query_params(client, stub):
    rec = stub({"alerts": []}, on="_get")
    client.get_alerts()
    assert rec.path == "/_plugins/_alerting/monitors/alerts"
    assert rec.params == {"size": 50, "sortField": "start_time", "sortOrder": "desc"}


def test_get_alerts_state_filter_is_sent_as_alert_state(client, stub):
    rec = stub({"alerts": []}, on="_get")
    client.get_alerts(state="ACKNOWLEDGED")
    assert rec.params["alertState"] == "ACKNOWLEDGED"


def test_get_alerts_monitor_filter_is_sent_as_monitor_id(client, stub):
    rec = stub({"alerts": []}, on="_get")
    client.get_alerts(monitor_id="mon-1")
    assert rec.params["monitorId"] == "mon-1"


@pytest.mark.parametrize("falsy", [None, ""])
def test_get_alerts_falsy_filters_are_omitted_entirely(client, stub, falsy):
    rec = stub({"alerts": []}, on="_get")
    client.get_alerts(state=falsy, monitor_id=falsy)
    assert "alertState" not in rec.params
    assert "monitorId" not in rec.params


def test_get_alerts_size_is_capped_and_says_so(client, stub):
    """`size` shares the search limit, because these are whole records rather than
    aggregation buckets. The cap has to be announced: silently returning 200 of
    10,000 alerts is how an agent concludes it has seen everything and stops."""
    rec = stub({"alerts": []}, on="_get")
    out = client.get_alerts(size=10_000)
    assert rec.params["size"] == MAX_SEARCH_LIMIT
    assert "size capped at 200" in out["warning"]
    assert "OPENSEARCH_MAX_SEARCH_LIMIT" in out["warning"]


def test_get_alerts_size_within_the_cap_is_not_warned_about(client, stub):
    """Off point for the cap boundary: at the limit exactly, nothing was clamped, so
    a warning would be noise the agent has to reason about."""
    rec = stub({"alerts": []}, on="_get")
    out = client.get_alerts(size=MAX_SEARCH_LIMIT)
    assert rec.params["size"] == MAX_SEARCH_LIMIT
    assert "warning" not in out


# ── list_detectors ────────────────────────────────────────────────────────────


def test_list_detectors_summarises_each_hit(client, stub):
    stub(
        {
            "hits": {
                "hits": [
                    {
                        "_id": "det-1",
                        "_source": {
                            "name": "Traffic spike",
                            "description": "bytes out",
                            "indices": ["wazuh-alerts-*"],
                            "detection_interval": {
                                "period": {"interval": 10, "unit": "Minutes"}
                            },
                            "feature_attributes": ["dropped"],
                        },
                    }
                ]
            }
        }
    )
    assert client.list_detectors() == [
        {
            "id": "det-1",
            "name": "Traffic spike",
            "description": "bytes out",
            "indices": ["wazuh-alerts-*"],
            "detection_interval": {"period": {"interval": 10, "unit": "Minutes"}},
        }
    ]


def test_list_detectors_missing_fields_become_none(client, stub):
    stub({"hits": {"hits": [{"_id": "det-2", "_source": {}}]}})
    assert client.list_detectors() == [
        {
            "id": "det-2",
            "name": None,
            "description": None,
            "indices": None,
            "detection_interval": None,
        }
    ]


def test_list_detectors_no_hits_yields_empty_list(client, stub):
    stub({"hits": {"hits": []}})
    assert client.list_detectors() == []


def test_list_detectors_missing_hits_envelope_yields_empty_list(client, stub):
    stub({})
    assert client.list_detectors() == []


def test_list_detectors_posts_a_match_all_to_the_ad_search_endpoint(client, stub):
    rec = stub({"hits": {"hits": []}})
    client.list_detectors(size=3)
    assert rec.path == "/_plugins/_anomaly_detection/detectors/_search"
    assert rec.body == {"size": 3, "query": {"match_all": {}}}


# ── get_anomaly_results ───────────────────────────────────────────────────────

ANOMALY_SOURCE = {
    "detector_id": "det-1",
    "anomaly_grade": 0.87,
    "confidence": 0.99,
    "data_start_time": 1704067200000,
    "data_end_time": 1704067800000,
    "anomaly_score": "dropped by the projection",
}


def test_get_anomaly_results_projects_the_documented_fields(client, stub):
    stub({"hits": {"total": {"value": 1}, "hits": [{"_source": ANOMALY_SOURCE}]}})
    result = client.get_anomaly_results()
    assert result["total"] == 1
    assert result["anomalies"] == [
        {
            "detector_id": "det-1",
            "anomaly_grade": 0.87,
            "confidence": 0.99,
            "data_start_time": 1704067200000,
            "data_end_time": 1704067800000,
        }
    ]


def test_get_anomaly_results_total_dict_shape_is_unwrapped(client, stub):
    stub({"hits": {"total": {"value": 512, "relation": "eq"}, "hits": []}})
    assert client.get_anomaly_results()["total"] == 512


def test_get_anomaly_results_total_int_shape_is_passed_through(client, stub):
    stub({"hits": {"total": 512, "hits": []}})
    assert client.get_anomaly_results()["total"] == 512


def test_get_anomaly_results_total_missing_defaults_to_zero(client, stub):
    stub({"hits": {"hits": []}})
    assert client.get_anomaly_results()["total"] == 0


def test_get_anomaly_results_empty_response(client, stub):
    stub({})
    out = client.get_anomaly_results(from_ts="2024-01-01T00:00:00Z", to_ts="2024-01-02T00:00:00Z")
    assert out == {"total": 0, "anomalies": []}


def test_get_anomaly_results_hit_without_source_yields_all_none(client, stub):
    stub({"hits": {"total": 1, "hits": [{"_id": "x"}]}})
    assert client.get_anomaly_results()["anomalies"] == [
        {
            "detector_id": None,
            "anomaly_grade": None,
            "confidence": None,
            "data_start_time": None,
            "data_end_time": None,
        }
    ]


def test_get_anomaly_results_sorts_by_grade_descending(client, stub):
    rec = stub({"hits": {"hits": []}})
    client.get_anomaly_results()
    assert rec.path == "/_plugins/_anomaly_detection/detectors/results/_search"
    assert rec.body["sort"] == [{"anomaly_grade": {"order": "desc"}}]


# min_grade boundary. The guard is `if min_grade and min_grade > 0`, an equality
# boundary at 0: the on point 0.0 takes the else branch, and the two off points
# either side land on opposite branches.


def test_min_grade_on_point_zero_filters_grade_strictly_greater_than_zero(
    client, stub
):
    rec = stub({"hits": {"hits": []}})
    client.get_anomaly_results(min_grade=0.0)
    assert rec.body["query"]["bool"]["filter"] == [
        {"range": {"anomaly_grade": {"gt": 0}}}
    ]


def test_min_grade_off_point_just_above_zero_uses_gte(client, stub):
    rec = stub({"hits": {"hits": []}})
    client.get_anomaly_results(min_grade=0.001)
    assert rec.body["query"]["bool"]["filter"] == [
        {"range": {"anomaly_grade": {"gte": 0.001}}}
    ]


def test_min_grade_off_point_just_below_zero_falls_back_to_gt_zero(client, stub):
    """A negative grade is meaningless, and the `> 0` half of the guard sends it
    down the default branch rather than building a nonsense filter. Recorded: it
    is silently ignored rather than rejected."""
    rec = stub({"hits": {"hits": []}})
    client.get_anomaly_results(min_grade=-0.001)
    assert rec.body["query"]["bool"]["filter"] == [
        {"range": {"anomaly_grade": {"gt": 0}}}
    ]


def test_min_grade_above_one_is_accepted_and_matches_nothing(client, stub):
    """anomaly_grade is defined on [0, 1]; there is no upper-bound validation, so
    min_grade=2 builds a filter that can never match. Recorded gap."""
    rec = stub({"hits": {"hits": []}})
    client.get_anomaly_results(min_grade=2)
    assert rec.body["query"]["bool"]["filter"] == [
        {"range": {"anomaly_grade": {"gte": 2}}}
    ]


def test_detector_id_filter_precedes_the_grade_filter(client, stub):
    rec = stub({"hits": {"hits": []}})
    client.get_anomaly_results(detector_id="det-1")
    assert rec.body["query"]["bool"]["filter"] == [
        {"term": {"detector_id": "det-1"}},
        {"range": {"anomaly_grade": {"gt": 0}}},
    ]


def test_time_bounds_filter_on_data_end_time_with_an_explicit_format(client, stub):
    rec = stub({"hits": {"hits": []}})
    client.get_anomaly_results(from_ts="2024-01-01", to_ts="2024-01-02")
    assert rec.body["query"]["bool"]["filter"][-1] == {
        "range": {
            "data_end_time": {
                "gte": "2024-01-01",
                "lte": "2024-01-02",
                "format": "strict_date_optional_time",
            }
        }
    }


def test_from_ts_only_omits_lte(client, stub):
    rec = stub({"hits": {"hits": []}})
    client.get_anomaly_results(from_ts="2024-01-01")
    rng = rec.body["query"]["bool"]["filter"][-1]["range"]["data_end_time"]
    assert rng == {"gte": "2024-01-01", "format": "strict_date_optional_time"}


def test_to_ts_only_omits_gte(client, stub):
    rec = stub({"hits": {"hits": []}})
    client.get_anomaly_results(to_ts="2024-01-02")
    rng = rec.body["query"]["bool"]["filter"][-1]["range"]["data_end_time"]
    assert rng == {"lte": "2024-01-02", "format": "strict_date_optional_time"}


def test_neither_bound_adds_no_time_filter(client, stub):
    rec = stub({"hits": {"hits": []}})
    client.get_anomaly_results()
    assert len(rec.body["query"]["bool"]["filter"]) == 1


def test_get_anomaly_results_warns_about_a_missing_time_range(client, stub):
    """`search_string`, `count` and `timeline` all warn when unbounded; this was the
    one read that scanned the full AD result index in silence. Consistency matters
    more than the individual warning: an agent that learns "no warning means bounded"
    from three tools will believe it from the fourth."""
    stub({"hits": {"hits": []}})
    out = client.get_anomaly_results()
    assert set(out) == {"total", "warning", "anomalies"}
    assert "No time range specified" in out["warning"]


def test_get_anomaly_results_bounded_does_not_warn(client, stub):
    """The other side of the boundary — with both bounds given there is nothing to
    report."""
    stub({"hits": {"hits": []}})
    out = client.get_anomaly_results(
        from_ts="2024-01-01T00:00:00Z", to_ts="2024-01-02T00:00:00Z"
    )
    assert "warning" not in out


def test_get_anomaly_results_size_is_forwarded(client, stub):
    rec = stub({"hits": {"hits": []}})
    client.get_anomaly_results(size=5)
    assert rec.body["size"] == 5
