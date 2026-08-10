"""
opensearch MCP server — read-only OpenSearch / OpenSearch Dashboards tools.

Exposes:
  opensearch_test                — connectivity check, shows active backend
  opensearch_cluster_health      — cluster status
  opensearch_list_indices        — all indices with doc count, size, health
  opensearch_list_index_patterns — Dashboards saved index patterns
  opensearch_get_mapping         — flattened field types for an index
  opensearch_discover_fields     — discover fields by sampling documents
  opensearch_search              — Lucene query string search
  opensearch_timeline            — chronological event timeline for one entity across fields
  opensearch_count               — count matching documents
  opensearch_terms               — top N values of a field
  opensearch_multi_terms         — multiple field frequency analyses in one call
  opensearch_histogram           — temporal event count histogram
  opensearch_stats               — numeric stats for a field
  opensearch_ppl                 — PPL (Piped Processing Language) query
  opensearch_api                 — escape hatch: allowlisted read GET endpoints
  opensearch_explain             — explain why a document matches a query
  opensearch_index_settings      — shard count, replicas, ILM policy, refresh interval
  opensearch_list_monitors       — list Alerting-plugin monitors (detection rules)
  opensearch_get_alerts          — fetch alerts raised by Alerting-plugin monitors
  opensearch_list_detectors      — list Anomaly Detection detectors
  opensearch_get_anomaly_results — fetch detected anomalies, most anomalous first
  opensearch_compare             — diff top field values across two time windows

Prompts:
  investigate_alert              — step-by-step single-agent investigation
  top_offenders                  — find top agents, rules, and IPs in a window
  triage_alerts                  — pull active alerts, then pivot on the top entity
  compare_time_windows           — compare alert patterns between two periods

Credentials (env vars or ~/.config/mcp-opensearch/config.json):
  OPENSEARCH_DASHBOARDS_URL  — tried first (e.g. https://opensearch.example.com)
  OPENSEARCH_URL             — direct fallback (e.g. https://opensearch.example.com:9200)
  OPENSEARCH_USERNAME
  OPENSEARCH_PASSWORD
  OPENSEARCH_VERIFY_SSL      — "true"/"false" (default: true)
"""

import logging
import os
import threading

from fastmcp import FastMCP

from lib.client import init_client
from lib.compare import compare_windows

# Configure only this server's logger, never the root logger. basicConfig() on
# the root at import time silenced lib.client's INFO lines — including the only
# two that say which backend was selected — which left an operator debugging a
# backend problem with no log and no latency data. Level is settable so that
# debugging does not require editing the source.
_LOG_LEVEL = os.environ.get("OPENSEARCH_LOG_LEVEL", "WARNING").upper()
logging.basicConfig(level=getattr(logging, _LOG_LEVEL, logging.WARNING))
logger = logging.getLogger("opensearch-mcp")

mcp = FastMCP("opensearch")

# No path guards live in this file. Read-only enforcement is the client's job:
# lib.client._ALLOWED_PATHS is the single allowlist and every request passes
# through OpenSearchClient._check_path, which raises PermissionError with the
# allowed paths in the message. Tools here stay thin delegates.

_client = None
_client_lock = threading.Lock()


def get_client():
    """The shared client, built once.

    FastMCP dispatches synchronous tool functions on a worker thread pool, so the
    plain `if _client is None` check-then-assign was a race: two concurrent first
    calls both saw None, both paid the backend probe, and one of the two
    `requests.Session` objects was silently orphaned. Double-checked locking keeps
    the fast path lock-free after initialisation while making the first call
    exactly-once.
    """
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:          # re-check: another thread may have won
                _client = init_client()
    return _client


# ── Connectivity / Meta ───────────────────────────────────────────────────────

@mcp.tool()
def opensearch_test() -> dict:
    """Call first in every session to confirm connectivity and see the active backend.

    Check the `username` field in the response to explain 403 errors on other tools —
    it shows exactly which role is authenticated. Returns backend ("dashboards" or
    "opensearch"), server version, URL, and username.
    """
    return get_client().test_connection()


@mcp.tool()
def opensearch_cluster_health() -> dict:
    """Requires cluster:monitor/health privilege — if you get 403, use opensearch_test instead.

    Returns cluster status (green/yellow/red), node count, active shards, and
    unassigned shards. Useful to confirm the backend is not degraded before
    trusting query results.
    """
    return get_client().cluster_health()


# ── Index discovery ───────────────────────────────────────────────────────────

@mcp.tool()
def opensearch_list_indices() -> list:
    """If you get 403, use opensearch_list_index_patterns instead (lower privilege requirement).

    Returns all indices sorted by name with doc count, store size, and health.
    Use this to find the exact index name before querying — date-sharded indices
    follow a pattern like wazuh-alerts-4.x-2026.06.24.
    """
    return get_client().list_indices()


@mcp.tool()
def opensearch_list_index_patterns() -> list:
    """Dashboards-only alternative to opensearch_list_indices when _cat/indices access is blocked.

    Returns id, title, and time field name for each index pattern as configured
    in the OpenSearch Dashboards UI. Requires the Dashboards backend to be active.
    """
    return get_client().list_index_patterns()


@mcp.tool()
def opensearch_get_mapping(index: str) -> dict:
    """Use to see all field names and types; if you get 403 use opensearch_discover_fields instead.

    opensearch_discover_fields only requires search privilege (not indices:admin/mappings/get)
    but only returns fields present in sampled documents. Returns nested fields
    flattened to dot-notation, e.g. "rule.level": "integer".

    Args:
        index: Index name or wildcard pattern, e.g. "wazuh-alerts-*".

    Returns:
        {index_name: {field_path: field_type}} for all matched indices.
    """
    return get_client().get_mapping(index)


@mcp.tool()
def opensearch_discover_fields(
    index: str,
    query_string: str = "*",
    from_ts: str | None = None,
    to_ts: str | None = None,
    ts_field: str = "@timestamp",
    sample_size: int = 10,
) -> dict:
    """Fallback for opensearch_get_mapping when the mapping API is blocked; samples live documents.

    Only returns fields that actually appear in the sampled documents — fields absent
    from the sample won't be listed. Unlike opensearch_get_mapping, only requires
    search privilege. Increase sample_size for broader field coverage (max 100).

    Args:
        index: Index name or wildcard pattern.
        query_string: Lucene filter to narrow the sample (default "*").
        from_ts: Sample from this timestamp, UTC ISO 8601 (e.g. "2026-06-01T00:00:00Z").
        to_ts: Sample up to this timestamp, UTC ISO 8601.
        ts_field: Timestamp field name (default "@timestamp").
        sample_size: Number of documents to sample (default 10, max 100).

    Returns:
        {field_name: python_type}
    """
    return get_client().discover_fields(
        index,
        query_string=query_string,
        from_ts=from_ts,
        to_ts=to_ts,
        ts_field=ts_field,
        sample_size=sample_size,
    )


# ── Search ────────────────────────────────────────────────────────────────────

@mcp.tool()
def opensearch_search(
    index: str,
    query_string: str = "*",
    from_ts: str | None = None,
    to_ts: str | None = None,
    ts_field: str = "@timestamp",
    limit: int = 50,
    offset: int = 0,
    sort_field: str | None = None,
    sort_dir: str = "desc",
    source_fields: list | None = None,
) -> dict:
    """Full-document retrieval using Lucene syntax (same as the Dashboards search bar).

    Always pass source_fields — full documents in security indices are large, so an
    unrestricted response consumes a large share of context. Omitting from_ts/to_ts
    scans the full index history; a time range lets OpenSearch skip shards and
    segments outside it and is usually much faster.

    Args:
        index: Index name or wildcard pattern, e.g. "wazuh-alerts-*".
        source_fields: Fields to include, e.g. ["agent.name", "rule.level", "@timestamp"].
                       Strongly recommended — omitting returns all fields.
        query_string: Lucene query, e.g. "rule.level:[12 TO *] AND agent.name:WIN-DC01".
                      Use "*" for all documents.
        from_ts: Start time, UTC ISO 8601, e.g. "2026-06-23T00:00:00Z".
        to_ts: End time, UTC ISO 8601, e.g. "2026-06-24T00:00:00Z".
        ts_field: Timestamp field name (default "@timestamp").
        limit: Max documents to return (default 50, capped at 200 by default —
               see OPENSEARCH_MAX_SEARCH_LIMIT).
        offset: Pagination offset — skip this many documents before returning
                results (default 0). Increment by limit to page: offset=0 → page 1,
                offset=50 → page 2, etc.
        sort_field: Field to sort by (default: ts_field).
        sort_dir: "desc" = newest first (default), "asc" = oldest first.

    Returns:
        {"total": N, "hits": [doc, ...]}
    """
    return get_client().search_string(
        index,
        query_string=query_string,
        from_ts=from_ts,
        to_ts=to_ts,
        ts_field=ts_field,
        limit=limit,
        offset=offset,
        sort_field=sort_field,
        sort_dir=sort_dir,
        source_fields=source_fields,
    )


@mcp.tool()
def opensearch_count(
    index: str,
    query_string: str = "*",
    from_ts: str | None = None,
    to_ts: str | None = None,
    ts_field: str = "@timestamp",
) -> dict:
    """Fastest way to check how many documents match a condition; never returns content.

    Prefer over opensearch_search when you only need the count — it never fills context
    with document data. Without from_ts/to_ts, scans the full index, which can be slow
    on a large one.

    Args:
        index: Index name or wildcard pattern.
        query_string: Lucene query string (default "*" = all documents).
        from_ts: Start time, UTC ISO 8601.
        to_ts: End time, UTC ISO 8601.
        ts_field: Timestamp field name (default "@timestamp").

    Returns:
        {"count": N}
    """
    return get_client().count(
        index,
        query_string=query_string,
        from_ts=from_ts,
        to_ts=to_ts,
        ts_field=ts_field,
    )


@mcp.tool()
def opensearch_timeline(
    index: str,
    entity: str,
    fields: list,
    from_ts: str | None = None,
    to_ts: str | None = None,
    ts_field: str = "@timestamp",
    limit: int = 100,
    source_fields: list | None = None,
    extra_query: str | None = None,
) -> dict:
    """Build a chronological event timeline for a single entity (IP, user, host) across fields.

    The core DFIR pivot: instead of running opensearch_search several times to chase
    one entity through source/destination/agent fields, this matches the entity against
    ALL given fields at once (OR) and returns events oldest-first. Always pass
    source_fields to keep the response small, and a time range to keep it fast.

    Args:
        index: Index name or wildcard pattern, e.g. "wazuh-alerts-*".
        entity: The value to trace, e.g. "10.0.0.5", "WIN-DC01", "jdoe".
        fields: Fields the entity may appear in, e.g.
                ["data.srcip", "data.dstip", "agent.ip", "agent.name"].
        from_ts: Start time, UTC ISO 8601, e.g. "2026-06-23T00:00:00Z".
        to_ts: End time, UTC ISO 8601.
        ts_field: Timestamp field name (default "@timestamp").
        limit: Max events to return, oldest-first (default 100, capped at 200 by
               default — see OPENSEARCH_MAX_SEARCH_LIMIT).
        source_fields: Fields to include per event — strongly recommended, e.g.
                       ["@timestamp", "rule.description", "rule.level", "data.srcip", "data.dstip"].
        extra_query: Optional Lucene filter ANDed with the entity match,
                     e.g. "rule.level:[10 TO *]".

    Returns:
        {"total": N, "entity": str, "fields": [...], "events": [doc, ...]}
    """
    return get_client().timeline(
        index,
        entity,
        fields,
        from_ts=from_ts,
        to_ts=to_ts,
        ts_field=ts_field,
        limit=limit,
        source_fields=source_fields,
        extra_query=extra_query,
    )


# ── Aggregations ──────────────────────────────────────────────────────────────

@mcp.tool()
def opensearch_terms(
    index: str,
    field: str,
    query_string: str = "*",
    from_ts: str | None = None,
    to_ts: str | None = None,
    ts_field: str = "@timestamp",
    size: int = 50,
) -> dict:
    """Frequency table for a keyword field — top N values with their document counts.

    If results look wrong or you see a heap warning, append .keyword to the field name
    (e.g. agent.name.keyword). Never use on analyzed text fields like rule.description
    — aggregations on text fields load fielddata into cluster heap.

    Args:
        index: Index name or wildcard pattern.
        field: Keyword field to aggregate, e.g. "agent.name", "rule.id", "data.srcip".
        query_string: Lucene filter (default "*").
        from_ts: Start time, UTC ISO 8601.
        to_ts: End time, UTC ISO 8601.
        ts_field: Timestamp field name (default "@timestamp").
        size: Number of top values to return (default 50).

    Returns:
        {value: count} sorted by count descending.
    """
    return get_client().terms(
        index,
        field,
        query_string=query_string,
        from_ts=from_ts,
        to_ts=to_ts,
        ts_field=ts_field,
        size=size,
    )


@mcp.tool()
def opensearch_multi_terms(
    index: str,
    aggregations: list,
    query_string: str = "*",
    from_ts: str | None = None,
    to_ts: str | None = None,
    ts_field: str = "@timestamp",
) -> dict:
    """Preferred over calling opensearch_terms in a loop — single round-trip for multiple fields.

    Inherits the .keyword guidance from opensearch_terms: append .keyword to any
    text-like field name to avoid fielddata heap pressure.

    Args:
        index: Index name or wildcard pattern.
        aggregations: List of aggregation specs, each a dict with:
            - id (str): Label for this aggregation in the result.
            - field (str): Keyword field to aggregate.
            - size (int, optional): Top N values (default 50).
          Example: [{"id": "agents",  "field": "agent.name",  "size": 20},
                    {"id": "rules",   "field": "rule.id",      "size": 10},
                    {"id": "sources", "field": "data.srcip",   "size": 30}]
        query_string: Lucene filter (default "*").
        from_ts: Start time, UTC ISO 8601.
        to_ts: End time, UTC ISO 8601.
        ts_field: Timestamp field name (default "@timestamp").

    Returns:
        {id: {value: count}} for each aggregation.
    """
    return get_client().multi_terms(
        index,
        aggregations,
        query_string=query_string,
        from_ts=from_ts,
        to_ts=to_ts,
        ts_field=ts_field,
    )


@mcp.tool()
def opensearch_histogram(
    index: str,
    from_ts: str,
    to_ts: str,
    ts_field: str = "@timestamp",
    interval: str = "1h",
    query_string: str = "*",
) -> dict:
    """Event count over time; always specify from_ts and to_ts (meaningless without a range).

    Use interval="auto" when unsure — it picks ~50 buckets and is always safe.
    Fine intervals over long ranges (e.g. "1m" over a week) are rejected before
    the query runs to protect cluster resources.

    Args:
        index: Index name or wildcard pattern.
        from_ts: Start time, UTC ISO 8601 (required), e.g. "2026-06-23T00:00:00Z".
        to_ts: End time, UTC ISO 8601 (required), e.g. "2026-06-24T00:00:00Z".
        ts_field: Timestamp field name (default "@timestamp").
        interval: Bucket size — e.g. "1h", "30m", "1d", "15m", or "auto".
        query_string: Lucene filter (default "*").

    Returns:
        {"interval_used": str, "results": {timestamp: count}}
    """
    return get_client().histogram(
        index,
        from_ts=from_ts,
        to_ts=to_ts,
        ts_field=ts_field,
        interval=interval,
        query_string=query_string,
    )


@mcp.tool()
def opensearch_stats(
    index: str,
    field: str,
    query_string: str = "*",
    from_ts: str | None = None,
    to_ts: str | None = None,
    ts_field: str = "@timestamp",
) -> dict:
    """Min/max/avg/std for a numeric field. Only works on numeric types (integer, float, long).

    Passing a text field returns a 400 error with a clear message. Use
    opensearch_terms if you want frequency counts for a keyword field instead.

    Args:
        index: Index name or wildcard pattern.
        field: Numeric field, e.g. "rule.level", "data.bytes".
        query_string: Lucene filter (default "*").
        from_ts: Start time, UTC ISO 8601.
        to_ts: End time, UTC ISO 8601.
        ts_field: Timestamp field name (default "@timestamp").

    Returns:
        {count, min, max, avg, sum, std_deviation}
    """
    return get_client().stats(
        index,
        field,
        query_string=query_string,
        from_ts=from_ts,
        to_ts=to_ts,
        ts_field=ts_field,
    )



@mcp.tool()
def opensearch_ppl(query: str) -> dict:
    """Execute a PPL (Piped Processing Language) query against OpenSearch.

    Prefer over opensearch_search when you need multi-step pipeline operations
    (filter → stats → sort) in a single query. Not interchangeable with Lucene —
    different syntax. Returns 404 if the PPL plugin is not installed.

    PPL syntax: source=<index> | <command> [| <command> ...]
    Common commands:
      where <condition>             — filter rows
      stats count() by <field>      — aggregate
      fields <f1>, <f2>             — select columns
      sort -<field>                 — order results (- = descending)
      head <n>                      — limit rows

    Example:
        source=wazuh-alerts-4.x-* | where rule.level > 10
        | stats count() as hits by agent.name | sort -hits | head 20

    Args:
        query: Full PPL query string.

    Returns:
        {"schema": [{"name": str, "type": str}], "datarows": [[values...]]}
    """
    return get_client().ppl(query)


@mcp.tool()
def opensearch_api(path: str) -> dict:
    """Escape hatch for read GET endpoints not covered by other tools.

    Use when you know the OpenSearch REST path but no dedicated tool exists.
    For search/count/terms/histogram use the dedicated tools — they add safety
    guards and better error messages. Only GET is supported, and only paths on
    the server's read-only allowlist: anything else is refused with a message
    listing what is available. Endpoints that could expose credentials (the
    security plugin, snapshot repositories, cluster settings) are never
    reachable, whatever their HTTP method.

    Examples of valid paths:
        /_nodes/stats
        /_cat/plugins?format=json
        /_plugins/_ism/policies
        /_plugins/_ism/policies/hot_rollover_policy
        /_cat/indices/my-index-name
        /my-index/_alias
        /my-index/_shard_stores

    Args:
        path: OpenSearch path starting with "/", e.g. "/_nodes/stats".

    Returns:
        Raw JSON response from OpenSearch. Endpoints that return a JSON array
        (e.g. the _cat/* APIs) are wrapped as {"result": [...]}.
    """
    result = get_client().api_get(path)
    # FastMCP requires structured output to be a dict; wrap array responses
    # (e.g. /_cat/* endpoints) so they don't fail serialization.
    if not isinstance(result, dict):
        return {"result": result}
    return result


@mcp.tool()
def opensearch_explain(
    index: str,
    doc_id: str,
    query_string: str = "*",
) -> dict:
    """Explain why a specific document matches (or doesn't match) a query.

    Use after opensearch_search returns unexpected results and you have a document
    ID. Take doc_id from the "ids" list that opensearch_search and
    opensearch_timeline return: ids[i] is the _id of hits[i], index-aligned and
    always the same length. Do NOT try to request "_id" via source_fields — _id is
    document metadata, not a _source field, so that returns nothing.

    Exact index name only — no wildcards.

    Args:
        index: Exact index name, e.g. "wazuh-alerts-4.x-2026.06.24". No "/".
        doc_id: Document _id, taken from the "ids" list of a prior search. No "/".
        query_string: Lucene query to evaluate against the document (default "*").

    Returns:
        {"matched": bool, "explanation": {...score breakdown...}}
    """
    query = {"query_string": {"query": query_string, "analyze_wildcard": True}}
    return get_client().explain(index, doc_id, query)


@mcp.tool()
def opensearch_index_settings(index: str) -> dict:
    """Get index settings: shard count, replicas, refresh interval, and ILM policy name.

    Use to understand why an index behaves unexpectedly — e.g. slow writes from a
    short refresh interval, data loss risk from zero replicas, or unexpected retention
    from an ILM policy. Prefer opensearch_get_mapping for field schema exploration.

    Args:
        index: Index name or wildcard pattern, e.g. "wazuh-alerts-*".

    Returns:
        {index_name: {number_of_shards, number_of_replicas, refresh_interval,
                       lifecycle_name, creation_date_ms}}

    Note: may require indices:monitor/settings/get privilege. Returns 403 if blocked.
    """
    return get_client().index_settings(index)


# ── Alerting (read-only) ────────────────────────────────────────────────────────

@mcp.tool()
def opensearch_list_monitors(size: int = 50) -> list:
    """List OpenSearch Alerting-plugin monitors (detection rules) and whether they are enabled.

    Use to see what detections exist before investigating why something did — or did not —
    fire. Pair with opensearch_get_alerts to see what those monitors actually raised.
    Requires the Alerting plugin and monitor-search privilege; returns 403/404 otherwise.

    Args:
        size: Max monitors to return (default 50).

    Returns:
        [{"id", "name", "enabled", "type", "schedule"}, ...]
    """
    return get_client().list_monitors(size=size)


@mcp.tool()
def opensearch_get_alerts(
    state: str | None = None,
    monitor_id: str | None = None,
    size: int = 50,
) -> dict:
    """Fetch alerts raised by Alerting-plugin monitors — the "what is firing right now?" tool.

    Start a triage session here: pull ACTIVE alerts, then pivot on the offending entity
    with opensearch_timeline. Requires the Alerting plugin; returns 403/404 otherwise.

    Args:
        state: Filter by state — "ACTIVE", "ACKNOWLEDGED", "COMPLETED", "ERROR".
               Omit for all states.
        monitor_id: Restrict to one monitor (get IDs from opensearch_list_monitors).
        size: Max alerts to return, newest-first (default 50).

    Returns:
        {"total": N, "alerts": [{"id", "monitor_name", "trigger_name", "state",
                                 "severity", "start_time", ...}, ...]}
    """
    return get_client().get_alerts(state=state, monitor_id=monitor_id, size=size)


# ── Anomaly Detection (read-only) ────────────────────────────────────────────────

@mcp.tool()
def opensearch_list_detectors(size: int = 50) -> list:
    """List OpenSearch Anomaly Detection detectors and the indices they watch.

    Use to see what anomaly detectors exist before pulling their results with
    opensearch_get_anomaly_results. Requires the Anomaly Detection plugin and
    detector-search privilege; returns 403/404 otherwise.

    Args:
        size: Max detectors to return (default 50).

    Returns:
        [{"id", "name", "description", "indices", "detection_interval"}, ...]
    """
    return get_client().list_detectors(size=size)


@mcp.tool()
def opensearch_get_anomaly_results(
    detector_id: str | None = None,
    from_ts: str | None = None,
    to_ts: str | None = None,
    min_grade: float = 0.0,
    size: int = 50,
) -> dict:
    """Fetch detected anomalies (beaconing, spikes, rare activity), most anomalous first.

    Surfaces ML-detected anomalies without hand-writing aggregations. Get detector IDs
    from opensearch_list_detectors. Requires the Anomaly Detection plugin; returns
    403/404 otherwise.

    Args:
        detector_id: Restrict to one detector (recommended).
        from_ts: Start time filter on data_end_time, UTC ISO 8601.
        to_ts: End time filter on data_end_time, UTC ISO 8601.
        min_grade: Only return anomalies with anomaly_grade >= this (0-1). Default 0
                   returns all real anomalies (grade > 0). Raise to ~0.7 for high-confidence.
        size: Max anomalies to return, highest grade first (default 50).

    Returns:
        {"total": N, "anomalies": [{"detector_id", "anomaly_grade", "confidence",
                                    "data_start_time", "data_end_time"}, ...]}
    """
    return get_client().get_anomaly_results(
        detector_id=detector_id,
        from_ts=from_ts,
        to_ts=to_ts,
        min_grade=min_grade,
        size=size,
    )


@mcp.tool()
def opensearch_compare(
    index: str,
    field: str,
    baseline_from: str,
    baseline_to: str,
    selection_from: str,
    selection_to: str,
    query_string: str = "*",
    ts_field: str = "@timestamp",
    size: int = 20,
) -> dict:
    """Compare the top values of a field between two time windows.

    Prefer over calling opensearch_terms twice manually — computes the diff and
    percent change automatically. Use to detect new patterns, increased/decreased
    activity, or disappeared sources between a baseline and a selection period.

    Args:
        index: Index name or wildcard pattern.
        field: Keyword field to compare, e.g. "rule.id", "agent.name", "data.srcip".
        baseline_from: Baseline window start, UTC ISO 8601.
        baseline_to: Baseline window end, UTC ISO 8601.
        selection_from: Selection window start, UTC ISO 8601.
        selection_to: Selection window end, UTC ISO 8601.
        query_string: Lucene filter applied to both windows (default "*").
        ts_field: Timestamp field (default "@timestamp").
        size: Top N values to fetch per window (default 20).

    Returns:
        {
          "added":     {value: count},           # in selection, absent in baseline
          "removed":   {value: count},           # in baseline, absent in selection
          "changed":   {value: {"baseline": N, "selection": N,
                                "delta": N, "pct_change": float}},  # sorted by |delta|
          "unchanged": {value: {"baseline": N, "selection": N}},
          "baseline_warning":  str | null,
          "selection_warning": str | null,
        }
    """
    client = get_client()
    window = dict(query_string=query_string, ts_field=ts_field, size=size)
    baseline = client.terms(index, field, from_ts=baseline_from, to_ts=baseline_to, **window)
    selection = client.terms(index, field, from_ts=selection_from, to_ts=selection_to, **window)
    return compare_windows(baseline, selection)


# ── Prompts ───────────────────────────────────────────────────────────────────

@mcp.prompt()
def investigate_alert(
    index: str,
    agent_name: str,
    from_ts: str,
    to_ts: str,
) -> str:
    """Step-by-step investigation guide for a specific agent's alerts in a time window."""
    return f"""You are investigating security alerts for agent '{agent_name}' in index '{index}'.
Time window: {from_ts} to {to_ts}.

Follow these steps in order:

1. Count total alerts:
   opensearch_count(index='{index}', query_string='agent.name:"{agent_name}"', from_ts='{from_ts}', to_ts='{to_ts}')

2. Get alert distribution by rule ID:
   opensearch_terms(index='{index}', field='rule.id', query_string='agent.name:"{agent_name}"', from_ts='{from_ts}', to_ts='{to_ts}', size=20)

3. Get top rule descriptions (for the rule IDs above):
   opensearch_terms(index='{index}', field='rule.description', query_string='agent.name:"{agent_name}"', from_ts='{from_ts}', to_ts='{to_ts}', size=10)

4. Show event timeline:
   opensearch_histogram(index='{index}', from_ts='{from_ts}', to_ts='{to_ts}', query_string='agent.name:"{agent_name}"', interval='auto')

5. Fetch the 5 highest-severity events:
   opensearch_search(index='{index}', query_string='agent.name:"{agent_name}" AND rule.level:[12 TO *]', from_ts='{from_ts}', to_ts='{to_ts}', limit=5, source_fields=['@timestamp','rule.level','rule.description','data.srcip','data.dstip'])

6. Summarize: Is this a known pattern or a spike? Sustained activity or isolated burst?
   Any lateral movement indicators (multiple destination IPs, new agents involved)?
"""


@mcp.prompt()
def top_offenders(
    index: str,
    from_ts: str,
    to_ts: str,
) -> str:
    """Guide to find the top agents, rules, and source IPs in a time window."""
    return f"""Find the top security offenders in index '{index}' from {from_ts} to {to_ts}.

Run these in parallel (they are independent):

- Top agents by alert count:
  opensearch_terms(index='{index}', field='agent.name', from_ts='{from_ts}', to_ts='{to_ts}', size=20)

- Top rules triggered:
  opensearch_terms(index='{index}', field='rule.id', from_ts='{from_ts}', to_ts='{to_ts}', size=20)

- Top source IPs:
  opensearch_terms(index='{index}', field='data.srcip', from_ts='{from_ts}', to_ts='{to_ts}', size=20)

- Top destination IPs:
  opensearch_terms(index='{index}', field='data.dstip', from_ts='{from_ts}', to_ts='{to_ts}', size=20)

- Overall timeline:
  opensearch_histogram(index='{index}', from_ts='{from_ts}', to_ts='{to_ts}', interval='auto')

After collecting results:
1. Identify the single agent with the most alerts — is the count anomalous vs normal?
2. Identify any rule IDs with unusually high counts — look up the rule description.
3. Flag any IP that appears in both source and destination lists (possible pivot point).
4. Note any spikes in the histogram and correlate with the top agents/rules at that time.
"""


@mcp.prompt()
def triage_alerts(index: str) -> str:
    """SOC triage flow: pull active alerts, then pivot on the top offending entity."""
    return f"""Triage the current security alerts, then investigate the most urgent entity.

Step 1 — See what is firing:
  opensearch_get_alerts(state='ACTIVE', size=50)

Step 2 — Understand the detections behind them (only if alerts exist):
  opensearch_list_monitors()
  Map each alert's monitor_name/trigger_name to what the monitor is meant to catch.

Step 3 — Pick the highest-severity / most-frequent alert and identify its entity
  (source IP, host, or user) from the alert or a quick lookup in '{index}'.

Step 4 — Pivot: build a full timeline for that entity across all relevant fields:
  opensearch_timeline(index='{index}', entity='<value>',
      fields=['data.srcip','data.dstip','agent.ip','agent.name'],
      from_ts='<alert start - 1h>', to_ts='<now>',
      source_fields=['@timestamp','rule.level','rule.description','data.srcip','data.dstip'])

Step 5 — Summarize: Is this a true positive? What is the first and last activity for
  this entity? Any lateral movement (multiple destinations) or privilege escalation?
  Recommend acknowledge / escalate / tune-the-rule.
"""


@mcp.prompt()
def compare_time_windows(
    index: str,
    baseline_from: str,
    baseline_to: str,
    selection_from: str,
    selection_to: str,
) -> str:
    """Guide to compare alert patterns between two time periods."""
    return f"""Compare alert patterns in index '{index}' between two periods.

Baseline:  {baseline_from} → {baseline_to}
Selection: {selection_from} → {selection_to}

Step 1 — Get a structured diff for rule IDs:
  opensearch_compare(index='{index}', field='rule.id', baseline_from='{baseline_from}', baseline_to='{baseline_to}', selection_from='{selection_from}', selection_to='{selection_to}', size=50)

Step 2 — Repeat for agent names and source IPs:
  opensearch_compare(index='{index}', field='agent.name', baseline_from='{baseline_from}', baseline_to='{baseline_to}', selection_from='{selection_from}', selection_to='{selection_to}', size=50)
  opensearch_compare(index='{index}', field='data.srcip', baseline_from='{baseline_from}', baseline_to='{baseline_to}', selection_from='{selection_from}', selection_to='{selection_to}', size=50)

Step 3 — For any value in "added" (appeared in selection but not baseline), fetch a sample:
  opensearch_search(index='{index}', query_string='<field>:"<value>"', from_ts='{selection_from}', to_ts='{selection_to}', limit=5, source_fields=['@timestamp','agent.name','rule.level','rule.description'])

Step 4 — For "changed" values with pct_change > 200%, investigate the specific agent or rule:
  opensearch_terms to drill into sub-fields (e.g. what rules is this agent triggering?).

Step 5 — Summarize: new threats? increased activity from known sources? agents that went quiet?
"""


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    mcp.run()


if __name__ == "__main__":
    main()
