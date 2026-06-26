# Changelog

## Unreleased

### Summary

This release adds 5 new tools, 3 investigation prompts, and rewrites all 12 existing tool descriptions. No new dependencies. Only `server.py` and `lib/client.py` were modified.

---

### Why tool descriptions were rewritten

A 2025 audit of 856 tools across 103 real-world MCP servers found that roughly 90% had at least one of: unstated limitations, missing usage guidelines, or opaque parameter descriptions. The practical consequence is that the LLM picks the wrong tool, passes the wrong arguments, or retries avoidable errors.

The original descriptions told the LLM what a tool does (e.g. "Get OpenSearch cluster health"). The rewritten descriptions tell the LLM *when to use this tool and when not to* — the decision rule that a human analyst applies intuitively but an LLM needs made explicit.

**Rule applied to every docstring:**
1. First sentence: decision rule — when to prefer this tool, or what to do instead if it fails
2. Second sentence: the most important failure mode (403, wrong field type, missing time range) with the exact error or workaround
3. Args block: strongest guidance first, not buried at the bottom

**Examples of what changed:**

| Tool | Before | After |
|---|---|---|
| `opensearch_cluster_health` | "Get OpenSearch cluster health." | "Requires cluster:monitor/health — if you get 403, use opensearch_test instead." |
| `opensearch_get_mapping` | "Get flattened field mappings for an index." | "Use to see all field names and types; if you get 403 use opensearch_discover_fields instead." |
| `opensearch_search` | "Search documents using a Lucene query string." | "Always pass source_fields to limit response size (50 full docs ≈ 237 KB)." |
| `opensearch_terms` | "Top N unique values of a field." | "If results look wrong, append .keyword. Never use on analyzed text fields." |
| `opensearch_count` | "Faster than opensearch_search when you only need the number." | "Fastest way to check match count; never fills context. Without time range, scans full index (slow)." |

---

### New tool: `opensearch_ppl`

**Why:** PPL (Piped Processing Language) is the native query language for OpenSearch observability workloads. It is the language OpenSearch Dashboards Discover uses internally, the language AWS CloudWatch Logs Insights is modelled on, and what OpenSearch's own agentic AI generates. Lucene query strings (`opensearch_search`) are expressive for filtering but cannot do multi-step transformations (filter → aggregate → sort → limit) in a single query. PPL can.

**What it does:** Sends a `POST /_plugins/_ppl` request with `{"query": "<ppl string>"}`. Returns `schema` (column names and types) and `datarows` (result rows as arrays), which is the native PPL response format.

**Implementation:** One new allowlist entry (`/_plugins/_ppl` added to `_ALLOWED_PATHS["POST"]`), one client method (`ppl()`), one tool function.

**When to prefer it over `opensearch_search`:** Multi-step pipelines — e.g. `source=wazuh-alerts-* | where rule.level > 10 | stats count() as hits by agent.name | sort -hits | head 20`. A Lucene equivalent would require two separate tool calls (search + terms aggregation).

---

### New tool: `opensearch_api`

**Why:** No fixed tool set covers every OpenSearch endpoint. Analysts regularly need one-off reads — ISM policies, node stats, alias mappings, shard store reports — that don't justify a dedicated tool. Without an escape hatch the LLM has no path forward and either hallucinates a response or asks the user to run a curl command manually.

**What it does:** Calls `raw_get(path)` on the client — a GET to any path, bypassing the allowlist — and returns the raw JSON response.

**Safety:** The tool layer checks the path for 14 keyword fragments that indicate write or admin operations (`_delete`, `_bulk`, `_update`, `_create`, `_reindex`, `_rollover`, `_shrink`, `_split`, `_clone`, `_open`, `_freeze`, `_unfreeze`, `_forcemerge`). If any fragment is found, it raises a `ValueError` before making a request. HTTP GET is also inherently safe in OpenSearch's REST semantics — there are no GET endpoints that mutate state.

**`raw_get` / `raw_post` on the client:** To support `opensearch_api` and `opensearch_explain`, two bypass methods were added to `OpenSearchClient`. These mirror the internal `_get` / `_post` methods exactly, minus the `_check_path` call. They are named `raw_get`/`raw_post` to signal clearly to any future reader that the allowlist is intentionally not invoked, and that the caller owns the path validation. Without these methods the only option would have been weakening `_check_path` itself, which would have reduced the safety of all other methods.

---

### New tool: `opensearch_explain`

**Why:** When `opensearch_search` returns unexpected results — a document that shouldn't match a filter, or a document that's ranked unexpectedly — there is currently no way to diagnose it without leaving the MCP context and running a manual `_explain` query. The `_explain` endpoint is the standard OpenSearch debugging tool for exactly this case.

**What it does:** Calls `POST /{index}/_explain/{doc_id}` with a `query_string` DSL query. Returns the OpenSearch explain response: whether the document matched and the full score breakdown.

**Why `raw_post` instead of the allowlist:** The explain path is `/{index}/_explain/{doc_id}`. The terminal path segment is always the document ID, not a fixed string. The existing allowlist uses suffix-matching (e.g. `/_search` matches `/my-index/_search`). There is no fixed suffix that can match `/_explain/{doc_id}` for all document IDs. Adding `raw_post` with regex validation at the tool layer (`^/[^/]+/_explain/[^/]+$`) is the correct approach — the regex structurally prevents the path from matching any write endpoint.

---

### New tool: `opensearch_index_settings`

**Why:** Analysts diagnosing unexpected index behaviour — slow writes, data retention anomalies, risk from zero replicas — need to inspect index settings. This information is not available through any existing tool. `opensearch_get_mapping` only returns field schemas, not operational configuration.

**What it does:** Calls `GET /{index}/_settings` and returns a simplified summary per index: shard count, replica count, refresh interval, ILM policy name, and creation timestamp. The raw settings response from OpenSearch is deeply nested; the client method flattens the relevant operational fields.

**Implementation:** `/_settings` added to `_ALLOWED_PATHS["GET"]` (suffix-match works correctly: `/wazuh-alerts-*/_settings` ends with `/_settings`). One client method (`index_settings()`), one tool function.

---

### New tool: `opensearch_compare`

**Why:** "Is this week different from last week?" is the most common log analysis pattern. Previously this required two `opensearch_terms` calls and manual inspection of both results to identify what changed. The LLM had to diff two unordered dictionaries in its head, which is error-prone and context-expensive.

**What it does:** Calls `client.terms()` twice (once for a baseline window, once for a selection window), strips the internal `_warning` keys, and computes a structured diff with four buckets: `added` (values present in selection but not baseline), `removed` (in baseline but not selection), `changed` (in both but with different counts, sorted by absolute delta descending so the biggest shifts appear first), and `unchanged`. Both `_warning` values are forwarded as `baseline_warning` / `selection_warning` keys so fielddata warnings are not silently dropped.

**Implementation:** Pure `server.py` change — no new client methods, no new allowlist entries. Reuses the existing `terms()` client method.

---

### New prompts: `investigate_alert`, `top_offenders`, `compare_time_windows`

**Why:** The MCP `prompts` primitive is a user-controlled message template that appears as a slash command in compatible clients. Most MCP servers ignore it entirely. For a log analysis server, pre-built investigation workflows are high-value — they encode the correct sequence of tool calls (with the right parameters and ordering) that an experienced analyst would follow, so a new user or a less context-rich LLM session can execute a complete investigation without needing to reason about tool selection from scratch.

Prompts are not tools — they don't add to the tool list and don't consume a tool call slot. They return a string (a message to the LLM) that contains pre-filled tool call examples using the parameters the user provided.

**`investigate_alert`:** Sequences a single-agent investigation: count → rule ID distribution → rule descriptions → timeline histogram → high-severity event sample → open-ended summary questions. The sequencing matters — counting first gives context before fetching documents, the rule ID distribution focuses the subsequent description lookup, and the summary questions guide the analyst to lateral movement indicators.

**`top_offenders`:** Runs five independent aggregations (agents, rules, source IPs, destination IPs, timeline) in parallel, then guides correlation across the results. The parallel instruction is explicit in the prompt — without it the LLM will run these sequentially, which is 5× slower for what are fully independent queries.

**`compare_time_windows`:** Orchestrates the `opensearch_compare` tool across three field dimensions, then guides drill-down into the `added` and high-`pct_change` buckets. This prompt is designed to be used with `opensearch_compare`, which was added in this same release.

---

### Internal: `raw_get` and `raw_post` on `OpenSearchClient`

These are not exposed as tools directly. They exist to support `opensearch_api` (uses `raw_get`) and `opensearch_explain` (uses `raw_post`). The design choice to bypass the allowlist at the method level rather than weakening `_check_path` keeps the safety model clear: `_check_path` is a blanket guard for all `_get`/`_post` calls, and `raw_*` are explicitly documented bypass paths where the caller (the tool function) is responsible for validation. The naming convention (`raw_` prefix) makes this visible to any future contributor without needing a comment.
