# Changelog

## [Unreleased]

### Changed
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
- README: documented the deleted denylist as the security model, claimed "4 investigation prompts" while `triage_alerts` appeared nowhere in the file, omitted the `{"result": [...]}` array-wrapping contract for `opensearch_api`, described env-overridable caps as absolute, and did not document `OPENSEARCH_ALLOW_INSECURE_CONFIG`. Unsourced performance figures ("50 full docs ≈ 237 KB", "4–5 s on 50 M docs", "up to 15×") were removed rather than restated — no benchmark exists behind them.

### Security
- `setup.sh` writes `config.json` under `umask 077` instead of creating it at the default umask and `chmod 600`-ing it sixteen lines later. The password was written into a 0644 file first, leaving a window in which any local user could read it — and leaving that file behind if the write failed under `set -e`. The `chmod` is kept as belt-and-braces. The new `.env` file is created the same way.

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
