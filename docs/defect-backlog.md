# Verified defect backlog — audit of 10 Aug 2026

Findings from an independent audit of commit `d145267`, ranked by consequence for an
LLM agent using these tools during an incident. Every entry was verified by
execution or by grep — none is speculative. Line numbers are from `d145267` and
have since drifted.

Ranking criterion is **what an agent would wrongly conclude**, not tidiness. A tool
that returns a confident wrong answer is worse than one that fails loudly, because
the agent builds on it.

Status legend: **done** · **in progress** · **open** · **accepted** (known, deliberately not fixed)

## Where this stands at the end of 10 Aug 2026

**Suite: 335 passed, 0 failed, 0 xfailed. `ruff check .` exits 0.** All eight
`xfail(strict=True)` markers are gone, which is the load-bearing fact: with
`xfail_strict = true`, a marker can only be removed once the bug it documents
actually stops reproducing, so their absence is evidence rather than assertion.

**Fixed and pinned by a test:** every entry in the "trust defects" table except the
`_warning` namespace collision; every entry in the "found by writing the tests" table
except `_coerce_bool` (T9); the terms/agg/plugin caps (#8); and the whole
documentation, packaging and release-gate group.

**Still open** and carried into the next session:

- `#1` `opensearch_test` reports `{"ok": true}` from cached state on a dead cluster.
- `#2` — partially closed. `search_string` now returns a parallel `ids` list, so a
  `doc_id` is obtainable and `opensearch_explain` is reachable; the tool docstrings
  describing the old impossible mechanism still need rewriting.
- `#4` retry/`RetryError`/429 and the ~246 s worst-case tool call.
- `#5` `_resolve_backend` blaming the URL for auth and TLS failures.
- `#6` permanent backend demotion with no re-probe or failover.
- `#12` the `.env` protection gap, and `load_dotenv()` resolving from `lib/`.
- `#16` `_dashboards_proxy` sending `{}` on proxied GETs — still unverified either way.
- `#18` the wheel claiming the top-level names `server` and `lib`.
- `#21` dead `last_query_ms`, suppressed backend log lines, no log-level env var.
- `#23` the `get_client()` check-then-assign race.
- `#24` the structural root cause: no injection seam, domain and adapter in one module.
- `T9` `_coerce_bool` treating `"0"`, `"no"`, `"off"` and `0` as True.

**Fitness functions still owed** — committed to by the `Compliance` sections of ADRs
0001, 0003 and 0004, and none of them written yet. Two are blocked on work above: the
layering check needs the `opensearch_compare` diff moved into `lib/` first, and the
no-hardcoded-version check is now unblocked (the `User-Agent` derives from package
metadata via `_package_version()`, with an honest `"unknown"` fallback for
source checkouts). The other two — no-bypass, and every `opensearch_api` docstring
example passing `_check_path` — have no prerequisites.

## Trust defects — the tool misleads the agent

| # | Location | Defect | Status |
|---|---|---|---|
| 1 | `client.py` `_resolve_backend` / `test_connection` | Returns early forever once `backend` is set, so `opensearch_test` does no I/O after the first call and reports `{"ok": true}` on a dead cluster. The agent then blames privileges or index names for the next failure. | open |
| 2 | `client.py` `search_string` | `_id` is dropped from hits, so **no tool can produce a `doc_id`** and `opensearch_explain` is unreachable. Its docstring tells the agent to request `_id` via `source_fields`, which yields `{}` per hit. | open |
| 3 | `client.py` `_flatten_mapping` | Recurses only into `properties`, never reads `fields`, so `.keyword` sub-fields are invisible — the exact thing every aggregation docstring instructs the agent to append. `terms` says "try `rule.description.keyword`"; `get_mapping` says it does not exist. | open |
| 7 | `client.py` `stats` | `"Bad request" in str(exc)` fires for *every* 400, so a malformed Lucene filter on a numeric field yields "Field 'rule.level' is not numeric … use a numeric field such as 'rule.level'" — advice that contradicts itself. | open |
| 9 | `client.py` `search_string` / `_flatten_doc` | Sort is always emitted with no `unmapped_type`, so `discover_fields` 400s on indices lacking `@timestamp` — i.e. the documented 403-fallback chain is circular, since `get_mapping` is the only way to learn the timestamp field. Separately `_flatten_doc` never descends into lists, so every field inside an array of objects reports as `"list"` with children invisible. | open |
| 10 | `client.py` `_parse_ts` | Rejects `+00:00` offsets, `HH:MM` precision and nanosecond fractions that `search`/`count`/`terms` accept (they pass strings to OpenSearch untouched). Timestamps are therefore not portable between tools despite every docstring claiming the same "UTC ISO 8601" contract. | open |
| 11 | `client.py` `multi_terms` | Raw `KeyError: 'id'` on a malformed spec, and duplicate ids are silently collapsed — the agent asks for two aggregations, receives one, with no warning and no way to tell which field the numbers belong to. `timeline` validates its list input properly; this does not. | open |
| — | `client.py` `_is_likely_text_field` | Substring matching, so the `log` hint fires inside `logonType`, `logonProcessName`, `catalogId`, `logger`; misses `process.commandline`. Every false positive tells the agent to append `.keyword` to a field that has none. | open |
| — | `client.py` `_check_histogram_buckets` / `histogram` | Accepts interval units `w`/`M`/`y`, then places them in `fixed_interval`, which supports only `ms/s/m/h/d`. Passes local validation, then fails at the cluster as a misleading "Bad request: check your query syntax or field names". | open |
| — | `client.py` `terms` / `multi_terms` / `discover_fields` | `_warning` is injected into the same dict as the data, so it collides with a real field value or agg id; `opensearch_compare` then `pop`s it. | open |

## Found by writing the tests (not by either audit)

These surfaced only when someone sat down and wrote assertions against the response-shaping
paths. Every one is a two-line pure-function test away from having been caught at the time
it was written, which is finding 24 arriving as a bill.

| # | Location | Defect | Status |
|---|---|---|---|
| T1 | `client.py` `timeline` | `entity.replace('"', '\\"')` escapes quotes but **not backslashes**. Verified: `C:\Windows\System32` reaches the cluster with its backslashes intact, so inside a Lucene phrase `\W` and `\S` are consumed as escapes and the term stops matching the document it came from — a silently wrong answer. `CORP\jdoe` likewise. An entity ending in a backslash escapes the closing quote, leaving an unterminated phrase whose 400 surfaces as "check your query syntax". This is the primary DFIR pivot tool and Windows paths and `DOMAIN\user` are its primary inputs. | in progress |
| T2 | `client.py` `stats` | `st.get("min", 0)` never fires, because `extended_stats` over zero docs returns the keys **present with JSON `null`**. Verified: `{'count': 0, 'min': None, …}` and `r['min'] + 1` raises `TypeError`. A post-condition violation in doc `05` §6 terms — the method promises numbers and returns `None`. | in progress |
| T3 | `client.py` `index_settings` | `s.get("lifecycle", {}).get("name")` guards only against an absent key; on an index whose ISM policy was detached the key is present-and-null, so the chained `.get` raises `AttributeError` and kills the whole tool call. Verified. The same idiom silently defeats the documented `"1s"` `refresh_interval` fallback. | in progress |
| T4 | `client.py` `_check_histogram_buckets` | `_parse_ts(None)` raises `AttributeError` from `None.rstrip`, but the `try` catches only `ValueError`, so the friendly translation is bypassed and callers doing `except ValueError` miss it. Verified both bounds. Also `interval="0m"` passes the regex and reaches a `ZeroDivisionError` (verified), and a reversed range is clamped to one bucket and dispatched silently. | in progress |
| T5 | `client.py` `discover_fields` | Consumes `search_string`'s result but reads only `hits`, discarding the no-time-range warning — so unbounded field discovery scans all index history in silence while every sibling tool warns. | in progress |
| T6 | `client.py` `histogram` | `b.get("key_as_string", str(b["key"]))` evaluates its default eagerly, so a bucket lacking `key` raises `KeyError` instead of using the `key_as_string` sitting right there. | in progress |
| T7 | `client.py` `search_string` | `offset` is clamped with `max(offset, 0)` but `limit` has no lower bound, so `limit=-5` is forwarded verbatim as `size`. | in progress |
| T8 | `client.py` `get_alerts` / `get_anomaly_results` | Sizes uncapped, unlike search limits; and `get_anomaly_results` is the only read that never warns about a missing time range. | in progress |
| T9 | `client.py` `_coerce_bool` | The string branch is an equality test against the single literal `"false"`, so `"0"`, `"no"`, `"off"`, `""` and `"false "` (trailing space — what a hand-edited `.env` produces) are all **True**; and for any non-`str`/`bool` the value is discarded and `default` returned, so `_coerce_bool(0)` is `True`. All verified. For `verify_ssl` this fails in the secure direction, but `OPENSEARCH_VERIFY_SSL=0` silently *not* disabling verification is a foot-gun. | open |

## Resilience and operability

| # | Location | Defect | Status |
|---|---|---|---|
| 4 | `client.py` retry config + `_raise_for_status` | `RetryError` and every connection/TLS failure are not `HTTPError`, so they bypass the actionable-message layer entirely. Worst case is 4×60 s + 6 s backoff ≈ **246 s per tool call** (≈492 s for `opensearch_compare`), so any MCP client with a 60–120 s timeout abandons the call while the server keeps hammering a cluster that is already shedding load. **429 is absent from `status_forcelist`** and surfaces as bare `HTTP 429` — and 429 is exactly what a large terms agg provokes. | open |
| 8 | `client.py` `terms` / `multi_terms` | No cap on `size` or on the number of aggregations, while `search` caps at 200 and `histogram` at 2000 buckets. `size=500000` passes through; 60 aggs × 100000 accepted. The guard covers the cheap operations and is absent from the one that reliably OOMs a coordinating node. | open |
| 5 | `client.py` `_resolve_backend` | Two bare `except Exception` collapse 401 and TLS failures into "Check OPENSEARCH_DASHBOARDS_URL / OPENSEARCH_URL" — and that message names `OPENSEARCH_URL` even when only the Dashboards URL was ever configured. | open |
| 6 | `client.py` `_resolve_backend` / `list_index_patterns` | Any non-200 from `/api/status` (401, 403, an SSO redirect) permanently demotes the backend for the process lifetime, and `list_index_patterns` — the documented fallback for a 403 on `_cat/indices` — then reports "Set OPENSEARCH_DASHBOARDS_URL to enable it" *while it is set*. No re-probe, no failover in either direction; recovery requires restarting the MCP server. | open |
| 21 | `client.py` `last_query_ms`, `server.py` logging | `last_query_ms` is written four times and read never, and is written *after* `_raise_for_status` so failures record nothing. `logging.basicConfig(level=WARNING)` on the root logger suppresses the only two lines identifying the active backend, and there is no log-level env var. Combined with #6, an operator debugging a silent demotion has neither a log line nor latency data. | open |
| 23 | `server.py` `get_client` | Check-then-assign on a module global is not atomic under FastMCP's worker thread pool: two concurrent first calls both probe and one `requests.Session` leaks. Thereafter all threads share one `Session` (not thread-safe) and mutate `backend` / `server_version` / `last_query_ms` unguarded, against a default `pool_maxsize=10`. The README advertises concurrent tool execution. | open |

## Credential handling

| # | Location | Defect | Status |
|---|---|---|---|
| 12 | `client.py` `_load_config` vs `init_client` | `config.json` is hard-gated at 0600, but the `.env` path has **no protection at all**, and `load_dotenv()` with no argument resolves from the directory of `lib/client.py` — not cwd, not `~/.config`. So the location the README tells users to create is never read on the `pip install` path, while a `.env` beside the source is loaded silently regardless of mode. | open |
| 15 | `setup.sh` | The config file is created at the default umask with the password already written into it, and `chmod 600` runs 16 lines later; a `set -e` failure in between leaves a 0644 file containing the secret. (Heredoc quoting and the key names are correct — do not "fix" those.) | in progress |

## Packaging, docs and release

| # | Location | Defect | Status |
|---|---|---|---|
| 17 | `.github/workflows/release.yml` | The `pypi` job gates on tag-vs-`pyproject.toml`; the `docker` job has no gate and no `needs:`, so a mistagged release fails PyPI and simultaneously publishes `:latest` from mismatched code. | in progress |
| 18 | `pyproject.toml` | `include = ["server.py", "lib/"]` with no package directory means the wheel claims the global names `server` and `lib` in site-packages. Any environment providing its own top-level `lib` shadows this one and `from lib.client import init_client` fails at MCP startup, where it is invisible except as "server failed to start". This is the path the README calls recommended. | open |
| 19 | `CHANGELOG.md` | The 0.3.3 entry omits the path-traversal fix that shipped in 0.3.3, so a consumer pinned to 0.3.2 cannot tell they are running with a known hole. | in progress |
| 13 | `README.md` | Claims 4 prompts and lists "alert triage", but `triage_alerts` appears zero times in 27 KB. | in progress |
| 20 | `README.md` | Documents the deleted denylist as the security model; omits `_close` from its enumeration; drops the `{"result": [...]}` array-wrapping contract and the `_cat/plugins` example — the very case that gets wrapped. | in progress |
| 14 | `setup.sh` / `Makefile` / `README.md` | The documented setup path is broken end to end: `setup.sh` writes `config.json`, every Docker target requires a `.env` that nothing creates, and the container never receives `config.json`. | in progress |
| — | `README.md` | Caps documented as absolute ("hard cap 200") are env-overridable; `OPENSEARCH_ALLOW_INSECURE_CONFIG` is undocumented; performance figures ("237 KB", "4–5 s on 50 M docs", "15×") are unsourced, and the 237 KB figure is quoted for 50 docs against an advertised cap of 200. | in progress |
| 16 | `client.py` `_dashboards_proxy` | `json=body or {}` makes every proxied GET carry a `{}` body, which the direct backend does not send. Confirmed on the wire; the downstream rejection is **unverified** without a real OSD+OpenSearch pair. The reportable defect is that nothing proves the two backends are equivalent and one line makes them differ. Param double-encoding was checked and is **not** a live bug. | open |

## Needs a maintainer decision, not a fix

| Location | Finding |
|---|---|
| git tags vs `CHANGELOG.md` vs `pyproject.toml` | **Only `v0.3.0`–`v0.3.3` exist as git tags**, yet the changelog carries dated `0.3.4` and `0.4.0` entries and `pyproject.toml` says `0.4.0`. Verified. So either two releases were published outside the tag-gated workflow (`make push` bypasses it entirely), or two changelog entries describe work that was never released. The release workflow only fires on `v*` tags, so nothing detected this — and the new `version` gate cannot fire until a tag is pushed either. Decide which it is: tag retroactively, or move those entries under `[Unreleased]`. |
| ADR 0004 exclusion list | The read-only guard's accepted risks need explicit sign-off, since a security decision is not self-approvable: the `/_cat` family allowed wholesale (including `repositories` and `snapshots` — names and types, never credentials), `/<index>/_settings` allowed, and index-level authorization left entirely to the backend. |
| `terms` / `multi_terms` / `discover_fields` `_warning` key | Fixing the namespace collision changes tool output shape, which affects any agent already consuming these tools. Deliberately excluded from the round-2 fix batch pending this call. |
| `~/.env` (outside the repo) | Mode `-rw-r--r--`, holding 13 live credentials across four systems, and loaded into this server's process by `load_dotenv()` via the repo-root symlink. `chmod 600 ~/.env` is the immediate fix. The project-side defect — `config.json` is hard-gated at 0600 while the `.env` path has no check at all — is backlog #12. |

## Root cause

| # | Finding | Status |
|---|---|---|
| 24 | `OpenSearchClient.__init__` builds its own `requests.Session` and mounts its own adapters, with no injection point; `server.py` imports the concrete adapter directly, so there is no port and no seam; `get_client()` is the singleton doc `05` warns against; and pure domain logic (`_is_likely_text_field`, `_parse_ts`, `_check_histogram_buckets`, `_flatten_doc`, `_flatten_mapping`) shares a 1039-line module with the HTTP adapter. Concrete evidence: the auditor had to stand up a threaded HTTP server to test `_parse_ts`, a pure string-to-float function. **Findings 7, 9, 10 and 11 each shipped because they were two lines of pure-function test away from being caught.** | open |

## Checked and found clean

Recorded so nobody re-audits them: the `server.py` module docstring matches the tool
set exactly in both directions (22 tools, 22 names, same order; 4 prompts). The README
tool inventory has no drift — all 22 present, no phantoms, and every documented default
matches its signature. `setup.sh`'s config keys match `init_client` exactly and its
heredoc quoting is correct. `.env` is properly gitignored and never tracked. The missing
`.dockerignore` is not a leak — the Dockerfile copies only three paths — its only cost is
build-context bloat. Retrying POST is safe as the code stands, since every POST is an
idempotent read; it becomes an amplifier only if a write ever passes the path guard.
Dashboards param double-encoding balances out and is not a live bug.

## Related

- `docs/adr/0004` — the read-only enforcement decision, which closed the guard findings.
- Finding 22 from the original audit (four bypass call sites, not two) was addressed by
  ADR 0004's deletion of `raw_get`/`raw_post`; the two remaining direct `_session` calls
  use fixed literal paths, which is what makes the no-bypass fitness function expressible.
