# Changelog

## [0.4.0] - 2026-07-31

### Added
- `opensearch_get_alerts` — fetch alerts raised by the OpenSearch Alerting plugin, filterable by state (ACTIVE/ACKNOWLEDGED/…) and monitor. Answers "what is firing right now?"
- `opensearch_list_monitors` — list configured Alerting-plugin monitors (detection rules) and their enabled state.
- `opensearch_timeline` — chronological event timeline for a single entity (IP, host, user) matched across multiple fields at once. The core DFIR pivot, previously requiring several manual `opensearch_search` calls.
- `opensearch_list_detectors` — list Anomaly Detection-plugin detectors and the indices they watch.
- `opensearch_get_anomaly_results` — fetch ML-detected anomalies (beaconing, spikes, rare activity), highest anomaly-grade first, filterable by detector, time range, and minimum grade.
- `triage_alerts` prompt — SOC flow that pulls active alerts, maps them to monitors, then pivots on the top offending entity's timeline.
- Read-only allowlist extended with the Alerting and Anomaly Detection read endpoints only. No write paths added — the read-only guarantee is unchanged.

### Fixed
- `opensearch_api`: array responses (e.g. the `_cat/*` APIs) previously crashed with a FastMCP "structured_content must be a dict" error. Array responses are now wrapped as `{"result": [...]}`.

## [0.3.3] - 2026-07-31

### Fixed
- GHCR release workflow: use `GITHUB_TOKEN` instead of `CR_PAT` so Docker image is actually published
- `_dashboards_proxy`: URL-encode query params with `urllib.parse.urlencode` (previously bare string concat could silently corrupt queries with special characters)
- `requirements.txt`: pin `fastmcp>=2,<4` to match `pyproject.toml` (was unpinned, risked pulling fastmcp v5+)
- README: `opensearch_count` example showed wrong key `result` — corrected to `count`
- `User-Agent` header: replaced fake Chrome UA with `mcp-opensearch/0.3.3`
- `setup.sh`: added Python 3.10+ version check before proceeding

---

## [0.3.2] - 2026-06-26

### Added
- GitHub Actions CI: ruff lint on every push and PR to main
- GitHub Actions Release: auto-publish to PyPI and push Docker image to GHCR on version tags
- PyPI installation path and badges documented in README
- Parallel request support documented in README

### Fixed
- Docker action versions upgraded (`login-action@v4`, `build-push-action@v6`) to suppress Node 20 deprecation warnings
- Release workflow now verifies `pyproject.toml` version matches the pushed tag before building
- `list_index_patterns` now falls back to `/api/data_views` when `/api/saved_objects/_find` returns 404
- `fastmcp` dependency pinned to `>=2,<4`
- Dockerfile: pinned to `python:3.12.10-slim`, runs as non-root user

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
- `raw_get` / `raw_post` bypass methods on `OpenSearchClient` for tools that need escape-hatch access

### Changed
- Rewrote all 12 existing tool descriptions with decision-rule format (when to use, when not to, key failure modes)

---

## [0.2.0] and earlier

Initial releases — core search, aggregation, and connectivity tools.
