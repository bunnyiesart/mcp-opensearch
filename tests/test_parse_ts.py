"""_parse_ts — ISO 8601 string to Unix epoch float.

Spec (step 1): accepts three strptime formats after stripping a trailing "Z":
    %Y-%m-%dT%H:%M:%S.%f   %Y-%m-%dT%H:%M:%S   %Y-%m-%d
Anything else raises ValueError. There is no timezone parsing: the value is
*assumed* UTC and stamped with `tzinfo=timezone.utc`.

Recorded behaviour (verified by execution, not assumed): a numeric UTC offset
such as "+00:00" is NOT accepted — it raises ValueError. See the offset tests
below; they assert the raise deliberately, because the failure is loud and
correct-by-contract for a function documented as UTC-only. Callers passing
`datetime.isoformat()` output (which emits "+00:00", not "Z") will hit it.
"""

import pytest

from lib.client import OpenSearchClient

parse = OpenSearchClient._parse_ts

# 2024-01-01T00:00:00Z
EPOCH_2024 = 1704067200.0


# ── The three accepted formats ────────────────────────────────────────────────


def test_parse_ts_date_only():
    assert parse("2024-01-01") == EPOCH_2024


def test_parse_ts_seconds_precision_with_z():
    assert parse("2024-01-01T00:00:00Z") == EPOCH_2024


def test_parse_ts_seconds_precision_without_z():
    """The trailing Z is optional — rstrip is a no-op when it is absent."""
    assert parse("2024-01-01T00:00:00") == EPOCH_2024


def test_parse_ts_microsecond_precision_with_z():
    assert parse("2024-01-01T00:00:00.123456Z") == pytest.approx(EPOCH_2024 + 0.123456)


def test_parse_ts_microsecond_precision_without_z():
    assert parse("2024-01-01T00:00:00.123456") == pytest.approx(EPOCH_2024 + 0.123456)


def test_parse_ts_millisecond_precision_is_accepted_by_the_f_directive():
    """%f accepts 1-6 digits, so OpenSearch's 3-digit millis parse fine."""
    assert parse("2024-01-01T00:00:00.500Z") == pytest.approx(EPOCH_2024 + 0.5)


def test_parse_ts_result_is_utc_not_local():
    """Regression guard: naive strptime + .timestamp() would use local time.

    The implementation stamps tzinfo=utc first, so the epoch value is stable
    regardless of the machine's TZ. A fixed expected constant proves it.
    """
    assert parse("1970-01-01T00:00:00Z") == 0.0


# ── Timezone offsets: verified NOT supported ──────────────────────────────────


@pytest.mark.parametrize(
    ("ts", "expected"),
    [
        ("2024-01-01T00:00:00+00:00", EPOCH_2024),
        ("2024-01-01T00:00:00.000+00:00", EPOCH_2024),
        ("2024-01-01T00:00:00+0000", EPOCH_2024),
        # A real offset must shift the instant, not merely be tolerated: 00:00 at
        # UTC-03:00 is 03:00 UTC.
        ("2024-01-01T00:00:00-03:00", EPOCH_2024 + 3 * 3600),
    ],
)
def test_parse_ts_accepts_numeric_utc_offsets(ts, expected):
    """Numeric offsets must parse, and must be applied rather than ignored.

    The old parser carried no `%z` in any of its three formats, so every offset
    raised — including "+00:00", which is exactly what
    `datetime.now(timezone.utc).isoformat()` emits. So the most natural way for a
    caller to express "now, in UTC" was rejected by the one tool that validated
    timestamps, while `search`/`count`/`terms` forwarded the same string to the
    cluster and accepted it. Timestamps have to be portable between tools that all
    document the same contract.
    """
    assert parse(ts) == expected


# ── Rejections and the rstrip quirk ───────────────────────────────────────────


@pytest.mark.parametrize(
    "ts",
    [
        "",
        "not-a-timestamp",
        "2024-13-01",  # month off the end of the valid partition
        "2024-01-01 00:00:00",  # space separator instead of T
        "1704067200",  # raw epoch
        "now-1h",  # OpenSearch date math
    ],
)
def test_parse_ts_rejects_unparseable_input(ts):
    with pytest.raises(ValueError, match="Cannot parse timestamp"):
        parse(ts)


def test_parse_ts_error_message_shows_the_value_the_caller_passed():
    """The message must quote the input byte-for-byte. The old parser stripped the
    "Z" before building the message, so an analyst debugging a rejected timestamp
    saw a value that was not the one they sent — the worst property an error
    message can have."""
    with pytest.raises(ValueError) as exc:
        parse("garbageZ")
    assert "'garbageZ'" in str(exc.value)


@pytest.mark.parametrize(
    "malformed",
    [
        "2024-01-01T00:00:00Z+00:00",   # trailing Z *and* a numeric offset
        "2024-01-01T00:00:00Z-03:00",
        "2024-01-01TZ00:00:00",         # zone marker not at the end
    ],
)
def test_parse_ts_rejects_two_zone_designators(malformed):
    """A zone may be given once, at the end — a trailing `Z` or a numeric offset,
    never both.

    This is enforced explicitly rather than delegated to `datetime.fromisoformat`,
    whose accepted grammar widened in Python 3.11: these strings were accepted on
    3.10 and rejected on 3.11+, so the parser's contract depended on the interpreter.
    CI caught it — the 3.10 job passed while 3.11, 3.12 and 3.13 failed — which is
    the argument for testing the declared floor and not only the newest version. The
    same server must not accept different timestamps on different Pythons.
    """
    with pytest.raises(ValueError, match="Cannot parse timestamp"):
        parse(malformed)


def test_parse_ts_rejects_repeated_z_suffix():
    """`ts.rstrip("Z")` was character-set stripping, not suffix removal, so a
    malformed "...ZZZ" was silently accepted as though it were a single Z. Accepting
    input no producer emits hides typos instead of reporting them."""
    with pytest.raises(ValueError, match="Cannot parse timestamp"):
        parse("2024-01-01T00:00:00ZZZ")


def test_parse_ts_accepts_lowercase_z():
    """ISO 8601 is case-insensitive on the zone designator. Only uppercase Z was in
    the old strip set, so "z" reached strptime and failed — an arbitrary distinction
    the caller has no way to predict from the documented contract."""
    assert parse("2024-01-01T00:00:00z") == EPOCH_2024


@pytest.mark.parametrize("bad", [None, 12345, "", "   "])
def test_parse_ts_non_string_input_raises_value_error(bad):
    """ValueError is the only exception type allowed to escape this helper.

    `ts.rstrip` on None used to raise AttributeError, which a caller guarding
    `except ValueError` does not catch — the root cause of the
    histogram-with-a-None-bound crash in test_histogram_buckets.py. The exception
    class a helper raises is part of its contract, not an implementation detail."""
    with pytest.raises(ValueError, match="Cannot parse timestamp"):
        parse(bad)
