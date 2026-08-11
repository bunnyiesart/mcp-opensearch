# Changelog

## [Unreleased]

### Added
- **Test suite: 456 tests, none touching the network.** The project had none. An autouse fixture replaces `HTTPAdapter.send` and `Session.request` with a hard failure, so a test that tries to reach a cluster fails loudly instead of passing against a stub that was never installed. `xfail_strict = true`, so a known-bug test that starts passing turns the build red until its marker is removed and cannot decay into a permanent exemption.
- **Architectural fitness functions** (`tests/test_architecture.py`, `tests/test_project_consistency.py`) enforcing what the ADRs decided: no method may reach the HTTP session without passing `_check_path`; `raw_get`/`raw_post` cannot be reintroduced; every example path in the `opensearch_api` docstring must pass the guard; `server.py` imports no HTTP library and holds no path validation; tool bodies stay within a statement budget; no version literal exists outside `pyproject.toml`; `requirements.txt` and `[project.dependencies]` agree; and the CI matrix covers the declared `requires-python` floor and every version claimed in the classifiers.
- CI runs the suite on Python 3.10–3.13 (the floor tracks `requires-python` — an untested floor is an unverified promise) and pins `ruff` to an exact version, so a lint failure always means the code changed rather than the tool.
- `opensearch_search` and `opensearch_timeline` return an `ids` list, index-aligned with `hits`, so `ids[i]` is the `_id` of `hits[i]`. **This is what makes `opensearch_explain` usable at all** — previously no tool could produce a document id.
- `opensearch_test` returns `latency_ms`.
- `OPENSEARCH_LOG_LEVEL`, `OPENSEARCH_MAX_TERMS_SIZE` and `OPENSEARCH_MAX_AGGREGATIONS`.

### Changed
- **`OPENSEARCH_TIMEOUT` is now the total wall-clock budget for one HTTP operation, retries included, rather than a per-attempt timeout.** Retries previously multiplied it: against a hung cluster a single tool call took 4 × timeout + backoff — a measured **246 s** at the default, or ~492 s for `opensearch_compare`, which makes two calls. Any MCP client with a 60–120 s tool timeout abandoned the call while this server kept hammering a cluster that was already shedding load. Now measured at **60 s** for the same scenario. A legitimately slow query still gets the entire budget on its first attempt, so "slow" has not become "impossible". Read timeouts are deliberately not retried — the cluster is still executing the query, and retrying doubles load it is already failing to carry.
- **HTTP 429 is deliberately not retried.** It means the cluster rejected the work, so repeating the same oversized aggregation is precisely wrong. It now returns a message naming the remedy — narrow the time range, reduce `size` — instead of a bare `HTTP 429`.
- **`opensearch_test` always performs a real probe.** It previously read a cached resolution, and because `init_client` warms that cache at startup, even the *first* call did no I/O — so the tool whose entire job is confirming connectivity would report `ok: true` against a cluster that had since died. Two consequences: it costs one round trip, and because it re-runs the backend preference order it can change the active backend mid-session.
- Backend resolution is no longer cached for the process lifetime. A degraded resolution (Dashboards configured, direct won) is re-probed after five minutes, and a transport failure drops the cache immediately so failover works in either direction. The preferred backend is not re-probed on a schedule, because a probe before every request would add a round trip to everything to detect a failure the request itself already reports. See [ADR 0002](docs/adr/0002-dashboards-proxy-first-with-direct-fallback.md), amended.
- **`opensearch_stats` returns `null`, not `0`, for every metric when no document matches.** OpenSearch returns those keys present-but-null for an empty result set, so the old `.get(key, 0)` defaults never fired and callers received `None` from a method whose docstring promised numbers. `0` would claim a measurement never taken and be indistinguishable from a real minimum of zero. Check `count` before doing arithmetic.
- Caps added where they were missing: `opensearch_terms` `size` (default 1,000), `opensearch_multi_terms` aggregation count (default 20), and the size of `opensearch_get_alerts` / `opensearch_get_anomaly_results`. Guards previously covered only the cheap operations while a high-cardinality terms aggregation — the most reliable way to exhaust a coordinating node — accepted any value. **Every clamp is announced**, because silently returning 200 of 10,000 records is how an agent concludes it has seen everything and stops looking.
- `opensearch_get_mapping` lists `fields` multi-fields, so the `.keyword` companion of a `text` field appears at its real dotted path. The aggregation warnings advise appending `.keyword`, and this is the tool used to confirm such a field exists — so the server was recommending a field it could not show you.
- `opensearch_discover_fields` descends into arrays of objects, so fields inside `rule.mitre.*` and Windows `eventdata` arrays are listed instead of the array being reported only as `"list"`. It also propagates the no-time-range warning it previously discarded.
- `opensearch_histogram` routes `w`/`M`/`y` to `calendar_interval` and `s`/`m`/`h`/`d` to `fixed_interval`. Calendar units were previously placed in `fixed_interval`, which OpenSearch accepts only for `ms/s/m/h/d`, so a weekly or monthly histogram passed local validation and then failed at the cluster as "Bad request: check your query syntax". `calendar_interval` permits a multiplier of exactly 1, so `2w` is now rejected locally with an explanation.
- `opensearch_multi_terms` rejects a duplicate aggregation `id` and a spec missing `id` or `field` with a clear `ValueError`. Duplicates previously collapsed into one result, so the caller received fewer answers than questions with no way to tell which field the numbers belonged to.
- Timestamp parsing accepts numeric UTC offsets (`+00:00`, `+0000`, `-03:00`), `HH:MM` precision, any number of fractional digits, and a lowercase `z`. `datetime.now(timezone.utc).isoformat()` emits `+00:00`, so the most natural way to express "now in UTC" was rejected by the one tool that validated timestamps while `search`/`count`/`terms` accepted the same string.
- The text-field heuristic matches whole tokens in the last dotted segment instead of substrings, so `logonType`, `logonProcessName`, `catalogId` and `logger` are no longer flagged merely for containing `log`. `process.commandline` is now flagged.
- `_coerce_bool` accepts the conventional falsey set (`false`, `0`, `no`, `off`, `n`), strips whitespace, and raises a `ValueError` naming the variable for anything ambiguous. Previously only the exact string `"false"` was falsey, so `OPENSEARCH_VERIFY_SSL=0` silently did **not** disable certificate verification.
- The compare diff moved out of the tool function into `lib/compare.py` as pure logic. It was the only behaviour in the codebase that could not be exercised without FastMCP.
- Logging goes to **stderr** and no longer reconfigures the root logger. Configuring the root at import silenced `lib.client`'s INFO lines, including the only two reporting which backend was selected — so an operator debugging a backend problem had neither a log line nor any latency figure. stdout is the MCP stdio transport, so a log line written there corrupts the protocol stream.

- **Potentially BREAKING — `opensearch_api` and `opensearch_explain` are now subject to the read-only allowlist.** Both tools previously took a caller-supplied path and sent it through bypass methods that skipped every check in the codebase; the only guard on `opensearch_api` was a substring denylist of write verbs. Enforcement is now the single `_ALLOWED_PATHS` allowlist in `lib/client.py`, checked on every outbound request. **Any `opensearch_api` call that reached an endpoint not on that allowlist will now be refused.** Endpoints deliberately excluded: `/_plugins/_security/*` (internal user database and password hashes), `/_cluster/settings` and `/_cluster/state` (carry snapshot-repository credentials), `/_snapshot/*` (repository definitions including cloud credentials), and bare `/_nodes` (echoes `opensearch.yml`). The `/_cat` family, `/_cluster/health`, `/_cluster/stats`, `/_cluster/pending_tasks`, `/_nodes/stats|usage|hot_threads`, `/_alias`, `/_mapping`, `/_template`, `/_index_template`, `/_plugins/_ism/policies`, `/_plugins/_ism/explain`, `/_plugins/_alerting/monitors/alerts` and the index-scoped `/<index>/_mapping|_settings|_alias|_stats|_shard_stores` remain available. Refusal messages are generated from the allowlist, so they list what is actually permitted. See [ADR 0004](docs/adr/0004-single-allowlist-as-the-read-only-enforcement-point.md).
- Path matching changed from suffix matching to two explicit modes — exact-or-sub-resource for cluster and plugin endpoints, and `/<index>/<action>` for index-scoped ones. Combined with the denylist's removal this **fixes two false refusals**: `/_plugins/_ism/policies/hot_rollover_policy` and `/_cat/indices/my_create_index` now work. Both were previously rejected twice over — by the denylist, because they contain the substrings `_rollover` and `_create`, and by suffix matching, which no sub-resource path could satisfy. `/<index>/_explain/<doc_id>` is likewise now expressible, which no suffix entry could match because the path ends in the document id.
- `/api/console/proxy` removed from the allowlist. It is a tunnel — the real method and path travel in its query string — so an entry for it would authorize anything. The Dashboards backend is unaffected: `_dashboards_proxy()` builds that request from an already-checked inner path.
- `Makefile` derives `VERSION` from `pyproject.toml` instead of declaring its own copy, and `make push` fails loudly if that read comes back empty. The two had drifted (`Makefile` said `0.3.3` while `pyproject.toml` said `0.4.0`), so `make push` was tagging images with the wrong version and moving `latest` onto them. See [ADR 0003](docs/adr/0003-single-source-of-truth-for-version.md).
- Release workflow: the tag-versus-`pyproject.toml` check moved into its own `version` job that both `pypi` and `docker` depend on. Previously only `pypi` was gated and `docker` had no `needs:`, so a mistagged release failed PyPI while simultaneously publishing the image and moving `:latest` onto code that self-reports a different version.
- `setup.sh` now also writes `~/.config/mcp-opensearch/.env` in `docker --env-file` format (plain `KEY=value`), alongside `config.json`. The documented Docker path — `make run`, `make shell`, and the `docker run --env-file` command in the README — required that file and nothing created it, so a user following the README got `docker: open ~/.config/mcp-opensearch/.env: no such file or directory`.

### Removed
- **`raw_get` and `raw_post` on `OpenSearchClient` are deleted** (added in 0.3.0, see below). Their docstrings read "Caller owns validation", and they skipped the allowlist and the traversal check by design. `_get`/`_post` are now the only exits from the class and both check first, so the read-only guarantee is structural rather than conventional. `opensearch_api` calls a narrow `api_get`; `explain` calls `_post`.
- `_WRITE_PATH_FRAGMENTS` (the write-verb denylist), `_EXPLAIN_PATH_RE` and `import re` are gone from `server.py`. All path validation lives in `lib/client.py`.

### Fixed
- **`opensearch_timeline` corrupted any entity containing a backslash.** Only `"` was escaped, never `\`, so `C:\Windows\System32` reached the cluster with `\W` and `\S` consumed as Lucene escapes and stopped matching the document it came from — a silently wrong answer on this tool's primary input. An entity ending in a backslash escaped the closing quote, leaving an unterminated phrase whose 400 surfaced as "check your query syntax".
- `opensearch_index_settings` crashed with `AttributeError` on an index whose ISM policy had been detached. `"lifecycle": null` is present-but-null, which the old `.get("lifecycle", {})` did not defend against, so the chained `.get` took down the whole tool call instead of reporting no policy. The same idiom silently defeated the documented `"1s"` `refresh_interval` fallback. Every instance of this pattern was audited and fixed.
- Backend resolution errors name the real cause per backend attempted — auth failed, TLS verification failed, unreachable, redirected to SSO, non-JSON login page, or an HTTP status — instead of collapsing all of them into "Check OPENSEARCH_DASHBOARDS_URL / OPENSEARCH_URL". That message also named `OPENSEARCH_URL` even when only the Dashboards URL had been configured, telling the operator to check a variable they never set.
- `opensearch_list_index_patterns` no longer claims `OPENSEARCH_DASHBOARDS_URL` is unset when it is set. Any non-200 from `/api/status` — a 401, a 403, an SSO redirect — used to demote the backend permanently and this tool, the documented fallback for a 403 on `_cat/indices`, then reported the wrong reason.
- Retry exhaustion, connection failures and TLS errors are translated into the same actionable error family as HTTP errors. They raise `RetryError`/`ConnectionError`/`SSLError` rather than `HTTPError`, so they bypassed the error-message layer entirely and reached the caller as raw urllib3 internals including the internal host and port.
- Status errors include the cluster's own `error.reason`, so a message like `unknown field [agent.nmae]` is no longer discarded.
- An HTML login page returned with HTTP 200 produces a readable error instead of a raw `JSONDecodeError`.
- `opensearch_stats` no longer misattributes every HTTP 400 to "field is not numeric". A malformed Lucene filter on a genuinely numeric field produced advice telling the caller to use `rule.level` instead of `rule.level`.
- Sorts declare `unmapped_type`, so a search over an index lacking the sort field degrades instead of returning 400. This made the documented 403 fallback chain circular: `opensearch_discover_fields` sorts on `@timestamp` by default and so failed on exactly the indices whose mapping the caller could not read, while `opensearch_get_mapping` was the only way to learn the timestamp field's name.
- `_check_histogram_buckets` raises `ValueError` rather than leaking `AttributeError` for a `None` bound or `ZeroDivisionError` for a zero-width interval, and rejects a reversed time range instead of silently clamping it to one plausible-looking bucket.
- A histogram bucket carrying only `key_as_string` no longer raises `KeyError`; the default was evaluated eagerly, indexing `key` even when the label was already present.
- A negative `limit` is clamped instead of being forwarded to the cluster verbatim.
- Proxied GETs no longer carry a `{}` JSON body, which the direct backend never sent. OpenSearch rejects a body on handlers that declare none, so the two backends were not behaviourally equivalent.
- `get_client()` was a check-then-assign race. FastMCP dispatches synchronous tool functions on a worker thread pool, so two concurrent first calls both probed the backend and one `requests.Session` was silently orphaned.
- `opensearch_explain`'s docstring no longer tells the caller to obtain a document id by passing `_id` in `source_fields`. That never worked — `_id` is document metadata, not a `_source` field — and the docstring contradicted itself in its own text.
- README: documented the deleted denylist as the security model, claimed "4 investigation prompts" while `triage_alerts` appeared nowhere in the file, omitted the `{"result": [...]}` array-wrapping contract for `opensearch_api`, described env-overridable caps as absolute, and did not document `OPENSEARCH_ALLOW_INSECURE_CONFIG`. Unsourced performance figures ("50 full docs ≈ 237 KB", "4–5 s on 50 M docs", "up to 15×") were removed rather than restated — no benchmark exists behind them.

### Security
- `setup.sh` writes `config.json` under `umask 077` instead of creating it at the default umask and `chmod 600`-ing it sixteen lines later. The password was written into a 0644 file first, leaving a window in which any local user could read it — and leaving that file behind if the write failed under `set -e`. The `chmod` is kept as belt-and-braces. The new `.env` file is created the same way.
- **The 0600 permission gate now covers every credential file the process reads, not just `config.json`.** `load_dotenv()` was previously called with no argument, so `find_dotenv()` walked up from the directory of the installed package. Two consequences, both fixed: the `~/.config/mcp-opensearch/.env` that `setup.sh` writes and the README documents was **never read by the Python process at all**, and a `.env` sitting beside the source was read silently whatever its mode — including world-readable. Both documented paths are now loaded explicitly and both are gated, honouring `OPENSEARCH_ALLOW_INSECURE_CONFIG` the same way `config.json` does. A world-readable password file is precisely what the check exists for, so the rule is now the same everywhere rather than strict in one place and absent in another.

---

## [0.4.0] - 2026-07-31

### Added
- `opensearch_get_alerts` — fetch alerts raised by the OpenSearch Alerting plugin, filterable by state (ACTIVE/ACKNOWLEDGED/…) and monitor. Answers "what is firing right now?"
- `opensearch_list_monitors` — list configured Alerting-plugin monitors (detection rules) and their enabled state.
- `opensearch_timeline` — chronological event timeline for a single entity (IP, host, user) matched across multiple fields at once. The core DFIR pivot, previously requiring several manual `opensearch_search` calls.
- `opensearch_list_detectors` — list Anomaly Detection-plugin detectors and the indices they watch.
- `opensearch_get_anomaly_results` — fetch ML-detected anomalies (beaconing, spikes, rare activity), highest anomaly-grade first, filterable by detector, time range, and minimum grade.
- `triage_alerts` prompt — SOC flow that pulls active alerts, maps them to monitors, then pivots on the top offending entity's timeline.
- Read-only allowlist extended with the Alerting and Anomaly Detection read endpoints only. No write paths added — the read-only guarantee is unchanged.

## [0.3.4] - 2026-07-31

### Fixed
- `opensearch_api`: array responses (e.g. the `_cat/*` APIs) previously crashed with a FastMCP "structured_content must be a dict" error. Array responses are now wrapped as `{"result": [...]}`.

## [0.3.3] - 2026-07-31

### Security
- **Path traversal in the `_check_path` allowlist guard is blocked** (`e45e596`). `posixpath.normpath` silently resolved `..` segments, so a crafted index name such as `../_cluster` passed the suffix-match allowlist while the outbound HTTP request reached a cluster-wide admin endpoint. Any path whose normalised form differs from the original is now rejected before the allowlist check runs. **Anyone pinned to 0.3.2 or earlier is running with this hole open.**

### Added
- LICENSE file (MIT)

### Fixed
- GHCR release workflow: use `GITHUB_TOKEN` instead of `CR_PAT` so Docker image is actually published
- `_dashboards_proxy`: URL-encode query params with `urllib.parse.urlencode` (previously bare string concat could silently corrupt queries with special characters)
- `requirements.txt`: pin `fastmcp>=2,<4` to match `pyproject.toml` (was unpinned, risked pulling fastmcp v5+)
- README: `opensearch_count` example showed wrong key `result` — corrected to `count`
- `User-Agent` header: replaced fake Chrome UA with `mcp-opensearch/0.3.3`
- `setup.sh`: added Python 3.10+ version check before proceeding
- Docker action versions upgraded (`login-action@v4`, `build-push-action@v6`) to suppress Node 20 deprecation warnings
- Release workflow now verifies `pyproject.toml` version matches the pushed tag before building
- `list_index_patterns` now falls back to `/api/data_views` when `/api/saved_objects/_find` returns 404
- `fastmcp` dependency pinned to `>=2,<4` in `pyproject.toml`
- Dockerfile: pinned to `python:3.12.10-slim`, runs as non-root user

> The last five entries were previously listed under 0.3.2. They shipped in `31ed35e`, which
> lands *after* the `v0.3.2` tag (`63ecfd4`), so the `v0.3.2` release does not contain them.
> Corrected here rather than rewritten in place.

---

## [0.3.2] - 2026-06-26

### Added
- GitHub Actions CI: ruff lint on every push and PR to main
- GitHub Actions Release: auto-publish to PyPI and push Docker image to GHCR on version tags
- PyPI installation path and badges documented in README
- Parallel request support documented in README

---

## [0.3.1] - 2026-06-20

### Fixed
- `opensearch_count` return type corrected (`count` key, was `result`)
- Warning key ordering made consistent across tools

---

## [0.3.0] - 2026-06-18

### Added
- 5 new tools: `opensearch_ppl`, `opensearch_api`, `opensearch_explain`, `opensearch_index_settings`, `opensearch_compare`
- 3 investigation prompts: `investigate_alert`, `top_offenders`, `compare_time_windows`
- PyPI package (`pip install mcp-opensearch`)
- `raw_get` / `raw_post` bypass methods on `OpenSearchClient` for tools that need escape-hatch access — **removed again in Unreleased; see that entry before relying on this.** They skipped the read-only allowlist by design, which is why they are gone.

### Changed
- Rewrote all 12 existing tool descriptions with decision-rule format (when to use, when not to, key failure modes)

---

## [0.2.0] and earlier

Initial releases — core search, aggregation, and connectivity tools.
