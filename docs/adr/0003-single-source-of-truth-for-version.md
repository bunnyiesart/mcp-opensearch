# 0003. Single source of truth for the version string

**Status:** Accepted

## Context

The version was written by hand in several places that must agree, and they
stopped agreeing. Observed on 10 Aug 2026:

| Location | Value |
|---|---|
| `pyproject.toml` `[project] version` | `0.4.0` |
| `Makefile` `VERSION` | **`0.3.3`** |
| `lib/client.py` `User-Agent` header | `0.4.0` |
| `CHANGELOG.md` latest entry | `0.4.0` |

The consequence was not hypothetical: `make push` builds and pushes
`ghcr.io/bunnyiesart/mcp-opensearch:0.3.3` from 0.4.0 source, and also moves the
`latest` tag onto it. Anyone pinning `0.3.3` silently receives 0.4.0 code, and the
0.3.3 image no longer matches the 0.3.3 tag in git.

This is the textbook shape of values that must change together but are stored
apart. The release workflow already guards the `pyproject.toml`-versus-git-tag
pair, so the failure was specifically in the places CI never looked at.

The same shape exists for dependencies, declared twice with identical content:
`requirements.txt` (used by the Dockerfile) and `pyproject.toml` `dependencies`
(used by the PyPI build). Nothing currently detects them diverging.

## Decision

**We will keep exactly one authoritative version — `pyproject.toml`
`[project] version` — and derive every other occurrence from it, rather than
re-synchronising copies by hand.**

Applied so far:

- The `Makefile` reads the version out of `pyproject.toml` instead of declaring
  it, and `make push` refuses to run if that read comes back empty. Drift between
  these two is now impossible rather than merely fixed.

Still to apply:

- The `User-Agent` in `lib/client.py` must derive from package metadata instead of
  carrying a literal.
- A fitness function must assert that no hardcoded version literal reappears
  anywhere outside `pyproject.toml`, and that `requirements.txt` and the
  `pyproject.toml` dependency list agree.

**Technical justification:** re-syncing copies fixes one instance and leaves the
mechanism intact. Deriving removes the class of bug. Where derivation is not
practical, an automated check is the substitute — this is the "important but not
urgent" category of rule that erodes precisely because nothing in the build ever
complains (`07` §7).

**Business justification:** cost, and it is a trust cost rather than a
compute cost. This is a published package and a published container image; a tag
that does not correspond to its contents is a supply-chain defect for whoever
pinned it, and the debugging it causes downstream lands on the maintainer. The
User-Agent matters for the same reason in reverse — it is what a cluster operator
sees in their logs when this client misbehaves, and it should not lie about which
version is talking.

## Consequences

**Positive:** the release path has one authoritative version. `make push` cannot
mislabel an image. A future contributor cannot reintroduce the drift by editing
one file, because there is only one file to edit.

**Negative, accepted:** the `Makefile` now parses TOML with `grep` and `cut`,
which is fragile if the `version` line ever changes shape — it assumes a
top-level `version = "…"` line with double quotes. That is why `make push` asserts
the value is non-empty before building: the failure mode is a loud stop, not a
mislabelled push. A `tomllib` read would be more correct but would add a Python
dependency to a target whose only other requirement is Docker.

**Trade-off taken:** we trade a little parsing robustness in the `Makefile` for
eliminating an entire class of release-labelling defect, and we contain the
residual risk by failing closed.

**Not addressed here:** the `0.3.3` image already pushed from other source, and
the `latest` tag, are historical facts in the registry. Deciding whether to
re-push or yank is a release-management call for the maintainer, not an
architectural decision, and is deliberately out of scope for this ADR.

## Compliance

- Automatable? **Yes.**
- Mechanism: fitness function — grep the tree for version-shaped literals outside
  `pyproject.toml` and fail on any hit; parse `requirements.txt` and
  `pyproject.toml` `dependencies` and assert the sets are equal.
- Where the check lives: `tests/test_project_consistency.py`
- When it runs: CI, on every push and pull request. It must also run before the
  release workflow builds anything, so a drifted tag fails the release rather
  than shipping.
- Code changes needed to make it measurable: the `User-Agent` literal in
  `lib/client.py` has to be derived first, otherwise the check lands red.

## Notes

- Author: repository maintainer (recorded with Claude Code, 10 Aug 2026)
- Approved by: self-approved — no cost, no cross-team impact, no security
  implication, and it corrects an active defect
- Approval date: 10 Aug 2026
