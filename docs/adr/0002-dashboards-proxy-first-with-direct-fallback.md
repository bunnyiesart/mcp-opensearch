# 0002. Dashboards proxy first, direct OpenSearch as fallback

**Status:** Accepted

Recorded retroactively. The behaviour is load-bearing and non-obvious, and it is
the single most likely thing for a future contributor to "simplify" without
understanding why it exists.

## Context

An analyst's credentials in a managed SOC environment frequently grant access to
**OpenSearch Dashboards** but not to the OpenSearch REST API on port 9200 — the
port is firewalled, or the role has no direct cluster privileges. Dashboards
exposes `/api/console/proxy`, the same endpoint its Dev Tools console uses, which
forwards a request to the cluster under the caller's session. That is the only
route into the data for a large class of real users.

Conversely, some deployments expose the cluster directly and run no Dashboards at
all, or run a version whose saved-objects API differs.

The server therefore has to work against either backend without the caller
knowing which one they have, and without a configuration flag they would have to
get right on the first try.

## Alternatives

### Option A — Require the caller to declare the backend

| Pros | Cons |
|---|---|
| Explicit; no probing, no ambiguity in error messages | Pushes a diagnosis onto the user that the server can make itself — and getting it wrong yields an opaque failure at first use (usability, supportability) |
| Trivially testable | Two config shapes to document and two ways to hold it wrong |

### Option B — Probe Dashboards, fall back to direct

| Pros | Cons |
|---|---|
| Works out of the box for both deployment shapes (usability) | Failure diagnosis is genuinely harder: a 401 on Dashboards and an unreachable Dashboards take the same code path |
| Degrades rather than failing — the more restricted credential still gets data (robustness, per `01` §4 on self-inflicted fragility) | Probing costs a round trip, and the two backends are not perfectly interchangeable |
| One documented config that covers both | Some capabilities exist on only one backend, so a few tools must fail with a backend-specific message |

## Decision

**We will probe OpenSearch Dashboards first via `/api/status`, and fall back to
the direct OpenSearch REST API when Dashboards is absent, unreachable, or refuses
the probe. The resolved backend is cached, subject to a re-resolution policy.**

> **Amended 10 Aug 2026.** This decision originally read "cached on the client for
> the process lifetime", and the Consequences below recorded the resulting
> diagnosability and permanence costs as accepted. Both were then fixed rather than
> merely tolerated, so the original text is preserved here for the record and the
> current behaviour is described below. The *decision* — Dashboards first, direct as
> fallback — is unchanged, so this is an amendment rather than a superseding ADR.

The caching policy, as built:

- The **preferred** backend, once active, is not re-probed on a schedule. A probe
  before every request would add a round trip to everything in order to detect a
  failure that the request itself already reports.
- A **degraded** resolution — Dashboards configured but direct won — is re-probed
  after `backend_recheck_seconds` (default 300), so a Dashboards blip heals within
  five minutes at a cost of at most one extra round trip per window.
- A **transport failure drops the cache immediately**, so failover in either
  direction costs one probe on the next call.
- `opensearch_test` and `list_index_patterns` force a probe, because answering
  "which backend is active, and is it alive?" is precisely what they are for.

Corollaries, all currently true in the code:

- Requests routed through Dashboards are rewritten onto `/api/console/proxy`,
  carrying the real OpenSearch path and method as query parameters.
- `opensearch_test` reports which backend won, plus the authenticated username —
  so a 403 on any other tool can be explained by the caller in one step rather
  than guessed at.
- Tools that genuinely require Dashboards (`opensearch_list_index_patterns`) fail
  with a message naming the requirement instead of a generic error.
- `list_index_patterns` tries the 2.x saved-objects API and then the newer
  data-views API, because the endpoint moved between versions.

**Technical justification:** the two backends have different access profiles and
different availability in the field. Probing turns a configuration problem the
user cannot easily diagnose into one the server resolves itself, and the fallback
means a restricted credential degrades to less capability rather than to nothing —
the explicit guidance in `01` §4 for external integration points.

**Business justification:** user satisfaction, in the operationally real sense.
The alternative is an analyst who cannot get the server working at all because
port 9200 is closed and nothing said so. Secondarily, strategic positioning: the
server is usable in locked-down managed environments, which is the environment it
was built for and the one where a read-only agent is most valuable.

## Consequences

**Positive:** one configuration works across both deployment shapes. Restricted
credentials still function. The active backend is always observable through
`opensearch_test`.

**Negative, accepted:**

- ~~**Diagnosis is harder, and this is the real cost.** The probe swallows the
  distinction between "Dashboards said 401", "TLS verification failed" and
  "Dashboards is not there"…~~ **Resolved 10 Aug 2026.** The raised error now lists
  each backend attempted with its real cause — auth failed, TLS verification failed,
  unreachable, redirected to SSO, non-JSON login page, or an HTTP status — and
  mentions `OPENSEARCH_URL` only when it was actually set, or says explicitly that
  it was not set so no fallback was attempted.
- The proxy path is not semantically identical to a direct call. A GET routed
  through `/api/console/proxy` is delivered as a POST to Dashboards with the real
  method as a parameter, and query parameters are flattened into the proxied path
  string. This is a second code path with its own encoding behaviour, and it is
  exercised only against a live Dashboards. **Partially reduced:** proxied GETs no
  longer carry a `{}` body, so that particular asymmetry between the two backends is
  gone. The separate code path, and the fact that its encoding is unverified against
  a real Dashboards + OpenSearch pair, both remain.
- ~~Backend resolution is cached, so a backend that recovers mid-session is not
  re-probed.~~ **Resolved 10 Aug 2026** — see the caching policy in the Decision
  above. A degraded resolution is re-probed on a timer and a transport failure drops
  the cache immediately, so recovery and failover both work in either direction.
- `init_client` probes eagerly at startup, so process launch depends on cluster
  reachability. **Still true**, and still accepted: paying the probe once at launch
  surfaces a configuration error immediately rather than on the first tool call.

**Trade-off taken:** we trade diagnosability and a single clean request path for
usability against restricted credentials, because a server that a firewalled
analyst cannot use at all is worse than one whose failure messages need a second
look.

## Compliance

- Automatable? **Partly** — and the automated half now exists.
- Mechanism: unit tests over the resolution logic with the probes stubbed, asserting
  both the chosen backend and that the raised message names the real cause. The
  matrix is broader than this section originally specified and now covers Dashboards
  200 / 401 / 403 / 302-to-SSO / HTML login page / unreachable / TLS failure, plus
  only-direct, only-Dashboards and neither-configured. The proxy **encoding**
  behaviour still needs an integration test against a live Dashboards + OpenSearch
  pair; until that exists that half is **manual and recorded as such** rather than
  quietly unverified.
- Where the check lives: `tests/test_resilience.py` (resolution, failover,
  re-probe policy); proxy encoding still unverified.
- When it runs: CI, every push and pull request, for the automated half.
- ~~Code changes needed to make it measurable: the two broad `except Exception`
  blocks in `_resolve_backend` need to preserve enough of the cause to assert on…~~
  **Done 10 Aug 2026.** `_resolve_backend` was replaced by `_candidates()` /
  `_probe()` / `_resolution_error()`, which classify each failure instead of
  discarding it, and a `session=None` constructor parameter provides the test seam.

## Notes

- Author: repository maintainer (recorded with Claude Code, 10 Aug 2026)
- Approved by: self-approved — records existing behaviour
- Approval date: 10 Aug 2026
- Follow-up: the diagnosability consequence is a defect worth fixing, not merely a
  cost worth accepting. Fixing it does not supersede this ADR.
