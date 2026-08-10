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

[![PyPI](https://img.shields.io/pypi/v/mcp-opensearch)](https://pypi.org/project/mcp-opensearch/)
[![Docker](https://img.shields.io/badge/ghcr.io-mcp--opensearch-blue)](https://ghcr.io/bunnyiesart/mcp-opensearch)

> Read-only MCP server for OpenSearch and OpenSearch Dashboards — search, aggregate, and explore your log data from Claude Code or any MCP-compatible AI assistant.

**Contents** · [Features](#features) · [Requirements](#requirements) · [Quick Start](#quick-start) · [Configuration](#configuration) · [Tool Reference](#tool-reference) · [Prompts](#prompts) · [Safety & Limits](#safety--limits) · [Known Limitations](#known-limitations) · [Development](#development) · [Architecture](#architecture) · [Testing](#testing) · [Decisions & known defects](#decisions--known-defects)

## Features

- **22 tools** covering connectivity checks, index/field discovery, full-text search, entity timelines, aggregations, time-series histograms, numeric stats, PPL queries, index settings, document explain, comparative analysis, Alerting-plugin monitors and alerts, Anomaly Detection detectors and results, and a generic GET escape hatch
- **4 investigation prompts** — reusable templates for common log analysis workflows (single-agent investigation, top-offenders sweep, alert triage, baseline comparison)
- **Parallel requests** — all tools support concurrent execution; Claude Code can fire multiple queries in a single turn (e.g. `opensearch_count` + `opensearch_terms` + `opensearch_search` simultaneously) for faster investigations
- Two backends: OpenSearch Dashboards proxy (preferred) or direct OpenSearch REST API
- **Read-only by construction** — a single allowlist of read endpoints is checked on every outbound request, and no method on the client can skip it. Credential surfaces (security plugin, snapshot repositories, cluster settings) are excluded on purpose even though they are reads (see [ADR 0004](docs/adr/0004-single-allowlist-as-the-read-only-enforcement-point.md))
- **Cluster-health guards on every expensive operation** — search size, histogram buckets, terms cardinality, aggregation count and result-set size, all overridable, and **every clamp is announced** so a trimmed result is never mistaken for a complete one
- Text-field aggregation warnings (fielddata heap pressure), matched on whole tokens rather than substrings
- No-time-range warnings on potentially expensive full-history queries, consistently across all five tools that can scan unbounded
- **335 tests, no network access**, on Python 3.10–3.13, with a pinned lint rule set — see [Testing](#testing)
- **Decisions and known defects are written down**, not folklore — see [Decisions & known defects](#decisions--known-defects)
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

The two run paths read configuration differently, so there are two files. Run the
interactive setup script and it writes both, `chmod 600`:

```bash
./setup.sh
```

| File | Read by | Format |
|---|---|---|
| `~/.config/mcp-opensearch/config.json` | the Python process (PyPI install, `python3 server.py`) | JSON |
| `~/.config/mcp-opensearch/.env` | `docker --env-file` — i.e. `docker run`, `make run`, `make shell` | plain `KEY=value` |

To write them by hand instead — `~/.config/mcp-opensearch/config.json`, then `chmod 600`:

```json
{
    "dashboards_url": "https://opensearch.example.com",
    "username": "myuser",
    "password": "mypassword",
    "verify_ssl": true,
    "timeout": 60
}
```

and `~/.config/mcp-opensearch/.env`, then `chmod 600`:

```ini
OPENSEARCH_DASHBOARDS_URL=https://opensearch.example.com
OPENSEARCH_USERNAME=myuser
OPENSEARCH_PASSWORD=mypassword
OPENSEARCH_VERIFY_SSL=true
```

Two things about the `.env` file that bite in practice:

- **It must be plain `KEY=value`.** `docker --env-file` is not a shell: `export FOO=bar`,
  quoted values and `source` lines are all rejected or taken literally. A `.env` written
  as a shell fragment will fail with a parse error, not a warning.
- **The Python process does not read it.** It is Docker that turns those lines into
  environment variables inside the container. On the PyPI and from-source paths, set the
  variables in your MCP client's `env` block (see step 4) or use `config.json`.

### 3a. Docker (recommended)

Pull the pre-built image from GHCR:

```bash
docker pull ghcr.io/bunnyiesart/mcp-opensearch:latest
```

Or build locally:

```bash
make build
```

Verify it works:

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}' | \
  docker run --rm -i --network host --env-file ~/.config/mcp-opensearch/.env ghcr.io/bunnyiesart/mcp-opensearch:latest
```

### 3b. PyPI (recommended for Python users)

```bash
pip install mcp-opensearch
```

This installs the `mcp-opensearch` command directly into your PATH — no cloning or Docker required.

### 3c. From source

```bash
pip install -r requirements.txt
python3 server.py
```

### 4. Register with Claude Code

Add the server to `~/.claude.json` under your project path.

**Via PyPI (`mcp-opensearch` command):**

```json
{
  "projects": {
    "/your/project": {
      "mcpServers": {
        "opensearch": {
          "type": "stdio",
          "command": "mcp-opensearch",
          "args": [],
          "env": {
            "OPENSEARCH_DASHBOARDS_URL": "https://opensearch.example.com",
            "OPENSEARCH_USERNAME": "myuser",
            "OPENSEARCH_PASSWORD": "mypassword"
          }
        }
      }
    }
  }
}
```

**Via Docker:**

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
            "ghcr.io/bunnyiesart/mcp-opensearch:latest"
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

Environment variables take priority over the config file. At least one of `OPENSEARCH_DASHBOARDS_URL` or `OPENSEARCH_URL` is required. The config file at `~/.config/mcp-opensearch/config.json` must be `chmod 600` — the server refuses to start otherwise.

This is the complete list of variables the code reads. Anything not in this table has no effect.

| Variable | Config key | Default | Description |
|---|---|---|---|
| `OPENSEARCH_DASHBOARDS_URL` | `dashboards_url` | — | Dashboards URL, tried first (e.g. `https://opensearch.example.com`) |
| `OPENSEARCH_URL` | `opensearch_url` | — | Direct OpenSearch URL, used as fallback (e.g. `https://os.example.com:9200`) |
| `OPENSEARCH_USERNAME` | `username` | — | Basic auth username |
| `OPENSEARCH_PASSWORD` | `password` | — | Basic auth password |
| `OPENSEARCH_VERIFY_SSL` | `verify_ssl` | `true` | Set `false` for self-signed certificates |
| `OPENSEARCH_TIMEOUT` | `timeout` | `60` | Request timeout in seconds |
| `OPENSEARCH_MAX_SEARCH_LIMIT` | `max_search_limit` | `200` | Cap applied to the search/timeline `limit` parameter. This is a **default, not an absolute** — raising it raises the documented cap |
| `OPENSEARCH_MAX_HISTOGRAM_BUCKETS` | `max_histogram_buckets` | `2000` | Reject histogram requests exceeding this estimated bucket count. Also a default, not an absolute |
| `OPENSEARCH_MAX_TERMS_SIZE` | `max_terms_size` | `1000` | Cap applied to the `size` of a terms aggregation. A high-cardinality terms agg loads its whole bucket set into cluster heap, which is the most reliable way to exhaust a coordinating node |
| `OPENSEARCH_MAX_AGGREGATIONS` | `max_aggregations` | `20` | Cap on how many aggregations one `opensearch_multi_terms` call may run. Aggregations beyond the cap are dropped and named in the response warning, never silently |
| `OPENSEARCH_ALLOW_INSECURE_CONFIG` | — | unset | Set to `true` to downgrade the `config.json` permission check from a hard failure to a logged warning. Intended for containers and CI where the file mode is not yours to control; it does not make the file safe |

The permission gate applies to `config.json` only. `~/.config/mcp-opensearch/.env` is not checked by the server (it never reads it) — `chmod 600` it yourself.

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

Returns all fields flattened to dot-notation, **including `fields` multi-fields** — so the
`.keyword` companion of a `text` field is listed at its real dotted path. That matters because
`opensearch_terms` tells you to append `.keyword`, and this is the tool you use to confirm such
a sub-field exists:

```json
{
  "wazuh-alerts-4.x-2026.06.24": {
    "agent.name": "keyword",
    "rule.level": "integer",
    "rule.description": "text",
    "rule.description.keyword": "keyword",
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
| `sample_size` | int | `10` | Documents to sample (max 100 — this one is a hard cap with no override) |

Arrays of objects are descended into, so fields nested inside them (`rule.mitre.*`, Windows
`eventdata` arrays) are listed rather than being reported only as `"list"`:

```json
{
  "agent.id": "str",
  "agent.name": "str",
  "rule.level": "int",
  "rule.mitre": "list",
  "rule.mitre.id": "str",
  "@timestamp": "str",
  "_warning": "No time range specified — this query scans the full index history..."
}
```

`_warning` carries the sample-size cap and the no-time-range notice, joined with ` | ` when both
apply.

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

Full-document retrieval using Lucene syntax — the same syntax as the OpenSearch Dashboards search bar. Always pass `source_fields`: full documents in security indices are large, so an unrestricted response can consume a substantial share of the model's context. Omitting `from_ts`/`to_ts` scans the full index history; a time range lets OpenSearch skip shards and segments outside it and is usually much faster.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `index` | str | — | Index name or wildcard pattern |
| `source_fields` | list | — | **Strongly recommended.** Fields to include, e.g. `["agent.name", "rule.level", "@timestamp"]` |
| `query_string` | str | `"*"` | Lucene query, e.g. `"rule.level:[12 TO *] AND agent.name:WIN-DC01"` |
| `from_ts` | str | — | ISO 8601 UTC start time |
| `to_ts` | str | — | ISO 8601 UTC end time |
| `ts_field` | str | `"@timestamp"` | Timestamp field name |
| `limit` | int | `50` | Max documents to return. Capped at 200 by default — see `OPENSEARCH_MAX_SEARCH_LIMIT` |
| `offset` | int | `0` | Pagination offset — increment by `limit` to page through results |
| `sort_field` | str | `ts_field` | Field to sort by |
| `sort_dir` | str | `"desc"` | `"desc"` = newest first, `"asc"` = oldest first |

```json
{
  "total": 19824851,
  "warning": "No time range specified — this query scans the full index history...",
  "ids": ["kQ2vXpABc1dEfGhIjKlM"],
  "hits": [{"agent.name": "WIN-DC01", "rule.level": 12, "@timestamp": "2026-06-24T10:23:11Z"}]
}
```

`warning` is present when the `limit` was capped or no time range was given.

`ids[i]` is the OpenSearch `_id` of `hits[i]` — the two lists are index-aligned and always the
same length. This is how you obtain a `doc_id` for [`opensearch_explain`](#opensearch_explain);
you do **not** request `_id` via `source_fields`, because `_id` is document metadata rather than
a `_source` field. It is deliberately kept out of each hit dict: a document's own `_source` may
contain a field literally named `_id`, and merging would silently overwrite one with the other.

---

#### `opensearch_count`

Fastest way to check how many documents match a condition. Never returns document content, so it never fills context. Without `from_ts`/`to_ts`, it scans the full index, which can be slow on a large one — pass a time range when you have one.

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

#### `opensearch_timeline`

Build a chronological event timeline for a single entity (IP, host, user) matched across several fields at once — the core DFIR pivot. Instead of running `opensearch_search` repeatedly to chase an entity through `data.srcip`, `data.dstip`, `agent.ip`, etc., this ORs all fields together and returns events oldest-first.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `index` | str | — | Index name or wildcard |
| `entity` | str | — | Value to trace, e.g. `"10.0.0.5"`, `"WIN-DC01"` |
| `fields` | list | — | Fields the entity may appear in, e.g. `["data.srcip","data.dstip","agent.ip"]` |
| `from_ts` | str | — | ISO 8601 UTC start time |
| `to_ts` | str | — | ISO 8601 UTC end time |
| `ts_field` | str | `"@timestamp"` | Timestamp field name |
| `limit` | int | `100` | Max events, oldest-first. Capped at 200 by default — see `OPENSEARCH_MAX_SEARCH_LIMIT` |
| `source_fields` | list | — | Fields to include per event (strongly recommended) |
| `extra_query` | str | — | Optional Lucene filter ANDed with the entity match |

```json
{"total": 42, "entity": "10.0.0.5", "fields": ["data.srcip","data.dstip"], "events": [ ... ]}
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

Get `doc_id` from the `ids` list returned by [`opensearch_search`](#opensearch_search) or
`opensearch_timeline`: `ids[i]` is the `_id` of `hits[i]`. Requesting `_id` through
`source_fields` does not work and never did — `_id` is document metadata, not a `_source` field.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `index` | str | — | Exact index name, e.g. `"wazuh-alerts-4.x-2026.06.24"`. Must not contain `/` |
| `doc_id` | str | — | Document `_id`, taken from the `ids` list of a prior search. Must not contain `/` |
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
| `size` | int | `50` | Number of top values to return. Capped at 1,000 by default — see `OPENSEARCH_MAX_TERMS_SIZE` |

```json
{
  "WIN-DC01": 4821,
  "srv-web01": 2103,
  "_warning": "Field 'rule.description' looks like a text field. Try 'rule.description.keyword'..."
}
```

`_warning` is present if the field name suggests an analyzed text type, or if `size` was capped
(both, joined with ` | `, when both apply). The text-field check matches whole tokens in the last
dotted segment, splitting on separators and camelCase — so `logonType`, `catalogId` and `logger`
are **not** flagged merely for containing the letters `log`.

> `_warning` shares the response dict with the bucket keys, so a field value literally equal to
> `_warning` would be shadowed by it. Known and tracked — see [the defect
> backlog](docs/defect-backlog.md); fixing it changes the response shape.

---

#### `opensearch_multi_terms`

Preferred over calling `opensearch_terms` in a loop — runs multiple field frequency analyses in a single round-trip. Significantly faster when you need counts for several fields at once.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `index` | str | — | Index name or wildcard |
| `aggregations` | list | — | List of aggregation specs (see below). Must not be empty. Capped at 20 by default — see `OPENSEARCH_MAX_AGGREGATIONS` |
| `query_string` | str | `"*"` | Lucene filter |
| `from_ts` | str | — | ISO 8601 UTC start time |
| `to_ts` | str | — | ISO 8601 UTC end time |
| `ts_field` | str | `"@timestamp"` | Timestamp field name |

Each item in `aggregations`:

```json
{"id": "agents", "field": "agent.name", "size": 20}
```

Each spec needs both `id` and `field`; `size` is optional (default 50, subject to the same terms
cap). A missing key or a **duplicate `id`** is rejected with a clear `ValueError` rather than
being absorbed — two specs sharing an id used to collapse into one result, so the caller received
fewer answers than questions with no way to tell which field the numbers came from.

```json
{
  "agents":  {"WIN-DC01": 4821, "srv-web01": 2103},
  "rules":   {"550": 12000, "5710": 8400},
  "sources": {"192.168.1.10": 3200}
}
```

`_warning` appears at the top level when a field looks text-like, a `size` was capped, or
aggregations beyond the cap were dropped — the dropped ids are named explicitly.

---

#### `opensearch_histogram`

Event count over time. Always specify `from_ts` and `to_ts` (meaningless without a range). Use `interval="auto"` when unsure — it picks ~50 buckets and is always safe. Fine intervals over long ranges (e.g. `"1m"` over a week) are rejected before the query runs.

Units are routed to the aggregation OpenSearch actually accepts for them: `s m h d` go to
`fixed_interval`, and `w M y` to `calendar_interval`. `calendar_interval` permits a multiplier of
exactly 1, so `1w` is valid and `2w` is rejected locally with an explanation. A reversed range, a
zero-width interval like `0m`, and a missing bound are all rejected as `ValueError` before any
query is dispatched.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `index` | str | — | Index name or wildcard |
| `from_ts` | str | — | **Required.** ISO 8601 UTC start time |
| `to_ts` | str | — | **Required.** ISO 8601 UTC end time |
| `ts_field` | str | `"@timestamp"` | Timestamp field name |
| `interval` | str | `"1h"` | Bucket size. Format: `<number><unit>` where unit is `s m h d` (any multiplier) or `w M y` (multiplier 1 only). Use `"auto"` for ~50 buckets. |
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

When no document matches, `count` is `0` and every metric is `null` rather than `0`. That is
deliberate: OpenSearch returns those keys present-but-null for an empty result set, and reporting
`"min": 0` would claim a measurement that was never taken and be indistinguishable from a real
minimum of zero. Check `count` before doing arithmetic on the metrics.

---

#### `opensearch_list_monitors`

List OpenSearch Alerting-plugin monitors (detection rules) and whether they are enabled. Use to see what detections exist before investigating why something did — or did not — fire.

> Requires the Alerting plugin and monitor-search privilege. Returns 403/404 otherwise.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `size` | int | `50` | Max monitors to return |

```json
[{"id": "abc123", "name": "High severity alerts", "enabled": true, "type": "query_level_monitor", "schedule": {"period": {"interval": 1, "unit": "MINUTES"}}}]
```

---

#### `opensearch_get_alerts`

Fetch alerts raised by Alerting-plugin monitors — the "what is firing right now?" tool. Start a triage session here, then pivot on the offending entity with `opensearch_timeline`.

> Requires the Alerting plugin. Returns 403/404 otherwise.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `state` | str | — | Filter: `ACTIVE`, `ACKNOWLEDGED`, `COMPLETED`, `ERROR` |
| `monitor_id` | str | — | Restrict to one monitor |
| `size` | int | `50` | Max alerts, newest-first. Shares the search cap — 200 by default, see `OPENSEARCH_MAX_SEARCH_LIMIT` |

```json
{"total": 3, "alerts": [{"id": "a1", "monitor_name": "High severity alerts", "trigger_name": "level>=12", "state": "ACTIVE", "severity": "1", "start_time": 1719100800000}]}
```

A `warning` key appears when `size` was capped. Silently returning 200 of 10,000 alerts is how an
agent concludes it has seen everything and stops looking, so the clamp is always announced.

---

#### `opensearch_list_detectors`

List OpenSearch Anomaly Detection detectors and the indices they watch. Use before pulling results with `opensearch_get_anomaly_results`.

> Requires the Anomaly Detection plugin and detector-search privilege. Returns 403/404 otherwise.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `size` | int | `50` | Max detectors to return |

```json
[{"id": "det1", "name": "srcip-beaconing", "description": "outbound beaconing", "indices": ["wazuh-alerts-*"], "detection_interval": {"period": {"interval": 10, "unit": "Minutes"}}}]
```

---

#### `opensearch_get_anomaly_results`

Fetch ML-detected anomalies (beaconing, spikes, rare activity), highest anomaly-grade first — no hand-written aggregations needed.

> Requires the Anomaly Detection plugin. Returns 403/404 otherwise.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `detector_id` | str | — | Restrict to one detector (recommended) |
| `from_ts` | str | — | Filter on `data_end_time`, ISO 8601 UTC |
| `to_ts` | str | — | Filter on `data_end_time`, ISO 8601 UTC |
| `min_grade` | float | `0.0` | Only anomalies with `anomaly_grade` ≥ this (0–1). Raise to ~0.7 for high-confidence |
| `size` | int | `50` | Max anomalies, highest grade first. Shares the search cap — 200 by default, see `OPENSEARCH_MAX_SEARCH_LIMIT` |

```json
{"total": 12, "anomalies": [{"detector_id": "det1", "anomaly_grade": 0.92, "confidence": 0.88, "data_start_time": 1719100200000, "data_end_time": 1719100800000}]}
```

A `warning` key appears when `size` was capped or when neither `from_ts` nor `to_ts` was given —
unbounded, this scans the detector's whole result history.

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

| Parameter | Type | Default | Description |
|---|---|---|---|
| `path` | str | — | OpenSearch path starting with `"/"`, e.g. `"/_nodes/stats"` |

**Permitted paths are an allowlist, not a denylist.** There is no list of forbidden write
keywords; instead `lib/client.py` holds a positive list of read endpoints (`_ALLOWED_PATHS`)
which every outbound request is checked against, so an endpoint nobody thought about is
refused rather than permitted. The full rationale is in
[ADR 0004](docs/adr/0004-single-allowlist-as-the-read-only-enforcement-point.md); `_ALLOWED_PATHS`
itself is the authoritative list.

Entries match in one of two ways:

- **Cluster and plugin endpoints** match exactly, or any sub-resource beneath them — so
  `/_cat` covers `/_cat/indices/my-index`, and `/_nodes/stats` covers `/_nodes/stats/jvm`:
  `/_cat` (the whole family), `/_cluster/health`, `/_cluster/stats`,
  `/_cluster/pending_tasks`, `/_nodes/stats`, `/_nodes/usage`, `/_nodes/hot_threads`,
  `/_alias`, `/_mapping`, `/_template`, `/_index_template`, `/_plugins/_ism/policies`,
  `/_plugins/_ism/explain`, `/_plugins/_alerting/monitors/alerts`, and the Dashboards
  endpoints `/api/status`, `/api/saved_objects/_find`, `/api/data_views`.
- **Index-scoped endpoints** are written `/<index>/<action>` and accept any index name or
  wildcard pattern in the first segment: `/_mapping`, `/_settings`, `/_alias`, `/_stats`,
  `/_shard_stores`. The index segment must not begin with `_`, so a crafted index name
  cannot smuggle in a cluster endpoint.

Deliberately **not** on the allowlist, and intended to stay that way:

| Excluded | Why |
|---|---|
| `/_plugins/_security/*` | The security plugin's internal user database — usernames, password hashes, roles |
| `/_cluster/settings` | Persistent and transient settings carry snapshot-repository credentials |
| `/_cluster/state` | Same, plus the full cluster metadata blob |
| `/_snapshot/*` | Repository definitions, including cloud provider credentials |
| `/_nodes` (bare) | Node info echoes `opensearch.yml`, including configured settings |

These are reads, not writes — which is the point. "Read-only" is not the same as "safe", so
the criterion for an allowlist entry is *read-only **and** not a credential surface*. Query
strings are stripped before matching (`?format=json` is fine), and any path containing `.`
or `..` segments is rejected before the allowlist is consulted, so a traversal such as
`/_cat/../_plugins/_security/api/internalusers` cannot slip past.

When a path is refused, the error message is generated *from* the allowlist rather than
hand-written, so it cannot drift from what is actually permitted; it lists the allowed paths
and points at the dedicated tools.

Example valid paths:
- `/_nodes/stats`
- `/_cat/plugins?format=json`
- `/_plugins/_ism/policies`
- `/_plugins/_ism/policies/hot_rollover_policy`
- `/_cat/indices/my-index-name`
- `/my-index/_alias`
- `/my-index/_shard_stores`

Returns the JSON response as the cluster sent it. **Endpoints that return a JSON array — the
`_cat/*` family, e.g. `/_cat/plugins?format=json` — are wrapped as `{"result": [...]}`,**
because MCP structured output must be an object. Object responses are passed through unchanged.

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

### `triage_alerts`

SOC triage flow for whatever is firing right now. Pulls `ACTIVE` alerts from the Alerting plugin, maps them back to the monitors that raised them, then pivots on the most urgent entity's full timeline. Walks through: active alerts → monitor definitions → pick the highest-severity alert and identify its entity → entity timeline across `data.srcip`/`data.dstip`/`agent.ip`/`agent.name` → true-positive verdict and an acknowledge / escalate / tune-the-rule recommendation.

> Requires the Alerting plugin for steps 1 and 2. Without it, use `top_offenders` to find candidate entities and `opensearch_timeline` to pivot.

| Parameter | Description |
|---|---|
| `index` | Index name or wildcard to pivot in, e.g. `"wazuh-alerts-4.x-*"` |

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
| Max search results (`opensearch_search`, `opensearch_timeline`) | 200 docs | `OPENSEARCH_MAX_SEARCH_LIMIT` |
| Max alerting / anomaly result size | 200 records | `OPENSEARCH_MAX_SEARCH_LIMIT` |
| Max histogram buckets | 2,000 | `OPENSEARCH_MAX_HISTOGRAM_BUCKETS` |
| Max terms aggregation `size` | 1,000 buckets | `OPENSEARCH_MAX_TERMS_SIZE` |
| Max aggregations per `multi_terms` call | 20 | `OPENSEARCH_MAX_AGGREGATIONS` |
| Max `discover_fields` sample size | 100 docs | none — hardcoded |
| Read-only path allowlist | every request checked | none — hardcoded |

Everything with an override is a configurable **default**, not an absolute ceiling: a tool
description that says "cap 200" describes the shipped default, and an operator who raises
`OPENSEARCH_MAX_SEARCH_LIMIT` raises that cap. The `discover_fields` sample size and the path
allowlist have no override.

**Every clamp is announced.** A guard that silently trims a result set is worse than no guard,
because the caller cannot distinguish "that is all the data" from "that is all I was given" — so
each capped call returns a warning naming the cap and the environment variable that governs it.

**Bucket pre-check** — Histogram requests are validated before execution. The expected bucket count is calculated as `(to_ts − from_ts) / interval`. If it exceeds the limit, the request is rejected with an actionable error message instead of firing a query that would hold OpenSearch threads for minutes.

**Text field warnings** — `opensearch_terms` and `opensearch_multi_terms` detect field names that suggest analyzed text types and include a `_warning` in the response. Aggregating on unindexed text fields triggers fielddata loading on the OpenSearch heap.

**No-time-range warnings** — `opensearch_search`, `opensearch_count`, `opensearch_timeline`, `opensearch_discover_fields` and `opensearch_get_anomaly_results` all include a `warning` when no `from_ts`/`to_ts` is given. The consistency is the point: an agent that learns "no warning means bounded" from four tools will believe it from the fifth. A full-index scan over tens of millions of documents is slow and expensive; a time range lets OpenSearch skip shards and segments outside it, which is usually a large saving. How large depends entirely on your index, sharding and retention — measure it on your own cluster rather than trusting a figure from someone else's.

**Read-only path allowlist** — every request the client issues, including the caller-supplied paths passed to `opensearch_api` and `opensearch_explain`, is checked against a single positive list of read endpoints in `lib/client.py` before it leaves the process. Nothing on the client can skip that check. Credential surfaces (the security plugin, snapshot repositories, cluster settings and state) are excluded on purpose even though they are reads. See [ADR 0004](docs/adr/0004-single-allowlist-as-the-read-only-enforcement-point.md) and the [`opensearch_api`](#opensearch_api) reference above.

## Known Limitations

Some tools require elevated privileges or plugins not available on all deployments:

| Tool | Required privilege | Alternative |
|---|---|---|
| `opensearch_cluster_health` | `cluster:monitor/health` | `opensearch_test` (basic connectivity) |
| `opensearch_list_indices` | `_cat/indices` via proxy | `opensearch_list_index_patterns` |
| `opensearch_get_mapping` | `indices:admin/mappings/get` | `opensearch_discover_fields` |
| `opensearch_index_settings` | `indices:monitor/settings/get` | — |
| `opensearch_ppl` | PPL plugin must be installed | `opensearch_search` (Lucene) |
| `opensearch_list_monitors` / `opensearch_get_alerts` | Alerting plugin + alerting read privilege | — |
| `opensearch_list_detectors` / `opensearch_get_anomaly_results` | Anomaly Detection plugin + AD read privilege | — |

These tools return a structured error message (not a raw stack trace) when the privilege is missing. The `opensearch_test` tool includes the authenticated `username` in its response, which immediately clarifies why specific calls fail.

Beyond privileges, there are **known defects** — verified, reproducible, and written down rather
than discovered by you at 3am. The ones most worth knowing before you rely on this server:

- **`opensearch_test` can report `{"ok": true}` against a dead cluster.** The backend is probed
  once and cached for the process lifetime, and this tool reads the cache without re-probing.
- **A backend demotion is permanent.** Any non-200 from Dashboards `/api/status` — a 401, an SSO
  redirect — falls back to direct OpenSearch for the whole session with no re-probe and no
  failover in either direction. Recovery means restarting the MCP server.
- **A hung cluster can hold a single tool call for minutes.** Retries multiply the per-request
  timeout, and HTTP 429 is not treated as retryable, so an overloaded cluster surfaces as a bare
  `HTTP 429` with no back-off guidance.
- **`_resolve_backend` blames the URL for authentication and TLS failures**, so a wrong password
  reads as "check OPENSEARCH_DASHBOARDS_URL / OPENSEARCH_URL".

The full ranked list, each entry with a concrete failure scenario and whether it was verified by
execution or by reading, is in [`docs/defect-backlog.md`](docs/defect-backlog.md) — including a
section recording what was **checked and found clean**, so nobody re-audits it.

## Development

```bash
make build   # build Docker image (opensearch-mcp:dev)
make run     # run interactively (reads ~/.config/mcp-opensearch/.env)
make shell   # open a bash shell inside the container for debugging
```

`make run` and `make shell` pass `~/.config/mcp-opensearch/.env` to `docker --env-file`, so
that file must exist and must be plain `KEY=value` — see [Configure](#2-configure).
`config.json` is **not** visible inside the container; it is not mounted or copied into the
image, so the Docker path is env-vars-only by design.

Override the env file path:

```bash
make run ENV_FILE=/path/to/other.env
```

The image version is derived from `pyproject.toml`, which is the single authoritative version —
never hardcode it in the `Makefile` again. `make push` refuses to build if that read comes back
empty, so the failure mode is a loud stop rather than a mislabelled image.

## Architecture

Two files, two roles. This is a **microkernel**: `server.py` is the core, `lib/client.py` is the
only adapter that knows anything about OpenSearch.

```
              ┌──────────────────────────────────────────┐
   MCP        │  server.py  — the core                   │
  client ────▶│  22 @mcp.tool()  +  4 @mcp.prompt()      │
              │  declares contracts, delegates, nothing  │
              │  else: no HTTP, no query building,       │
              │  no path validation, no computation      │
              └────────────────┬─────────────────────────┘
                               │  init_client()
              ┌────────────────▼─────────────────────────┐
              │  lib/client.py — the single adapter      │
              │  ┌────────────────────────────────────┐  │
              │  │ _check_path()  ← every request      │  │
              │  │ _ALLOWED_PATHS  (read-only)         │  │
              │  └────────────────┬───────────────────┘  │
              │        _get() / _post()  ← only exits    │
              └───────┬───────────────────────┬──────────┘
                      │                       │
             ┌────────▼────────┐     ┌────────▼────────┐
             │   Dashboards    │ or  │ direct OpenSearch│
             │ /api/console/   │     │   REST :9200     │
             │     proxy       │     │                  │
             └─────────────────┘     └──────────────────┘
                 tried first          fallback
```

Two rules keep the boundary real rather than aspirational:

1. **A tool function in `server.py` is a thin delegate.** It declares the contract the model reads
   — name, signature, docstring — and forwards. Everything else belongs in the adapter.
2. **All OpenSearch knowledge lives in the adapter**: query bodies, response shaping, caps,
   warnings, and the read-only guard.

The payoff is that adding a tool is a local change with a test scope limited to itself, and the
pure logic is unit-testable without a cluster. The cost, accepted deliberately, is that sharing
code *between* tools is awkward on purpose — plug-ins talking to plug-ins is the documented failure
mode of this style. Full reasoning, including where the boundary has already eroded, is in
[ADR 0001](docs/adr/0001-microkernel-with-single-opensearch-adapter.md).

## Testing

```bash
pip install -e ".[dev]"     # pytest + ruff
pytest                      # 335 tests, ~0.2s
ruff check .                # pinned rule set, pinned version
```

No test touches the network. An autouse fixture replaces `HTTPAdapter.send` and `Session.request`
with a hard failure, so a test that tries to reach a cluster fails loudly rather than silently
skipping. Stubs sit at the `_get`/`_post` seam.

Two conventions worth knowing before you add tests:

- **`xfail_strict = true`.** A known bug is pinned with `@pytest.mark.xfail(strict=True)` asserting
  the *correct* behaviour. When the bug is fixed the test starts passing, which turns the build red
  until the marker is removed — so a known-bug test cannot decay into a permanent exemption. There
  are currently **zero** xfail markers, which is evidence that every bug they documented is
  genuinely fixed, not an assertion that it is.
- **Boundaries are tested at the on point and the off point**, with two off points where the
  condition is an equality. Caps are tested at the limit and one past it, not at a random interior
  value.

CI runs the suite on Python 3.10–3.13 — the floor tracks `requires-python`, because an untested
floor is an unverified promise. `ruff` is pinned to an exact version: an unpinned install made the
job's meaning change with each release, and a lint failure should always mean the code changed, not
that the tool did.

## Decisions & known defects

The *why* behind the structure is recorded, so it does not have to be re-argued from scratch:

| Document | What it holds |
|---|---|
| [`docs/adr/`](docs/adr/) | Architecture Decision Records — one file per significant decision, each with the trade-off accepted and how it is governed |
| [ADR 0001](docs/adr/0001-microkernel-with-single-opensearch-adapter.md) | Microkernel with a single adapter, and the two rules above |
| [ADR 0002](docs/adr/0002-dashboards-proxy-first-with-direct-fallback.md) | Why Dashboards is probed first — the behaviour most likely to be "simplified" by someone who does not know it is load-bearing |
| [ADR 0003](docs/adr/0003-single-source-of-truth-for-version.md) | One authoritative version, derived everywhere else |
| [ADR 0004](docs/adr/0004-single-allowlist-as-the-read-only-enforcement-point.md) | The read-only guarantee: one allowlist, no bypass, and which read endpoints are excluded as credential surfaces |
| [`docs/defect-backlog.md`](docs/defect-backlog.md) | Every verified defect, ranked by what an agent would wrongly conclude, plus what was checked and found clean |

Every ADR carries a `Compliance` section stating how the decision is enforced — automatically, or
manually and why. Filling that section in is what surfaces the real cost of a decision: it is how
we learned that the layering rule cannot be checked until the `opensearch_compare` diff moves into
`lib/`. Those checks are named in the ADRs and **not yet written**; the backlog tracks them.

## License

MIT
