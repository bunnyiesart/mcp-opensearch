"""lib/compare.py — the two-window diff.

Every test here is dict in, dict out. No client, no session, no stubbed transport,
no FastMCP. That is the entire argument for having extracted this from the tool
function: the logic did not change, but it went from requiring the MCP layer to
requiring nothing at all.
"""

import pytest

from lib.compare import compare_windows, diff_counts, percent_change, split_warning

# ── split_warning ─────────────────────────────────────────────────────────────


def test_warning_is_lifted_out_of_the_counts():
    """`terms()` returns its warning in the same dict as the buckets, so it must be
    removed before anything iterates values — otherwise the warning string is diffed
    as though it were a field value."""
    data, warning = split_warning({"a": 1, "_warning": "size capped at 1,000"})
    assert data == {"a": 1}
    assert warning == "size capped at 1,000"


def test_absent_warning_yields_none():
    data, warning = split_warning({"a": 1})
    assert data == {"a": 1}
    assert warning is None


def test_split_warning_does_not_mutate_its_input():
    """The previous implementation used `dict.pop`, mutating a structure it did not
    own. Harmless with one caller; the sort of thing that stops being harmless
    silently."""
    original = {"a": 1, "_warning": "w"}
    split_warning(original)
    assert original == {"a": 1, "_warning": "w"}


@pytest.mark.parametrize("bad", [None, [], "string", 42])
def test_split_warning_tolerates_a_non_dict(bad):
    """Defensive: this consumes another component's output, and returning ({}, None)
    keeps a malformed upstream response from turning into an AttributeError inside
    the diff."""
    assert split_warning(bad) == ({}, None)


def test_non_string_warning_is_ignored():
    """A bucket genuinely named `_warning` with an integer count is data, not a
    warning. This is the known namespace collision: the value is still lost from the
    diff, but it is not misreported as a warning string."""
    data, warning = split_warning({"_warning": 7})
    assert warning is None
    assert data == {}


# ── percent_change ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("baseline", "selection", "expected"),
    [
        (100, 150, 50.0),
        (100, 50, -50.0),
        (100, 100, 0.0),
        (3, 1, -66.7),      # rounds to one decimal
        (1, 1000, 99900.0),
    ],
)
def test_percent_change(baseline, selection, expected):
    assert percent_change(baseline, selection) == expected


def test_percent_change_from_zero_is_none_not_infinity():
    """Undefined rather than infinite. A value that appeared from nothing belongs in
    `added`, and reporting a percentage there would invite an agent to describe new
    activity as an enormous increase."""
    assert percent_change(0, 500) is None


# ── diff_counts: the four groups partition the keys ───────────────────────────


def test_the_four_groups_are_a_partition_of_the_union():
    """Every key lands in exactly one group. If a key could appear twice, or vanish,
    an agent summing the groups would double-count or under-report."""
    baseline = {"keep": 5, "gone": 3, "moved": 10}
    selection = {"keep": 5, "new": 1, "moved": 20}
    out = diff_counts(baseline, selection)

    groups = ["added", "removed", "changed", "unchanged"]
    seen = [k for g in groups for k in out[g]]
    assert sorted(seen) == sorted(set(baseline) | set(selection))
    assert len(seen) == len(set(seen)), "a key appeared in more than one group"


def test_added_is_present_only_in_selection():
    out = diff_counts({}, {"new": 4})
    assert out["added"] == {"new": 4}
    assert out["removed"] == out["changed"] == out["unchanged"] == {}


def test_removed_is_present_only_in_baseline():
    """"Absent" means absent, not zero — a terms aggregation emits no zero buckets,
    so a missing value genuinely did not occur in that window."""
    out = diff_counts({"gone": 9}, {})
    assert out["removed"] == {"gone": 9}
    assert out["added"] == {}


def test_changed_carries_delta_and_percent():
    out = diff_counts({"x": 100}, {"x": 250})
    assert out["changed"]["x"] == {
        "baseline": 100,
        "selection": 250,
        "delta": 150,
        "pct_change": 150.0,
    }


def test_equal_counts_are_unchanged_not_changed():
    """Boundary between `changed` and `unchanged`: the on point is equality."""
    out = diff_counts({"x": 7}, {"x": 7})
    assert out["unchanged"] == {"x": {"baseline": 7, "selection": 7}}
    assert out["changed"] == {}


def test_off_point_by_one_is_changed():
    """One either side of equality lands in `changed`, with the sign preserved."""
    assert diff_counts({"x": 7}, {"x": 8})["changed"]["x"]["delta"] == 1
    assert diff_counts({"x": 7}, {"x": 6})["changed"]["x"]["delta"] == -1


def test_changed_is_sorted_by_absolute_delta_descending():
    """The caller is an agent choosing what to investigate first, so the largest
    movement in either direction must come first. A drop of 5,000 matters as much as
    a rise of 5,000, and sorting by percentage would rank 1→3 above 4000→9000."""
    out = diff_counts(
        {"small": 10, "big_drop": 6000, "big_rise": 100},
        {"small": 12, "big_drop": 1000, "big_rise": 4000},
    )
    assert list(out["changed"]) == ["big_drop", "big_rise", "small"]
    assert out["changed"]["big_drop"]["delta"] == -5000
    assert out["changed"]["big_rise"]["delta"] == 3900


def test_diff_counts_does_not_mutate_its_inputs():
    baseline, selection = {"a": 1}, {"a": 2}
    diff_counts(baseline, selection)
    assert baseline == {"a": 1} and selection == {"a": 2}


def test_two_empty_windows_yield_four_empty_groups():
    out = diff_counts({}, {})
    assert out == {"added": {}, "removed": {}, "changed": {}, "unchanged": {}}


# ── compare_windows: the whole tool body ──────────────────────────────────────


def test_compare_windows_diffs_and_reports_both_warnings():
    out = compare_windows(
        {"a": 10, "gone": 1, "_warning": "baseline capped"},
        {"a": 25, "new": 2, "_warning": "selection capped"},
    )
    assert out["changed"]["a"]["pct_change"] == 150.0
    assert out["added"] == {"new": 2}
    assert out["removed"] == {"gone": 1}
    assert out["baseline_warning"] == "baseline capped"
    assert out["selection_warning"] == "selection capped"


def test_warnings_are_none_when_neither_window_warned():
    out = compare_windows({"a": 1}, {"a": 1})
    assert out["baseline_warning"] is None
    assert out["selection_warning"] is None


def test_a_warning_never_leaks_into_the_diff_groups():
    """The regression that matters: if `_warning` reached `diff_counts`, it would be
    reported as an added or removed field value, and an analyst would go hunting for
    a source IP called "_warning"."""
    out = compare_windows({"a": 1}, {"a": 1, "_warning": "w"})
    for group in ("added", "removed", "changed", "unchanged"):
        assert "_warning" not in out[group]


def test_compare_windows_returns_exactly_the_documented_keys():
    """The tool docstring promises these six keys; the shape is the contract."""
    out = compare_windows({"a": 1}, {"b": 2})
    assert set(out) == {
        "added",
        "removed",
        "changed",
        "unchanged",
        "baseline_warning",
        "selection_warning",
    }
