# Architecture Decision Records

One file per significant decision. The point is not ceremony — it is to stop the
same question being re-argued every few months because nobody wrote down *why*.

A decision belongs here if it affects **structure**, an **`-ility`** that matters
to this project, a **dependency**, an **interface/contract**, or a **construction
technique** (Nygard's criteria). "It involves a specific technology, so it's just
a technical decision" is not an exemption: if the technology was chosen because it
supports an architectural characteristic, the decision is architectural.

## Index

| ADR | Title | Status |
|---|---|---|
| [0001](0001-microkernel-with-single-opensearch-adapter.md) | Microkernel with a single OpenSearch adapter | Accepted |
| [0002](0002-dashboards-proxy-first-with-direct-fallback.md) | Dashboards proxy first, direct OpenSearch as fallback | Accepted |
| [0003](0003-single-source-of-truth-for-version.md) | Single source of truth for the version string | Accepted |
| [0004](0004-single-allowlist-as-the-read-only-enforcement-point.md) | A single allowlist is the only read-only enforcement point | Accepted — exclusion list pending maintainer review |

## Conventions

- **Numbering** is sequential and never reused. Copy [`0000-template.md`](0000-template.md).
- **Status** is one of `Proposed`, `Accepted`, `Superseded by NNNN`, or
  `Request For Comments, Deadline DD MMM YYYY`. An RFC always carries a deadline —
  that is what stops analysis paralysis.
- **Superseding is marked in both directions.** The old ADR gets
  `Superseded by NNNN`; the new one gets `Accepted, supersedes NNNN`. Never delete
  a superseded ADR — the historical record of what was true *at the time* is the
  most valuable thing in this directory.
- **Decisions are written in the affirmative:** "We will use X", not "I think X
  would probably be best". The latter records an opinion, not a decision.
- **Every `Decision` section carries both a technical and a business
  justification.** The business one is the one that is usually missing, and it is
  also the litmus test: a decision that delivers no value to anyone using this
  server is probably not a good decision.
- **Every ADR fills in `Compliance`** — is this checkable automatically, or only
  by review? If automatically, the fitness function is named and lives in `tests/`.

## Self-approval

Following the cost / cross-team-impact / security criteria: a decision here may be
self-approved when it costs nothing but implementation time, affects no consumer of
the published package or image, and has no security implication. Anything touching
the read-only guarantee, the published tool contract, or the credential handling
path is **not** self-approvable and starts as `Proposed`.
