# 0004. A single allowlist is the only read-only enforcement point

**Status:** Accepted

## Context

The server's central promise is in its own name and first line of documentation:
read-only. Before this decision, that promise was enforced by two mechanisms that
disagreed with each other, and neither covered the tools that needed it most.

- `lib/client.py` held an **allowlist** (`_ALLOWED_PATHS`, suffix-matched) checked
  by `_get` and `_post`.
- `server.py` held a **denylist** (`_WRITE_PATH_FRAGMENTS`) of fourteen write-ish
  substrings, applied only inside the `opensearch_api` tool.
- `OpenSearchClient` also exposed `raw_get` and `raw_post`, which skipped the
  allowlist by design — their docstrings read "Caller owns validation."

The two tools that accept a caller-supplied path both routed through the bypass:
`opensearch_api` through `raw_get`, and `opensearch_explain` through `raw_post`.
So the allowlist and the traversal check never ran on the only paths an LLM could
influence. What ran instead was the denylist, which enumerated write verbs and
nothing else — leaving the entire administrative *read* surface open.

Two consequences were verified by execution, not inferred:

1. `opensearch_api("/_plugins/_security/api/internalusers")` passed every check in
   the codebase and reached the cluster. That endpoint returns the security
   plugin's internal user database, including password hashes. `/_snapshot/*`,
   `/_cluster/settings` and `/_cluster/state` were equally reachable.
2. **A second route existed even against a hypothetical `_plugins/_security`
   substring block.** `requests` collapses dot segments when it prepares a URL:
   `https://host/_cat/../_plugins/_security/api/internalusers` prepares as
   `https://host/_plugins/_security/api/internalusers`. A substring denylist
   inspects the string *before* that collapse and cannot see through it. The
   allowlist's `posixpath.normpath` check would have caught it — but it was on the
   bypassed path.

Nothing here was a write: the escape hatch was GET-only and `raw_post` was
reachable only through the fixed `_explain` path. The defect was that the code
treated *read-only* as equivalent to *safe*, and credential surfaces are reads.

Additionally, the denylist's substring matching produced false positives that
blocked legitimate work: `/_plugins/_ism/policies/hot_rollover_policy` contains
`_rollover`, and `/_cat/indices/my_create_index` contains `_create`.

## Alternatives

### Option A — Route `raw_get`/`raw_post` through `_check_path`

| Pros | Cons |
|---|---|
| Smallest possible diff; closes the immediate hole | Leaves two general-purpose "any path" methods on the public API whose names and docstrings still advertise a bypass — the next contributor reaches for them again (the guarantee stays a convention) |
| Preserves an extension point for future tools | Two ways to make a request means two things to keep correct |

### Option B — Delete the bypass methods entirely

| Pros | Cons |
|---|---|
| `_get`/`_post` become the *only* exits from the class, and both check first — the guarantee becomes structural, not conventional (security) | Any future endpoint needs an allowlist entry; there is no escape hatch left (extensibility) |
| The class's public surface stops advertising a way around its own guard | Slightly larger diff, and the allowlist has to be extended to keep the existing tool working |

## Decision

**We will make `_ALLOWED_PATHS` the single enforcement point, delete `raw_get` and
`raw_post`, and remove all path-validation logic from `server.py`.**

As built:

- `raw_get`/`raw_post` are gone. `opensearch_api` calls a narrow `api_get`, and
  `explain` calls `_post`. Verified: the only remaining direct `_session` calls
  use literal, fixed paths — the two backend probes, the two Dashboards endpoints
  in `list_index_patterns`, and `_dashboards_proxy` (already checked upstream).
- **The matching primitive changed**, because suffix matching could not express
  the required cases. `_ALLOWED_PATHS` now distinguishes:
  - `absolute` — matches the entry exactly or any sub-resource beneath it. Needed
    for `/_cat/indices/my_create_index` and `/_nodes/stats/jvm`, both of which a
    suffix match rejects.
  - `indexed` — the path parses as `/<index>/<_action>/...` and `/<_action>` is
    listed. Needed for `/<index>/_explain/<doc_id>`, which **no** suffix entry can
    match, because the path ends in the document id. The index segment must be
    non-empty and must not begin with `_`, so a crafted index cannot smuggle in a
    cluster endpoint.
- `_WRITE_PATH_FRAGMENTS`, `_EXPLAIN_PATH_RE` and `import re` are gone from
  `server.py`. Both tools are thin delegates again, per ADR 0001 rule 1.
  `opensearch_api` retains only the array-to-`{"result": [...]}` wrap, which is
  FastMCP serialization, not a security rule.
- The explain path is constructed in exactly one place, with an explicit
  pre-condition on `index` and `doc_id`.
- **`/api/console/proxy` was removed from the allowlist.** It was vestigial, and
  it is a tunnel: the real method and path travel in its query string, so an entry
  for it would authorize anything for a future caller. The Dashboards backend is
  unaffected — the inner path is what gets checked.
- The refusal message is generated *from* the allowlist, so it cannot drift from
  what is actually permitted, and it points the caller at the dedicated tools.
- `/_plugins/_security/*`, `/_cluster/settings`, `/_cluster/state`, `/_snapshot/*`
  and bare `/_nodes` are excluded on purpose, with the reason recorded in a
  comment beside the dict.

**Technical justification:** an allowlist fails closed and a denylist fails open,
and this codebase demonstrated both halves of that — the denylist missed the whole
admin read surface while simultaneously blocking legitimate reads. Deleting the
bypass converts the guarantee from something maintained by discipline into
something maintained by structure, which is the only kind that survives
contributors who have not read this file. This is fallacy #4 applied
inward: every endpoint needs protecting, and "we only issue GETs" is not
protection when credentials are served over GET.

**Business justification:** strategic positioning, and it is the whole product
thesis rather than a nice-to-have. This server exists to be pointed at production
security telemetry by an autonomous agent. The reason an operator grants it
credentials at all is the read-only guarantee on the label. A tool that can be
talked into fetching password hashes is not a read-only tool, and one demonstrated
instance of that would end the case for running it anywhere that matters. The
secondary benefit is user satisfaction: the false positives that blocked
`hot_rollover_policy` and `my_create_index` are gone, so the escape hatch now
works on the paths its own documentation advertises.

## Consequences

**Positive:** one enforcement point, checked on every outbound request, with no
method on the class capable of skipping it. Traversal is caught for every path,
not just the ones that happened to route through `_get`/`_post`. Refusal messages
are derived from the allowlist and are actionable. The false positives are gone.

**Negative, accepted:**

- **Every new endpoint now requires an allowlist entry.** This is the cost of
  failing closed and it is charged on every future tool. Accepted deliberately:
  a contributor who must add an entry is a contributor who has to think about
  whether the endpoint is a read and whether it exposes secrets.
- **Index-level authorization remains entirely the backend's.** The allowlist
  constrains endpoints, not index names: `/<any-index>/_search` is permitted for
  any index the account can read. Constraining that is a configuration feature,
  not a guard fix, and is out of scope here.
- **The `/_cat` family is allowed wholesale**, including `/_cat/repositories` and
  `/_cat/snapshots`. Every `_cat` endpoint is GET-only and none returns
  credentials — they expose repository and snapshot names and types. Accepted.
- **`/<index>/_settings` remains allowed.** Index settings are not a designed
  credential surface, but an operator who stored a secret in a custom index
  setting would expose it. Accepted.
- `_dashboards_proxy` has no check of its own. It is unreachable except through
  `_get`/`_post`, which check first; a second check there was declined in favour
  of keeping one enforcement point, and is a one-line addition if defence in
  depth is later wanted.

**Trade-off taken:** we trade extensibility — the freedom to call an arbitrary
endpoint without touching the allowlist — for a read-only guarantee that holds
structurally. Given that the guarantee is the reason anyone grants this server
credentials, that is not a close call.

## Compliance

- Automatable? **Yes**, and two distinct checks are needed.
- Mechanism:
  1. **No-bypass fitness function.** Assert that no method on `OpenSearchClient`
     issues a request with a caller-supplied path without passing through
     `_check_path` — concretely, that the only `_session.get`/`_session.post` call
     sites are the known fixed-path ones, so reintroducing a `raw_*` breaks the
     build. This is the check that stops the original defect from growing back.
  2. **Docstring-versus-allowlist fitness function.** Every example path in the
     `opensearch_api` tool docstring must pass `_check_path`. The tool docstring is
     the contract the LLM reads; if it advertises a path the guard refuses, the
     agent burns an incident-response cycle on a refusal. This check keeps the
     documentation and the guard from drifting in either direction.
  3. Ordinary unit tests over `_check_path`: the excluded credential surfaces, the
     traversal forms, the two previously-false-positive paths, and every path the
     21 public client methods actually emit.
- Where the check lives: `tests/test_path_guard.py` and `tests/test_architecture.py`
- When it runs: CI, on every push and pull request.
- Code changes needed to make it measurable: none — the current structure already
  supports all three checks.

## Notes

- Author: repository maintainer (implemented with Claude Code, 10 Aug 2026)
- Approved by: **not self-approvable** — this decision has a direct security
  implication, so it requires the maintainer's explicit sign-off before release
  even though the code is written. Recorded as `Accepted` on the strength of the
  verified behaviour; convert to `Proposed` if the maintainer wants to revisit the
  exclusion list.
- Approval date: pending maintainer review of the exclusion list
- Supersedes no earlier ADR; the previous arrangement was never recorded, which is
  a large part of why it drifted into two contradictory mechanisms.
- ~~`CHANGELOG.md` still advertises `raw_get`/`raw_post` as a feature.~~ **Resolved
  10 Aug 2026**: the 0.3.0 entry now describes what that release did with a forward
  pointer to the removal, and the removal is recorded under `[Unreleased]` and
  flagged as potentially breaking.
