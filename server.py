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
  opensearch_count               — count matching documents
  opensearch_terms               — top N values of a field
  opensearch_multi_terms         — multiple field frequency analyses in one call
  opensearch_histogram           — temporal event count histogram
  opensearch_stats               — numeric stats for a field
  opensearch_ppl                 — PPL (Piped Processing Language) query
  opensearch_api                 — escape hatch: any read GET endpoint
  opensearch_explain             — explain why a document matches a query
  opensearch_index_settings      — shard count, replicas, ILM policy, refresh interval
  opensearch_compare             — diff top field values across two time windows

Prompts:
  investigate_alert              — step-by-step single-agent investigation
  top_offenders                  — find top agents, rules, and IPs in a window
  compare_time_windows           — compare alert patterns between two periods

Credentials (env vars or ~/.config/mcp-opensearch/config.json):
  OPENSEARCH_DASHBOARDS_URL  — tried first (e.g. https://opensearch.example.com)
  OPENSEARCH_URL             — direct fallback (e.g. https://opensearch.example.com:9200)
  OPENSEARCH_USERNAME
  OPENSEARCH_PASSWORD
  OPENSEARCH_VERIFY_SSL      — "true"/"false" (default: true)
"""

import logging
import re
from typing import Optional

from fastmcp import FastMCP

from lib.client import init_client

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("opensearch-mcp")

mcp = FastMCP("opensearch")

# Path fragments that indicate write or admin operations — blocked by opensearch_api
_WRITE_PATH_FRAGMENTS = frozenset([
    "_delete", "_close", "_bulk", "_update", "_create",
    "_reindex", "_rollover", "_shrink", "_split", "_clone",
    "_open", "_freeze", "_unfreeze", "_forcemerge",
])

# Validates /{index}/_explain/{doc_id} paths for opensearch_explain
_EXPLAIN_PATH_RE = re.compile(r"^/[^/]+/_explain/[^/]+$")

_client = None


def get_client():
    global _client
    if _client is None:
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
    from_ts: Optional[str] = None,
    to_ts: Optional[str] = None,
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
    from_ts: Optional[str] = None,
    to_ts: Optional[str] = None,
    ts_field: str = "@timestamp",
    limit: int = 50,
    offset: int = 0,
    sort_field: Optional[str] = None,
    sort_dir: str = "desc",
    source_fields: Optional[list] = None,
) -> dict:
    """Full-document retrieval using Lucene syntax (same as the Dashboards search bar).

    Always pass source_fields to limit response size — 50 full docs ≈ 237 KB and will
    fill context quickly. Omitting from_ts/to_ts scans the full index history, which is
    slow and expensive; adding a time range reduces query time by up to 15×.

    Args:
        index: Index name or wildcard pattern, e.g. "wazuh-alerts-*".
        source_fields: Fields to include, e.g. ["agent.name", "rule.level", "@timestamp"].
                       Strongly recommended — omitting returns all fields.
        query_string: Lucene query, e.g. "rule.level:[12 TO *] AND agent.name:WIN-DC01".
                      Use "*" for all documents.
        from_ts: Start time, UTC ISO 8601, e.g. "2026-06-23T00:00:00Z".
        to_ts: End time, UTC ISO 8601, e.g. "2026-06-24T00:00:00Z".
        ts_field: Timestamp field name (default "@timestamp").
        limit: Max documents to return (default 50, hard cap 200).
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
    from_ts: Optional[str] = None,
    to_ts: Optional[str] = None,
    ts_field: str = "@timestamp",
) -> dict:
    """Fastest way to check how many documents match a condition; never returns content.

    Prefer over opensearch_search when you only need the count — it never fills context
    with document data. Without from_ts/to_ts, scans the full index (4–5 s on 50 M docs).

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


# ── Aggregations ──────────────────────────────────────────────────────────────

@mcp.tool()
def opensearch_terms(
    index: str,
    field: str,
    query_string: str = "*",
    from_ts: Optional[str] = None,
    to_ts: Optional[str] = None,
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
    from_ts: Optional[str] = None,
    to_ts: Optional[str] = None,
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
    from_ts: Optional[str] = None,
    to_ts: Optional[str] = None,
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


# ── New tools ─────────────────────────────────────────────────────────────────

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
    """Escape hatch for any read GET endpoint not covered by other tools.

    Use when you know the OpenSearch REST path but no dedicated tool exists.
    For search/count/terms/histogram use the dedicated tools — they add safety
    guards and better error messages. Only GET is supported; write/admin paths
    (_delete, _bulk, _update, _reindex, etc.) are blocked.

    Examples of valid paths:
        /_nodes/stats
        /_plugins/_ism/policies
        /my-index/_alias
        /my-index/_shard_stores

    Args:
        path: OpenSearch path starting with "/", e.g. "/_nodes/stats".

    Returns:
        Raw JSON response from OpenSearch.
    """
    if not path.startswith("/"):
        raise ValueError(f"path must start with '/'. Got: {path!r}")
    hits = [f for f in _WRITE_PATH_FRAGMENTS if f in path.lower()]
    if hits:
        raise ValueError(
            f"Path {path!r} contains restricted keyword(s) {hits} — "
            "only read endpoints are permitted."
        )
    return get_client().raw_get(path)


@mcp.tool()
def opensearch_explain(
    index: str,
    doc_id: str,
    query_string: str = "*",
) -> dict:
    """Explain why a specific document matches (or doesn't match) a query.

    Use after opensearch_search returns unexpected results and you have a known
    document ID. Get the doc ID from a prior search by including "_id" in
    source_fields (note: _id is a metadata field — use opensearch_search and
    read the _id from hits). Exact index name only — no wildcards.

    Args:
        index: Exact index name, e.g. "wazuh-alerts-4.x-2026.06.24".
        doc_id: Document _id as returned by a prior search.
        query_string: Lucene query to evaluate against the document (default "*").

    Returns:
        {"matched": bool, "explanation": {...score breakdown...}}
    """
    path = f"/{index}/_explain/{doc_id}"
    if not _EXPLAIN_PATH_RE.match(path):
        raise ValueError(
            f"Invalid explain path {path!r}. "
            "index must not contain '/' and doc_id must not be empty."
        )
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
    baseline = client.terms(
        index, field,
        query_string=query_string,
        from_ts=baseline_from, to_ts=baseline_to,
        ts_field=ts_field, size=size,
    )
    selection = client.terms(
        index, field,
        query_string=query_string,
        from_ts=selection_from, to_ts=selection_to,
        ts_field=ts_field, size=size,
    )
    b_warn = baseline.pop("_warning", None)
    s_warn = selection.pop("_warning", None)

    all_keys = set(baseline) | set(selection)
    added, removed, changed, unchanged = {}, {}, {}, {}
    for k in all_keys:
        b, s = baseline.get(k), selection.get(k)
        if b is None:
            added[k] = s
        elif s is None:
            removed[k] = b
        elif b != s:
            pct = round((s - b) / b * 100, 1) if b else None
            changed[k] = {"baseline": b, "selection": s, "delta": s - b, "pct_change": pct}
        else:
            unchanged[k] = {"baseline": b, "selection": s}

    return {
        "added":    added,
        "removed":  removed,
        "changed":  dict(sorted(changed.items(), key=lambda x: abs(x[1]["delta"]), reverse=True)),
        "unchanged": unchanged,
        "baseline_warning":  b_warn,
        "selection_warning": s_warn,
    }


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
