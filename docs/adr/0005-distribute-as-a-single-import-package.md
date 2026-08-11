# 0005. Distribute as a single import package

**Status:** Proposed

## Context

The wheel installs top-level modules named `server` and `lib`:

```toml
[tool.hatch.build.targets.wheel]
include = ["server.py", "lib/"]
```

With no package directory, `pip install mcp-opensearch` claims the global import names
`server` and `lib` in `site-packages`. Both are generic enough to collide, and `lib`
is a name several packages have squatted historically.

The failure mode is nasty because of where it lands. If any other installed
distribution provides a top-level `lib`, then `from lib.client import init_client` at
the top of `server.py` resolves to the wrong module — or raises `ModuleNotFoundError`
— **at MCP server startup**, where there is no console to read. An MCP client reports
"server failed to start" and nothing else. The user is left comparing environments.

This affects the installation path the README calls "recommended for Python users".
It does not affect the Docker path, which copies the files into `/app` and runs them
directly, nor a source checkout.

Nothing detects this today: CI installs `requirements.txt` and runs the suite from the
repository root, where the flat layout resolves correctly. The wheel's import surface
is never exercised.

## Alternatives

### Option A — Leave it; document the risk

| Pros | Cons |
|---|---|
| Zero churn now | The failure is silent, remote, and lands on a user rather than on us |
| No import changes to test files | Keeps a known packaging defect in the recommended install path |

### Option B — Map into a package at build time only (hatch `sources`)

| Pros | Cons |
|---|---|
| No files move on disk; source layout unchanged | The installed layout stops matching the source layout, so `from lib.client import …` works in the repo and fails in the wheel — the divergence is exactly the class of bug being fixed |
| Small diff | Two import realities to reason about; the console-script entry point still needs changing |

### Option C — Restructure into a real package (`mcp_opensearch/`)

| Pros | Cons |
|---|---|
| One import name, matching the distribution name; no possibility of collision (correctness) | Renames a module that every test file imports — roughly twenty files touched |
| Installed layout matches source layout, so the repo and the wheel behave identically (testability) | Touches `pyproject.toml`, `Dockerfile` and the console-script entry point together |
| Conventional; a reader familiar with Python packaging finds what they expect (supportability) | Must land as one atomic change, because a half-applied rename does not import at all |

## Decision

**We will restructure into a single import package, `mcp_opensearch/`, containing the
server module and the current contents of `lib/`, with the console script pointing at
`mcp_opensearch.server:main`.**

**Technical justification:** Option B fixes the symptom while introducing the disease
— a source tree that imports differently from the artifact it produces is how you get
bugs that only reproduce after publishing. Option A leaves a defect whose blast radius
is "the server does not start" in an environment we cannot see. Option C is the only
one where the thing we test is the thing we ship.

**Business justification:** cost, in support time rather than compute. A startup
failure with no diagnostic output is among the most expensive kinds of bug to field,
because the reporter cannot tell you anything useful and the maintainer cannot
reproduce it without matching their dependency set. Avoiding one such report pays for
the refactor several times over.

## Consequences

**Positive:** one import name; wheel and repository behave identically; the entry point
becomes conventional; no possibility of a third-party `lib` shadowing ours.

**Negative, accepted:**

- **It is a wide, shallow diff** — every `from lib.client import …` in the test suite
  changes. Mechanical, but it touches many files at once, which makes it noisy to
  review and impossible to land partially.
- **The Dockerfile and the console script change with it.** `ENTRYPOINT ["python3",
  "server.py"]` becomes a module invocation, and that path is only really verified by
  building and running the image.
- **It is a breaking change for anyone importing this package as a library** rather
  than running it as an MCP server. That is unlikely — it is a server, not a library —
  but it is a real API break and belongs in the changelog as one.

**Trade-off taken:** we trade a large one-off mechanical diff for eliminating a class
of startup failure that is invisible to us and undiagnosable by the user.

**Deliberately not done in the same change as anything else.** A rename this wide must
land alone. It was deferred out of the 10 Aug 2026 session specifically because other
work was in flight in `lib/client.py` at the time, and a rename colliding with
concurrent edits to the renamed file is how you lose changes.

## Compliance

- Automatable? **Yes**, and the check is what makes this decision worth anything: the
  defect exists precisely because nothing exercises the wheel's import surface.
- Mechanism: a CI job that builds the wheel, installs it into a clean virtualenv
  **that also contains a deliberately conflicting top-level `lib` module**, and then
  imports the entry point and runs `--help` or an equivalent smoke check. Testing the
  installed artifact rather than the source tree is the entire point; a check that
  imports from the repo root would pass today and prove nothing.
- Where the check lives: a new `wheel` job in `.github/workflows/ci.yml`
- When it runs: CI on every push and pull request, and necessarily before the release
  workflow publishes.
- Code changes needed to make it measurable: the restructure itself, plus a trivial
  importable entry point for the smoke check to call.

## Notes

- Author: repository maintainer (recorded with Claude Code, 10 Aug 2026)
- Status is `Proposed`, not `Accepted`: this is a breaking change to the published
  artifact's import surface, so it is not self-approvable under the criteria in
  `docs/adr/README.md`, and it should land as its own commit with its own review.
- Tracked as `#18` in [`docs/defect-backlog.md`](../defect-backlog.md).
