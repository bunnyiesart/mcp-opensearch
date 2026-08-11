"""Two-window frequency comparison — pure domain logic, no infrastructure.

This lives in its own module rather than in `client.py` on purpose. It is the only
real computation in the project, and it knows nothing about OpenSearch: it takes two
already-fetched `{value: count}` maps and produces a diff. Keeping it away from the
adapter means it can be tested by calling it with two dicts — no client, no session,
no stubbed transport.

It was previously inlined in the `opensearch_compare` tool function in `server.py`,
which made it the one piece of behaviour in the codebase that could not be exercised
without FastMCP. That is the violation ADR 0001 rule 1 exists to prevent, and the
reason `tests/test_architecture.py` enforces a statement budget on tool functions.
"""

from __future__ import annotations

# Key used by the client to smuggle a warning back inside a `{value: count}` map.
# Popping it here is what stops it being mistaken for a field value in the diff.
_WARNING_KEY = "_warning"


def split_warning(counts: dict) -> tuple[dict, str | None]:
    """Separate the client's `_warning` from the actual `{value: count}` data.

    `terms()` returns warnings in the same dict as the buckets, so the warning has to
    be lifted out before anything iterates the values — otherwise it is diffed as
    though it were a field value with a string "count".

    Returns a NEW dict, leaving the caller's untouched. The previous implementation
    used `dict.pop`, mutating a structure it did not own; harmless while there was
    exactly one caller, and exactly the kind of thing that stops being harmless
    quietly.
    """
    if not isinstance(counts, dict):
        return {}, None
    warning = counts.get(_WARNING_KEY)
    data = {k: v for k, v in counts.items() if k != _WARNING_KEY}
    return data, warning if isinstance(warning, str) else None


def percent_change(baseline: float, selection: float) -> float | None:
    """Change from `baseline` to `selection` as a percentage, rounded to 0.1.

    None when the baseline is zero, because the change is undefined rather than
    infinite — and reporting a number there would invite an agent to describe a value
    that appeared from nothing as an enormous percentage increase instead of as new.
    Such values are reported under "added" anyway.
    """
    if not baseline:
        return None
    return round((selection - baseline) / baseline * 100, 1)


def diff_counts(baseline: dict, selection: dict) -> dict:
    """Compare two `{value: count}` maps.

    Buckets are classified into exactly one of four groups, so the four returned maps
    partition the union of the keys:

      added      present in selection, absent from baseline  — new behaviour
      removed    present in baseline, absent from selection  — something went quiet
      changed    in both, different counts, sorted by |delta| descending
      unchanged  in both, identical counts

    "Absent" means absent, not zero: a terms aggregation does not emit a zero bucket,
    so a value missing from one window genuinely did not occur in it.

    `changed` is sorted by the absolute delta because the caller is an agent deciding
    what to look at first, and the biggest movement in either direction is the most
    interesting. Sorting by percentage would put a 1→3 count above a 4000→9000 one.

    Pure: no I/O, no mutation of the inputs.
    """
    added, removed, changed, unchanged = {}, {}, {}, {}

    for key in set(baseline) | set(selection):
        b = baseline.get(key)
        s = selection.get(key)
        if b is None:
            added[key] = s
        elif s is None:
            removed[key] = b
        elif b != s:
            changed[key] = {
                "baseline": b,
                "selection": s,
                "delta": s - b,
                "pct_change": percent_change(b, s),
            }
        else:
            unchanged[key] = {"baseline": b, "selection": s}

    return {
        "added": added,
        "removed": removed,
        "changed": dict(
            sorted(changed.items(), key=lambda kv: abs(kv[1]["delta"]), reverse=True)
        ),
        "unchanged": unchanged,
    }


def compare_windows(baseline_counts: dict, selection_counts: dict) -> dict:
    """Full `opensearch_compare` result from two raw `terms()` responses.

    Lifts each window's warning out, diffs the remainder, and returns the diff plus
    the two warnings. This is the whole body of the tool function, which is now a
    declaration and two delegations.
    """
    baseline, baseline_warning = split_warning(baseline_counts)
    selection, selection_warning = split_warning(selection_counts)
    return {
        **diff_counts(baseline, selection),
        "baseline_warning": baseline_warning,
        "selection_warning": selection_warning,
    }
