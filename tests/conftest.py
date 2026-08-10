"""Shared fixtures for the mcp-opensearch unit suite.

Design rules enforced here:

1. **No unit test touches the network.** `_no_network` is autouse and replaces
   the lowest urllib3/requests seam (`HTTPAdapter.send`) with a hard failure, so
   an accidental real call fails loudly instead of hanging on a timeout.
2. **Stub at the `_post` / `_get` seam, not below it.** Everything under those
   two methods (transport, retries, HTTP status mapping) is infrastructure and
   belongs to integration tests. Everything above them is result-shaping logic,
   which is what this suite covers.
3. **Nothing simple is mocked.** `OpenSearchClient` is constructed for real with
   a fake URL; the query dicts, timestamps and OpenSearch response payloads are
   plain literals. Per doc 05 §7, only the slow/external dependency is doubled.
"""

import pytest
import requests
from requests.adapters import HTTPAdapter

from lib.client import BACKEND_DASHBOARDS, BACKEND_OPENSEARCH, OpenSearchClient

FAKE_OS_URL = "http://opensearch.invalid:9200"
FAKE_DASH_URL = "http://dashboards.invalid:5601"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Make any real HTTP attempt an immediate, obvious test failure."""

    def _boom(self, request, *args, **kwargs):  # pragma: no cover - guard only
        raise AssertionError(
            f"unit test attempted a real network call: {request.method} {request.url}"
        )

    monkeypatch.setattr(HTTPAdapter, "send", _boom)
    # Belt and braces: some code paths build a bare Session.
    monkeypatch.setattr(
        requests.Session,
        "request",
        lambda self, method, url, *a, **kw: _boom(
            self, type("R", (), {"method": method, "url": url})()
        ),
    )


class SeamRecorder:
    """Callable stand-in for `_post` / `_get`.

    Records every call and replays canned responses. When more responses are fed
    than there are calls, they are consumed in order; the last one repeats.
    """

    def __init__(self, responses):
        if not responses:
            responses = [{}]
        self._responses = list(responses)
        self.calls = []

    def __call__(self, path, body=None, params=None):
        self.calls.append({"path": path, "body": body, "params": params})
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]

    # ── convenience accessors for the most recent call ──
    @property
    def path(self):
        return self.calls[-1]["path"]

    @property
    def body(self):
        return self.calls[-1]["body"]

    @property
    def params(self):
        return self.calls[-1]["params"]

    @property
    def query(self):
        """The `query` clause of the last POSTed search body."""
        return self.calls[-1]["body"]["query"]

    @property
    def call_count(self):
        return len(self.calls)


def make_client(**overrides):
    """A client wired to a fake URL with the backend already resolved.

    Pre-setting `backend` short-circuits `_resolve_backend()`, which is the only
    thing in the read path that would otherwise probe the cluster.
    """
    kwargs = {
        "opensearch_url": FAKE_OS_URL,
        "username": "tester",
        "password": "secret",
        "verify_ssl": False,
    }
    backend = overrides.pop("backend", BACKEND_OPENSEARCH)
    kwargs.update(overrides)
    client = OpenSearchClient(**kwargs)
    client.backend = backend
    client.server_version = "2.11.0"
    return client


@pytest.fixture
def client():
    return make_client()


@pytest.fixture
def dashboards_client():
    return make_client(dashboards_url=FAKE_DASH_URL, backend=BACKEND_DASHBOARDS)


@pytest.fixture
def stub(client, monkeypatch):
    """Install a SeamRecorder over `client._post` (or `_get`) and return it.

    Usage:
        rec = stub({"hits": {...}})            # stubs _post
        rec = stub({"alerts": []}, on="_get")  # stubs _get
    """

    def _install(*responses, on="_post"):
        recorder = SeamRecorder(responses)
        monkeypatch.setattr(client, on, recorder)
        return recorder

    return _install


# ── Canned OpenSearch payload builders ────────────────────────────────────────


def search_response(hits=(), total=None, total_as_int=False):
    """A minimal `_search` response.

    `total_as_int` produces the pre-7.0 flat integer shape, which is the other
    half of the dict-vs-int normalisation partition in `search_string`.
    """
    sources = [{"_source": h} for h in hits]
    if total is None:
        total = len(sources)
    total_value = total if total_as_int else {"value": total, "relation": "eq"}
    return {"hits": {"total": total_value, "hits": sources}}


def terms_response(name, buckets):
    """A terms-aggregation response: buckets given as [(key, doc_count), ...]."""
    return {
        "aggregations": {
            name: {
                "doc_count_error_upper_bound": 0,
                "sum_other_doc_count": 0,
                "buckets": [{"key": k, "doc_count": c} for k, c in buckets],
            }
        }
    }
