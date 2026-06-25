"""
OpenSearch MCP client — read-only.

Tries OpenSearch Dashboards first (via /api/console/proxy),
falls back to direct OpenSearch REST API on failure.

Auth: basic auth (username + password).
Config priority: env vars > ~/.config/mcp-opensearch/config.json
"""

import json
import logging
import os
import posixpath
import stat

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

CONFIG_DIR = os.path.expanduser("~/.config/mcp-opensearch")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

BACKEND_DASHBOARDS = "dashboards"
BACKEND_OPENSEARCH = "opensearch"

# Read-only path allowlist — suffix match
# e.g. /wazuh-alerts-*/_search matches "/_search"
_ALLOWED_PATHS = {
    "GET": [
        "/_cat/indices",
        "/_cluster/health",
        "/_mapping",
        "/api/status",
        "/api/index_patterns/index_pattern",
    ],
    "POST": [
        "/_search",
        "/_count",
        "/_msearch",
        "/api/console/proxy",   # Dashboards proxy (carries the real path)
    ],
}


class OpenSearchClient:
    """Read-only client for OpenSearch / OpenSearch Dashboards.

    On first use, probes Dashboards (/api/status). On failure, falls back to
    direct OpenSearch. All writes are blocked via _check_path().
    """

    def __init__(
        self,
        dashboards_url=None,
        opensearch_url=None,
        username=None,
        password=None,
        verify_ssl=True,
        timeout=60,
    ):
        self.dashboards_url = dashboards_url.rstrip("/") if dashboards_url else None
        self.opensearch_url = opensearch_url.rstrip("/") if opensearch_url else None
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self.backend = None
        self.server_version = None
        self.last_query_ms = 0

        self._session = requests.Session()
        if username and password:
            self._session.auth = (username, password)

        self._session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "osd-xsrf": "true",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        })

        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[502, 503, 504],
            allowed_methods=["GET", "POST"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)

    # ── Read-only guard ───────────────────────────────────────

    def _check_path(self, method: str, path: str):
        """Block any path not in the read-only allowlist.

        Uses suffix-match so /wazuh-alerts-*/_search passes "/_search".
        """
        clean = posixpath.normpath(path.split("?")[0])
        allowed = _ALLOWED_PATHS.get(method, [])
        if not any(clean == p or clean.endswith(p) for p in allowed):
            raise PermissionError(
                f"Blocked {method} {path} — not in the read-only allowlist. "
                "Only search, count, mapping, and discovery calls are permitted."
            )

    # ── Backend resolution ────────────────────────────────────

    def _resolve_backend(self):
        """Probe Dashboards first, then direct OpenSearch. Raises if both fail."""
        if self.backend is not None:
            return

        if self.dashboards_url:
            try:
                r = self._session.get(
                    f"{self.dashboards_url}/api/status",
                    verify=self.verify_ssl,
                    timeout=10,
                )
                if r.status_code == 200:
                    data = r.json()
                    self.backend = BACKEND_DASHBOARDS
                    self.server_version = data.get("version", {}).get("number", "?")
                    logger.info(
                        "Backend: OpenSearch Dashboards %s (v%s)",
                        self.dashboards_url, self.server_version,
                    )
                    return
                logger.warning(
                    "Dashboards returned HTTP %s — trying direct OpenSearch",
                    r.status_code,
                )
            except Exception as exc:
                logger.warning("Dashboards unreachable (%s) — trying direct OpenSearch", exc)

        if self.opensearch_url:
            try:
                r = self._session.get(
                    f"{self.opensearch_url}/",
                    verify=self.verify_ssl,
                    timeout=10,
                )
                r.raise_for_status()
                data = r.json()
                self.backend = BACKEND_OPENSEARCH
                self.server_version = data.get("version", {}).get("number", "?")
                logger.info(
                    "Backend: direct OpenSearch %s (v%s)",
                    self.opensearch_url, self.server_version,
                )
                return
            except Exception as exc:
                logger.error("Direct OpenSearch also unreachable: %s", exc)

        raise RuntimeError(
            "Could not connect to OpenSearch Dashboards or direct OpenSearch. "
            "Check OPENSEARCH_DASHBOARDS_URL / OPENSEARCH_URL."
        )

    # ── Low-level requests ────────────────────────────────────

    def _get(self, path: str, params: dict = None) -> dict:
        self._check_path("GET", path)
        self._resolve_backend()
        if self.backend == BACKEND_DASHBOARDS:
            return self._dashboards_proxy("GET", path, params=params)
        r = self._session.get(
            f"{self.opensearch_url}{path}",
            params=params,
            verify=self.verify_ssl,
            timeout=self.timeout,
        )
        r.raise_for_status()
        self.last_query_ms = int(r.elapsed.total_seconds() * 1000)
        return r.json()

    def _post(self, path: str, body: dict = None, params: dict = None) -> dict:
        self._check_path("POST", path)
        self._resolve_backend()
        if self.backend == BACKEND_DASHBOARDS:
            return self._dashboards_proxy("POST", path, body=body, params=params)
        r = self._session.post(
            f"{self.opensearch_url}{path}",
            json=body,
            params=params,
            verify=self.verify_ssl,
            timeout=self.timeout,
        )
        r.raise_for_status()
        self.last_query_ms = int(r.elapsed.total_seconds() * 1000)
        return r.json()

    def _dashboards_proxy(
        self, method: str, path: str, body: dict = None, params: dict = None
    ) -> dict:
        """Route an OpenSearch request through Dashboards /api/console/proxy."""
        os_path = path.lstrip("/")
        if params:
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            os_path = f"{os_path}?{qs}"

        r = self._session.post(
            f"{self.dashboards_url}/api/console/proxy",
            params={"path": os_path, "method": method},
            json=body or {},
            verify=self.verify_ssl,
            timeout=self.timeout,
        )
        r.raise_for_status()
        self.last_query_ms = int(r.elapsed.total_seconds() * 1000)
        return r.json()

    # ── Query builders ────────────────────────────────────────

    def _with_time_range(
        self, query: dict, from_ts: str, to_ts: str, ts_field: str
    ) -> dict:
        """Wrap a query dict in a bool filter adding a time range."""
        if not from_ts and not to_ts:
            return query or {"match_all": {}}
        range_clause = {"range": {ts_field: {}}}
        if from_ts:
            range_clause["range"][ts_field]["gte"] = from_ts
        if to_ts:
            range_clause["range"][ts_field]["lte"] = to_ts
        return {
            "bool": {
                "must": query or {"match_all": {}},
                "filter": [range_clause],
            }
        }

    def _qs_query(
        self, query_string: str, from_ts: str, to_ts: str, ts_field: str
    ) -> dict:
        base = {"query_string": {"query": query_string or "*", "analyze_wildcard": True}}
        return self._with_time_range(base, from_ts, to_ts, ts_field)

    # ── Mapping helper ────────────────────────────────────────

    def _flatten_mapping(self, properties: dict, prefix: str = "") -> dict:
        fields = {}
        for name, cfg in properties.items():
            full = f"{prefix}.{name}" if prefix else name
            fields[full] = cfg.get("type", "object")
            if "properties" in cfg:
                fields.update(self._flatten_mapping(cfg["properties"], prefix=full))
        return fields

    # ── Public read-only API ──────────────────────────────────

    def test_connection(self) -> dict:
        """Probe connectivity. Returns backend, version, and URL."""
        self._resolve_backend()
        return {
            "ok": True,
            "backend": self.backend,
            "version": self.server_version,
            "url": (
                self.dashboards_url
                if self.backend == BACKEND_DASHBOARDS
                else self.opensearch_url
            ),
        }

    def cluster_health(self) -> dict:
        """Get OpenSearch cluster health."""
        return self._get("/_cluster/health")

    def list_indices(self) -> list:
        """List all indices with doc count, size, and health."""
        data = self._get(
            "/_cat/indices",
            params={"format": "json", "h": "index,docs.count,store.size,health,status"},
        )
        if isinstance(data, list):
            return sorted(data, key=lambda x: x.get("index", ""))
        return data

    def list_index_patterns(self) -> list:
        """List Dashboards saved index patterns. Dashboards backend only."""
        self._resolve_backend()
        if self.backend != BACKEND_DASHBOARDS:
            raise RuntimeError(
                "list_index_patterns requires the OpenSearch Dashboards backend. "
                "Set OPENSEARCH_DASHBOARDS_URL to enable it."
            )
        r = self._session.get(
            f"{self.dashboards_url}/api/index_patterns/index_pattern",
            verify=self.verify_ssl,
            timeout=self.timeout,
        )
        r.raise_for_status()
        data = r.json()
        patterns = data.get("index_pattern", [])
        if not isinstance(patterns, list):
            patterns = [patterns] if patterns else []
        return [
            {
                "id": p.get("id"),
                "title": p.get("attributes", {}).get("title"),
                "timeFieldName": p.get("attributes", {}).get("timeFieldName"),
            }
            for p in patterns
        ]

    def get_mapping(self, index: str) -> dict:
        """Flattened field mappings for an index. Returns {index: {field: type}}."""
        result = self._get(f"/{index}/_mapping")
        return {
            idx: self._flatten_mapping(mapping.get("mappings", {}).get("properties", {}))
            for idx, mapping in result.items()
        }

    def discover_fields(
        self,
        index: str,
        query_string: str = "*",
        from_ts: str = None,
        to_ts: str = None,
        ts_field: str = "@timestamp",
        sample_size: int = 10,
    ) -> dict:
        """Sample documents and return {field_name: python_type}."""
        result = self.search_string(
            index,
            query_string=query_string,
            from_ts=from_ts,
            to_ts=to_ts,
            ts_field=ts_field,
            limit=sample_size,
        )
        fields = {}
        for hit in result.get("hits", []):
            for k, v in hit.items():
                if k not in fields:
                    fields[k] = type(v).__name__
        return fields

    def search_string(
        self,
        index: str,
        query_string: str = "*",
        from_ts: str = None,
        to_ts: str = None,
        ts_field: str = "@timestamp",
        limit: int = 50,
        sort_field: str = None,
        sort_dir: str = "desc",
        source_fields: list = None,
    ) -> dict:
        """Search with a Lucene query string. Returns {"total": N, "hits": [...]}."""
        q = self._qs_query(query_string, from_ts, to_ts, ts_field)
        body = {
            "query": q,
            "size": limit,
            "sort": [{(sort_field or ts_field): {"order": sort_dir}}],
        }
        if source_fields:
            body["_source"] = source_fields
        result = self._post(f"/{index}/_search", body=body)
        hits = result.get("hits", {})
        total = hits.get("total", {})
        if isinstance(total, dict):
            total = total.get("value", 0)
        return {
            "total": total,
            "hits": [h.get("_source", {}) for h in hits.get("hits", [])],
        }

    def count(
        self,
        index: str,
        query_string: str = "*",
        from_ts: str = None,
        to_ts: str = None,
        ts_field: str = "@timestamp",
    ) -> int:
        """Count documents matching a query."""
        q = self._qs_query(query_string, from_ts, to_ts, ts_field)
        result = self._post(f"/{index}/_count", body={"query": q})
        return result.get("count", 0)

    def terms(
        self,
        index: str,
        field: str,
        query_string: str = "*",
        from_ts: str = None,
        to_ts: str = None,
        ts_field: str = "@timestamp",
        size: int = 50,
    ) -> dict:
        """Top N values of a field. Returns {value: count} sorted descending."""
        q = self._qs_query(query_string, from_ts, to_ts, ts_field)
        body = {
            "size": 0,
            "query": q,
            "aggs": {"top_values": {"terms": {"field": field, "size": size}}},
        }
        result = self._post(f"/{index}/_search", body=body)
        buckets = result.get("aggregations", {}).get("top_values", {}).get("buckets", [])
        return {b["key"]: b["doc_count"] for b in buckets}

    def multi_terms(
        self,
        index: str,
        aggregations: list,
        query_string: str = "*",
        from_ts: str = None,
        to_ts: str = None,
        ts_field: str = "@timestamp",
    ) -> dict:
        """Multiple field frequency analyses in one call.

        aggregations: [{"id": "...", "field": "...", "size": N}, ...]
        Returns {id: {value: count}}.
        """
        q = self._qs_query(query_string, from_ts, to_ts, ts_field)
        aggs = {
            a["id"]: {"terms": {"field": a["field"], "size": a.get("size", 50)}}
            for a in aggregations
        }
        result = self._post(f"/{index}/_search", body={"size": 0, "query": q, "aggs": aggs})
        out = {}
        for a in aggregations:
            buckets = (
                result.get("aggregations", {}).get(a["id"], {}).get("buckets", [])
            )
            out[a["id"]] = {b["key"]: b["doc_count"] for b in buckets}
        return out

    def histogram(
        self,
        index: str,
        from_ts: str,
        to_ts: str,
        ts_field: str = "@timestamp",
        interval: str = "1h",
        query_string: str = "*",
    ) -> dict:
        """Temporal histogram. Returns {"results": {timestamp: count}}."""
        q = self._qs_query(query_string, from_ts, to_ts, ts_field)
        if interval == "auto":
            agg_spec = {"auto_date_histogram": {"field": ts_field, "buckets": 50}}
        else:
            agg_spec = {
                "date_histogram": {
                    "field": ts_field,
                    "fixed_interval": interval,
                    "min_doc_count": 0,
                    "extended_bounds": {"min": from_ts, "max": to_ts},
                }
            }
        result = self._post(
            f"/{index}/_search",
            body={"size": 0, "query": q, "aggs": {"over_time": agg_spec}},
        )
        buckets = result.get("aggregations", {}).get("over_time", {}).get("buckets", [])
        return {
            "results": {
                b.get("key_as_string", str(b["key"])): b["doc_count"]
                for b in buckets
            }
        }

    def stats(
        self,
        index: str,
        field: str,
        query_string: str = "*",
        from_ts: str = None,
        to_ts: str = None,
        ts_field: str = "@timestamp",
    ) -> dict:
        """Numeric stats (count, min, max, avg, sum, std_deviation) for a field."""
        q = self._qs_query(query_string, from_ts, to_ts, ts_field)
        body = {
            "size": 0,
            "query": q,
            "aggs": {"field_stats": {"extended_stats": {"field": field}}},
        }
        result = self._post(f"/{index}/_search", body=body)
        st = result.get("aggregations", {}).get("field_stats", {})
        return {
            "count": st.get("count", 0),
            "min": st.get("min", 0),
            "max": st.get("max", 0),
            "avg": st.get("avg", 0),
            "sum": st.get("sum", 0),
            "std_deviation": st.get("std_deviation", 0),
        }


# ── Config loading ────────────────────────────────────────────────────────────

def _load_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        return {}
    file_stat = os.stat(CONFIG_FILE)
    if file_stat.st_mode & 0o077:
        msg = (
            f"Config file {CONFIG_FILE} is readable by other users "
            f"(mode {stat.filemode(file_stat.st_mode)}). "
            f"Run: chmod 600 {CONFIG_FILE}"
        )
        if os.environ.get("OPENSEARCH_ALLOW_INSECURE_CONFIG", "").lower() == "true":
            logger.warning(msg)
        else:
            raise PermissionError(msg)
    with open(CONFIG_FILE) as f:
        return json.load(f)


def _coerce_bool(value, default=True) -> bool:
    if isinstance(value, str):
        return value.lower() != "false"
    if isinstance(value, bool):
        return value
    return default


def init_client() -> OpenSearchClient:
    """Initialise OpenSearchClient from env vars or config file.

    Env vars (priority over config file):
        OPENSEARCH_DASHBOARDS_URL  — e.g. https://siem.bsdtrust.com
        OPENSEARCH_URL             — e.g. https://siem.bsdtrust.com:9200 (fallback)
        OPENSEARCH_USERNAME
        OPENSEARCH_PASSWORD
        OPENSEARCH_VERIFY_SSL      — "true"/"false" (default: true)
        OPENSEARCH_TIMEOUT         — seconds (default: 60)
    """
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    config = _load_config()

    dashboards_url = os.environ.get("OPENSEARCH_DASHBOARDS_URL") or config.get("dashboards_url")
    opensearch_url = os.environ.get("OPENSEARCH_URL") or config.get("opensearch_url")
    username = os.environ.get("OPENSEARCH_USERNAME") or config.get("username")
    password = os.environ.get("OPENSEARCH_PASSWORD") or config.get("password")

    if not dashboards_url and not opensearch_url:
        raise RuntimeError(
            "Neither OPENSEARCH_DASHBOARDS_URL nor OPENSEARCH_URL is set. "
            "Set at least one via env var or ~/.config/mcp-opensearch/config.json"
        )

    env_ssl = os.environ.get("OPENSEARCH_VERIFY_SSL")
    verify_ssl = (
        _coerce_bool(env_ssl) if env_ssl is not None
        else _coerce_bool(config.get("verify_ssl", True))
    )
    timeout = int(os.environ.get("OPENSEARCH_TIMEOUT", config.get("timeout", 60)))

    client = OpenSearchClient(
        dashboards_url=dashboards_url,
        opensearch_url=opensearch_url,
        username=username,
        password=password,
        verify_ssl=verify_ssl,
        timeout=timeout,
    )
    logger.info(
        "OpenSearch client ready (dashboards=%s, direct=%s)",
        dashboards_url or "none",
        opensearch_url or "none",
    )
    return client
