# mcp-opensearch

```
           /\     /\
          /  \___/  \
         / (o)   (o) \
        |   ~~ v ~~   |
        |   `-----`   |         mcp-opensearch
        |  /       \  |         ─────────────────────────────────────
        | |    ─    | |         Read-only MCP server for
         \|         |/          OpenSearch & OpenSearch Dashboards.
          |         |           Fuzzy log hunting.
         /|         |\
        / |         | \
       (  |         |  )~~~~~
        \_|_________|_/     ~~
```

> Read-only MCP server for OpenSearch and OpenSearch Dashboards — search, aggregate, and explore your log data from Claude Code or any MCP-compatible AI assistant.

## Features

- 12 tools covering connectivity checks, index/field discovery, full-text search, aggregations, time-series histograms, and numeric stats
- Two backends: OpenSearch Dashboards proxy (preferred) or direct OpenSearch REST API
- Hard limits on search result size (default 200) and histogram bucket count (default 2,000) to protect cluster health
- Text field aggregation warnings (fielddata heap pressure)
- No-time-range warnings on potentially expensive full-history queries
- Read-only write guard — only safe endpoints are in the allowlist
- Configurable via environment variables or `~/.config/mcp-opensearch/config.json`
- Docker image or bare Python (no Docker required)

## Requirements

- Python 3.10+ **or** Docker
- OpenSearch ≥ 2.x or OpenSearch Dashboards ≥ 2.x
- Basic auth credentials

## Quick Start

### 1. Clone

```bash
git clone https://github.com/bunnyiesart/mcp-opensearch.git
cd mcp-opensearch
```

### 2. Configure

Create a `.env` file with your credentials:

```ini
# ~/.config/mcp-opensearch/.env
OPENSEARCH_DASHBOARDS_URL=https://opensearch.example.com
OPENSEARCH_USERNAME=myuser
OPENSEARCH_PASSWORD=mypassword
OPENSEARCH_VERIFY_SSL=true
```

Or run the interactive setup script:

```bash
./setup.sh
```

### 3a. Docker (recommended)

```bash
make build

# Verify it works:
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}' | \
  docker run --rm -i --network host --env-file ~/.config/mcp-opensearch/.env opensearch-mcp:dev
```

### 3b. Python (no Docker)

```bash
pip install -r requirements.txt
python3 server.py
```

### 4. Register with Claude Code

Add the server to `~/.claude.json` under your project path:

```json
{
  "projects": {
    "/your/project": {
      "mcpServers": {
        "opensearch": {
          "type": "stdio",
          "command": "docker",
          "args": [
            "run", "--rm", "-i", "--network", "host",
            "--env-file", "/home/youruser/.config/mcp-opensearch/.env",
            "opensearch-mcp:dev"
          ],
          "env": {}
        }
      }
    }
  }
}
```

Restart Claude Code, then call `opensearch_test` to confirm the connection is healthy.

## Configuration

Environment variables take priority over the config file. At least one of `OPENSEARCH_DASHBOARDS_URL` or `OPENSEARCH_URL` is required. The config file at `~/.config/mcp-opensearch/config.json` must be `chmod 600`.

| Variable | Config key | Default | Description |
|---|---|---|---|
| `OPENSEARCH_DASHBOARDS_URL` | `dashboards_url` | — | Dashboards URL, tried first (e.g. `https://opensearch.example.com`) |
| `OPENSEARCH_URL` | `opensearch_url` | — | Direct OpenSearch URL, used as fallback (e.g. `https://os.example.com:9200`) |
| `OPENSEARCH_USERNAME` | `username` | — | Basic auth username |
| `OPENSEARCH_PASSWORD` | `password` | — | Basic auth password |
| `OPENSEARCH_VERIFY_SSL` | `verify_ssl` | `true` | Set `false` for self-signed certificates |
| `OPENSEARCH_TIMEOUT` | `timeout` | `60` | Request timeout in seconds |
| `OPENSEARCH_MAX_SEARCH_LIMIT` | `max_search_limit` | `200` | Hard cap on search `limit` parameter |
| `OPENSEARCH_MAX_HISTOGRAM_BUCKETS` | `max_histogram_buckets` | `2000` | Reject histogram requests exceeding this estimated bucket count |

## Tool Reference

### Connectivity

#### `opensearch_test`

Test connectivity and confirm the active backend.

No parameters.

```json
{
  "ok": true,
  "backend": "dashboards",
  "version": "2.19.3",
  "url": "https://opensearch.example.com",
  "username": "myuser"
}
```

Start every session with this tool. The `username` field immediately explains why certain tools return 403.

---

### Index Discovery

#### `opensearch_cluster_health` ⚠️

> Requires `cluster:monitor/health` privilege. Returns a permission error if the user lacks it — use `opensearch_test` for basic connectivity instead.

No parameters. Returns cluster status (`green`/`yellow`/`red`), node count, and active/unassigned shard counts.

---

#### `opensearch_list_indices` ⚠️

> Requires `_cat/indices` access via the Dashboards proxy. If you get 403, use `opensearch_list_index_patterns` instead.

No parameters. Returns a list sorted by index name:

```json
[
  {"index": "wazuh-alerts-4.x-2026.06.24", "docs.count": "559359", "store.size": "1.2gb", "health": "green"}
]
```

---

#### `opensearch_list_index_patterns`

List saved index patterns from OpenSearch Dashboards. Requires the Dashboards backend.

No parameters.

```json
[
  {"id": "abc123", "title": "wazuh-alerts-*", "timeFieldName": "@timestamp"}
]
```

---

#### `opensearch_get_mapping` ⚠️

> Requires `indices:admin/mappings/get` privilege. If you get 403, use `opensearch_discover_fields` instead.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `index` | str | — | Index name or wildcard, e.g. `"wazuh-alerts-*"` |

Returns all fields flattened to dot-notation:

```json
{
  "wazuh-alerts-4.x-2026.06.24": {
    "agent.name": "keyword",
    "rule.level": "integer",
    "@timestamp": "date"
  }
}
```

---

#### `opensearch_discover_fields`

Discover fields by sampling live documents. Works without mapping privileges.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `index` | str | — | Index name or wildcard |
| `query_string` | str | `"*"` | Lucene filter to narrow the sample |
| `from_ts` | str | — | ISO 8601 UTC start time |
| `to_ts` | str | — | ISO 8601 UTC end time |
| `ts_field` | str | `"@timestamp"` | Timestamp field name |
| `sample_size` | int | `10` | Documents to sample (max 100) |

Returns only fields present in the sampled documents, flattened to dot-notation:

```json
{
  "agent.id": "str",
  "agent.name": "str",
  "rule.level": "int",
  "rule.description": "str",
  "@timestamp": "str"
}
```

---

### Search

#### `opensearch_search`

Full-text search using Lucene query syntax — the same syntax as the OpenSearch Dashboards search bar.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `index` | str | — | Index name or wildcard |
| `query_string` | str | `"*"` | Lucene query, e.g. `"rule.level:[12 TO *] AND agent.name:WIN-DC01"` |
| `from_ts` | str | — | ISO 8601 UTC start time |
| `to_ts` | str | — | ISO 8601 UTC end time |
| `ts_field` | str | `"@timestamp"` | Timestamp field name |
| `limit` | int | `50` | Max documents to return (hard cap: 200) |
| `offset` | int | `0` | Pagination offset — skip this many documents before returning results |
| `sort_field` | str | `ts_field` | Field to sort by |
| `sort_dir` | str | `"desc"` | `"desc"` = newest first, `"asc"` = oldest first |
| `source_fields` | list | — | Fields to include, e.g. `["agent.name", "rule.level"]`. Strongly recommended — reduces response size by up to 44×. |

```json
{
  "total": 19824851,
  "hits": [{"agent.name": "WIN-DC01", "rule.level": 12, "@timestamp": "2026-06-24T10:23:11Z"}],
  "warning": "No time range specified — this query scans the full index history..."
}
```

`warning` is present when the `limit` was capped or no time range was given. To paginate, increment `offset` by `limit` on each call.

---

#### `opensearch_count`

Count matching documents without fetching them. Much faster than `opensearch_search` when you only need the number.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `index` | str | — | Index name or wildcard |
| `query_string` | str | `"*"` | Lucene query |
| `from_ts` | str | — | ISO 8601 UTC start time |
| `to_ts` | str | — | ISO 8601 UTC end time |
| `ts_field` | str | `"@timestamp"` | Timestamp field name |

```json
{"count": 559359, "warning": "No time range specified..."}
```

---

### Aggregations

#### `opensearch_terms`

Top N unique values of a field with their document counts (frequency analysis).

| Parameter | Type | Default | Description |
|---|---|---|---|
| `index` | str | — | Index name or wildcard |
| `field` | str | — | Field to aggregate. Must be a keyword field — for text fields, try appending `.keyword`. |
| `query_string` | str | `"*"` | Lucene filter |
| `from_ts` | str | — | ISO 8601 UTC start time |
| `to_ts` | str | — | ISO 8601 UTC end time |
| `ts_field` | str | `"@timestamp"` | Timestamp field name |
| `size` | int | `50` | Number of top values to return |

```json
{
  "WIN-DC01": 4821,
  "srv-web01": 2103,
  "db-primary": 987,
  "_warning": "Field 'rule.description' looks like a text field. Try 'rule.description.keyword'..."
}
```

`_warning` is present if the field name suggests an analyzed text type (fielddata heap pressure risk).

---

#### `opensearch_multi_terms`

Run multiple field frequency analyses in a single API call. Significantly faster than multiple `opensearch_terms` calls.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `index` | str | — | Index name or wildcard |
| `aggregations` | list | — | List of aggregation specs (see below). Must not be empty. |
| `query_string` | str | `"*"` | Lucene filter |
| `from_ts` | str | — | ISO 8601 UTC start time |
| `to_ts` | str | — | ISO 8601 UTC end time |
| `ts_field` | str | `"@timestamp"` | Timestamp field name |

Each item in `aggregations`:

```json
{"id": "agents", "field": "agent.name", "size": 20}
```

Example call:

```json
{
  "aggregations": [
    {"id": "agents",  "field": "agent.name", "size": 20},
    {"id": "rules",   "field": "rule.id",    "size": 10},
    {"id": "sources", "field": "data.srcip", "size": 30}
  ]
}
```

```json
{
  "agents":  {"WIN-DC01": 4821, "srv-web01": 2103},
  "rules":   {"550": 12000, "5710": 8400},
  "sources": {"192.168.1.10": 3200}
}
```

---

#### `opensearch_histogram`

Event count histogram over a time range.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `index` | str | — | Index name or wildcard |
| `from_ts` | str | — | **Required.** ISO 8601 UTC start time |
| `to_ts` | str | — | **Required.** ISO 8601 UTC end time |
| `ts_field` | str | `"@timestamp"` | Timestamp field name |
| `interval` | str | `"1h"` | Bucket size. Format: `<number><unit>` where unit is one of `s m h d w M y`. Use `"auto"` to let OpenSearch choose (~50 buckets). |
| `query_string` | str | `"*"` | Lucene filter |

Requests that would produce more than 2,000 buckets are rejected before the query runs. A 1-minute interval over a 1-year range produces ~525,000 buckets and would cause a server-side timeout — the pre-check catches this instantly.

```json
{
  "interval_used": "1h",
  "results": {
    "2026-06-24T00:00:00.000Z": 1203,
    "2026-06-24T01:00:00.000Z": 987,
    "2026-06-24T02:00:00.000Z": 1541
  }
}
```

`interval_used` reflects the actual bucket size chosen by OpenSearch when `interval="auto"`.

---

#### `opensearch_stats`

Numeric statistics for a field.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `index` | str | — | Index name or wildcard |
| `field` | str | — | Numeric field, e.g. `"rule.level"`, `"data.bytes"` |
| `query_string` | str | `"*"` | Lucene filter |
| `from_ts` | str | — | ISO 8601 UTC start time |
| `to_ts` | str | — | ISO 8601 UTC end time |
| `ts_field` | str | `"@timestamp"` | Timestamp field name |

```json
{
  "count": 559359,
  "min": 0,
  "max": 15,
  "avg": 7.4,
  "sum": 4139834,
  "std_deviation": 3.1
}
```

Returns a clear error if a non-numeric field is passed.

---

## Safety & Limits

| Guard | Default | Override |
|---|---|---|
| Max search results | 200 docs | `OPENSEARCH_MAX_SEARCH_LIMIT` |
| Max histogram buckets | 2,000 | `OPENSEARCH_MAX_HISTOGRAM_BUCKETS` |
| Max `discover_fields` sample size | 100 docs | hardcoded |
| Write guard | all writes blocked | hardcoded |

**Bucket pre-check** — Histogram requests are validated before execution. The expected bucket count is calculated as `(to_ts − from_ts) / interval`. If it exceeds the limit, the request is rejected with an actionable error message instead of firing a query that would hold OpenSearch threads for minutes.

**Text field warnings** — `opensearch_terms` and `opensearch_multi_terms` detect field names that suggest analyzed text types and include a `_warning` in the response. Aggregating on unindexed text fields triggers fielddata loading on the OpenSearch heap.

**No-time-range warnings** — `opensearch_search` and `opensearch_count` include a `warning` when no `from_ts`/`to_ts` is given. A full-index scan on tens of millions of documents is slow and expensive; adding a time range typically reduces query time by 10–15×.

## Known Limitations

Three tools require elevated privileges not available on all deployments:

| Tool | Required privilege | Alternative |
|---|---|---|
| `opensearch_cluster_health` | `cluster:monitor/health` | `opensearch_test` (basic connectivity) |
| `opensearch_list_indices` | `_cat/indices` via proxy | `opensearch_list_index_patterns` |
| `opensearch_get_mapping` | `indices:admin/mappings/get` | `opensearch_discover_fields` |

These tools return a structured error message (not a raw stack trace) when the privilege is missing. The `opensearch_test` tool includes the authenticated `username` in its response, which immediately clarifies why specific calls fail.

## Development

```bash
make build   # build Docker image (opensearch-mcp:dev)
make run     # run interactively (reads ~/.config/mcp-opensearch/.env)
make shell   # open a bash shell inside the container for debugging
```

Override the env file path:

```bash
make run ENV_FILE=/path/to/other.env
```

## License

MIT
