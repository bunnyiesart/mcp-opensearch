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

Credentials (env vars or ~/.config/mcp-opensearch/config.json):
  OPENSEARCH_DASHBOARDS_URL  — tried first (e.g. https://opensearch.example.com)
  OPENSEARCH_URL             — direct fallback (e.g. https://opensearch.example.com:9200)
  OPENSEARCH_USERNAME
  OPENSEARCH_PASSWORD
  OPENSEARCH_VERIFY_SSL      — "true"/"false" (default: true)
"""

import logging
from typing import Optional

from fastmcp import FastMCP

from lib.client import init_client

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("opensearch-mcp")

mcp = FastMCP("opensearch")

_client = None


def get_client():
    global _client
    if _client is None:
        _client = init_client()
    return _client


# ── Connectivity / Meta ───────────────────────────────────────────────────────

@mcp.tool()
def opensearch_test() -> dict:
    """Test connectivity to OpenSearch / OpenSearch Dashboards.

    Returns the active backend ("dashboards" or "opensearch"), server version,
    and the URL in use. Call this first to confirm the connection is healthy.
    """
    return get_client().test_connection()


@mcp.tool()
def opensearch_cluster_health() -> dict:
    """Get OpenSearch cluster health.

    Returns status (green/yellow/red), number of nodes, active shards, and
    unassigned shards. Useful to know if the backend is degraded before
    trusting query results.

    Note: requires cluster:monitor/health privilege. Returns a permission error
    if the authenticated user does not have this privilege — in that case, skip
    this tool and use opensearch_test to confirm basic connectivity instead.
    """
    return get_client().cluster_health()


# ── Index discovery ───────────────────────────────────────────────────────────

@mcp.tool()
def opensearch_list_indices() -> list:
    """List all OpenSearch indices with document count, size, and health status.

    Returns a list sorted by index name. Use this to find the right index name
    before querying (e.g. wazuh-alerts-4.x-2026.06.24).

    Note: requires index-level read access via the Dashboards proxy
    (_cat/indices privilege). Returns a permission error if the authenticated
    user lacks this privilege — use opensearch_list_index_patterns as an
    alternative to discover available index patterns.
    """
    return get_client().list_indices()


@mcp.tool()
def opensearch_list_index_patterns() -> list:
    """List saved index patterns from OpenSearch Dashboards.

    Only available when the Dashboards backend is active. Returns id, title,
    and time field name for each pattern (these are what you see in the
    Dashboards index picker).
    """
    return get_client().list_index_patterns()


@mcp.tool()
def opensearch_get_mapping(index: str) -> dict:
    """Get flattened field mappings for an index.

    Args:
        index: Index name or wildcard pattern, e.g. "wazuh-alerts-*".

    Returns:
        {index_name: {field_path: field_type}} for all matched indices.
        Nested fields are flattened with dot notation (e.g. "rule.level": "integer").

    Note: requires indices:admin/mappings/get privilege. Returns a permission
    error if the authenticated user lacks this privilege — use
    opensearch_discover_fields as an alternative (samples live documents to
    infer field names and types).
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
    """Discover fields actually present in documents by sampling.

    Unlike get_mapping (which shows all registered fields), this returns only
    fields that have real data in the sampled documents.

    Args:
        index: Index name or wildcard pattern.
        query_string: Lucene filter to narrow the sample (default "*").
        from_ts: Sample from this timestamp, UTC ISO 8601 (e.g. "2026-06-01T00:00:00Z").
        to_ts: Sample up to this timestamp, UTC ISO 8601.
        ts_field: Timestamp field name (default "@timestamp").
        sample_size: Number of documents to sample (default 10).

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
    """Search documents in an OpenSearch index using a Lucene query string.

    Same query syntax as the OpenSearch Dashboards search bar.

    Args:
        index: Index name or wildcard pattern, e.g. "wazuh-alerts-*".
        query_string: Lucene query, e.g. "rule.level:[12 TO *] AND agent.name:WIN-DC01".
                      Use "*" for all documents.
        from_ts: Start time, UTC ISO 8601, e.g. "2026-06-23T00:00:00Z".
        to_ts: End time, UTC ISO 8601, e.g. "2026-06-24T00:00:00Z".
        ts_field: Timestamp field name (default "@timestamp").
        limit: Max documents to return (default 50, hard cap 200).
        offset: Pagination offset — skip this many documents before returning
                results (default 0). Use with limit to page through large result
                sets: offset=0 → page 1, offset=200 → page 2, etc.
        sort_field: Field to sort by (default: ts_field).
        sort_dir: "desc" = newest first (default), "asc" = oldest first.
        source_fields: Fields to include, e.g. ["agent.name", "rule.description"].
                       Omit for all fields. Strongly recommended to reduce size.

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
) -> int:
    """Count documents matching a query in an OpenSearch index.

    Faster than opensearch_search when you only need the number.

    Args:
        index: Index name or wildcard pattern.
        query_string: Lucene query string (default "*" = all documents).
        from_ts: Start time, UTC ISO 8601.
        to_ts: End time, UTC ISO 8601.
        ts_field: Timestamp field name (default "@timestamp").

    Returns:
        Total document count as an integer.
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
    """Top N unique values of a field with their document counts (frequency analysis).

    Args:
        index: Index name or wildcard pattern.
        field: Field to aggregate, e.g. "agent.name", "rule.id", "data.srcip".
               Must be a keyword field (not analyzed text).
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
    """Run multiple field frequency analyses in a single API call.

    Args:
        index: Index name or wildcard pattern.
        aggregations: List of aggregation specs, each a dict with:
            - id (str): Label for this aggregation in the result.
            - field (str): Field to aggregate.
            - size (int, optional): Top N values (default 50).
          Example: [{"id": "agents",  "field": "agent.name",      "size": 20},
                    {"id": "rules",   "field": "rule.id",          "size": 10},
                    {"id": "sources", "field": "data.srcip",       "size": 30}]
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
    """Temporal histogram of event counts over a time range.

    Args:
        index: Index name or wildcard pattern.
        from_ts: Start time, UTC ISO 8601 (required), e.g. "2026-06-23T00:00:00Z".
        to_ts: End time, UTC ISO 8601 (required), e.g. "2026-06-24T00:00:00Z".
        ts_field: Timestamp field name (default "@timestamp").
        interval: Bucket size — e.g. "1h", "30m", "1d", "15m", or "auto".
        query_string: Lucene filter (default "*").

    Returns:
        {"results": {timestamp: count}}
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
    """Numeric statistics (count, min, max, avg, sum, std_deviation) for a field.

    Args:
        index: Index name or wildcard pattern.
        field: Numeric field to compute stats on, e.g. "rule.level", "data.bytes".
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


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
