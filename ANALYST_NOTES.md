# MCP OpenSearch — Analyst Notes

> Generated from live testing against OpenSearch Dashboards 2.19.3.
> All timings and sizes are real measurements from the test run.

---

## Executive Summary

- **CRITICAL — Histogram DoS**: Requesting a 1-year range with `interval=1m` (525,600 potential buckets) caused a 246-second server-side timeout. This is a single call that can freeze the OpenSearch cluster for all users.
- **CRITICAL — Unbounded search results**: `limit=10000` returns 39.5 MB in ~15s with no server-side or MCP-side cap. At `limit=50` (the default), full documents already produce 237 KB per call. Claude's context fills quickly and silently degrades.
- **HIGH — 3 of 11 tools are permanently broken** for this user: `cluster_health`, `list_indices`, and `get_mapping` return 403; `list_index_patterns` returns 404 (wrong API path for Dashboards 2.x). These are listed as available tools but always fail.
- **HIGH — No time range enforcement**: `count('*')` with no `from_ts` scans 19.8 million documents and takes 4.7s. Several tools accept unbounded queries with no default lookback, creating slow and expensive queries by accident.
- **MEDIUM — discover_fields hides nested field structure**: Nested fields (`agent.name`, `rule.level`, `rule.description`) are returned only as `agent: dict`, `rule: dict` — completely opaque to the analyst who needs to know the sub-fields to write queries.

---

## Per-Tool Findings

### 1. `opensearch_test`
**Status: ✅ Works**

- Returns `{ok, backend, version, url}` in ~1.3s (first call, includes connection setup).
- Good diagnostic tool. No overload risk.
- **Missing**: Does not return the authenticated user/role, which would immediately explain why 4 tools return 403. Adding `"username": "coelho"` and `"role": [...]` to the response would save time troubleshooting.

---

### 2. `opensearch_cluster_health`
**Status: ❌ 403 Forbidden**

- The `coelho` user lacks `cluster:monitor/health` privilege on the OpenSearch backend.
- The error propagates as a raw `requests.HTTPError`, which is unhelpful.
- **Impact**: Tool is listed in the MCP manifest and Claude will attempt to use it, get a 403, and have to retry with a different approach — wasted tokens on every session start.
- **Fix**: Either (a) document in the tool description that it requires cluster-monitor privileges, or (b) catch the 403 and return `{"ok": false, "reason": "insufficient_permissions", "required_privilege": "cluster:monitor/health"}`.

---

### 3. `opensearch_list_indices`
**Status: ❌ 403 Forbidden**

- Same permission issue as cluster_health — `_cat/indices` requires index-level read access via the console proxy.
- Without this, Claude cannot enumerate what indices exist. The only workaround is `discover_fields` with a known index pattern.
- **Fix**: Same as above — catch 403, return structured error. Alternatively, use the Dashboards saved objects API to list index patterns (if fixed, see tool 4).

---

### 4. `opensearch_list_index_patterns`
**Status: ❌ 404 Not Found**

- The code calls `/api/index_patterns/index_pattern` which does not exist in Dashboards 2.x.
- The correct endpoint for Dashboards 2.x is:
  ```
  GET /api/saved_objects/_find?type=index-pattern&fields=title&fields=timeFieldName
  ```
- This is a straightforward API version mismatch, not a permissions issue.
- **Fix**: Update `list_index_patterns()` in `lib/client.py` to use `/api/saved_objects/_find?type=index-pattern`.

---

### 5. `opensearch_get_mapping`
**Status: ❌ 403 Forbidden**

- `_mapping` endpoint is blocked by the proxy for this user role.
- The fallback `discover_fields` works but returns far less detail (only top-level Python types).
- Bad index names also return 403 (not 404), so there's no way to distinguish "wrong index" from "no permission".
- **Fix**: Catch 403, suggest using `discover_fields` instead. Document the privilege required (`indices:admin/mappings/get`).

---

### 6. `opensearch_discover_fields`
**Status: ✅ Works — with caveats**

| sample_size | fields found | time    |
|-------------|-------------|---------|
| 1           | 18          | 899ms   |
| 10          | 18          | 671ms   |
| 100         | 19          | 1,431ms |
| 1000        | 20          | 3,985ms |

- Works by fetching `sample_size` documents and introspecting their keys.
- **No upper bound on `sample_size`**: a user could set `sample_size=100000`, fetching 100k full documents just to discover field names. At 4s for 1000 docs, this scales linearly into minutes.
- **Critical UX flaw**: Nested objects are returned as `agent: dict`, `rule: dict`, etc. The analyst cannot see `agent.name`, `rule.level`, `rule.description` — the actual fields they need to write queries. This defeats the primary purpose of the tool.
- **Suggested fixes**:
  1. Hard cap `sample_size` at 100.
  2. Flatten nested dicts to dot-notation in the result: `{"agent.name": "str", "agent.id": "str", "rule.level": "int", ...}`.
  3. Return unique value counts or example values alongside types.

---

### 7. `opensearch_search`
**Status: ✅ Works — OVERLOAD RISK**

| limit  | response size | time   |
|--------|--------------|--------|
| 50     | 237 KB       | 1.8s   |
| 500    | 1.9 MB       | 1.7s   |
| 1,000  | 4.1 MB       | 1.7s   |
| 10,000 | **39.5 MB**  | 15.1s  |

- **No upper bound on `limit`**. A model or user can request `limit=100000` — this would attempt to return hundreds of MB from OpenSearch in a single call, likely timing out or saturating the connection.
- **Context window risk**: Even at the default `limit=50`, full documents return 237 KB. Claude's 200k token context fills after roughly 5–6 search calls without `source_fields`. The model does not know it's filling context.
- **No time range default**: Without `from_ts`, the query hits all 19.8M documents. The `total: 10000` cap in the response is misleading — it suggests a small dataset when it's actually a scan of tens of millions.
- **`source_fields` is vastly underused**: The same 50 hits with `source_fields=['agent.name','rule.level','@timestamp']` returns only 5 KB (vs 237 KB). This is a **44x reduction** but it's entirely opt-in with no guidance.
- Bad query strings (e.g., `rule.level:[BADVALUE TO *]`) return a 400, propagated raw.
- **Suggested fixes**:
  1. Hard cap `limit` at 200 (or make it configurable with a server-side max).
  2. Add a `max_bytes` guardrail: if estimated response > threshold, truncate and warn.
  3. Add a default `from_ts` lookback (e.g., last 24h) when no time range is given, or at minimum warn.
  4. Add `source_fields` to the tool description with a strong "use this" recommendation.
  5. Translate 400 errors into readable Lucene syntax hints.

---

### 8. `opensearch_count`
**Status: ✅ Works — slow without time range**

| query                   | result     | time   |
|-------------------------|------------|--------|
| `*` (no time range)     | 19,824,851 | 4.7s   |
| `rule.level:15`         | 820,150    | 2.5s   |
| today only (`from_ts`)  | 559,359    | 300ms  |

- No overload risk — returns a single integer regardless of match count.
- However, a full `count('*')` scan takes 4.7s because it touches every shard across all indices. With a time range, the same count drops to 300ms.
- **Suggested fix**: Warn (or add a note in the return value) when no time range is provided and count > 1M, indicating the query may be expensive.

---

### 9. `opensearch_terms`
**Status: ✅ Works — silent fielddata risk**

- `agent.name` (keyword field): fast, correct.
- `rule.description` (analyzed text field): **silently works**, returns term frequency on tokenized text. This triggers fielddata loading on the text field — a significant cluster heap pressure risk in production. OpenSearch logs a warning but the MCP user sees nothing.
- `size=500` or `size=1000` with only 15 unique values takes 2–3 seconds — the excess size parameter makes OpenSearch do unnecessary work.
- **Suggested fixes**:
  1. Detect when a text field is used and either warn or automatically append `.keyword`.
  2. Cap `size` at 500 max.
  3. Return a `"sampled": true/false` flag if OpenSearch returned fewer buckets than `size` (indicating exhausted cardinality).

---

### 10. `opensearch_multi_terms`
**Status: ✅ Works**

| aggregations | response size | time   |
|-------------|--------------|--------|
| 1           | small        | 1.3s   |
| 5           | 985 B        | 863ms  |
| 10          | 4.8 KB       | 3.4s   |
| 0 (empty)   | `{}`         | —      |

- 5 aggregations in one call (863ms) is far better than 5 separate `terms` calls (~2s each). This is the tool's main value.
- Empty aggregations list silently returns `{}` — should be a validation error.
- No cap on number of aggregations. 50+ concurrent aggs could be expensive.
- **Suggested fixes**:
  1. Raise `ValueError` on empty `aggregations` list.
  2. Cap aggregations at ~20 per call.
  3. Inherit the same fielddata warning as `terms`.

---

### 11. `opensearch_histogram`
**Status: ✅ Works — CRITICAL DoS RISK**

| interval | range     | buckets | size    | time     |
|----------|-----------|---------|---------|----------|
| 1m       | 1 hour    | 61      | 2 KB    | 1.2s     |
| 15m      | 6 hours   | 25      | 863 B   | 289ms    |
| 1h       | 1 day     | 24      | 841 B   | 369ms    |
| 1d       | 25 days   | 25      | 913 B   | 406ms    |
| auto     | 1 day     | 41      | 1.4 KB  | 343ms    |
| **1h**   | **1 year**| **8,784**| **277 KB** | **2.9s** |
| **1m**   | **1 year**| —       | —       | **TIMEOUT (246s)** |

- **The 1m interval over a 1-year range caused a 246-second server timeout.** This is not a soft failure — it likely held OpenSearch threads for the full duration, impacting all cluster users.
- There is **zero validation** of the bucket count before firing the query. The formula `(to_ts - from_ts) / interval` should be computed and rejected if > a safe threshold (e.g., 2,000 buckets).
- `auto` interval delegates to OpenSearch and returns a sensible result — the best default but the actual chosen interval is not surfaced in the response.
- **Suggested fixes**:
  1. **Before executing**: calculate expected bucket count. If > 2,000, reject with: `"error": "Too many buckets: ~525600 expected. Use a coarser interval or narrow the time range."`.
  2. Return `"interval_used"` in the response (especially when `interval="auto"`).
  3. Validate `interval` format — currently accepts any string; `"5x"` or `"banana"` would send a malformed request.

---

### 12. `opensearch_stats`
**Status: ✅ Works — with graceful edge cases**

| field              | result                                     | time   |
|--------------------|--------------------------------------------|--------|
| `rule.level` (int) | count, min, max, avg, sum, std_deviation   | 489ms  |
| `agent.name` (text)| **400 Bad Request** (propagated raw)       | 324ms  |
| nonexistent field  | all nulls, count=0 (graceful)             | 1.8s   |

- Numeric fields work correctly and return useful stats.
- Text field returns a raw 400 — should be caught and explained: "Field `agent.name` is not numeric. Use a numeric field like `rule.level`."
- Nonexistent fields return null stats silently — this is acceptable but a `"field_exists": false` flag would help.

---

## Prioritized Improvement Backlog

### P0 — Prevents cluster damage or data loss

| ID | Issue | Fix | Impact |
|----|-------|-----|--------|
| P0-1 | Histogram with small interval + large range causes server timeout (246s observed) | Pre-calculate bucket count; reject if > 2,000 | Prevents DoS of shared OpenSearch cluster |
| P0-2 | `search` has no upper bound on `limit` — 39.5 MB at limit=10000 | Hard cap at 200; add server-side `max_limit` config | Prevents memory/bandwidth overload |
| P0-3 | `list_index_patterns` uses wrong API endpoint for Dashboards 2.x | Change to `/api/saved_objects/_find?type=index-pattern` | Fixes permanently broken tool |

### P1 — Significant UX or efficiency improvement

| ID | Issue | Fix | Impact |
|----|-------|-----|--------|
| P1-1 | `discover_fields` returns nested fields as opaque `dict`, hiding all sub-fields | Recursively flatten to dot-notation | Analyst can actually use the tool to learn field names |
| P1-2 | `discover_fields` has no cap on `sample_size` | Hard cap at 100 | Prevents multi-MB sampling queries |
| P1-3 | No default time range on `search`/`count` — scans full history silently | Add a default 24h lookback or emit a warning when `from_ts` is absent | Prevents multi-second full-history scans |
| P1-4 | `terms` and `multi_terms` silently trigger fielddata on text fields | Detect text fields, warn or append `.keyword` automatically | Prevents cluster heap pressure |
| P1-5 | Raw HTTP errors (400/403/404) propagated as exceptions | Wrap in structured errors with explanation and remediation hint | Saves analyst debug time per failure |
| P1-6 | `opensearch_test` doesn't show authenticated user | Add `"username"` to response | Instantly explains 403 errors on other tools |

### P2 — Nice to have

| ID | Issue | Fix | Impact |
|----|-------|-----|--------|
| P2-1 | `histogram` doesn't return the actual interval used when `interval="auto"` | Include `"interval_used"` in response | Analyst knows what granularity they're seeing |
| P2-2 | `search` has no pagination support | Add `from` (offset) parameter | Enables iterating large result sets safely |
| P2-3 | `multi_terms` accepts empty aggregations list silently | Raise `ValueError` | Prevents confusing empty responses |
| P2-4 | `stats` on text field returns raw 400 | Catch and explain | Saves analyst confusion |
| P2-5 | `opensearch_test` takes ~1.3s due to connection setup | Connection pooling / lazy init warmup | Faster first call in each session |
| P2-6 | `histogram` accepts arbitrary string as `interval` | Validate format against regex `^\d+[smhdwMy]$` or `^auto$` | Prevents silent 400 errors on typos |
| P2-7 | Tools broken by permissions (403) still appear in MCP manifest | Add a `"requires_privilege"` field to tool descriptions | Claude stops attempting calls it knows will fail |
