"""
OpenSearch MCP client — read-only.

Tries OpenSearch Dashboards first (via /api/console/proxy),
falls back to direct OpenSearch REST API on failure.

Auth: basic auth (username + password).
Config priority: env vars > ~/.config/mcp-opensearch/config.json
"""

from __future__ import annotations

import json
import logging
import os
import posixpath
import re
import stat
import sys
import time
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectTimeout, ReadTimeout, RetryError, SSLError

logger = logging.getLogger(__name__)

CONFIG_DIR = os.path.expanduser("~/.config/mcp-opensearch")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
# The .env setup.sh writes and the README documents. Read explicitly: bare
# load_dotenv() resolves via find_dotenv(), which walks up from *this file's*
# directory, so this path was never read by the Python process at all.
ENV_FILE = os.path.join(CONFIG_DIR, ".env")
# A .env beside the checkout (the source-run path). Loaded on purpose, and
# permission-checked exactly like the other two credential files.
PROJECT_ENV_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
)

BACKEND_DASHBOARDS = "dashboards"
BACKEND_OPENSEARCH = "opensearch"


def _package_version() -> str:
    """This package's version, from distribution metadata.

    pyproject.toml is the single authoritative version (ADR 0003), so it must not
    be duplicated as a literal here. The package is frequently run straight from
    a source checkout, where no distribution metadata exists — that must degrade
    to an honest "unknown" rather than raising at import and taking down server
    startup.
    """
    try:
        return _pkg_version("mcp-opensearch")
    except PackageNotFoundError:
        return "unknown"


MAX_SEARCH_LIMIT = 200        # default cap on search result size, overridable via OPENSEARCH_MAX_SEARCH_LIMIT
MAX_HISTOGRAM_BUCKETS = 2000  # default cap on histogram buckets, overridable via OPENSEARCH_MAX_HISTOGRAM_BUCKETS
MAX_SAMPLE_SIZE = 100         # hard cap on discover_fields sample_size (no override)
MAX_TERMS_SIZE = 1000         # default cap on a terms agg `size`, overridable via OPENSEARCH_MAX_TERMS_SIZE
MAX_AGGREGATIONS = 20         # default cap on multi_terms agg count, overridable via OPENSEARCH_MAX_AGGREGATIONS

DEFAULT_TIMEOUT = 60          # total wall-clock budget for ONE HTTP operation, incl. retries

# ── Retry / time-budget policy ────────────────────────────────────────────────
#
# `self.timeout` is a **budget for the whole operation**, not a per-attempt
# timeout. It used to be per attempt, mounted under urllib3's
# Retry(total=3, backoff_factor=1), so one tool call could occupy
# 4 attempts x 60 s + 6 s of backoff ~= 246 s — longer than any MCP client's tool
# and spent hammering a cluster that is already shedding load. The retry loop now
# lives in _send() with an explicit deadline, so the worst case is the budget
# itself (measured: 60.0 s at timeout=60, 2.0 s at timeout=2).
#
# Deliberate asymmetry, and the reason a plain Retry() cannot express this:
#   * a **read** timeout is never retried — the cluster is still executing the
#     query, so a retry doubles the load it is failing to carry, and the first
#     attempt already got the full budget;
#   * a **connect** failure is retried, because it costs the cluster nothing and
#     is the case that is genuinely transient;
#   * 502/503/504 are retried, because a shedding node answers fast;
#   * TLS failures are never retried — a bad certificate stays bad;
#   * 429 is deliberately NOT retried. It means the cluster rejected the work
#     (es_rejected_execution_exception, circuit breaker). Repeating the same
#     oversized aggregation is exactly the wrong move; _raise_for_status says so
#     and names the remedy instead.
_MAX_ATTEMPTS = 3             # 1 initial attempt + at most 2 retries
_RETRY_STATUSES = frozenset({502, 503, 504})
_BACKOFF_BASE_SECONDS = 0.5   # doubles per retry: 0.5 s, 1.0 s
_BACKOFF_MAX_SECONDS = 4.0
_MIN_ATTEMPT_SECONDS = 2.0    # never start an attempt with less budget left than this
_CONNECT_TIMEOUT_SECONDS = 10.0
_PROBE_BUDGET_SECONDS = 10.0  # backend liveness probes get their own small budget

# HTTP status → (message prefix, actionable hint). The prefixes "Bad request",
# "Permission denied" and "Not found" are load-bearing: stats() keys off the
# first one to add its own field-type advice.
_STATUS_MESSAGES = {
    400: ("Bad request", "Check your query syntax or field names."),
    401: (
        "Authentication failed",
        "The credentials were rejected (HTTP 401). Check OPENSEARCH_USERNAME / "
        "OPENSEARCH_PASSWORD, or username/password in "
        "~/.config/mcp-opensearch/config.json.",
    ),
    403: ("Permission denied", "The authenticated user lacks the required privilege."),
    404: ("Not found", "Check the index name or Dashboards version."),
    429: (
        "Rejected to shed load (HTTP 429)",
        "The cluster refused the work — a full queue "
        "(es_rejected_execution_exception) or a tripped circuit breaker. Do NOT "
        "repeat the request unchanged: narrow the time range, reduce `size` or "
        "the number of aggregations, aggregate on a keyword field instead of a "
        "text one, or target one index instead of a wildcard. Then wait a few "
        "seconds before retrying.",
    ),
    502: (
        "Backend unavailable",
        "A gateway or proxy in front of OpenSearch returned HTTP 502 after "
        "retries. Check the Dashboards/reverse-proxy layer, not the query.",
    ),
    503: (
        "Backend unavailable",
        "OpenSearch answered HTTP 503 after retries — it is restarting, "
        "shedding load, or has unassigned shards. Check cluster health before "
        "trusting any other result.",
    ),
    504: (
        "Backend timed out",
        "A gateway in front of OpenSearch gave up (HTTP 504) after retries. "
        "The query is likely too broad for the deployment's proxy timeout — "
        "narrow the time range.",
    ),
}

# Conventional boolean spellings, for env vars and JSON config values alike.
# Anything outside these two sets is ambiguous and is rejected rather than
# silently read as True.
_TRUE_STRINGS = frozenset({"1", "true", "t", "yes", "y", "on"})
_FALSE_STRINGS = frozenset({"0", "false", "f", "no", "n", "off"})

# Interval string → seconds. The w/M/y values are nominal (7d / 30d / 365d) and
# are used only to estimate a bucket count locally; the cluster does the real
# calendar arithmetic. See _CALENDAR_UNITS.
_INTERVAL_SECONDS = {
    "s": 1, "m": 60, "h": 3600, "d": 86400,
    "w": 604800, "M": 2592000, "y": 31536000,
}
_INTERVAL_RE = re.compile(r"^(\d+)([smhdwMy])$")

# Units OpenSearch accepts only in `calendar_interval`, never in
# `fixed_interval`. calendar_interval additionally accepts a multiplier of 1
# only, which _parse_interval enforces.
_CALENDAR_UNITS = frozenset({"w", "M", "y"})

# Timestamp normalisation for _parse_ts, applied to the part after the "T" only
# (a date like "2024-01-01" ends in something an offset pattern would match).
_TS_FRACTION_RE = re.compile(r"\.(\d+)")
_TS_OFFSET_NO_COLON_RE = re.compile(r"([+-])(\d{2})(\d{2})$")
_TS_OFFSET_HOURS_ONLY_RE = re.compile(r"([+-])(\d{2})$")

# Read-only path allowlist — the single enforcement point for outbound requests.
#
# Two match kinds, because OpenSearch paths come in two shapes and a plain
# suffix match is wrong for one of them:
#
#   "absolute" — cluster- or plugin-level endpoints. Matches the entry itself or
#                any sub-resource below it, so "/_cat" covers
#                "/_cat/indices/my_create_index" and "/_nodes/stats" covers
#                "/_nodes/stats/jvm". A suffix match would reject both.
#   "indexed"  — index-scoped endpoints: first segment is the index name or
#                wildcard pattern, second is the action. "/_search" covers
#                "/wazuh-alerts-*/_search"; "/_explain" covers
#                "/my-index/_explain/<doc_id>", which no suffix match can express.
#                The index segment must not start with "_", so a crafted index
#                like "_cluster/settings" cannot smuggle in a cluster endpoint.
#
# Criterion for an entry: read-only AND not a credential/secret surface.
# Deliberately NOT allowlisted, and must stay that way:
#   /_plugins/_security/*  — internal user database, password hashes, roles
#   /_cluster/settings, /_cluster/state — carry snapshot repository credentials
#   /_snapshot/*           — repository definitions incl. cloud credentials
#   /_nodes (bare)         — node info echoes opensearch.yml settings
_ALLOWED_PATHS = {
    "GET": {
        "absolute": [
            "/_cat",                    # whole _cat family is GET-only and read-only
            "/_cluster/health",
            "/_cluster/stats",
            "/_cluster/pending_tasks",
            "/_nodes/stats",
            "/_nodes/usage",
            "/_nodes/hot_threads",
            "/_alias",                  # cluster-wide alias listing
            "/_mapping",                # cluster-wide mapping / _mapping/field/*
            "/_template",
            "/_index_template",
            "/_plugins/_ism/policies",  # ISM policies (read); GET only
            "/_plugins/_ism/explain",
            "/_plugins/_alerting/monitors/alerts",  # Alerting plugin: alerts (read)
            "/api/status",                          # Dashboards
            "/api/saved_objects/_find",             # Dashboards
            "/api/data_views",                      # Dashboards (newer versions)
        ],
        "indexed": [
            "/_mapping",
            "/_settings",
            "/_alias",
            "/_stats",
            "/_shard_stores",
        ],
    },
    "POST": {
        "absolute": [
            "/_search",
            "/_count",
            "/_msearch",
            "/_plugins/_ppl",
            "/_plugins/_alerting/monitors/_search",  # Alerting plugin: search monitors (read)
            "/_plugins/_anomaly_detection/detectors/_search",          # AD plugin: search detectors (read)
            "/_plugins/_anomaly_detection/detectors/results/_search",  # AD plugin: search results (read)
            # /api/console/proxy is deliberately NOT here. It is a tunnel: the
            # real method and path travel in its query string, so allowlisting
            # it would allow anything. _dashboards_proxy() builds that request
            # itself, from a path _check_path has already approved.
        ],
        "indexed": [
            "/_search",
            "/_count",
            "/_msearch",
            "/_explain",
        ],
    },
}


def _dig(data, *keys) -> dict:
    """Walk nested dicts, treating a present-but-null key exactly like a missing one.

    `d.get(k, {}).get(x)` only defends against `k` being *absent*: OpenSearch
    routinely sends the key with a JSON null instead (a detached ISM policy, an
    empty aggregation envelope), and the chained `.get` then raises
    AttributeError on None. Post-condition: always returns a dict, so the result
    is safe to chain.
    """
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return {}
        value = current.get(key)
        current = {} if value is None else value
    return current if isinstance(current, dict) else {}


def _val(data, key: str, default=None):
    """`data[key]`, falling back to `default` when the key is absent OR null."""
    if not isinstance(data, dict):
        return default
    value = data.get(key)
    return default if value is None else value


def _items(data, key: str) -> list:
    """The list at `data[key]`, or [] when it is absent, null or not a list."""
    value = data.get(key) if isinstance(data, dict) else None
    return value if isinstance(value, list) else []


def _matches_allowlist(path: str, rules: dict) -> bool:
    """True when `path` (query string already stripped, normalised) is allowed.

    See _ALLOWED_PATHS for the meaning of the "absolute" and "indexed" kinds.
    """
    for entry in rules.get("absolute", ()):
        if path == entry or path.startswith(entry + "/"):
            return True
    parts = path.split("/")
    # ["", "<index>", "<_action>", ...] — index must not masquerade as an endpoint
    return (
        len(parts) >= 3
        and parts[0] == ""
        and bool(parts[1])
        and not parts[1].startswith("_")
        and f"/{parts[2]}" in rules.get("indexed", ())
    )


def _allowlist_hint(method: str) -> str:
    """Human-readable summary of what `method` may reach, built from the allowlist."""
    rules = _ALLOWED_PATHS.get(method, {})
    absolute = ", ".join(rules.get("absolute", ())) or "none"
    indexed = ", ".join(f"/<index>{e}" for e in rules.get("indexed", ())) or "none"
    return f"Allowed {method} paths: {absolute}, {indexed}"


_NO_TIME_RANGE_WARNING = (
    "No time range specified — this query scans the full index history "
    "and may be slow or expensive. Pass from_ts/to_ts to limit the scope."
)


# Whole-word hints that suggest an analyzed text type. Each entry is a single
# token: matching is token equality, never substring containment, because the
# short hints ("log", "text") occur inside plenty of keyword field names —
# logonType, logonProcessName, catalogId, logger — and a false positive tells the
# analyst to append `.keyword` to a field that has none.
# Keyword fields (agent.name, rule.id, data.srcip, etc.) match nothing here.
_TEXT_FIELD_HINTS = (
    "description", "message", "log", "content",
    "text", "comment", "detail", "summary", "reason", "output",
    "commandline", "cmdline",
)

# Tokenises a field-name segment: an all-caps run, a CamelCase word, or a
# lowercase/digit run. Non-alphanumerics (_ - @ etc.) are separators.
_SEGMENT_TOKEN_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")


def _segment_tokens(segment: str) -> list:
    """Lowercase word tokens of one field-name segment.

    "logonType" → ["logon", "type"]; "full_log" → ["full", "log"];
    "DESCRIPTION" → ["description"]; "command_line" → ["command", "line"].
    """
    return [t.lower() for t in _SEGMENT_TOKEN_RE.findall(segment)]


def _is_likely_text_field(field: str) -> bool:
    """Heuristic: true when the field name suggests an analyzed text type.

    Only the last dotted segment is examined, and only whole tokens count. The
    de-separated segment ("command_line" → "commandline") is also compared, so
    the three spellings of the same field name behave identically.
    """
    tokens = _segment_tokens(field.rsplit(".", 1)[-1])
    if not tokens:
        return False
    joined = "".join(tokens)
    token_set = set(tokens)
    return any(hint in token_set or hint == joined for hint in _TEXT_FIELD_HINTS)


def _flatten_doc(doc: dict, prefix: str = "") -> dict:
    """Recursively flatten a nested document to dot-notation keys.

    Arrays are descended into: an array of objects contributes its own entry
    (typed "list", so the array is still visible) plus one entry per leaf inside
    its elements, at the dotted path OpenSearch itself queries — `rule.mitre.id`,
    not `rule.mitre[0].id`. Element structures are unioned across the array; on a
    type conflict the last element wins, the same rule discover_fields already
    applies across sampled documents. An array of scalars keeps only its own
    "list" entry, since its elements have no field names to report.
    """
    out = {}
    for k, v in doc.items():
        full = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten_doc(v, prefix=full))
        else:
            out[full] = type(v).__name__
            if isinstance(v, list):
                out.update(_flatten_list(v, prefix=full))
    return out


def _bucket_label(bucket: dict) -> str | None:
    """The label for a date_histogram bucket, or None when it has no identity.

    `key_as_string` is preferred and `key` is the fallback — evaluated lazily,
    because `b.get("key_as_string", str(b["key"]))` indexes `key` on every bucket
    and so raised KeyError on a bucket that carried only the label.
    """
    label = bucket.get("key_as_string")
    if label is None:
        label = bucket.get("key")
    return None if label is None else str(label)


def _flatten_list(values: list, prefix: str) -> dict:
    """Leaf fields inside an array, at `prefix`-relative dotted paths."""
    out = {}
    for item in values:
        if isinstance(item, dict):
            out.update(_flatten_doc(item, prefix=prefix))
        elif isinstance(item, list):
            out.update(_flatten_list(item, prefix=prefix))
    return out


# ── Transport failure translation ─────────────────────────────────────────────
#
# A request that never produced an HTTP response used to escape this module raw:
# urllib3's MaxRetryError arrives as requests.RetryError, which is NOT an
# HTTPError, so _raise_for_status never saw it and the caller received
# "RetryError: HTTPConnectionPool(...) (Caused by ResponseError('too many 503
# error responses'))" — no indication of which backend, which URL, or what to do.
# Connection refused, DNS failure and certificate rejection had the same problem.


class TransportError(RuntimeError):
    """No HTTP response was obtained: connection, TLS, timeout or retry exhaustion.

    A RuntimeError subclass on purpose — every caller and every tool already
    treats RuntimeError as "actionable failure with a readable message", so this
    joins that family rather than starting a second one. `reason` is the short
    cause phrase, reused by the backend-resolution error so it can name the real
    cause per backend attempted.
    """

    def __init__(self, message: str, reason: str):
        super().__init__(message)
        self.reason = reason


def _transport_reason(exc: Exception) -> str:
    """Short phrase naming what actually failed. Never blames the URL by default."""
    if isinstance(exc, SSLError):
        return f"TLS verification failed ({exc})"
    if isinstance(exc, ConnectTimeout):
        return "connection timed out (no TCP/TLS handshake)"
    if isinstance(exc, ReadTimeout):
        return "the cluster accepted the request but did not answer in time"
    if isinstance(exc, RetryError):
        return f"retries exhausted ({exc})"
    if isinstance(exc, requests.ConnectionError):
        return f"unreachable ({exc.__class__.__name__}: {exc})"
    if isinstance(exc, requests.Timeout):
        return "timed out"
    return f"{exc.__class__.__name__}: {exc}"


def _transport_hint(exc: Exception, budget) -> str:
    """What the operator should do about a transport failure."""
    if isinstance(exc, SSLError):
        return (
            "Install the CA that signed the endpoint's certificate, or set "
            "OPENSEARCH_VERIFY_SSL=false if you accept an unverified connection."
        )
    if isinstance(exc, (requests.Timeout, RetryError)):
        return (
            f"OPENSEARCH_TIMEOUT is the total budget for one call and is {budget}s. "
            "Narrow the time range or reduce `size` first; raise the budget only "
            "if the query is legitimately that slow."
        )
    return (
        "Check the URL, that the port is open from here, and that the service is "
        "running. This is a network/DNS/TLS failure, not a query or privilege problem."
    )


def _is_retryable_error(exc: Exception) -> bool:
    """True for failures a second attempt can plausibly fix. See the policy note above."""
    if isinstance(exc, (SSLError, ReadTimeout)):
        return False
    return isinstance(exc, (requests.ConnectionError, ConnectTimeout, RetryError))


def _error_detail(r) -> str:
    """OpenSearch's own explanation of a failure, or "" when it gave none.

    Only structured fields are used (`error.reason`, `error.type`, Dashboards'
    `message`) and the result is truncated — an HTML error page must not be
    pasted into a tool response, but "unknown field [foo]" is the single most
    useful thing a 400 can tell the caller and it used to be discarded.
    """
    try:
        payload = r.json()
    except (ValueError, AttributeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, dict):
        detail = _val(error, "reason") or _val(error, "type") or ""
    elif isinstance(error, str):
        detail = error
    else:
        detail = _val(payload, "message", "")
    detail = " ".join(str(detail).split())
    return f"Cluster said: {detail[:300]}" if detail else ""


def _decode_json(r, context: str):
    """`r.json()`, translating a non-JSON body into an actionable error.

    A login page or an HTML proxy error answering with HTTP 200 used to surface as
    a bare `json.JSONDecodeError` from inside the client.
    """
    try:
        return r.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Non-JSON response: {context} (HTTP {getattr(r, 'status_code', '?')} "
            f"from {getattr(r, 'url', '?')}). Something other than OpenSearch "
            "answered — typically an SSO login page or a reverse-proxy error page."
        ) from exc


def _probe_status_reason(r) -> str:
    """Why a liveness probe's non-200 response is not a usable backend."""
    status = r.status_code
    if status == 401:
        return (
            "authentication failed (HTTP 401) — check OPENSEARCH_USERNAME / "
            "OPENSEARCH_PASSWORD"
        )
    if status == 403:
        return (
            "authenticated but not authorised (HTTP 403) — the account exists but "
            "may not be allowed to use this backend"
        )
    if 300 <= status < 400:
        location = r.headers.get("Location", "an undisclosed location")
        return (
            f"redirected (HTTP {status}) to {location} — typically an SSO login "
            "flow in front of the API"
        )
    detail = _error_detail(r)
    return f"HTTP {status}" + (f" — {detail}" if detail else "")


def _resolution_error(attempts: list, dashboards_url, opensearch_url) -> str:
    """The message raised when no backend resolved.

    Names the real cause for each backend actually attempted, and mentions an
    environment variable only when it is relevant to what was tried — the old
    message told operators who had only ever set OPENSEARCH_DASHBOARDS_URL to go
    and check OPENSEARCH_URL.
    """
    if not attempts:
        return (
            "No OpenSearch backend is configured. Set OPENSEARCH_DASHBOARDS_URL or "
            "OPENSEARCH_URL (or dashboards_url / opensearch_url in "
            f"{CONFIG_FILE})."
        )
    parts = ["Could not reach any OpenSearch backend. What was tried:"]
    parts += [f"  - {label}: {reason}" for label, reason in attempts]
    if not opensearch_url:
        parts.append(
            "  OPENSEARCH_URL is not set, so no direct-OpenSearch fallback was "
            "attempted. Set it if port 9200 is reachable from here."
        )
    elif not dashboards_url:
        parts.append(
            "  OPENSEARCH_DASHBOARDS_URL is not set, so Dashboards was not attempted."
        )
    return "\n".join(parts)


class OpenSearchClient:
    """Read-only client for OpenSearch / OpenSearch Dashboards.

    On first use, probes Dashboards (/api/status). On failure, falls back to
    direct OpenSearch.

    Guarantee: every request this class issues goes out through _get or _post,
    and both call _check_path() first, so only paths on the _ALLOWED_PATHS
    read-only allowlist can be reached — there is no bypass method. The
    allowlist is a positive list of read endpoints, not a denylist of write
    ones, so an unknown endpoint is refused rather than permitted.

    What it does NOT guarantee: the backend's own RBAC still decides what the
    authenticated user may read. The allowlist keeps this client away from
    credential surfaces (security plugin, snapshot repositories, cluster
    settings), but it is not a substitute for a least-privilege account.

    Four methods name self._session directly instead of going through _get/_post:
    _probe() issues the two literal liveness-probe URLs, _dashboards_proxy() posts
    an already-checked path to the Dashboards console proxy, list_index_patterns()
    reads the two literal Dashboards saved-object endpoints, and _get/_post
    themselves. None of them takes a caller-supplied path.
    """

    def __init__(
        self,
        dashboards_url=None,
        opensearch_url=None,
        username=None,
        password=None,
        verify_ssl=True,
        timeout=DEFAULT_TIMEOUT,
        max_search_limit=MAX_SEARCH_LIMIT,
        max_histogram_buckets=MAX_HISTOGRAM_BUCKETS,
        max_terms_size=MAX_TERMS_SIZE,
        max_aggregations=MAX_AGGREGATIONS,
        backend_recheck_seconds=300,
        session=None,
    ):
        """Build a client.

        timeout: total wall-clock budget for one HTTP operation, retries and
            backoff included — not a per-attempt timeout. See the retry policy
            note at the top of this module.
        backend_recheck_seconds: how long a *degraded* backend resolution (a
            Dashboards URL is configured but the direct API won the probe) may be
            reused before it is re-probed. A resolution that got the preferred
            backend is never re-probed on a schedule; it is dropped on failure
            instead, so the steady state costs no extra round trips.
        session: optional requests.Session to use instead of building one — the
            injection seam for tests and for callers that need their own
            connection pool. Its auth and headers are configured here; its
            adapters are left alone, because transport is then the caller's call.
        """
        self.dashboards_url = dashboards_url.rstrip("/") if dashboards_url else None
        self.opensearch_url = opensearch_url.rstrip("/") if opensearch_url else None
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self.max_search_limit = max_search_limit
        self.max_histogram_buckets = max_histogram_buckets
        self.max_terms_size = max_terms_size
        self.max_aggregations = max_aggregations
        self.backend_recheck_seconds = backend_recheck_seconds
        self.backend = None
        self.server_version = None
        # Latency of the most recent HTTP attempt, success or failure. Reported by
        # test_connection() as "latency_ms" and logged per request at DEBUG — it
        # used to be assigned in four places, read in none, and written only after
        # the error check, so failures (the case worth measuring) recorded nothing.
        self.last_query_ms = 0
        self._backend_resolved_at = None
        # [(backend label, reason it was not usable)] from the last resolution.
        # Keeps list_index_patterns able to say why Dashboards is not active.
        self._backend_attempts = []

        self._session = requests.Session() if session is None else session
        if username and password:
            self._session.auth = (username, password)

        self._session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "osd-xsrf": "true",
            "User-Agent": f"mcp-opensearch/{_package_version()}",
        })

        if session is None:
            # No transport-level retries: _send() owns retrying, because only a
            # deadline-aware loop can bound the wall time of one tool call. An
            # adapter-level Retry multiplies the timeout instead, and raises
            # RetryError, which bypasses every message this class produces.
            adapter = HTTPAdapter(max_retries=0)
            self._session.mount("http://", adapter)
            self._session.mount("https://", adapter)

    # ── Read-only guard ───────────────────────────────────────

    def _check_path(self, method: str, path: str):
        """Refuse any request that is not on the read-only allowlist.

        Pre-condition: `path` is an absolute OpenSearch path starting with "/".
        Post-condition: returns None only for paths allowed for `method`; every
        other path raises. This is the only guard, and _get/_post are the only
        ways out of this class, so it cannot be bypassed by a caller.

        Raises:
            ValueError: path is not an absolute path.
            PermissionError: path contains traversal segments, or is not on the
                allowlist for this method. The message names the allowed paths.
        """
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError(f"path must be an absolute path starting with '/'. Got: {path!r}")
        raw = path.split("?")[0]
        clean = raw.rstrip("/") or "/"
        if posixpath.normpath(clean) != clean:
            raise PermissionError(
                f"Blocked {method} {path} — path traversal detected. "
                "Paths must not contain '.' or '..' segments."
            )
        if _matches_allowlist(clean, _ALLOWED_PATHS.get(method, {})):
            return
        raise PermissionError(
            f"Blocked {method} {path} — not on the read-only allowlist. "
            f"{_allowlist_hint(method)}. "
            "Entries shown as /<index>/... accept any index name or wildcard "
            "pattern, e.g. /wazuh-alerts-*/_search. Security, snapshot and "
            "cluster-settings endpoints are never reachable from this server; "
            "prefer the dedicated tools for search, count, mapping, settings "
            "and aggregations."
        )

    # ── Structured error handling ─────────────────────────────

    @staticmethod
    def _raise_for_status(r, context: str):
        """Re-raise HTTP errors as clean RuntimeErrors with actionable messages.

        Post-condition: returns None only for a response below 400. Every 4xx/5xx
        raises a RuntimeError naming the context, what the status means for this
        client, and whatever the cluster itself said about it.

        429 is handled here rather than by retrying: see the retry policy note at
        the top of this module.
        """
        if r.status_code < 400:
            return
        prefix, hint = _STATUS_MESSAGES.get(
            r.status_code, (f"HTTP {r.status_code}", "")
        )
        detail = _error_detail(r)
        message = " ".join(p for p in (f"{prefix}: {context}.", hint, detail) if p)
        raise RuntimeError(message) from None

    # ── Transport: one bounded HTTP operation ─────────────────

    def _record(self, context: str, started: float, status):
        """Record and log the latency of one attempt, successful or not."""
        self.last_query_ms = int((time.monotonic() - started) * 1000)
        logger.debug(
            "%s -> %s in %d ms (backend=%s)",
            context, status if status is not None else "transport error",
            self.last_query_ms, self.backend,
        )

    def _attempt(self, send, url: str, context: str, deadline: float, kwargs: dict):
        """One HTTP attempt inside the operation's deadline.

        Returns ("ok", response), ("retry_status", response) for 502/503/504, or
        ("error", exception) when no response was obtained. Never raises for a
        network failure — _send decides what is retryable and what is fatal.
        """
        remaining = max(deadline - time.monotonic(), _MIN_ATTEMPT_SECONDS)
        started = time.monotonic()
        try:
            r = send(
                url,
                timeout=(min(_CONNECT_TIMEOUT_SECONDS, remaining), remaining),
                verify=self.verify_ssl,
                **kwargs,
            )
        except requests.RequestException as exc:
            self._record(context, started, None)
            return "error", exc
        self._record(context, started, r.status_code)
        return ("retry_status" if r.status_code in _RETRY_STATUSES else "ok"), r

    def _wait_for_retry(self, attempt: int, deadline: float) -> bool:
        """Sleep this attempt's backoff and report whether another attempt fits.

        This is the bound: a retry happens only when the attempt allowance is left
        AND enough of the wall-clock budget remains for the backoff plus a
        worthwhile attempt, so retrying can never push an operation past its
        deadline.
        """
        if attempt >= _MAX_ATTEMPTS:
            return False
        backoff = min(
            _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), _BACKOFF_MAX_SECONDS
        )
        if deadline - time.monotonic() < backoff + _MIN_ATTEMPT_SECONDS:
            return False
        logger.info(
            "Retrying in %.1fs (attempt %d of %d)", backoff, attempt + 1, _MAX_ATTEMPTS
        )
        time.sleep(backoff)
        return True

    def _send(self, send, url: str, context: str, *, budget=None, check_status=True, **kwargs):
        """Issue one HTTP operation with bounded retries and a hard time budget.

        `send` is a bound session method (`self._session.get` / `.post`), passed in
        so that retry, timeout, latency recording and error translation exist in
        exactly one place instead of once per call site.

        Post-conditions:
          * total wall time <= the budget (default self.timeout) plus the cost of
            one in-flight attempt returning;
          * a failure to obtain a response raises TransportError — a RuntimeError
            with the real cause named — never a raw RetryError/ConnectionError;
          * a 4xx/5xx raises via _raise_for_status unless check_status=False, which
            liveness probes and the index-pattern endpoint fallback use because
            they need to inspect the status themselves;
          * self.last_query_ms reflects the final attempt either way.
        """
        deadline = time.monotonic() + (self.timeout if budget is None else budget)
        started = time.monotonic()
        attempt = 0
        while True:
            attempt += 1
            kind, value = self._attempt(send, url, context, deadline, kwargs)
            retryable = kind == "retry_status" or (
                kind == "error" and _is_retryable_error(value)
            )
            if retryable and self._wait_for_retry(attempt, deadline):
                continue
            if kind == "error":
                # The backend itself failed, so stop trusting the cached
                # resolution: the next call re-probes and can fail over.
                self._forget_backend()
                raise self._transport_error(
                    value, context, attempt, time.monotonic() - started
                ) from value
            if check_status:
                self._raise_for_status(value, context)
            return value

    def _transport_error(self, exc, context: str, attempts: int, elapsed: float) -> TransportError:
        """Build the actionable error for a request that never got a response."""
        reason = _transport_reason(exc)
        return TransportError(
            f"Cannot reach OpenSearch: {reason}: {context} — gave up after "
            f"{attempts} attempt(s) in {elapsed:.1f}s of a {self.timeout}s budget. "
            f"{_transport_hint(exc, self.timeout)}",
            reason,
        )

    # ── Backend resolution ────────────────────────────────────

    def _forget_backend(self):
        """Drop the cached resolution so the next call re-probes and can fail over."""
        if self.backend is not None:
            logger.warning(
                "Backend %s failed — dropping the cached resolution; the next call re-probes",
                self.backend,
            )
        self.backend = None
        self._backend_resolved_at = None

    def _is_degraded(self) -> bool:
        """True when the active backend is not the preferred one for this config."""
        return bool(self.dashboards_url) and self.backend != BACKEND_DASHBOARDS

    def _needs_resolution(self) -> bool:
        """Whether _resolve_backend must probe rather than reuse its cached answer.

        Policy, and why (backlog #6 — one 401 or SSO redirect at first-call time
        used to demote the backend for the entire life of a long-running MCP
        server, with no way back short of a restart):

          * nothing resolved yet -> probe;
          * the preferred backend is active -> never probe on a schedule. Adding a
            round trip to every request to detect a failure the request itself
            will report is a poor trade; a failed request calls _forget_backend()
            instead, so recovery costs one probe on the *next* call;
          * a degraded resolution (Dashboards configured, direct API won) -> probe
            again once backend_recheck_seconds has passed, so a transient
            Dashboards fault heals itself within that window at a cost of at most
            one extra round trip per window;
          * a backend set from outside (tests, callers) has no recorded age and is
            taken at face value.
        """
        if self.backend is None:
            return True
        if not self._is_degraded() or self._backend_resolved_at is None:
            return False
        return (time.monotonic() - self._backend_resolved_at) >= self.backend_recheck_seconds

    def _candidates(self) -> list:
        """(probe url, backend, context, operator-facing label) in preference order."""
        candidates = []
        if self.dashboards_url:
            candidates.append((
                f"{self.dashboards_url}/api/status",
                BACKEND_DASHBOARDS,
                "GET /api/status (Dashboards liveness probe)",
                f"OpenSearch Dashboards {self.dashboards_url} (OPENSEARCH_DASHBOARDS_URL)",
            ))
        if self.opensearch_url:
            candidates.append((
                f"{self.opensearch_url}/",
                BACKEND_OPENSEARCH,
                "GET / (OpenSearch liveness probe)",
                f"direct OpenSearch {self.opensearch_url} (OPENSEARCH_URL)",
            ))
        return candidates

    def _probe(self, url: str, backend: str, context: str):
        """Probe one backend. Returns None on success, else the reason it lost.

        The reason is the point: the two bare `except Exception` blocks this
        replaces collapsed 401, an expired certificate and "nothing is listening"
        into one message that told the operator to check the URL.
        """
        try:
            r = self._send(
                self._session.get, url, context,
                budget=min(_PROBE_BUDGET_SECONDS, self.timeout), check_status=False,
            )
        except TransportError as exc:
            logger.warning("%s: %s", context, exc.reason)
            return exc.reason
        if r.status_code != 200:
            reason = _probe_status_reason(r)
            logger.warning("%s: %s", context, reason)
            return reason
        try:
            data = r.json()
        except ValueError:
            return (
                f"answered HTTP 200 with a non-JSON body (final URL {r.url}) — "
                "typically an SSO or login page rather than the API"
            )
        self.backend = backend
        self.server_version = _val(_dig(data, "version"), "number", "?")
        self._backend_resolved_at = time.monotonic()
        logger.info(
            "Backend: %s %s (v%s), probe %d ms",
            backend, url, self.server_version, self.last_query_ms,
        )
        return None

    def _resolve_backend(self, force: bool = False):
        """Resolve the active backend, Dashboards first, then direct OpenSearch.

        Raises RuntimeError naming what was attempted and why each attempt failed.
        See _needs_resolution for the caching policy; `force=True` always probes,
        which is what makes test_connection() an honest liveness check.
        """
        if not force and not self._needs_resolution():
            return
        attempts = []
        self._backend_attempts = attempts
        for url, backend, context, label in self._candidates():
            reason = self._probe(url, backend, context)
            if reason is None:
                return
            attempts.append((label, reason))
        self._forget_backend()
        raise RuntimeError(
            _resolution_error(attempts, self.dashboards_url, self.opensearch_url)
        )

    # ── Low-level requests ────────────────────────────────────

    def _get(self, path: str, params: dict | None = None) -> dict:
        self._check_path("GET", path)
        self._resolve_backend()
        if self.backend == BACKEND_DASHBOARDS:
            return self._dashboards_proxy("GET", path, params=params)
        context = f"GET {path}"
        r = self._send(
            self._session.get, f"{self.opensearch_url}{path}", context, params=params
        )
        return _decode_json(r, context)

    def _post(self, path: str, body: dict | None = None, params: dict | None = None) -> dict:
        self._check_path("POST", path)
        self._resolve_backend()
        if self.backend == BACKEND_DASHBOARDS:
            return self._dashboards_proxy("POST", path, body=body, params=params)
        context = f"POST {path}"
        r = self._send(
            self._session.post,
            f"{self.opensearch_url}{path}",
            context,
            json=body,
            params=params,
        )
        return _decode_json(r, context)

    def _dashboards_proxy(
        self, method: str, path: str, body: dict | None = None, params: dict | None = None
    ) -> dict:
        """Route an OpenSearch request through Dashboards /api/console/proxy.

        No body is sent when there is none. `json=body or {}` used to put a 2-byte
        `{}` on the wire for every proxied GET, which the direct path never does —
        so cluster_health, list_indices, get_mapping, index_settings and get_alerts
        differed between backends on handlers that declare no body. Nothing proved
        the two paths equivalent; now one line fewer makes them differ.
        """
        os_path = path.lstrip("/")
        if params:
            os_path = f"{os_path}?{urlencode(params)}"
        context = f"{method} {path} (via Dashboards proxy)"
        r = self._send(
            self._session.post,
            f"{self.dashboards_url}/api/console/proxy",
            context,
            params={"path": os_path, "method": method},
            json=body if body else None,
        )
        return _decode_json(r, context)

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
        """Flatten a mapping's `properties` tree to {dotted_field: type}.

        Both nesting keys are followed: `properties` (sub-objects) and `fields`
        (multi-fields). The latter is what makes `rule.description.keyword`
        visible — the exact remediation every aggregation warning advises, which
        was previously absent from get_mapping's answer.
        """
        fields = {}
        for name, cfg in properties.items():
            full = f"{prefix}.{name}" if prefix else name
            if not isinstance(cfg, dict):
                continue
            fields[full] = _val(cfg, "type", "object")
            for nesting_key in ("properties", "fields"):
                sub = cfg.get(nesting_key)
                if isinstance(sub, dict):
                    fields.update(self._flatten_mapping(sub, prefix=full))
        return fields

    # ── Public read-only API ──────────────────────────────────

    def test_connection(self) -> dict:
        """Probe connectivity for real. Returns backend, version, URL, username, latency.

        Always issues one request (a ~10s-budget liveness probe), and re-runs the
        backend preference order, so it doubles as the manual failover trigger.

        This used to be the one tool that could not fail: it called
        _resolve_backend(), which returned immediately once a backend was cached,
        so after the first successful call it performed no I/O at all and answered
        {"ok": true} against a dead cluster. Its own docstring tells the agent to
        call it first to confirm connectivity, so a false green here made every
        subsequent failure look like a privilege or index-name problem.

        Raises RuntimeError (naming the real cause per backend) when nothing is
        reachable — "ok": True is now a fact about this instant, not a memory.
        """
        self._resolve_backend(force=True)
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
            "latency_ms": self.last_query_ms,
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
            return sorted(data, key=lambda x: str(_val(x, "index", "")))
        return data

    def _no_dashboards_message(self) -> str:
        """Why the Dashboards backend is not active, truthfully.

        The old message said "Set OPENSEARCH_DASHBOARDS_URL to enable it" even when
        it was set and the probe had merely been refused — and this tool is the
        documented fallback for a 403 on _cat/indices, so that lie cost an
        investigation step at exactly the wrong moment.
        """
        head = "list_index_patterns requires the OpenSearch Dashboards backend."
        if not self.dashboards_url:
            return (
                f"{head} OPENSEARCH_DASHBOARDS_URL is not set, so no Dashboards "
                f"was ever probed; the active backend is {self.backend}. Set it to "
                "enable this tool, or use list_indices instead."
            )
        reasons = [
            reason for label, reason in self._backend_attempts
            if label.startswith("OpenSearch Dashboards")
        ]
        why = reasons[-1] if reasons else "the probe did not succeed"
        return (
            f"{head} OPENSEARCH_DASHBOARDS_URL is set ({self.dashboards_url}) but "
            f"its /api/status probe failed: {why}. The active backend is "
            f"{self.backend}, which has no saved index patterns — fix the "
            "Dashboards problem above, or use list_indices."
        )

    def list_index_patterns(self) -> list:
        """List Dashboards saved index patterns. Dashboards backend only.

        Re-probes before refusing when a Dashboards URL is configured but is not
        the active backend: this tool exists precisely for the case where direct
        access is restricted, so a stale demotion must not be allowed to make it
        permanently unavailable.
        """
        self._resolve_backend(force=self._is_degraded())
        if self.backend != BACKEND_DASHBOARDS:
            raise RuntimeError(self._no_dashboards_message())
        # Try saved_objects API (Dashboards 2.x), then data_views API (newer versions)
        for endpoint, params, extractor in [
            (
                "/api/saved_objects/_find",
                {"type": "index-pattern", "fields": ["title", "timeFieldName"], "per_page": 200},
                lambda data: [
                    {
                        "id": p.get("id"),
                        "title": _val(_dig(p, "attributes"), "title"),
                        "timeFieldName": _val(_dig(p, "attributes"), "timeFieldName"),
                    }
                    for p in _items(data, "saved_objects")
                ],
            ),
            (
                "/api/data_views",
                {},
                lambda data: [
                    {
                        "id": p.get("id"),
                        "title": p.get("title"),
                        "timeFieldName": p.get("timeFieldName"),
                    }
                    for p in _items(data, "data_view")
                ],
            ),
        ]:
            context = f"GET {endpoint} (index patterns)"
            r = self._send(
                self._session.get,
                f"{self.dashboards_url}{endpoint}",
                context,
                params=params,
                check_status=False,
            )
            if r.status_code == 404:
                continue
            self._raise_for_status(r, context)
            return extractor(_decode_json(r, context))
        raise RuntimeError(
            "Could not list index patterns — neither /api/saved_objects/_find "
            "nor /api/data_views returned a valid response. "
            "Check Dashboards version and user privileges."
        )

    def get_mapping(self, index: str) -> dict:
        """Flattened field mappings for an index. Returns {index: {field: type}}."""
        result = self._get(f"/{index}/_mapping")
        return {
            idx: self._flatten_mapping(_dig(mapping, "mappings", "properties"))
            for idx, mapping in _dig(result).items()
        }

    def discover_fields(
        self,
        index: str,
        query_string: str = "*",
        from_ts: str | None = None,
        to_ts: str | None = None,
        ts_field: str = "@timestamp",
        sample_size: int = 10,
    ) -> dict:
        """Sample documents and return {field_name: python_type} in dot-notation.

        Nested fields and fields inside arrays of objects are flattened:
        agent.name, rule.level, rule.mitre.id.
        sample_size is capped at MAX_SAMPLE_SIZE to prevent large fetches.

        Adds a "_warning" key when the sample size is capped and/or no time range
        is given; both messages are joined with " | ", as search_string does.
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
        for hit in _items(result, "hits"):
            fields.update(_flatten_doc(hit))
        out = dict(sorted(fields.items()))
        warnings = []
        if capped < sample_size:
            warnings.append(
                f"sample_size capped at {capped} (requested {sample_size}). "
                f"Maximum is {MAX_SAMPLE_SIZE}."
            )
        # search_string's own warnings (no time range, limit cap) would otherwise
        # be dropped on the floor: this method reads only `hits` from its result.
        inherited = result.get("warning")
        if inherited:
            warnings.append(inherited)
        if warnings:
            out["_warning"] = " | ".join(warnings)
        return out

    def search_string(
        self,
        index: str,
        query_string: str = "*",
        from_ts: str | None = None,
        to_ts: str | None = None,
        ts_field: str = "@timestamp",
        limit: int = 50,
        offset: int = 0,
        sort_field: str | None = None,
        sort_dir: str = "desc",
        source_fields: list | None = None,
    ) -> dict:
        """Search with a Lucene query string.

        Returns {"total": N, "ids": [...], "hits": [...]}. `ids[i]` is the
        OpenSearch `_id` of `hits[i]` — the two lists are index-aligned and always
        the same length. `_id` is deliberately NOT merged into each hit dict: a
        document's own `_source` may contain a field literally called `_id`, and
        merging would silently overwrite one with the other. This is the only way
        to obtain the doc_id that `explain` requires.

        Use offset for pagination: offset=200 fetches the next page after the first 200.
        Adds a "warning" key when limit is capped or no time range is given.
        """
        capped = max(min(limit, self.max_search_limit), 0)
        q = self._qs_query(query_string, from_ts, to_ts, ts_field)
        sort_on = sort_field or ts_field
        body = {
            "query": q,
            "size": capped,
            "from": max(offset, 0),
            # unmapped_type makes the sort degrade instead of 400ing on an index
            # that lacks the field — a wildcard where only some indices carry
            # @timestamp, or an index with no timestamp field at all. Without it
            # the documented "use discover_fields when get_mapping is blocked"
            # fallback is circular, because discover_fields sorts too.
            "sort": [{
                sort_on: {
                    "order": sort_dir,
                    "unmapped_type": "date" if sort_on == ts_field else "keyword",
                }
            }],
        }
        if source_fields:
            body["_source"] = source_fields
        result = self._post(f"/{index}/_search", body=body)
        hits = _dig(result, "hits")
        total = _val(hits, "total", 0)
        if isinstance(total, dict):
            total = _val(total, "value", 0)
        elif not isinstance(total, (int, float)):
            total = 0
        warnings = []
        if capped < limit:
            warnings.append(
                f"limit capped at {capped} (requested {limit}). "
                "Use source_fields to reduce response size, or paginate with multiple calls."
            )
        if not from_ts and not to_ts:
            warnings.append(_NO_TIME_RANGE_WARNING)
        raw_hits = _items(hits, "hits")
        out = {"total": total}
        if warnings:
            out["warning"] = " | ".join(warnings)
        out["ids"] = [h.get("_id") if isinstance(h, dict) else None for h in raw_hits]
        out["hits"] = [_dig(h, "_source") for h in raw_hits]
        return out

    def timeline(
        self,
        index: str,
        entity: str,
        fields: list,
        from_ts: str | None = None,
        to_ts: str | None = None,
        ts_field: str = "@timestamp",
        limit: int = 100,
        source_fields: list | None = None,
        extra_query: str | None = None,
    ) -> dict:
        """Chronological event timeline for one entity across multiple fields.

        Matches `entity` against any of `fields` (OR) and returns hits oldest-first.
        Returns {"total": N, "entity": str, "fields": [...], "events": [...]}.
        """
        if not fields:
            raise ValueError("fields must be a non-empty list of field names to match the entity against.")
        # Backslash first, then the quote: inside a Lucene phrase the backslash is
        # itself the escape character, so a Windows path ("C:\Windows\System32")
        # or a DOMAIN\user value is corrupted — \W and \S are consumed as escapes
        # and the phrase stops matching the document it came from — and a trailing
        # backslash escapes the closing quote, producing an unparseable query.
        # Escaping the quote first would double-escape the backslashes it added.
        escaped = str(entity).replace("\\", "\\\\").replace('"', '\\"')
        clauses = " OR ".join(f'{f}:"{escaped}"' for f in fields)
        qs = f"({clauses})"
        if extra_query:
            qs = f"{qs} AND ({extra_query})"
        result = self.search_string(
            index,
            query_string=qs,
            from_ts=from_ts,
            to_ts=to_ts,
            ts_field=ts_field,
            limit=limit,
            sort_field=ts_field,
            sort_dir="asc",
            source_fields=source_fields,
        )
        out = {
            "total": result.get("total", 0),
            "entity": entity,
            "fields": fields,
            "events": result.get("hits", []),
        }
        if "warning" in result:
            out["warning"] = result["warning"]
        return out

    def count(
        self,
        index: str,
        query_string: str = "*",
        from_ts: str | None = None,
        to_ts: str | None = None,
        ts_field: str = "@timestamp",
    ) -> dict:
        """Count documents matching a query. Returns {"count": N}.

        Adds a "warning" key when no time range is given (full-index scan).
        """
        q = self._qs_query(query_string, from_ts, to_ts, ts_field)
        result = self._post(f"/{index}/_count", body={"query": q})
        out = {"count": _val(result, "count", 0)}
        if not from_ts and not to_ts:
            out["warning"] = _NO_TIME_RANGE_WARNING
        return out

    def terms(
        self,
        index: str,
        field: str,
        query_string: str = "*",
        from_ts: str | None = None,
        to_ts: str | None = None,
        ts_field: str = "@timestamp",
        size: int = 50,
    ) -> dict:
        """Top N values of a field. Returns {value: count} sorted descending.

        `size` is capped at self.max_terms_size (default MAX_TERMS_SIZE, override
        with OPENSEARCH_MAX_TERMS_SIZE): a high-cardinality terms aggregation is
        the cheapest way for a caller to exhaust a coordinating node's heap.

        Adds a "_warning" key when the size is capped and/or the field may be a
        text field (no .keyword suffix), which triggers fielddata and loads heap
        memory on the cluster. Multiple messages are joined with " | ".
        """
        q = self._qs_query(query_string, from_ts, to_ts, ts_field)
        capped = self._cap_terms_size(size)
        body = {
            "size": 0,
            "query": q,
            "aggs": {"top_values": {"terms": {"field": field, "size": capped}}},
        }
        result = self._post(f"/{index}/_search", body=body)
        buckets = _items(_dig(result, "aggregations", "top_values"), "buckets")
        out = {b["key"]: b["doc_count"] for b in buckets}
        warnings = []
        if capped != size:
            warnings.append(self._terms_size_warning(size, capped))
        if _is_likely_text_field(field):
            warnings.append(
                f"Field '{field}' looks like a text field. If results look wrong, "
                f"try '{field}.keyword' instead. Using text fields in aggregations "
                "loads fielddata into heap memory."
            )
        if warnings:
            out["_warning"] = " | ".join(warnings)
        return out

    # ── Aggregation caps ──────────────────────────────────────

    def _cap_terms_size(self, size) -> int:
        """Clamp a requested terms `size` into [1, self.max_terms_size]."""
        try:
            requested = int(size)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"size must be an integer. Got: {size!r}") from exc
        return max(min(requested, self.max_terms_size), 1)

    def _terms_size_warning(self, requested, capped: int) -> str:
        return (
            f"size capped at {capped:,} (requested {requested}). Maximum is "
            f"{self.max_terms_size:,} (OPENSEARCH_MAX_TERMS_SIZE). A terms "
            "aggregation over very high cardinality loads the whole bucket set "
            "into cluster heap; narrow the query or the time range instead."
        )

    def _cap_result_size(self, size) -> tuple[int, str | None]:
        """Clamp a requested result-set `size` into [1, self.max_search_limit].

        Used by the Alerting and Anomaly Detection reads, which return whole
        records rather than aggregation buckets and so share the search limit
        rather than the terms limit.

        Returns (capped, warning). The warning is None when nothing was clamped —
        silently returning fewer records than asked for is how an agent concludes
        "that is all there is" and stops looking, so the caller must surface it.
        """
        try:
            requested = int(size)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"size must be an integer. Got: {size!r}") from exc
        capped = max(min(requested, self.max_search_limit), 1)
        if capped == requested:
            return capped, None
        return capped, (
            f"size capped at {capped:,} (requested {requested}). Maximum is "
            f"{self.max_search_limit:,} (OPENSEARCH_MAX_SEARCH_LIMIT). "
            "Results may be incomplete — narrow the filter, or page by "
            "restricting to one monitor or detector at a time."
        )

    def multi_terms(
        self,
        index: str,
        aggregations: list,
        query_string: str = "*",
        from_ts: str | None = None,
        to_ts: str | None = None,
        ts_field: str = "@timestamp",
    ) -> dict:
        """Multiple field frequency analyses in one call.

        Pre-conditions: `aggregations` is a non-empty list of dicts, each with a
        non-empty string "id" and "field" and an optional positive integer "size".
        Ids must be unique — two specs sharing an id used to collapse into a
        single aggregation, so the caller silently received fewer answers than
        questions asked, with no way to tell which field the numbers came from.
        Every violation raises ValueError naming the offending entry, before any
        request is issued.

        aggregations: [{"id": "...", "field": "...", "size": N}, ...]
        Returns {id: {value: count}}.
        Adds a "_warning" key listing any capping that happened and any fields
        that may cause fielddata heap pressure, joined with " | ".
        """
        specs, warnings = self._validate_aggregations(aggregations)
        q = self._qs_query(query_string, from_ts, to_ts, ts_field)
        aggs = {
            a["id"]: {"terms": {"field": a["field"], "size": a["size"]}}
            for a in specs
        }
        result = self._post(f"/{index}/_search", body={"size": 0, "query": q, "aggs": aggs})
        out = {}
        for a in specs:
            buckets = _items(_dig(result, "aggregations", a["id"]), "buckets")
            out[a["id"]] = {b["key"]: b["doc_count"] for b in buckets}
        warnings.extend(
            f"Field '{a['field']}' looks like a text field — "
            f"try '{a['field']}.keyword' to avoid fielddata heap pressure."
            for a in specs
            if _is_likely_text_field(a["field"])
        )
        if warnings:
            out["_warning"] = " | ".join(warnings)
        return out

    def _validate_aggregations(self, aggregations) -> tuple:
        """Validate and normalise multi_terms specs. Returns (specs, warnings).

        Post-condition: every returned spec has a unique non-empty "id", a
        non-empty "field" and an integer "size" within the terms-size cap.
        """
        if not aggregations:
            raise ValueError("aggregations list must not be empty.")
        if not isinstance(aggregations, list):
            raise ValueError(
                "aggregations must be a list of dicts, each with an 'id' and a "
                f"'field'. Got: {type(aggregations).__name__}."
            )
        warnings = []
        kept = aggregations
        if len(aggregations) > self.max_aggregations:
            kept = aggregations[: self.max_aggregations]
            dropped = [
                str(a.get("id")) if isinstance(a, dict) else repr(a)
                for a in aggregations[self.max_aggregations:]
            ]
            warnings.append(
                f"Only the first {self.max_aggregations} aggregations were run "
                f"(requested {len(aggregations)}). Maximum is "
                f"{self.max_aggregations} (OPENSEARCH_MAX_AGGREGATIONS). Not "
                f"requested, no results returned for: {', '.join(dropped)}. "
                "Split them across several calls."
            )
        specs, seen = [], set()
        for position, spec in enumerate(kept):
            self._check_aggregation_shape(spec, position, seen)
            seen.add(spec["id"])
            capped = self._cap_terms_size(spec.get("size", 50))
            if capped != spec.get("size", 50):
                warnings.append(f"[{spec['id']}] {self._terms_size_warning(spec.get('size'), capped)}")
            specs.append({"id": spec["id"], "field": spec["field"], "size": capped})
        return specs, warnings

    @staticmethod
    def _check_aggregation_shape(spec, position: int, seen: set):
        """Raise ValueError unless `spec` is a usable aggregation request.

        Split out of _validate_aggregations to keep each function under the
        complexity ceiling the build enforces — the pre-condition checks and the
        capping/accumulation are two separate jobs, and the ceiling firing was the
        signal to separate them rather than to raise the ceiling.
        """
        if not isinstance(spec, dict):
            raise ValueError(
                f"aggregations[{position}] must be a dict with 'id' and "
                f"'field' keys. Got: {spec!r}"
            )
        for key in ("id", "field"):
            value = spec.get(key)
            if not value or not isinstance(value, str):
                raise ValueError(
                    f"aggregations[{position}] is missing a non-empty string "
                    f"'{key}'. Each entry needs both, e.g. "
                    '{"id": "agents", "field": "agent.name", "size": 20}. '
                    f"Got: {spec!r}"
                )
        if spec["id"].startswith("_"):
            # The result dict carries both aggregation results and metadata
            # ("_warning"), so a caller-supplied id beginning with "_" can collide
            # with a metadata key and silently replace the aggregation the caller
            # asked for with a warning string. Reserving the prefix closes that at
            # no cost to the response shape; the alternative was restructuring the
            # output of three tools, which would break every consumer for a case a
            # caller can simply be told not to create.
            raise ValueError(
                f"aggregation id {spec['id']!r} at aggregations[{position}] "
                "starts with '_', which is reserved for response metadata such "
                "as '_warning'. Such an id would be overwritten by, or would "
                "overwrite, that metadata. Choose a name not starting with '_'."
            )
        if spec["id"] in seen:
            raise ValueError(
                f"Duplicate aggregation id {spec['id']!r} at aggregations"
                f"[{position}]. Ids label the results, so they must be "
                "unique — otherwise two aggregations collapse into one and "
                "you cannot tell which field the counts belong to."
            )

    @staticmethod
    def _parse_ts(ts: str) -> float:
        """Parse a UTC ISO-8601 timestamp to a Unix epoch float.

        Accepts everything OpenSearch's strict_date_optional_time accepts and
        everything `datetime.isoformat()` emits, so a timestamp is portable
        between this guard and the tools that forward strings to the cluster
        untouched: date only, "HH:MM" or "HH:MM:SS" precision, any number of
        fractional digits (truncated to microseconds), a "Z"/"z" suffix, and
        numeric offsets written "+HH:MM", "+HHMM" or "+HH".

        A timestamp carrying no zone is treated as UTC, which is the documented
        contract of every ts parameter in this server.

        Pre-condition: `ts` is a non-empty string.
        Raises:
            ValueError: for a non-string, an empty string, or anything the ISO
                grammar does not cover. This is the only exception type that may
                escape — callers translate ValueError into an actionable message,
                so a TypeError or AttributeError would bypass that layer.
        """
        if not isinstance(ts, str) or not ts.strip():
            raise ValueError(f"Cannot parse timestamp: {ts!r}")
        s = ts.strip()
        if " " in s:
            raise ValueError(
                f"Cannot parse timestamp: {ts!r}. Use the ISO 8601 'T' separator, "
                "e.g. '2026-06-23T00:00:00Z' — a space is not accepted by "
                "OpenSearch either."
            )
        # Suffix removal, not character-set stripping: "...ZZZ" is malformed and
        # must be rejected rather than silently read as a single Z.
        if s[-1] in ("Z", "z"):
            s = s[:-1] + "+00:00"
        head, sep, tail = s.partition("T")
        if sep:
            tail = _TS_FRACTION_RE.sub(
                lambda m: "." + m.group(1)[:6].ljust(6, "0"), tail, count=1
            )
            tail = _TS_OFFSET_NO_COLON_RE.sub(r"\1\2:\3", tail)
            tail = _TS_OFFSET_HOURS_ONLY_RE.sub(r"\1\2:00", tail)
            s = f"{head}T{tail}"
        try:
            parsed = datetime.fromisoformat(s)
        except ValueError as exc:
            raise ValueError(f"Cannot parse timestamp: {ts!r}") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()

    @staticmethod
    def _parse_interval(interval: str) -> tuple:
        """Validate an interval string. Returns (magnitude, unit).

        Post-condition: the returned pair is dispatchable — the magnitude is >= 1
        and the unit/magnitude combination is one OpenSearch will accept in the
        date_histogram key that _interval_agg_key() picks for it.
        """
        m = _INTERVAL_RE.match(interval or "")
        if not m:
            raise ValueError(
                f"Invalid interval {interval!r}. "
                "Use a number + unit, e.g. '15m', '1h', '1d'. "
                "Valid units: s m h d w M y. Or use 'auto'."
            )
        magnitude, unit = int(m.group(1)), m.group(2)
        if magnitude < 1:
            raise ValueError(
                f"Invalid interval {interval!r}: the magnitude must be at least 1. "
                "Use e.g. '1m' for one-minute buckets, or 'auto'."
            )
        if unit in _CALENDAR_UNITS and magnitude != 1:
            raise ValueError(
                f"Invalid interval {interval!r}: w, M and y are calendar units, and "
                f"OpenSearch accepts a calendar interval only with a multiplier of 1. "
                f"Use '1{unit}', or express the span with a fixed unit (s m h d) — "
                "e.g. '14d' instead of '2w'."
            )
        return magnitude, unit

    @staticmethod
    def _interval_agg_key(unit: str) -> str:
        """The date_histogram key a unit belongs in.

        w/M/y are calendar-only: putting them in `fixed_interval` — which accepts
        only ms/s/m/h/d — makes the cluster answer 400, which _raise_for_status
        then renders as the misleading "check your query syntax or field names".
        """
        return "calendar_interval" if unit in _CALENDAR_UNITS else "fixed_interval"

    def _check_histogram_buckets(self, from_ts: str, to_ts: str, interval: str):
        """Validate the interval and bounds; reject bucket-count explosions.

        Post-condition: returns None only when the request is dispatchable — a
        valid interval, two parseable bounds in chronological order, and an
        expected bucket count within self.max_histogram_buckets. Every other
        outcome raises ValueError, and ValueError alone: callers guard on it to
        turn the failure into an actionable message.
        """
        if interval == "auto":
            return  # auto delegates to OpenSearch with a fixed cap of 50
        magnitude, unit = self._parse_interval(interval)
        try:
            t0 = self._parse_ts(from_ts)
            t1 = self._parse_ts(to_ts)
        except ValueError as e:
            raise ValueError(f"Cannot compute bucket count: {e}") from e
        if t1 < t0:
            raise ValueError(
                f"Reversed time range: from_ts ({from_ts}) is after to_ts ({to_ts}). "
                "Swap the bounds — a reversed range matches no documents, so the "
                "histogram would be empty rather than wrong-looking."
            )
        interval_secs = magnitude * _INTERVAL_SECONDS[unit]
        expected = int((t1 - t0) / interval_secs) + 1
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
        """Temporal histogram. Returns {"interval_used": str, "results": {timestamp: count}}.

        Calendar units (w, M, y) are routed to `calendar_interval` and everything
        else to `fixed_interval`, because OpenSearch accepts each unit in exactly
        one of the two.
        """
        self._check_histogram_buckets(from_ts, to_ts, interval)
        q = self._qs_query(query_string, from_ts, to_ts, ts_field)
        if interval == "auto":
            agg_spec = {"auto_date_histogram": {"field": ts_field, "buckets": 50}}
        else:
            _, unit = self._parse_interval(interval)
            agg_spec = {
                "date_histogram": {
                    "field": ts_field,
                    self._interval_agg_key(unit): interval,
                    "min_doc_count": 0,
                    "extended_bounds": {"min": from_ts, "max": to_ts},
                }
            }
        result = self._post(
            f"/{index}/_search",
            body={"size": 0, "query": q, "aggs": {"over_time": agg_spec}},
        )
        agg_result = _dig(result, "aggregations", "over_time")
        buckets = _items(agg_result, "buckets")
        # Resolve the actual interval used (relevant when interval="auto")
        interval_used = interval
        if interval == "auto" and buckets:
            interval_used = _val(agg_result, "interval", "auto")
        results = {}
        for b in buckets:
            label = _bucket_label(b)
            if label is not None:
                results[label] = b.get("doc_count")
        return {"interval_used": interval_used, "results": results}

    def stats(
        self,
        index: str,
        field: str,
        query_string: str = "*",
        from_ts: str | None = None,
        to_ts: str | None = None,
        ts_field: str = "@timestamp",
    ) -> dict:
        """Numeric stats (count, min, max, avg, sum, std_deviation) for a field.

        Post-condition: "count" is always an integer. The other five are the
        numbers OpenSearch computed, or **None when there was nothing to compute**
        — extended_stats over zero matching documents returns min/max/avg/
        std_deviation as JSON null, and null is reported as null rather than
        coerced to 0. Coercing would be a silent wrong answer: an agent could not
        distinguish "no documents matched" from "the minimum really was zero".
        Callers must therefore check for None before doing arithmetic; count == 0
        is the reliable signal that the other metrics carry no information.
        """
        q = self._qs_query(query_string, from_ts, to_ts, ts_field)
        body = {
            "size": 0,
            "query": q,
            "aggs": {"field_stats": {"extended_stats": {"field": field}}},
        }
        try:
            result = self._post(f"/{index}/_search", body=body)
        except RuntimeError as exc:
            if "Bad request" in str(exc):
                raise RuntimeError(
                    f"Field '{field}' is not numeric or does not support stats aggregation. "
                    "Use a numeric field such as 'rule.level' or 'data.bytes'. "
                    f"Original error: {exc}"
                ) from None
            raise
        st = _dig(result, "aggregations", "field_stats")
        return {
            "count": _val(st, "count", 0),
            "min": st.get("min"),
            "max": st.get("max"),
            "avg": st.get("avg"),
            "sum": st.get("sum"),
            "std_deviation": st.get("std_deviation"),
        }

    # ── Escape hatch (still allowlisted) ──────────────────────

    def api_get(self, path: str, params: dict | None = None):
        """GET a read-only endpoint that has no dedicated method here.

        Pre-condition: `path` is an absolute path on the GET read-only
        allowlist. Anything else raises PermissionError (or ValueError when the
        path is not absolute) — see _check_path.

        Returns the decoded JSON exactly as the cluster sent it, so callers must
        cope with both objects and arrays: the _cat/* APIs return arrays.
        """
        return self._get(path, params=params)

    def ppl(self, query: str) -> dict:
        """Execute a PPL (Piped Processing Language) query."""
        if not query or not query.strip():
            raise ValueError("PPL query must not be empty.")
        return self._post("/_plugins/_ppl", body={"query": query})

    def index_settings(self, index: str) -> dict:
        """Fetch index settings. Returns a simplified summary per index.

        Every lookup treats a present-but-null value as absent: an index whose ISM
        policy has been detached carries `"lifecycle": null`, which used to raise
        AttributeError and kill the whole call, and a null refresh_interval used to
        defeat the documented "1s" default instead of applying it.
        """
        raw = self._get(f"/{index}/_settings")
        out = {}
        for idx_name, cfg in _dig(raw).items():
            s = _dig(cfg, "settings", "index")
            out[idx_name] = {
                "number_of_shards": s.get("number_of_shards"),
                "number_of_replicas": s.get("number_of_replicas"),
                "refresh_interval": _val(s, "refresh_interval", "1s"),
                "lifecycle_name": _val(_dig(s, "lifecycle"), "name"),
                "creation_date_ms": s.get("creation_date"),
            }
        return out

    def explain(self, index: str, doc_id: str, query: dict) -> dict:
        """Explain why a document matches or doesn't match a query.

        Pre-conditions: `index` is a single exact index name and `doc_id` a
        single document id — neither may be empty or contain "/", since both are
        interpolated into the request path. This is the only place that path is
        built; _check_path then decides whether it may be issued.
        """
        for name, value in (("index", index), ("doc_id", doc_id)):
            if not value or not isinstance(value, str) or "/" in value:
                raise ValueError(
                    f"{name} must be a non-empty string without '/'. Got: {value!r}"
                )
        return self._post(f"/{index}/_explain/{doc_id}", body={"query": query})

    # ── Alerting plugin (read-only) ───────────────────────────

    def list_monitors(self, size: int = 50) -> list:
        """List configured Alerting-plugin monitors. Returns a summary per monitor.

        Requires the OpenSearch Alerting plugin and cluster:admin/opendistro/
        alerting/monitor/search privilege. Returns 403/404 if unavailable.
        """
        body = {"size": size, "query": {"match_all": {}}}
        result = self._post("/_plugins/_alerting/monitors/_search", body=body)
        out = []
        for hit in _items(_dig(result, "hits"), "hits"):
            src = _dig(hit, "_source")
            mon = _dig(src, "monitor") or src
            out.append({
                "id": hit.get("_id"),
                "name": mon.get("name"),
                "enabled": mon.get("enabled"),
                "type": mon.get("monitor_type"),
                "schedule": mon.get("schedule"),
            })
        return out

    def get_alerts(
        self,
        state: str | None = None,
        monitor_id: str | None = None,
        size: int = 50,
    ) -> dict:
        """Fetch alerts raised by Alerting-plugin monitors.

        state: filter by alert state, e.g. "ACTIVE", "ACKNOWLEDGED", "COMPLETED".
        monitor_id: restrict to a single monitor.
        size: capped at self.max_search_limit (default MAX_SEARCH_LIMIT, override
              with OPENSEARCH_MAX_SEARCH_LIMIT); a "warning" key says so when it
              is capped.
        Requires the Alerting plugin. Returns 403/404 if unavailable.
        """
        capped, size_warning = self._cap_result_size(size)
        params = {"size": capped, "sortField": "start_time", "sortOrder": "desc"}
        if state:
            params["alertState"] = state
        if monitor_id:
            params["monitorId"] = monitor_id
        result = self._get("/_plugins/_alerting/monitors/alerts", params=params)
        alerts = [
            {
                "id": a.get("alert_id") or a.get("id"),
                "monitor_name": a.get("monitor_name"),
                "trigger_name": a.get("trigger_name"),
                "state": a.get("state"),
                "severity": a.get("severity"),
                "start_time": a.get("start_time"),
                "last_notification_time": a.get("last_notification_time"),
                "acknowledged_time": a.get("acknowledged_time"),
            }
            for a in result.get("alerts", [])
        ]
        out = {"total": result.get("totalAlerts", len(alerts))}
        if size_warning:
            out["warning"] = size_warning
        out["alerts"] = alerts
        return out

    # ── Anomaly Detection plugin (read-only) ──────────────────

    def list_detectors(self, size: int = 50) -> list:
        """List configured Anomaly Detection detectors. Returns a summary per detector.

        Requires the OpenSearch Anomaly Detection plugin and detector-search
        privilege. Returns 403/404 if unavailable.
        """
        body = {"size": size, "query": {"match_all": {}}}
        result = self._post("/_plugins/_anomaly_detection/detectors/_search", body=body)
        out = []
        for hit in result.get("hits", {}).get("hits", []):
            src = hit.get("_source", {})
            out.append({
                "id": hit.get("_id"),
                "name": src.get("name"),
                "description": src.get("description"),
                "indices": src.get("indices"),
                "detection_interval": src.get("detection_interval"),
            })
        return out

    def get_anomaly_results(
        self,
        detector_id: str | None = None,
        from_ts: str | None = None,
        to_ts: str | None = None,
        min_grade: float = 0.0,
        size: int = 50,
    ) -> dict:
        """Fetch anomaly results, most anomalous first.

        detector_id: restrict to a single detector.
        min_grade: only return results with anomaly_grade >= this (0-1); default 0
                   returns anomalies only (grade > 0 is filtered when min_grade > 0).
        from_ts/to_ts: filter on data_end_time (UTC ISO 8601). Omitting both scans
                   the detector's whole result history; a "warning" key says so.
        size: capped at self.max_search_limit (default MAX_SEARCH_LIMIT, override
                   with OPENSEARCH_MAX_SEARCH_LIMIT); a "warning" key says so when
                   it is capped.
        Requires the Anomaly Detection plugin. Returns 403/404 if unavailable.
        """
        capped, size_warning = self._cap_result_size(size)
        filters = []
        if detector_id:
            filters.append({"term": {"detector_id": detector_id}})
        if min_grade and min_grade > 0:
            filters.append({"range": {"anomaly_grade": {"gte": min_grade}}})
        else:
            filters.append({"range": {"anomaly_grade": {"gt": 0}}})
        if from_ts or to_ts:
            rng = {}
            if from_ts:
                rng["gte"] = from_ts
            if to_ts:
                rng["lte"] = to_ts
            filters.append({"range": {"data_end_time": {**rng, "format": "strict_date_optional_time"}}})
        body = {
            "size": capped,
            "query": {"bool": {"filter": filters}},
            "sort": [{"anomaly_grade": {"order": "desc"}}],
        }
        result = self._post(
            "/_plugins/_anomaly_detection/detectors/results/_search", body=body
        )
        hits = _dig(result, "hits")
        total = _val(hits, "total", 0)
        if isinstance(total, dict):
            total = _val(total, "value", 0)
        anomalies = [
            {
                "detector_id": _val(_dig(h, "_source"), "detector_id"),
                "anomaly_grade": _val(_dig(h, "_source"), "anomaly_grade"),
                "confidence": _val(_dig(h, "_source"), "confidence"),
                "data_start_time": _val(_dig(h, "_source"), "data_start_time"),
                "data_end_time": _val(_dig(h, "_source"), "data_end_time"),
            }
            for h in _items(hits, "hits")
        ]
        warnings = [w for w in (size_warning,) if w]
        if not from_ts and not to_ts:
            warnings.append(_NO_TIME_RANGE_WARNING)
        out = {"total": total}
        if warnings:
            out["warning"] = " | ".join(warnings)
        out["anomalies"] = anomalies
        return out


# ── Config loading ────────────────────────────────────────────────────────────

def _allow_insecure_config() -> bool:
    """Whether the 0600 requirement on credential files is downgraded to a warning."""
    return _coerce_bool(
        os.environ.get("OPENSEARCH_ALLOW_INSECURE_CONFIG"),
        default=False,
        name="OPENSEARCH_ALLOW_INSECURE_CONFIG",
    )


def _require_private(path: str, what: str):
    """Refuse to read a credential file that other users can read.

    Applied to every file this module reads secrets from, which previously meant
    config.json only: the `.env` path had no check at all, so a world-readable
    file full of passwords was loaded in silence. One rule, all three paths, same
    OPENSEARCH_ALLOW_INSECURE_CONFIG escape hatch.
    """
    file_stat = os.stat(path)
    if not file_stat.st_mode & 0o077:
        return
    msg = (
        f"{what} {path} is readable by other users "
        f"(mode {stat.filemode(file_stat.st_mode)}). "
        f"Run: chmod 600 {path}"
    )
    if _allow_insecure_config():
        logger.warning(msg)
    else:
        raise PermissionError(msg)


def _load_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        return {}
    _require_private(CONFIG_FILE, "Config file")
    with open(CONFIG_FILE) as f:
        return json.load(f)


def _load_env_files() -> list:
    """Load the .env files this project documents, explicitly. Returns paths loaded.

    Two named paths, in this order (load_dotenv never overrides a real environment
    variable, so the first file to define a key wins):

      1. `<checkout>/.env`                     — the source-run / development path
      2. `~/.config/mcp-opensearch/.env`       — what setup.sh writes and the README documents

    Both are permission-checked. Previously `load_dotenv()` was called with no
    argument, so find_dotenv() walked up from the directory of *this file* — which
    meant (a) the documented ~/.config path was never read by the Python process
    at all, and (b) a .env beside the source was read silently whatever its mode.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return []
    loaded = []
    for path in (PROJECT_ENV_FILE, ENV_FILE):
        if not os.path.isfile(path):
            continue
        _require_private(path, "Credential file")
        load_dotenv(path)
        logger.info("Loaded environment from %s", path)
        loaded.append(path)
    return loaded


def _configure_logging():
    """Apply OPENSEARCH_LOG_LEVEL to this package's logger, if it is set.

    Needed because server.py calls logging.basicConfig(level=WARNING), which
    silences the only lines that say which backend won and why the other lost.
    Setting the level on this package's own logger is enough: propagated records
    are gated by the originating logger and by handler levels, not by the root
    logger's level.

    Logs go to stderr. stdout is the MCP stdio transport and a stray line there
    corrupts the protocol.
    """
    name = os.environ.get("OPENSEARCH_LOG_LEVEL")
    if not name:
        return
    level = logging.getLevelName(name.strip().upper())
    if not isinstance(level, int):
        raise ValueError(
            f"OPENSEARCH_LOG_LEVEL={name!r} is not a log level. "
            "Use DEBUG, INFO, WARNING, ERROR or CRITICAL."
        )
    package_logger = logging.getLogger(__name__.split(".")[0])
    package_logger.setLevel(level)
    if not logging.getLogger().handlers and not package_logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        package_logger.addHandler(handler)


def _coerce_bool(value, default=True, *, name="value") -> bool:
    """Interpret an env-var string or a JSON config value as a bool.

    Accepts the conventional spellings, case-insensitively and whitespace-stripped:
    true/false, 1/0, yes/no, y/n, t/f, on/off. `None` and an empty/whitespace-only
    string mean "not set" and yield `default` — an empty environment variable is
    how every shell spells "unset", and disabling TLS verification must be
    deliberate. Real bools pass through. `0`/`1` as integers are accepted.

    Raises:
        ValueError: for anything else, naming `name` and the accepted spellings.
            The old implementation compared against the single literal "false" and
            discarded any non-str/non-bool, so "0", "no", "off", "false " (a
            hand-edited .env leaves the trailing space) and the integer 0 all came
            back True — OPENSEARCH_VERIFY_SSL=0 silently did not disable
            verification. Guessing is what made that a silent foot-gun, so
            ambiguity is now refused rather than resolved to the default.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        text = value.strip().lower()
        if not text:
            return default
        if text in _TRUE_STRINGS:
            return True
        if text in _FALSE_STRINGS:
            return False
    elif isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise ValueError(
        f"Cannot interpret {name}={value!r} as a boolean. Use one of "
        "true/false, 1/0, yes/no, y/n, t/f, on/off (case-insensitive), "
        "or leave it unset."
    )


def init_client() -> OpenSearchClient:
    """Initialise OpenSearchClient from env vars or config file.

    Env vars (priority over config file). This list is the complete set the code
    reads — keep it that way; an undocumented knob is one nobody can turn:
        OPENSEARCH_DASHBOARDS_URL        — e.g. https://opensearch.example.com
        OPENSEARCH_URL                   — e.g. https://opensearch.example.com:9200 (fallback)
        OPENSEARCH_USERNAME
        OPENSEARCH_PASSWORD
        OPENSEARCH_VERIFY_SSL            — true/false, 1/0, yes/no, on/off
                                           (default: true). Anything else is an
                                           error rather than a silent default.
        OPENSEARCH_TIMEOUT               — total wall-clock budget in seconds for
                                           ONE call, retries included (default: 60)
        OPENSEARCH_MAX_SEARCH_LIMIT      — default cap on search results (default: 200)
        OPENSEARCH_MAX_HISTOGRAM_BUCKETS — default cap on histogram buckets (default: 2000)
        OPENSEARCH_MAX_TERMS_SIZE        — default cap on a terms agg size (default: 1000)
        OPENSEARCH_MAX_AGGREGATIONS      — default cap on multi_terms agg count (default: 20)
        OPENSEARCH_LOG_LEVEL             — DEBUG/INFO/WARNING/ERROR/CRITICAL for
                                           this package's logger, to stderr
                                           (default: inherit whatever the host
                                           process configured). INFO shows which
                                           backend won and why the other lost;
                                           DEBUG adds per-request latency.
        OPENSEARCH_ALLOW_INSECURE_CONFIG — true downgrades the 0600 permission
                                           check on config.json AND on both .env
                                           paths from a hard PermissionError to a
                                           logged warning

    Credential files read, in order (the first definition of a key wins, and a
    real environment variable always beats all of them). All three are refused
    unless they are 0600, see OPENSEARCH_ALLOW_INSECURE_CONFIG:
        <checkout>/.env
        ~/.config/mcp-opensearch/.env
        ~/.config/mcp-opensearch/config.json
    """
    _load_env_files()
    _configure_logging()

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
        _coerce_bool(env_ssl, name="OPENSEARCH_VERIFY_SSL") if env_ssl is not None
        else _coerce_bool(config.get("verify_ssl", True), name="verify_ssl (config.json)")
    )
    timeout = int(os.environ.get("OPENSEARCH_TIMEOUT", config.get("timeout", DEFAULT_TIMEOUT)))
    max_search_limit = int(
        os.environ.get("OPENSEARCH_MAX_SEARCH_LIMIT", config.get("max_search_limit", MAX_SEARCH_LIMIT))
    )
    max_histogram_buckets = int(
        os.environ.get("OPENSEARCH_MAX_HISTOGRAM_BUCKETS", config.get("max_histogram_buckets", MAX_HISTOGRAM_BUCKETS))
    )
    max_terms_size = int(
        os.environ.get("OPENSEARCH_MAX_TERMS_SIZE", config.get("max_terms_size", MAX_TERMS_SIZE))
    )
    max_aggregations = int(
        os.environ.get("OPENSEARCH_MAX_AGGREGATIONS", config.get("max_aggregations", MAX_AGGREGATIONS))
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
        max_terms_size=max_terms_size,
        max_aggregations=max_aggregations,
    )
    # Warm the connection at startup so the first tool call doesn't pay the
    # backend probe cost (~1.3s). Also surfaces config errors immediately.
    client._resolve_backend()
    logger.info(
        "OpenSearch client ready (backend=%s, dashboards=%s, direct=%s)",
        client.backend,
        dashboards_url or "none",
        opensearch_url or "none",
    )
    return client
