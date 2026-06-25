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
import re
import stat
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import HTTPError
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

CONFIG_DIR = os.path.expanduser("~/.config/mcp-opensearch")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

BACKEND_DASHBOARDS = "dashboards"
BACKEND_OPENSEARCH = "opensearch"

MAX_SEARCH_LIMIT = 200        # hard cap on search result size
MAX_HISTOGRAM_BUCKETS = 2000  # reject histograms that would exceed this
MAX_SAMPLE_SIZE = 100         # hard cap on discover_fields sample_size

# Interval string → seconds
_INTERVAL_SECONDS = {
    "s": 1, "m": 60, "h": 3600, "d": 86400,
    "w": 604800, "M": 2592000, "y": 31536000,
}
_INTERVAL_RE = re.compile(r"^(\d+)([smhdwMy])$")

# Read-only path allowlist — suffix match
# e.g. /wazuh-alerts-*/_search matches "/_search"
_ALLOWED_PATHS = {
    "GET": [
        "/_cat/indices",
        "/_cluster/health",
        "/_mapping",
        "/api/status",
        "/api/saved_objects/_find",
    ],
    "POST": [
        "/_search",
        "/_count",
        "/_msearch",
        "/api/console/proxy",   # Dashboards proxy (carries the real path)
    ],
}

_NO_TIME_RANGE_WARNING = (
    "No time range specified — this query scans the full index history "
    "and may be slow or expensive. Pass from_ts/to_ts to limit the scope."
)


def _flatten_doc(doc: dict, prefix: str = "") -> dict:
    """Recursively flatten a nested document to dot-notation keys."""
    out = {}
    for k, v in doc.items():
        full = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten_doc(v, prefix=full))
        else:
            out[full] = type(v).__name__
    return out


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
        max_search_limit=MAX_SEARCH_LIMIT,
        max_histogram_buckets=MAX_HISTOGRAM_BUCKETS,
    ):
        self.dashboards_url = dashboards_url.rstrip("/") if dashboards_url else None
        self.opensearch_url = opensearch_url.rstrip("/") if opensearch_url else None
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self.max_search_limit = max_search_limit
        self.max_histogram_buckets = max_histogram_buckets
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

    # ── Structured error handling ─────────────────────────────

    @staticmethod
    def _raise_for_status(r, context: str):
        """Re-raise HTTP errors as clean RuntimeErrors with actionable messages."""
        try:
            r.raise_for_status()
        except HTTPError:
            status = r.status_code
            if status == 403:
                raise RuntimeError(
                    f"Permission denied: {context}. "
                    "The authenticated user lacks the required privilege."
                ) from None
            if status == 404:
                raise RuntimeError(
                    f"Not found: {context}. "
                    "Check the index name or Dashboards version."
                ) from None
            if status == 400:
                raise RuntimeError(
                    f"Bad request: {context}. "
                    "Check your query syntax or field names."
                ) from None
            raise RuntimeError(f"HTTP {status}: {context}.") from None

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
                self._raise_for_status(r, "GET /")
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
        self._raise_for_status(r, f"GET {path}")
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
        self._raise_for_status(r, f"POST {path}")
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
        self._raise_for_status(r, f"{method} {path} (via Dashboards proxy)")
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
        """Probe connectivity. Returns backend, version, URL, and authenticated username."""
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
            "username": self._session.auth[0] if self._session.auth else None,
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
        # Dashboards 2.x uses saved_objects API; older versions used index_patterns API
        r = self._session.get(
            f"{self.dashboards_url}/api/saved_objects/_find",
            params={"type": "index-pattern", "fields": ["title", "timeFieldName"], "per_page": 200},
            verify=self.verify_ssl,
            timeout=self.timeout,
        )
        self._raise_for_status(r, "GET /api/saved_objects/_find (index patterns)")
        data = r.json()
        saved_objects = data.get("saved_objects", [])
        return [
            {
                "id": p.get("id"),
                "title": p.get("attributes", {}).get("title"),
                "timeFieldName": p.get("attributes", {}).get("timeFieldName"),
            }
            for p in saved_objects
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
        """Sample documents and return {field_name: python_type} in dot-notation.

        Nested fields are flattened: agent.name, rule.level, etc.
        sample_size is capped at MAX_SAMPLE_SIZE to prevent large fetches.
        """
        capped = min(sample_size, MAX_SAMPLE_SIZE)
        result = self.search_string(
            index,
            query_string=query_string,
            from_ts=from_ts,
            to_ts=to_ts,
            ts_field=ts_field,
            limit=capped,
        )
        fields = {}
        for hit in result.get("hits", []):
            fields.update(_flatten_doc(hit))
        out = dict(sorted(fields.items()))
        if capped < sample_size:
            out["_warning"] = (
                f"sample_size capped at {capped} (requested {sample_size}). "
                f"Maximum is {MAX_SAMPLE_SIZE}."
            )
        return out

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
        """Search with a Lucene query string. Returns {"total": N, "hits": [...]}.

        Adds a "warning" key when limit is capped or no time range is given.
        """
        capped = min(limit, self.max_search_limit)
        q = self._qs_query(query_string, from_ts, to_ts, ts_field)
        body = {
            "query": q,
            "size": capped,
            "sort": [{(sort_field or ts_field): {"order": sort_dir}}],
        }
        if source_fields:
            body["_source"] = source_fields
        result = self._post(f"/{index}/_search", body=body)
        hits = result.get("hits", {})
        total = hits.get("total", {})
        if isinstance(total, dict):
            total = total.get("value", 0)
        out = {
            "total": total,
            "hits": [h.get("_source", {}) for h in hits.get("hits", [])],
        }
        warnings = []
        if capped < limit:
            warnings.append(
                f"limit capped at {capped} (requested {limit}). "
                "Use source_fields to reduce response size, or paginate with multiple calls."
            )
        if not from_ts and not to_ts:
            warnings.append(_NO_TIME_RANGE_WARNING)
        if warnings:
            out["warning"] = " | ".join(warnings)
        return out

    def count(
        self,
        index: str,
        query_string: str = "*",
        from_ts: str = None,
        to_ts: str = None,
        ts_field: str = "@timestamp",
    ) -> dict:
        """Count documents matching a query. Returns {"count": N}.

        Adds a "warning" key when no time range is given (full-index scan).
        """
        q = self._qs_query(query_string, from_ts, to_ts, ts_field)
        result = self._post(f"/{index}/_count", body={"query": q})
        out = {"count": result.get("count", 0)}
        if not from_ts and not to_ts:
            out["warning"] = _NO_TIME_RANGE_WARNING
        return out

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
        """Top N values of a field. Returns {value: count} sorted descending.

        Adds a "warning" key when the field may be a text field (no .keyword suffix),
        which triggers fielddata and loads heap memory on the cluster.
        """
        q = self._qs_query(query_string, from_ts, to_ts, ts_field)
        body = {
            "size": 0,
            "query": q,
            "aggs": {"top_values": {"terms": {"field": field, "size": size}}},
        }
        result = self._post(f"/{index}/_search", body=body)
        buckets = result.get("aggregations", {}).get("top_values", {}).get("buckets", [])
        out = {b["key"]: b["doc_count"] for b in buckets}
        if not field.endswith(".keyword"):
            out["_warning"] = (
                f"Field '{field}' may be a text field. If results look wrong, "
                f"try '{field}.keyword' instead. Using text fields in aggregations "
                "loads fielddata into heap memory."
            )
        return out

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
        Adds "_warnings" key listing any fields that may cause fielddata heap pressure.
        """
        if not aggregations:
            raise ValueError("aggregations list must not be empty.")
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
        text_fields = [a["field"] for a in aggregations if not a["field"].endswith(".keyword")]
        if text_fields:
            out["_warnings"] = [
                f"Field '{f}' may be a text field — try '{f}.keyword' to avoid fielddata heap pressure."
                for f in text_fields
            ]
        return out

    @staticmethod
    def _parse_ts(ts: str) -> float:
        """Parse an ISO 8601 UTC timestamp to a Unix epoch float."""
        ts = ts.rstrip("Z")
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                continue
        raise ValueError(f"Cannot parse timestamp: {ts!r}")

    def _check_histogram_buckets(self, from_ts: str, to_ts: str, interval: str):
        """Raise ValueError if the expected bucket count exceeds the safe limit."""
        if interval == "auto":
            return  # auto delegates to OpenSearch with a fixed cap of 50
        m = _INTERVAL_RE.match(interval)
        if not m:
            raise ValueError(
                f"Invalid interval {interval!r}. "
                "Use a number + unit, e.g. '15m', '1h', '1d'. "
                "Valid units: s m h d w M y. Or use 'auto'."
            )
        interval_secs = int(m.group(1)) * _INTERVAL_SECONDS[m.group(2)]
        try:
            t0 = self._parse_ts(from_ts)
            t1 = self._parse_ts(to_ts)
        except ValueError as e:
            raise ValueError(f"Cannot compute bucket count: {e}") from e
        range_secs = max(t1 - t0, 0)
        expected = int(range_secs / interval_secs) + 1
        if expected > self.max_histogram_buckets:
            raise ValueError(
                f"Too many buckets: ~{expected:,} expected "
                f"({from_ts} → {to_ts} at interval {interval}). "
                f"Limit is {self.max_histogram_buckets:,}. "
                "Use a coarser interval or a narrower time range."
            )

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
        self._check_histogram_buckets(from_ts, to_ts, interval)
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
        OPENSEARCH_DASHBOARDS_URL       — e.g. https://opensearch.example.com
        OPENSEARCH_URL                  — e.g. https://opensearch.example.com:9200 (fallback)
        OPENSEARCH_USERNAME
        OPENSEARCH_PASSWORD
        OPENSEARCH_VERIFY_SSL           — "true"/"false" (default: true)
        OPENSEARCH_TIMEOUT              — seconds (default: 60)
        OPENSEARCH_MAX_SEARCH_LIMIT     — hard cap on search results (default: 200)
        OPENSEARCH_MAX_HISTOGRAM_BUCKETS — hard cap on histogram buckets (default: 2000)
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
    max_search_limit = int(
        os.environ.get("OPENSEARCH_MAX_SEARCH_LIMIT", config.get("max_search_limit", MAX_SEARCH_LIMIT))
    )
    max_histogram_buckets = int(
        os.environ.get("OPENSEARCH_MAX_HISTOGRAM_BUCKETS", config.get("max_histogram_buckets", MAX_HISTOGRAM_BUCKETS))
    )

    client = OpenSearchClient(
        dashboards_url=dashboards_url,
        opensearch_url=opensearch_url,
        username=username,
        password=password,
        verify_ssl=verify_ssl,
        timeout=timeout,
        max_search_limit=max_search_limit,
        max_histogram_buckets=max_histogram_buckets,
    )
    logger.info(
        "OpenSearch client ready (dashboards=%s, direct=%s)",
        dashboards_url or "none",
        opensearch_url or "none",
    )
    return client
