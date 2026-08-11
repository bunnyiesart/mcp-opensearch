"""Resilience and operability: what the client says and does when things break.

The theme of every test here is a single question — **when something goes wrong,
does this client tell the operator what and why?** A tool that fails with a
misleading message is worse than one that fails opaquely, because an LLM agent
acts on the message: "check OPENSEARCH_URL" sends it to a variable the user never
set, and `{"ok": true}` from a liveness check sends it looking for a privilege
problem that does not exist.

Seam: these tests stub `requests.Session` itself (the new `session=` constructor
parameter), one level below the `_get`/`_post` seam the rest of the suite uses,
because retry policy, time budget, status translation and backend resolution all
live *between* the public methods and the socket. Nothing here touches the
network — the autouse `_no_network` guard in conftest still applies, and
`FakeSession` is not a `requests.Session` at all.

Covers backlog #1, #4, #5, #6, #12, #16, #21.
"""

import logging
import os
import pathlib
import re
import stat

import pytest
import requests

import lib.client as lc
from lib.client import (
    BACKEND_DASHBOARDS,
    BACKEND_OPENSEARCH,
    OpenSearchClient,
    TransportError,
)

DASH = "http://dashboards.invalid:5601"
DIRECT = "http://opensearch.invalid:9200"

_NO_JSON = object()


class FakeResponse:
    """Just enough of `requests.Response` for the transport layer."""

    def __init__(self, status_code=200, payload=None, headers=None, url="http://fake.invalid/x"):
        self.status_code = status_code
        self._payload = {} if payload is None else payload
        self.headers = headers or {}
        self.url = url

    def json(self):
        if self._payload is _NO_JSON:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


def ok(payload=None, **kw):
    return FakeResponse(200, payload if payload is not None else {"version": {"number": "2.11.0"}}, **kw)


def status(code, payload=None, **kw):
    return FakeResponse(code, payload, **kw)


class FakeSession:
    """Records every call and replays whatever `handler` decides.

    `handler(method, url, kwargs, call_number)` returns a FakeResponse, or an
    Exception instance to be raised — which is how a connection reset, a TLS
    rejection or a urllib3 RetryError is expressed without a socket.
    """

    def __init__(self, handler):
        self.handler = handler
        self.headers = {}
        self.auth = None
        self.calls = []

    def get(self, url, **kwargs):
        return self._send("GET", url, kwargs)

    def post(self, url, **kwargs):
        return self._send("POST", url, kwargs)

    def _send(self, method, url, kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        result = self.handler(method, url, kwargs, len(self.calls))
        if isinstance(result, Exception):
            raise result
        return result

    # ── accessors ──
    @property
    def urls(self):
        return [c["url"] for c in self.calls]

    def count(self, fragment):
        return sum(1 for u in self.urls if fragment in u)


def always(result):
    """A handler that answers every request the same way."""
    return lambda method, url, kwargs, n: result


def by_url(default, **routes):
    """A handler keyed on a fragment of the URL. `routes` values may be callables."""

    def handler(method, url, kwargs, n):
        for fragment, result in routes.items():
            if fragment.replace("_", "/") in url or fragment in url:
                return result(n) if callable(result) else result
        return default(n) if callable(default) else default

    return handler


def make(handler, **kwargs):
    """A client whose transport is a FakeSession, with the backend already resolved."""
    resolved = kwargs.pop("resolved", BACKEND_OPENSEARCH)
    session = FakeSession(handler)
    kwargs.setdefault("opensearch_url", DIRECT)
    kwargs.setdefault("timeout", 60)
    client = OpenSearchClient(session=session, **kwargs)
    if resolved:
        client.backend = resolved
        client.server_version = "2.11.0"
    return client, session


class FakeClock:
    """A monotonic clock the test drives, so a time budget is testable in 0 ms."""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(lc.time, "monotonic", c.monotonic)
    monkeypatch.setattr(lc.time, "sleep", c.sleep)
    return c


@pytest.fixture(autouse=True)
def _never_really_sleep(monkeypatch):
    """Retry backoff must not make the unit suite slow. Records instead of sleeping."""
    slept = []
    monkeypatch.setattr(lc.time, "sleep", slept.append)
    return slept


# ── #1 test_connection reported {"ok": true} on a dead cluster ────────────────


def test_test_connection_issues_a_request_every_time():
    """The whole defect in one assertion: it used to do zero I/O after the first call.

    `_resolve_backend` returned immediately once `self.backend` was set, and
    `test_connection` did nothing else, so the tool whose docstring says "call
    first in every session to confirm connectivity" was answering from memory.
    """
    client, session = make(always(ok()))
    client.test_connection()
    client.test_connection()
    client.test_connection()
    assert session.count("/") == 3


def test_test_connection_fails_once_the_cluster_is_dead():
    """Probe once successfully, then let everything 503. The old code still said ok."""
    client, _session = make(by_url(lambda n: ok() if n == 1 else status(503)))
    assert client.test_connection()["ok"] is True
    with pytest.raises(RuntimeError) as exc:
        client.test_connection()
    assert "503" in str(exc.value)
    assert DIRECT in str(exc.value)


def test_test_connection_agrees_with_the_other_tools():
    """The trust property: a green test_connection must not coexist with a broken
    cluster_health, because the agent uses the first to interpret the second."""
    client, _ = make(always(status(503)))
    with pytest.raises(RuntimeError):
        client.cluster_health()
    with pytest.raises(RuntimeError):
        client.test_connection()


def test_test_connection_still_reports_backend_version_url_and_username():
    client, _ = make(always(ok()), username="tester", password="secret")
    result = client.test_connection()
    assert result["backend"] == BACKEND_OPENSEARCH
    assert result["version"] == "2.11.0"
    assert result["url"] == DIRECT
    assert result["username"] == "tester"


def test_test_connection_reruns_the_preference_order_so_it_is_the_failover_trigger():
    """A client demoted to direct returns to Dashboards when Dashboards is healthy."""
    client, session = make(
        by_url(ok()), dashboards_url=DASH, resolved=BACKEND_OPENSEARCH
    )
    assert client.test_connection()["backend"] == BACKEND_DASHBOARDS
    assert session.count("/api/status") == 1


# ── #4 retry exhaustion, transport failures, 429, and the time budget ─────────


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (requests.exceptions.RetryError("too many 503 error responses"), "retries exhausted"),
        (requests.exceptions.ConnectionError("Connection refused"), "unreachable"),
        (requests.exceptions.ReadTimeout("timed out"), "did not answer in time"),
        (requests.exceptions.ConnectTimeout("connect timed out"), "connection timed out"),
        (requests.exceptions.SSLError("certificate verify failed"), "TLS verification failed"),
    ],
)
def test_every_transport_failure_reaches_the_caller_translated(exc, expected):
    """None of these is an HTTPError, so none of them ever reached _raise_for_status.

    A `RetryError: HTTPConnectionPool(...) (Caused by ResponseError('too many 503
    error responses'))` names neither the backend, nor the operation, nor a next
    step — and it is the single most likely failure against an overloaded cluster.
    """
    client, _ = make(always(exc))
    with pytest.raises(TransportError) as raised:
        client.cluster_health()
    message = str(raised.value)
    assert expected in message
    assert "/_cluster/health" in message          # which operation
    assert "budget" in message                    # and how long it was given


def test_translated_transport_errors_are_runtime_errors():
    """Every tool and every caller already treats RuntimeError as "actionable
    failure with a readable message". A parallel exception family would need
    handling everywhere, so TransportError joins this one."""
    assert issubclass(TransportError, RuntimeError)
    client, _ = make(always(requests.exceptions.ConnectionError("boom")))
    with pytest.raises(RuntimeError):
        client.cluster_health()


def test_the_original_cause_survives_translation():
    original = requests.exceptions.ConnectionError("[Errno 61] Connection refused")
    client, _ = make(always(original))
    with pytest.raises(TransportError) as raised:
        client.cluster_health()
    assert raised.value.__cause__ is original
    assert "Connection refused" in str(raised.value)


def test_a_tls_failure_names_the_verify_ssl_switch_and_is_not_retried():
    """A rejected certificate is deterministic: retrying it wastes the budget and
    tells the operator nothing new."""
    client, session = make(always(requests.exceptions.SSLError("certificate verify failed")))
    with pytest.raises(TransportError) as raised:
        client.cluster_health()
    assert "OPENSEARCH_VERIFY_SSL" in str(raised.value)
    assert len(session.calls) == 1


def test_a_read_timeout_is_not_retried():
    """The cluster is still executing the query. A retry doubles the load it is
    already failing to carry, and the first attempt had the whole budget."""
    client, session = make(always(requests.exceptions.ReadTimeout("timed out")))
    with pytest.raises(TransportError):
        client.cluster_health()
    assert len(session.calls) == 1


def test_a_connect_failure_is_retried_and_can_succeed():
    """Retrying is still correct for these idempotent reads — it costs the cluster
    nothing when the connection never landed."""
    client, session = make(
        by_url(lambda n: requests.exceptions.ConnectionError("refused") if n < 3 else ok({"status": "green"}))
    )
    assert client.cluster_health() == {"status": "green"}
    assert len(session.calls) == 3


@pytest.mark.parametrize("code", [502, 503, 504])
def test_5xx_is_retried_then_translated_with_a_hint(code):
    client, session = make(always(status(code)))
    with pytest.raises(RuntimeError) as raised:
        client.cluster_health()
    assert len(session.calls) == lc._MAX_ATTEMPTS
    assert "after retries" in str(raised.value)


def test_429_is_not_retried_and_says_what_to_do_instead():
    """429 is what a large terms aggregation provokes (es_rejected_execution_exception
    or a tripped circuit breaker). It used to surface as a bare "HTTP 429", which
    tells an agent nothing — so it retries the same oversized query. The message
    now names the remedy, and the client does not repeat the request itself."""
    client, session = make(always(status(429, {"error": {"type": "es_rejected_execution_exception"}})))
    with pytest.raises(RuntimeError) as raised:
        client.terms("idx", "agent.name", size=1000)
    message = str(raised.value)
    assert len(session.calls) == 1
    assert "429" in message
    assert "narrow the time range" in message
    assert "size" in message
    assert message != "HTTP 429: POST /idx/_search."


def test_the_cluster_s_own_explanation_is_included():
    """A 400 saying "unknown field [foo]" is the most useful thing in the response
    and it was thrown away, leaving only "check your query syntax or field names"."""
    client, _ = make(always(status(400, {"error": {"reason": "unknown field [agent.nmae]"}})))
    with pytest.raises(RuntimeError) as raised:
        client.cluster_health()
    assert "unknown field [agent.nmae]" in str(raised.value)


def test_the_bad_request_prefix_survives_because_stats_keys_off_it():
    """stats() turns "Bad request" into field-type advice. Changing the prefix would
    silently break that translation, so it is pinned here."""
    client, _ = make(always(status(400)))
    with pytest.raises(RuntimeError, match=r"^Bad request"):
        client.cluster_health()


def test_no_retry_when_the_budget_cannot_pay_for_one():
    """With a 2 s budget there is no room for a backoff plus a worthwhile attempt,
    so the client answers immediately instead of pretending it can retry."""
    client, session = make(always(status(503)), timeout=2)
    with pytest.raises(RuntimeError):
        client.cluster_health()
    assert len(session.calls) == 1


def test_retries_never_exceed_the_wall_clock_budget(clock):
    """The bound, measured on a clock the test controls.

    Before: `Retry(total=3, backoff_factor=1)` multiplied the *per-request* timeout
    by four and added 6 s of backoff, so a hung cluster held one tool call for
    4 x 60 s + 6 s ~= 246 s — past any MCP client's tool timeout, and spent
    hammering a cluster that was already shedding load.
    """
    budget = 30

    def handler(method, url, kwargs, n):
        # A hung endpoint: the attempt consumes exactly the timeout it was handed,
        # which is what requests does, and is the reason the deadline holds.
        connect_timeout, _read_timeout = kwargs["timeout"]
        clock.now += connect_timeout
        return requests.exceptions.ConnectTimeout("connect timed out")

    client, session = make(handler, timeout=budget)
    start = clock.now
    with pytest.raises(TransportError):
        client.cluster_health()
    elapsed = clock.now - start
    # Tolerance is one minimum attempt: an attempt already in flight is not
    # aborted mid-connection. The old behaviour had no bound at all — four times
    # the configured timeout plus 6 s of backoff, i.e. 126 s for this budget.
    assert elapsed <= budget + lc._MIN_ATTEMPT_SECONDS, f"one call took {elapsed}s of {budget}s"
    assert len(session.calls) == lc._MAX_ATTEMPTS


def test_the_per_attempt_timeout_is_what_is_left_of_the_budget(clock):
    """A slow-but-legitimate query still gets the whole budget on its first attempt —
    the fix must not turn "slow" into "impossible" by slicing the timeout."""
    client, session = make(always(ok()), timeout=45)
    client.cluster_health()
    connect_timeout, read_timeout = session.calls[0]["timeout"]
    assert read_timeout == 45
    assert connect_timeout == lc._CONNECT_TIMEOUT_SECONDS


def test_backoff_grows_and_is_capped(_never_really_sleep):
    client, _ = make(always(status(503)), timeout=60)
    with pytest.raises(RuntimeError):
        client.cluster_health()
    assert _never_really_sleep == [0.5, 1.0]
    assert all(s <= lc._BACKOFF_MAX_SECONDS for s in _never_really_sleep)


# ── #5 the resolution error blamed the URL for auth and TLS failures ──────────


def test_resolution_error_names_the_real_cause_per_backend():
    """Before: "Could not connect ... Check OPENSEARCH_DASHBOARDS_URL /
    OPENSEARCH_URL." for a wrong password, with HTTP 401 visible only in a
    suppressed logger.error."""
    client, _ = make(
        always(status(401)), dashboards_url=DASH, username="u", password="wrong", resolved=None
    )
    with pytest.raises(RuntimeError) as raised:
        client._resolve_backend()
    message = str(raised.value)
    assert message.count("authentication failed (HTTP 401)") == 2
    assert DASH in message and DIRECT in message
    assert "OPENSEARCH_USERNAME" in message


def test_resolution_error_mentions_only_the_variables_that_were_tried():
    """The sharpest edge of this defect: an operator who configured only
    OPENSEARCH_DASHBOARDS_URL was told to go and check OPENSEARCH_URL."""
    client, _ = make(
        always(status(401)), dashboards_url=DASH, opensearch_url=None, resolved=None
    )
    with pytest.raises(RuntimeError) as raised:
        client._resolve_backend()
    message = str(raised.value)
    assert "OPENSEARCH_URL is not set" in message
    assert "Check OPENSEARCH_DASHBOARDS_URL / OPENSEARCH_URL" not in message


def test_resolution_error_distinguishes_tls_from_unreachable():
    def handler(method, url, kwargs, n):
        if "/api/status" in url:
            return requests.exceptions.SSLError("certificate verify failed: self signed certificate")
        return requests.exceptions.ConnectionError("[Errno 61] Connection refused")

    client, _ = make(handler, dashboards_url=DASH, resolved=None)
    with pytest.raises(RuntimeError) as raised:
        client._resolve_backend()
    message = str(raised.value)
    assert "TLS verification failed" in message
    assert "unreachable" in message


def test_resolution_error_recognises_an_sso_login_page():
    """A 200 carrying HTML is the commonest managed-SOC failure and used to be
    reported as "unreachable" via the JSON decode error."""
    client, _ = make(
        always(ok(_NO_JSON, url=f"{DASH}/login?next=%2Fapi%2Fstatus")),
        dashboards_url=DASH,
        opensearch_url=None,
        resolved=None,
    )
    with pytest.raises(RuntimeError) as raised:
        client._resolve_backend()
    assert "non-JSON" in str(raised.value)
    assert "SSO" in str(raised.value)


def test_resolution_error_reports_a_403_and_a_redirect_as_themselves():
    def handler(method, url, kwargs, n):
        if "/api/status" in url:
            return status(302, headers={"Location": "https://sso.example.com/login"})
        return status(403)

    client, _ = make(handler, dashboards_url=DASH, resolved=None)
    with pytest.raises(RuntimeError) as raised:
        client._resolve_backend()
    message = str(raised.value)
    assert "sso.example.com" in message
    assert "HTTP 403" in message


def test_no_backend_configured_says_so_rather_than_blaming_a_url():
    client = OpenSearchClient(session=FakeSession(always(ok())))
    with pytest.raises(RuntimeError, match="No OpenSearch backend is configured"):
        client._resolve_backend()


# ── #6 one blip demoted the backend for the whole process ────────────────────


def test_a_dashboards_401_no_longer_demotes_the_session_forever():
    """MCP servers are long-lived. One 401 at first-call time used to cost every
    later call in the session, recoverable only by restarting the server."""
    state = {"dashboards_ok": False}

    def handler(method, url, kwargs, n):
        if "/api/status" in url:
            return ok() if state["dashboards_ok"] else status(401)
        if "saved_objects" in url:
            return ok({"saved_objects": [{"id": "p1", "attributes": {"title": "wazuh-*"}}]})
        return ok({"status": "green"})

    client, _ = make(handler, dashboards_url=DASH, resolved=None)
    client._resolve_backend()
    assert client.backend == BACKEND_OPENSEARCH  # degraded, correctly
    state["dashboards_ok"] = True
    assert client.list_index_patterns()[0]["title"] == "wazuh-*"
    assert client.backend == BACKEND_DASHBOARDS


def test_list_index_patterns_says_why_dashboards_is_not_active():
    """It used to raise "Set OPENSEARCH_DASHBOARDS_URL to enable it" while it was
    set — and this tool is the README's documented fallback for a 403 on
    _cat/indices, so the lie lands mid-incident."""
    client, _ = make(
        by_url(ok({"status": "green"}), **{"/api/status": status(403)}),
        dashboards_url=DASH,
        resolved=None,
    )
    client._resolve_backend()
    with pytest.raises(RuntimeError) as raised:
        client.list_index_patterns()
    message = str(raised.value)
    assert "OPENSEARCH_DASHBOARDS_URL is set" in message
    assert DASH in message
    assert "HTTP 403" in message
    assert "Set OPENSEARCH_DASHBOARDS_URL to enable it" not in message


def test_list_index_patterns_message_when_dashboards_really_is_unconfigured():
    client, _ = make(always(ok()))
    with pytest.raises(RuntimeError) as raised:
        client.list_index_patterns()
    assert "OPENSEARCH_DASHBOARDS_URL is not set" in str(raised.value)


def test_the_preferred_backend_is_not_reprobed_on_every_request():
    """Re-probing per request would add a round trip to everything, which is the
    reason the cache existed. The policy keeps the steady state free."""
    client, session = make(always(ok({"status": "green"})), dashboards_url=DASH, resolved=None)
    client._resolve_backend()
    probes = session.count("/api/status")
    for _ in range(5):
        client.cluster_health()
    assert session.count("/api/status") == probes


def test_a_degraded_resolution_is_reprobed_once_the_recheck_window_passes(clock):
    state = {"dashboards_ok": False}

    def handler(method, url, kwargs, n):
        if "/api/status" in url:
            return ok() if state["dashboards_ok"] else status(503)
        return ok({"status": "green"})

    client, _ = make(handler, dashboards_url=DASH, resolved=None, backend_recheck_seconds=300)
    client._resolve_backend()
    assert client.backend == BACKEND_OPENSEARCH
    state["dashboards_ok"] = True
    client.cluster_health()
    assert client.backend == BACKEND_OPENSEARCH, "must not re-probe inside the window"
    clock.now += 301
    client.cluster_health()
    assert client.backend == BACKEND_DASHBOARDS


def test_a_dead_backend_is_dropped_so_the_next_call_fails_over():
    """The reverse direction of the same defect: Dashboards wins the probe, then
    dies, and the client used to keep routing to it forever."""
    def handler(method, url, kwargs, n):
        if "console/proxy" in url or "/api/status" in url:
            return requests.exceptions.ConnectionError("[Errno 61] Connection refused")
        return ok({"status": "green"})

    client, _ = make(handler, dashboards_url=DASH, resolved=BACKEND_DASHBOARDS)
    with pytest.raises(TransportError):
        client.cluster_health()
    assert client.backend is None
    assert client.cluster_health() == {"status": "green"}
    assert client.backend == BACKEND_OPENSEARCH


def test_a_status_failure_does_not_drop_the_backend():
    """A 403 or a 400 is about the request or the account, not the transport.
    Forgetting the backend there would add a probe to every ordinary error."""
    client, _ = make(always(status(403)))
    with pytest.raises(RuntimeError):
        client.cluster_health()
    assert client.backend == BACKEND_OPENSEARCH


# ── #16 every proxied GET carried a {} body ───────────────────────────────────


def test_a_proxied_get_sends_no_body():
    """`json=body or {}` put `Content-Type: application/json` and two bytes of body
    on every proxied GET, which the direct path never does. OpenSearch rejects a
    body on handlers that declare none, so cluster_health, list_indices,
    get_mapping, index_settings and get_alerts were structurally at risk of a 400
    that cannot happen on the direct path."""
    client, session = make(
        always(ok({"status": "green"})), dashboards_url=DASH, resolved=BACKEND_DASHBOARDS
    )
    client.cluster_health()
    assert session.calls[0]["json"] is None


def test_a_proxied_post_still_sends_its_body():
    client, session = make(
        always(ok({"count": 3})), dashboards_url=DASH, resolved=BACKEND_DASHBOARDS
    )
    client.count("idx", "*")
    assert "query" in session.calls[0]["json"]


def test_the_proxied_path_and_method_still_travel_as_parameters():
    client, session = make(
        always(ok({"status": "green"})), dashboards_url=DASH, resolved=BACKEND_DASHBOARDS
    )
    client.cluster_health()
    assert session.calls[0]["params"] == {"path": "_cluster/health", "method": "GET"}


def test_a_non_json_response_is_reported_as_such():
    """An HTML proxy error page used to escape as a raw JSONDecodeError."""
    client, _ = make(always(ok(_NO_JSON)))
    with pytest.raises(RuntimeError, match="Non-JSON response"):
        client.cluster_health()


# ── #21 the only instrument was write-only, and the log lines were suppressed ──


def test_latency_is_recorded_for_a_failed_call():
    """last_query_ms was assigned *after* _raise_for_status, so the failures worth
    measuring recorded nothing at all."""
    client, _ = make(always(status(503)))
    client.last_query_ms = -1
    with pytest.raises(RuntimeError):
        client.cluster_health()
    assert client.last_query_ms >= 0


def test_latency_is_surfaced_where_a_caller_can_read_it():
    """A write-only metric is worse than none: it looks like instrumentation."""
    client, _ = make(always(ok()))
    assert "latency_ms" in client.test_connection()


def test_the_log_level_env_var_lifts_this_package_out_of_basicConfig_warning(monkeypatch):
    """server.py calls logging.basicConfig(level=WARNING), which silences the only
    lines naming the active backend. Setting the level on this package's own logger
    is enough, because propagated records are gated by the originating logger."""
    package_logger = logging.getLogger("lib")
    monkeypatch.setattr(package_logger, "level", logging.NOTSET)
    monkeypatch.setenv("OPENSEARCH_LOG_LEVEL", "debug")
    lc._configure_logging()
    assert package_logger.level == logging.DEBUG


def test_an_invalid_log_level_is_rejected_rather_than_ignored(monkeypatch):
    monkeypatch.setenv("OPENSEARCH_LOG_LEVEL", "verbose")
    with pytest.raises(ValueError, match="OPENSEARCH_LOG_LEVEL"):
        lc._configure_logging()


def test_no_log_level_set_changes_nothing(monkeypatch):
    monkeypatch.delenv("OPENSEARCH_LOG_LEVEL", raising=False)
    before = logging.getLogger("lib").level
    lc._configure_logging()
    assert logging.getLogger("lib").level == before


def test_which_backend_won_is_logged_at_info(caplog):
    client, _ = make(always(ok()), resolved=None)
    with caplog.at_level(logging.INFO, logger="lib.client"):
        client._resolve_backend()
    assert any("Backend:" in r.message for r in caplog.records)


def test_why_a_backend_lost_is_logged_at_warning(caplog):
    client, _ = make(by_url(ok(), **{"/api/status": status(401)}), dashboards_url=DASH, resolved=None)
    with caplog.at_level(logging.WARNING, logger="lib.client"):
        client._resolve_backend()
    assert "authentication failed (HTTP 401)" in caplog.text


def test_init_client_documents_every_env_var_the_module_reads():
    """init_client's docstring claims to be the complete set of variables the code
    reads. That claim is only worth having if it is checked."""
    source = pathlib.Path(lc.__file__).read_text()
    read = set(re.findall(r'os\.environ\.get\(\s*"(OPENSEARCH_[A-Z_]+)"', source))
    documented = set(re.findall(r"(OPENSEARCH_[A-Z_]+)", lc.init_client.__doc__))
    assert read, "no env vars found — the regex has drifted from the code"
    assert read <= documented, f"undocumented env vars: {sorted(read - documented)}"


# ── #12 the .env path had no permission check and was the wrong path ─────────


@pytest.fixture
def env_files(tmp_path, monkeypatch):
    """Redirect both .env paths into a tmp dir and keep os.environ clean."""
    monkeypatch.setattr(os, "environ", dict(os.environ))
    project = tmp_path / "checkout" / ".env"
    documented = tmp_path / "config" / ".env"
    project.parent.mkdir(parents=True)
    documented.parent.mkdir(parents=True)
    monkeypatch.setattr(lc, "PROJECT_ENV_FILE", str(project))
    monkeypatch.setattr(lc, "ENV_FILE", str(documented))
    return project, documented


def write(path, text, mode=0o600):
    path.write_text(text)
    path.chmod(mode)
    return path


def test_the_documented_env_path_is_actually_read(env_files):
    """`load_dotenv()` with no argument resolves through find_dotenv(), which walks
    up from the directory of lib/client.py — so ~/.config/mcp-opensearch/.env, the
    file setup.sh writes and the README documents, was never read at all."""
    _, documented = env_files
    write(documented, "OPENSEARCH_MARKER_DOC=yes\n")
    assert lc._load_env_files() == [str(documented)]
    assert os.environ["OPENSEARCH_MARKER_DOC"] == "yes"


def test_a_world_readable_env_file_is_refused_like_config_json(env_files):
    """config.json was hard-gated at 0600 while the file beside it — same directory,
    same password — had no check at all."""
    _, documented = env_files
    write(documented, "OPENSEARCH_PASSWORD=hunter2\n", mode=0o644)
    with pytest.raises(PermissionError) as raised:
        lc._load_env_files()
    assert "chmod 600" in str(raised.value)
    assert "OPENSEARCH_PASSWORD" not in str(raised.value)  # never echo the contents


def test_the_documented_escape_hatch_downgrades_it_to_a_warning(env_files, caplog, monkeypatch):
    _, documented = env_files
    write(documented, "OPENSEARCH_MARKER_INSECURE=yes\n", mode=0o644)
    monkeypatch.setitem(os.environ, "OPENSEARCH_ALLOW_INSECURE_CONFIG", "true")
    with caplog.at_level(logging.WARNING, logger="lib.client"):
        assert lc._load_env_files() == [str(documented)]
    assert any("readable by other users" in r.message for r in caplog.records)


def test_a_project_local_env_is_loaded_but_not_silently(env_files):
    """A .env beside the source is useful for development, which is why the old
    code found it — but it was read whatever its mode, and read *instead of* the
    documented one."""
    project, documented = env_files
    write(project, "OPENSEARCH_MARKER_PROJECT=yes\n")
    write(documented, "OPENSEARCH_MARKER_DOC2=yes\n")
    assert lc._load_env_files() == [str(project), str(documented)]
    assert os.environ["OPENSEARCH_MARKER_PROJECT"] == "yes"
    assert os.environ["OPENSEARCH_MARKER_DOC2"] == "yes"


def test_a_project_local_env_is_permission_checked_too(env_files):
    project, _ = env_files
    write(project, "OPENSEARCH_PASSWORD=hunter2\n", mode=0o604)
    with pytest.raises(PermissionError):
        lc._load_env_files()


def test_a_real_environment_variable_still_wins_over_a_file(env_files):
    _, documented = env_files
    write(documented, "OPENSEARCH_MARKER_PRECEDENCE=from-file\n")
    os.environ["OPENSEARCH_MARKER_PRECEDENCE"] = "from-env"
    lc._load_env_files()
    assert os.environ["OPENSEARCH_MARKER_PRECEDENCE"] == "from-env"


def test_missing_env_files_are_simply_absent(env_files):
    assert lc._load_env_files() == []


def test_config_json_keeps_its_own_permission_gate(tmp_path, monkeypatch):
    """The check moved into a shared helper; the behaviour it guaranteed must not."""
    config = tmp_path / "config.json"
    config.write_text('{"opensearch_url": "http://x:9200"}')
    config.chmod(0o644)
    monkeypatch.setattr(lc, "CONFIG_FILE", str(config))
    with pytest.raises(PermissionError, match=stat.filemode(0o100644)):
        lc._load_config()
    config.chmod(0o600)
    assert lc._load_config() == {"opensearch_url": "http://x:9200"}
