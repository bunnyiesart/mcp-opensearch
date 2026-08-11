"""_check_histogram_buckets — local guard against bucket explosions.

Spec (step 1): given from_ts, to_ts and an interval string, raise ValueError if
the histogram would produce more than `self.max_histogram_buckets` buckets.
"auto" is exempt (OpenSearch caps auto_date_histogram itself).

  expected = int(max(t1 - t0, 0) / interval_secs) + 1
  raise if expected > self.max_histogram_buckets

Boundary analysis (step 4). The condition is a strict `>`, so:
  on point  = expected == limit      -> allowed
  off point = expected == limit + 1  -> raises
Both are exercised against a small injected limit (readable arithmetic) and
against the production default of 2000 (proves the real constant is wired up).
"""

import re
from datetime import datetime, timedelta, timezone

import pytest

from lib.client import (
    _INTERVAL_RE,
    _INTERVAL_SECONDS,
    MAX_HISTOGRAM_BUCKETS,
    OpenSearchClient,
)

from .conftest import make_client


@pytest.fixture
def tiny_client():
    """max_histogram_buckets=10, so the boundary is 10 vs 11 buckets."""
    return make_client(max_histogram_buckets=10)


def _plus_seconds(seconds):
    """An ISO timestamp `seconds` after 2024-01-01T00:00:00."""
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return (base + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S")


# ── The bucket-count boundary ─────────────────────────────────────────────────


def test_on_point_exactly_at_the_limit_is_allowed(tiny_client):
    """9 minutes at 1m => int(540/60)+1 == 10 buckets == the limit."""
    tiny_client._check_histogram_buckets(
        "2024-01-01T00:00:00", "2024-01-01T00:09:00", "1m"
    )


def test_off_point_one_bucket_over_the_limit_raises(tiny_client):
    """10 minutes at 1m => int(600/60)+1 == 11 buckets == limit + 1."""
    with pytest.raises(ValueError, match="Too many buckets"):
        tiny_client._check_histogram_buckets(
            "2024-01-01T00:00:00", "2024-01-01T00:10:00", "1m"
        )


def test_default_limit_on_point_2000_buckets_is_allowed(client):
    """1999 minutes at 1m => exactly MAX_HISTOGRAM_BUCKETS buckets."""
    assert client.max_histogram_buckets == MAX_HISTOGRAM_BUCKETS
    end = 1999 * 60
    client._check_histogram_buckets(
        "2024-01-01T00:00:00", _plus_seconds(end), "1m"
    )


def test_default_limit_off_point_2001_buckets_raises(client):
    end = 2000 * 60
    with pytest.raises(ValueError, match=r"~2,001 expected"):
        client._check_histogram_buckets("2024-01-01T00:00:00", _plus_seconds(end), "1m")


def test_single_bucket_when_from_equals_to(tiny_client):
    """Degenerate range: expected == 1, well inside the limit."""
    tiny_client._check_histogram_buckets(
        "2024-01-01T00:00:00", "2024-01-01T00:00:00", "1m"
    )


def test_error_message_names_the_range_the_interval_and_the_limit(tiny_client):
    with pytest.raises(ValueError) as exc:
        tiny_client._check_histogram_buckets(
            "2024-01-01T00:00:00", "2024-01-01T01:00:00", "1m"
        )
    msg = str(exc.value)
    assert "2024-01-01T00:00:00" in msg
    assert "2024-01-01T01:00:00" in msg
    assert "interval 1m" in msg
    assert "Limit is 10" in msg


# ── "auto" is exempt ──────────────────────────────────────────────────────────


def test_auto_interval_skips_the_check_entirely(tiny_client):
    """Returns before parsing the timestamps, so it tolerates a range that would
    otherwise be rejected — and even unparseable bounds."""
    tiny_client._check_histogram_buckets("2020-01-01", "2030-01-01", "auto")
    tiny_client._check_histogram_buckets("garbage", "garbage", "auto")


# ── Interval syntax ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("interval", ["1s", "30s", "1m", "15m", "1h", "1d", "7d"])
def test_valid_fixed_intervals_are_accepted(client, interval):
    """A 10-minute range keeps every one of these inside the 2000-bucket limit,
    so the only thing under test is the interval syntax."""
    client._check_histogram_buckets("2024-01-01T00:00:00", "2024-01-01T00:10:00",
                                    interval)


@pytest.mark.parametrize(
    "interval",
    [
        "",
        "auto ",  # off point of the `== "auto"` equality
        "Auto",  # case-sensitive, so this is NOT the auto path
        "1",  # unit missing
        "h",  # magnitude missing
        "5x",  # unknown unit
        "1.5h",  # non-integer magnitude
        "-1h",  # sign not in the pattern
        " 1h",  # anchored regex rejects surrounding whitespace
        "1h ",
    ],
)
def test_malformed_intervals_are_rejected_with_actionable_text(client, interval):
    with pytest.raises(ValueError, match="Invalid interval"):
        client._check_histogram_buckets("2024-01-01", "2024-01-02", interval)


def test_zero_magnitude_interval_is_rejected_as_a_value_error(client):
    """"0m" satisfies _INTERVAL_RE (`\\d+` matches "0") and used to reach
    `range_secs / interval_secs`, escaping as ZeroDivisionError — an exception class
    a validation helper should never emit, and one no caller thinks to catch. A
    zero-width bucket is a caller error, so it must surface as ValueError like every
    other malformed interval."""
    with pytest.raises(ValueError):
        client._check_histogram_buckets("2024-01-01", "2024-01-02", "0m")


def test_every_unit_accepted_by_the_regex_has_a_seconds_entry():
    """Structural guard: a unit accepted by _INTERVAL_RE but missing from
    _INTERVAL_SECONDS would be a KeyError at runtime, not a ValueError.

    The unit set is read out of the pattern rather than hardcoded, so this stays
    honest if the calendar-unit bug below is fixed by shrinking the regex.
    """
    units = re.search(r"\[([^\]]+)\]", _INTERVAL_RE.pattern).group(1)
    for unit in units:
        assert unit in _INTERVAL_SECONDS, unit


# ── Reversed range ────────────────────────────────────────────────────────────


def test_reversed_range_is_rejected(tiny_client):
    """`max(t1 - t0, 0)` used to turn to_ts < from_ts into a one-bucket histogram,
    so the guard passed and the query was dispatched. The analyst got an empty
    result with no explanation — a transposed pair of bounds is a mistake worth
    naming, not a plausible-looking answer."""
    with pytest.raises(ValueError, match="Reversed time range"):
        tiny_client._check_histogram_buckets(
            "2024-06-01T00:00:00", "2024-01-01T00:00:00", "1m"
        )


# ── Unparseable bounds ────────────────────────────────────────────────────────


def test_unparseable_bounds_are_rewrapped_as_value_error(client):
    with pytest.raises(ValueError, match="Cannot compute bucket count"):
        client._check_histogram_buckets("yesterday", "today", "1h")


def test_missing_bounds_are_rewrapped_as_value_error(client):
    """The `try` around `_parse_ts` caught only ValueError, but `_parse_ts(None)`
    raised AttributeError from `None.rstrip` — so a None bound escaped as a bare
    AttributeError, bypassing the "Cannot compute bucket count" translation, and
    any caller doing `except ValueError` to build a friendly message missed it
    entirely. The exception class a helper raises is part of its contract."""
    with pytest.raises(ValueError, match="Cannot compute bucket count"):
        client._check_histogram_buckets(None, None, "1h")


# ── Calendar units are routed, not rejected ───────────────────────────────────


@pytest.mark.parametrize("interval", ["1w", "1M", "1y"])
def test_calendar_units_with_multiplier_one_are_accepted(client, interval):
    """w/M/y used to pass local validation and then be placed in `fixed_interval`,
    which OpenSearch accepts only for ms/s/m/h/d — so the cluster returned 400 and
    `_raise_for_status` rewrote it as the misleading "Bad request: check your query
    syntax or field names".

    Rejecting them locally was the cheaper fix; routing them is the correct one,
    because a weekly or monthly histogram is a legitimate thing for an analyst to
    ask for. These now reach `calendar_interval`."""
    client._check_histogram_buckets("2024-01-01", "2024-03-01", interval)


@pytest.mark.parametrize("interval", ["2w", "3M", "2y"])
def test_calendar_units_reject_multipliers_above_one(client, interval):
    """`calendar_interval` accepts a multiplier of exactly 1 — "2w" is not a thing
    OpenSearch will parse. Caught locally with an explanatory message rather than
    left to become another opaque 400."""
    with pytest.raises(ValueError, match="calendar"):
        client._check_histogram_buckets("2024-01-01", "2024-03-01", interval)


@pytest.mark.parametrize(
    ("unit", "expected"),
    [("s", "fixed_interval"), ("m", "fixed_interval"), ("h", "fixed_interval"),
     ("d", "fixed_interval"), ("w", "calendar_interval"),
     ("M", "calendar_interval"), ("y", "calendar_interval")],
)
def test_every_unit_routes_to_the_aggregation_key_opensearch_accepts(unit, expected):
    """The routing table is the whole point of the fix, so pin it per unit. `d` is
    the boundary that matters: it is the largest unit `fixed_interval` accepts, and
    `w` is the smallest that requires `calendar_interval`."""
    assert OpenSearchClient._interval_agg_key(unit) == expected
