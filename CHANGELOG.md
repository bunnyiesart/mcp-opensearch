# Changelog

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
- `opensearch_count` return type corrected (`result` key)
- Warning key ordering made consistent across tools
- Docker image published to GHCR (`ghcr.io/bunnyiesart/mcp-opensearch`)

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
