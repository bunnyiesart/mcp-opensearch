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

- **17 tools** covering connectivity checks, index/field discovery, full-text search, aggregations, time-series histograms, numeric stats, PPL queries, index settings, document explain, comparative analysis, and a generic GET escape hatch
- **3 investigation prompts** — reusable templates for common log analysis workflows (single-agent investigation, top-offenders sweep, baseline comparison)
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

Call first in every session to confirm connectivity and see the active backend. The `username` field immediately explains why certain tools return 403 — it shows exactly which role is authenticated.

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

---

#### `opensearch_cluster_health` ⚠️

> Requires `cluster:monitor/health` privilege. If you get 403, use `opensearch_test` for basic connectivity instead.

No parameters. Returns cluster status (`green`/`yellow`/`red`), node count, and active/unassigned shard counts.

---

### Index Discovery

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

Dashboards-only alternative to `opensearch_list_indices` when `_cat/indices` access is blocked. Returns saved index patterns as configured in the Dashboards UI.

No parameters.

```json
[
  {"id": "abc123", "title": "wazuh-alerts-*", "timeFieldName": "@timestamp"}
]
```

---

#### `opensearch_get_mapping` ⚠️

> Requires `indices:admin/mappings/get` privilege. If you get 403, use `opensearch_discover_fields` instead (only requires search privilege).

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

Fallback for `opensearch_get_mapping` when the mapping API is blocked. Samples live documents instead of reading schema metadata — only returns fields present in the sampled documents.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `index` | str | — | Index name or wildcard |
| `query_string` | str | `"*"` | Lucene filter to narrow the sample |
| `from_ts` | str | — | ISO 8601 UTC start time |
| `to_ts` | str | — | ISO 8601 UTC end time |
| `ts_field` | str | `"@timestamp"` | Timestamp field name |
| `sample_size` | int | `10` | Documents to sample (max 100) |

```json
{
  "agent.id": "str",
  "agent.name": "str",
  "rule.level": "int",
  "@timestamp": "str"
}
```

---

#### `opensearch_index_settings`

Get index operational settings: shard count, replicas, refresh interval, and ILM policy. Use when diagnosing unexpected index behaviour — slow writes, data retention issues, or replication risk. Prefer `opensearch_get_mapping` for field schema exploration.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `index` | str | — | Index name or wildcard, e.g. `"wazuh-alerts-*"` |

```json
{
  "wazuh-alerts-4.x-2026.06.24": {
    "number_of_shards": "3",
    "number_of_replicas": "1",
    "refresh_interval": "1s",
    "lifecycle_name": "wazuh-alerts-policy",
    "creation_date_ms": "1750550400000"
  }
}
```

> May require `indices:monitor/settings/get` privilege. Returns 403 if blocked.

---

### Search

#### `opensearch_search`

Full-document retrieval using Lucene syntax — the same syntax as the OpenSearch Dashboards search bar. Always pass `source_fields` to limit response size (50 full docs ≈ 237 KB). Omitting `from_ts`/`to_ts` scans the full index history; adding a time range reduces query time by up to 15×.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `index` | str | — | Index name or wildcard pattern |
| `source_fields` | list | — | **Strongly recommended.** Fields to include, e.g. `["agent.name", "rule.level", "@timestamp"]` |
| `query_string` | str | `"*"` | Lucene query, e.g. `"rule.level:[12 TO *] AND agent.name:WIN-DC01"` |
| `from_ts` | str | — | ISO 8601 UTC start time |
| `to_ts` | str | — | ISO 8601 UTC end time |
| `ts_field` | str | `"@timestamp"` | Timestamp field name |
| `limit` | int | `50` | Max documents to return (hard cap: 200) |
| `offset` | int | `0` | Pagination offset — increment by `limit` to page through results |
| `sort_field` | str | `ts_field` | Field to sort by |
| `sort_dir` | str | `"desc"` | `"desc"` = newest first, `"asc"` = oldest first |

```json
{
  "total": 19824851,
  "hits": [{"agent.name": "WIN-DC01", "rule.level": 12, "@timestamp": "2026-06-24T10:23:11Z"}],
  "warning": "No time range specified — this query scans the full index history..."
}
```

`warning` is present when the `limit` was capped or no time range was given.

---

#### `opensearch_count`

Fastest way to check how many documents match a condition. Never returns document content, so it never fills context. Without `from_ts`/`to_ts`, scans the full index (can take 4–5 s on 50 M docs).

| Parameter | Type | Default | Description |
|---|---|---|---|
| `index` | str | — | Index name or wildcard |
| `query_string` | str | `"*"` | Lucene query |
| `from_ts` | str | — | ISO 8601 UTC start time |
| `to_ts` | str | — | ISO 8601 UTC end time |
| `ts_field` | str | `"@timestamp"` | Timestamp field name |

```json
{"count": 559359}
```

---

#### `opensearch_ppl`

Execute a PPL (Piped Processing Language) query. Prefer over `opensearch_search` when you need multi-step pipeline operations (filter → stats → sort) in a single query. PPL is not interchangeable with Lucene — it uses a different syntax native to OpenSearch observability workloads.

> Returns 404 if the PPL plugin is not installed on the cluster.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `query` | str | — | Full PPL query string |

**PPL syntax:** `source=<index> | <command> [| <command> ...]`

Common commands:

| Command | Description |
|---|---|
| `where <condition>` | Filter rows |
| `stats count() by <field>` | Aggregate |
| `fields <f1>, <f2>` | Select columns |
| `sort -<field>` | Order results (- = descending) |
| `head <n>` | Limit rows |

```
source=wazuh-alerts-4.x-* | where rule.level > 10
| stats count() as hits by agent.name | sort -hits | head 20
```

```json
{
  "schema": [{"name": "agent.name", "type": "keyword"}, {"name": "hits", "type": "integer"}],
  "datarows": [["WIN-DC01", 4821], ["srv-web01", 2103]]
}
```

---

#### `opensearch_explain`

Explain why a specific document matches (or doesn't match) a query. Use after `opensearch_search` returns unexpected results and you have a known document ID. Requires an exact index name — no wildcards.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `index` | str | — | Exact index name, e.g. `"wazuh-alerts-4.x-2026.06.24"` |
| `doc_id` | str | — | Document `_id` from a prior search |
| `query_string` | str | `"*"` | Lucene query to evaluate against the document |

```json
{
  "matched": true,
  "explanation": {
    "value": 1.0,
    "description": "ConstantScore(agent.name:WIN-DC01)",
    "details": []
  }
}
```

> Requires `indices:data/read/explain` privilege.

---

### Aggregations

#### `opensearch_terms`

Frequency table for a keyword field — top N values with their document counts. If results look wrong or you see a heap warning, append `.keyword` to the field name (e.g. `agent.name.keyword`). Never use on analyzed text fields like `rule.description` — it loads fielddata into cluster heap.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `index` | str | — | Index name or wildcard |
| `field` | str | — | Keyword field to aggregate, e.g. `"agent.name"`, `"rule.id"` |
| `query_string` | str | `"*"` | Lucene filter |
| `from_ts` | str | — | ISO 8601 UTC start time |
| `to_ts` | str | — | ISO 8601 UTC end time |
| `ts_field` | str | `"@timestamp"` | Timestamp field name |
| `size` | int | `50` | Number of top values to return |

```json
{
  "WIN-DC01": 4821,
  "srv-web01": 2103,
  "_warning": "Field 'rule.description' looks like a text field. Try 'rule.description.keyword'..."
}
```

`_warning` is present if the field name suggests an analyzed text type.

---

#### `opensearch_multi_terms`

Preferred over calling `opensearch_terms` in a loop — runs multiple field frequency analyses in a single round-trip. Significantly faster when you need counts for several fields at once.

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

```json
{
  "agents":  {"WIN-DC01": 4821, "srv-web01": 2103},
  "rules":   {"550": 12000, "5710": 8400},
  "sources": {"192.168.1.10": 3200}
}
```

---

#### `opensearch_histogram`

Event count over time. Always specify `from_ts` and `to_ts` (meaningless without a range). Use `interval="auto"` when unsure — it picks ~50 buckets and is always safe. Fine intervals over long ranges (e.g. `"1m"` over a week) are rejected before the query runs.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `index` | str | — | Index name or wildcard |
| `from_ts` | str | — | **Required.** ISO 8601 UTC start time |
| `to_ts` | str | — | **Required.** ISO 8601 UTC end time |
| `ts_field` | str | `"@timestamp"` | Timestamp field name |
| `interval` | str | `"1h"` | Bucket size. Format: `<number><unit>` where unit is `s m h d w M y`. Use `"auto"` for ~50 buckets. |
| `query_string` | str | `"*"` | Lucene filter |

```json
{
  "interval_used": "1h",
  "results": {
    "2026-06-24T00:00:00.000Z": 1203,
    "2026-06-24T01:00:00.000Z": 987
  }
}
```

`interval_used` reflects the actual bucket size chosen when `interval="auto"`.

---

#### `opensearch_stats`

Min/max/avg/std for a numeric field. Only works on numeric types (integer, float, long) — passing a text field returns a 400 error with a clear message.

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

---

#### `opensearch_compare`

Compare the top values of a field between two time windows. Returns a structured diff with added, removed, and changed values sorted by absolute delta. Prefer over calling `opensearch_terms` twice manually.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `index` | str | — | Index name or wildcard |
| `field` | str | — | Keyword field, e.g. `"rule.id"`, `"agent.name"` |
| `baseline_from` | str | — | Baseline window start, ISO 8601 UTC |
| `baseline_to` | str | — | Baseline window end, ISO 8601 UTC |
| `selection_from` | str | — | Selection window start, ISO 8601 UTC |
| `selection_to` | str | — | Selection window end, ISO 8601 UTC |
| `query_string` | str | `"*"` | Lucene filter applied to both windows |
| `ts_field` | str | `"@timestamp"` | Timestamp field name |
| `size` | int | `20` | Top N values to fetch per window |

```json
{
  "added":     {"new-host-01": 342},
  "removed":   {"decommissioned-srv": 12},
  "changed":   {
    "WIN-DC01": {"baseline": 1200, "selection": 4821, "delta": 3621, "pct_change": 301.8}
  },
  "unchanged": {"srv-web01": {"baseline": 2100, "selection": 2103}},
  "baseline_warning": null,
  "selection_warning": null
}
```

`changed` is sorted by absolute delta descending so the most significant shifts appear first.

---

### Escape Hatch

#### `opensearch_api`

Generic GET escape hatch for any read endpoint not covered by the other tools. Use when you know the OpenSearch REST path but no dedicated tool exists. For search, aggregations, and histograms, use the dedicated tools — they add safety guards and better error messages.

Write and admin paths are blocked: any path containing `_delete`, `_bulk`, `_update`, `_create`, `_reindex`, `_rollover`, `_shrink`, `_split`, `_clone`, `_open`, `_freeze`, `_unfreeze`, or `_forcemerge` raises an error before any request is made.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `path` | str | — | OpenSearch path starting with `"/"`, e.g. `"/_nodes/stats"` |

Example valid paths:
- `/_nodes/stats`
- `/_plugins/_ism/policies`
- `/my-index/_alias`
- `/my-index/_shard_stores`

Returns the raw JSON response from OpenSearch.

---

## Prompts

MCP Prompts are reusable investigation templates. In compatible clients they appear as slash commands. Each prompt returns a step-by-step workflow pre-filled with the parameters you provide.

### `investigate_alert`

Step-by-step investigation guide for a specific agent's alerts in a time window. Walks through: total count → rule distribution → rule descriptions → event timeline → highest-severity sample → summary questions.

| Parameter | Description |
|---|---|
| `index` | Index name or wildcard, e.g. `"wazuh-alerts-4.x-*"` |
| `agent_name` | Agent to investigate, e.g. `"WIN-DC01"` |
| `from_ts` | Window start, ISO 8601 UTC |
| `to_ts` | Window end, ISO 8601 UTC |

---

### `top_offenders`

Find the top agents, rules, and source/destination IPs in a time window. Runs five independent aggregations in parallel, then guides you through correlating spikes, pivot points, and anomalous counts.

| Parameter | Description |
|---|---|
| `index` | Index name or wildcard |
| `from_ts` | Window start, ISO 8601 UTC |
| `to_ts` | Window end, ISO 8601 UTC |

---

### `compare_time_windows`

Compare alert patterns between a baseline period and a selection period. Uses `opensearch_compare` across rule IDs, agent names, and source IPs, then guides you through drilling into new threats, increased activity, and agents that went quiet.

| Parameter | Description |
|---|---|
| `index` | Index name or wildcard |
| `baseline_from` | Baseline start, ISO 8601 UTC |
| `baseline_to` | Baseline end, ISO 8601 UTC |
| `selection_from` | Selection start, ISO 8601 UTC |
| `selection_to` | Selection end, ISO 8601 UTC |

---

## Safety & Limits

| Guard | Default | Override |
|---|---|---|
| Max search results | 200 docs | `OPENSEARCH_MAX_SEARCH_LIMIT` |
| Max histogram buckets | 2,000 | `OPENSEARCH_MAX_HISTOGRAM_BUCKETS` |
| Max `discover_fields` sample size | 100 docs | hardcoded |
| Write guard | all writes blocked | hardcoded |
| `opensearch_api` write fragments | blocked | hardcoded |

**Bucket pre-check** — Histogram requests are validated before execution. The expected bucket count is calculated as `(to_ts − from_ts) / interval`. If it exceeds the limit, the request is rejected with an actionable error message instead of firing a query that would hold OpenSearch threads for minutes.

**Text field warnings** — `opensearch_terms` and `opensearch_multi_terms` detect field names that suggest analyzed text types and include a `_warning` in the response. Aggregating on unindexed text fields triggers fielddata loading on the OpenSearch heap.

**No-time-range warnings** — `opensearch_search` and `opensearch_count` include a `warning` when no `from_ts`/`to_ts` is given. A full-index scan on tens of millions of documents is slow and expensive; adding a time range typically reduces query time by 10–15×.

**Write-fragment blocklist** — `opensearch_api` checks the path for 14 keywords that indicate write or admin operations before making any request.

## Known Limitations

Four tools require elevated privileges not available on all deployments:

| Tool | Required privilege | Alternative |
|---|---|---|
| `opensearch_cluster_health` | `cluster:monitor/health` | `opensearch_test` (basic connectivity) |
| `opensearch_list_indices` | `_cat/indices` via proxy | `opensearch_list_index_patterns` |
| `opensearch_get_mapping` | `indices:admin/mappings/get` | `opensearch_discover_fields` |
| `opensearch_index_settings` | `indices:monitor/settings/get` | — |
| `opensearch_ppl` | PPL plugin must be installed | `opensearch_search` (Lucene) |

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
