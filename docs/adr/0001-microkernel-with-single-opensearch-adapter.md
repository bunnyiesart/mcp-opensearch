# 0001. Microkernel with a single OpenSearch adapter

**Status:** Accepted

This ADR records retroactively a decision the code already embodies. It is written
now because the *why* was never captured, and the structure was starting to erode
in ways that only make sense to argue about once the original intent is on paper.

## Context

This server exposes OpenSearch to an LLM agent as a set of MCP tools. The unit of
growth is "one more tool" — over its life the project went from a handful of search
and aggregation tools to 22, adding index settings, PPL, explain, Alerting reads,
Anomaly Detection reads and a two-window comparison. That growth pattern is the
dominant force on the design: it is not the domain logic that grows, it is the
number of discrete, mostly independent capabilities exposed over one backend.

Two structures were available:

- A **layered** split (transport / query building / HTTP), organised by technical
  capability.
- A **microkernel**: a core that declares and dispatches tools, plus one adapter
  that knows how to talk to OpenSearch. Each new tool is a plug-in on the core.

## Alternatives

### Option A — Layered by technical capability

| Pros | Cons |
|---|---|
| Familiar; easy to say where a given kind of code goes | The domain gets smeared across the layers — adding one tool touches every layer (`03` §2) |
| Reuse of common query-building code is natural | Highest ceremony for the actual change pattern here, which is "add one independent capability" |

### Option B — Microkernel (core + one adapter)

| Pros | Cons |
|---|---|
| Isomorphic with the domain: a tool *is* a plug-in, added without touching the others (extensibility) | Kryptonite: the core must stay stable. If the core keeps changing, either the style is wrong or the wrong things were made plug-ins |
| Simple structure — two moving parts, low total cost (simplicity, viability) | Tempting to let plug-ins share state or logic through the core, which is how the boundary rots |
| Tool-level isolation keeps test scope and change risk small (testability, deployability) | Does nothing for operational characteristics — irrelevant here, this is a stdio process with one user |

## Decision

**We will keep a microkernel: `server.py` is the core that declares and dispatches
tools; `lib/client.py` is the single adapter that owns all OpenSearch knowledge.**

Two rules make it real rather than aspirational:

1. **A tool function in `server.py` is a thin delegate.** It declares the contract
   the LLM reads (name, signature, docstring) and forwards to the client. It
   contains no HTTP, no query construction, no path validation, and no computation
   over results.
2. **All OpenSearch knowledge lives in the adapter** — query bodies, response
   shaping, caps, warnings, and the read-only guard.

**Technical justification:** the change pattern is "add one independent
capability", and the microkernel is the only style whose topology matches that
pattern. It also keeps the core free of anything that needs a network to test:
with the domain in the adapter, the pure logic is unit-testable and the adapter
is integration-testable, which is the split `05` §2 argues for.

**Business justification:** time to market for new tools. A new detection source
or plugin read becomes one function plus one client method, with a test scope
limited to itself — no regression surface across the other 21 tools. The secondary
benefit is user satisfaction in the narrow sense that matters here: a tool
docstring is the contract the agent reads at 3am during an incident, and keeping
declarations separate from mechanics keeps those docstrings honest and reviewable.

## Consequences

**Positive:** adding a tool is a local change. Tool declarations read as a single
reviewable inventory of the contract. The adapter can be exercised without
FastMCP, and the core can be reasoned about without a cluster.

**Negative, accepted:**

- Reuse *between* plug-ins is deliberately awkward. Two tools needing the same
  helper must put it in the adapter, not pass it between themselves — plug-ins
  talking to plug-ins is the documented failure mode of this style (`03` §4.3).
- The style gives us nothing operationally. That is fine: a stdio MCP process
  serving one agent has no scalability or elasticity requirement worth designing
  for, and pretending otherwise would be the Vasa error.

**Trade-off taken:** we trade convenient code sharing between tools for a stable
core and a low per-tool change cost, because extensibility and testability are
what this project's growth pattern actually demands.

**Known erosion at the time of writing** (each is a violation of rule 1 or 2 above,
recorded here so the boundary is measurable rather than aspirational):

- `opensearch_compare` computes its diff, delta and percent change in the tool
  function instead of the adapter — the one tool carrying real domain logic in the
  core, and consequently the one that cannot be tested without FastMCP.
- Path-validation rules lived in the core (a denylist and a regex) in parallel
  with the adapter's allowlist. See ADR 0004.

## Compliance

- Automatable? **Yes**, and it should be — this is exactly the "important but not
  urgent" rule that erodes silently without a check in the build (`07` §7).
- Mechanism: fitness function asserting the layering, not the behaviour.
  Concretely: `server.py` imports no HTTP library and no `lib.client` internals
  beyond `init_client`; `server.py` contains no path-validation pattern; each
  `@mcp.tool()` body stays under a small statement budget so computation cannot
  quietly accumulate there again.
- Where the check lives: `tests/test_architecture.py`
- When it runs: CI, on every push and pull request.
- Code changes needed to make it measurable: the `opensearch_compare` diff must
  move to `lib/` first, otherwise the check lands red on arrival. That extraction
  is a real work item, not a footnote — which is the point of filling in this
  section (`04` §3).

## Notes

- Author: repository maintainer (recorded with Claude Code, 10 Aug 2026)
- Approved by: self-approved — records existing structure, no cost, no consumer
  impact, no security implication
- Approval date: 10 Aug 2026
