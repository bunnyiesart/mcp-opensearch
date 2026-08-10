"""_is_likely_text_field — heuristic that drives the fielddata warning.

Spec (step 1): given a dotted field name, return True when the name *suggests*
an analyzed `text` mapping, so `terms()` / `multi_terms()` can warn the user to
aggregate on `<field>.keyword` instead.

Implementation: take the last dot-segment, lowercase it, and return True if any
of `_TEXT_FIELD_HINTS` appears as a **substring**.

KNOWN BUG — substring matching, not token matching. Short hints (notably "log")
appear inside unrelated keyword field names, so the warning fires on fields that
are perfectly safe to aggregate. The `xfail(strict=True)` tests below assert the
*correct* behaviour, so they convert to passes the moment the heuristic is fixed
and cannot silently rot if someone "fixes" it halfway.
"""

import pytest

from lib import client as client_module
from lib.client import _is_likely_text_field

# ── Currently correct: true positives ─────────────────────────────────────────


@pytest.mark.parametrize(
    "field",
    [
        "rule.description",
        "full_log",
        "data.message",
        "syscheck.audit.process.name.text",
        "alert.summary",
        "rule.reason",
        "command.output",
        "analyst.comment",
        "event.detail",
        "page.content",
    ],
)
def test_analyzed_text_fields_are_flagged(field):
    assert _is_likely_text_field(field) is True


def test_every_hint_matches_itself_as_a_whole_segment():
    """Sanity floor: each configured hint must at minimum flag itself.

    The hint list is read off the module (not import-bound) so this test tracks
    whatever the hints become when the substring bug below is fixed.
    """
    for hint in client_module._TEXT_FIELD_HINTS:
        assert _is_likely_text_field(hint) is True, hint


# ── Currently correct: true negatives ─────────────────────────────────────────


@pytest.mark.parametrize(
    "field",
    [
        "agent.name",
        "data.srcip",
        "rule.level",
        "rule.id",
        "@timestamp",
        "manager.name",
        "GeoLocation.country_name",
    ],
)
def test_keyword_fields_are_not_flagged(field):
    assert _is_likely_text_field(field) is False


def test_only_the_last_segment_is_examined():
    """`description` in a parent segment must not leak into the decision."""
    assert _is_likely_text_field("description.id") is False
    assert _is_likely_text_field("rule.description") is True


def test_keyword_multifield_suffix_suppresses_the_warning():
    """`rule.description.keyword` ends in `keyword`, so it is correctly silent —
    this is what makes the warning's own remediation advice self-consistent."""
    assert _is_likely_text_field("rule.description.keyword") is False


def test_matching_is_case_insensitive():
    assert _is_likely_text_field("rule.DESCRIPTION") is True


def test_empty_field_name_is_not_flagged():
    assert _is_likely_text_field("") is False


# ── KNOWN BUG: substring false positives ──────────────────────────────────────
#
# Each of these is a real Wazuh/ECS keyword field. All four are flagged today
# because a hint occurs as a substring of the last segment:
#
#   logonType        -> contains "log"
#   logonProcessName -> contains "log"
#   catalogId        -> contains "log"   (cata-LOG-Id)
#   logger           -> contains "log"
#
# The warning tells the analyst to switch to `<field>.keyword`, which for a
# keyword field either does not exist or is a no-op — so the advice is wrong and
# erodes trust in every other warning the server emits.


@pytest.mark.parametrize(
    "field",
    [
        "data.win.eventdata.logonType",
        "win.eventdata.logonProcessName",
        "event.catalogId",
        "app.logger",
    ],
)
def test_keyword_fields_containing_log_must_not_be_flagged(field):
    """Regression guard: the hint 'log' must not fire inside an unrelated keyword
    name. Substring matching used to flag all four of these, telling the analyst to
    append `.keyword` to a field that has no such sub-field. Token matching on the
    last dotted segment (camelCase and separators both split) is what fixes it."""
    assert _is_likely_text_field(field) is False


# ── The matching false negative ───────────────────────────────────────────────


def test_command_line_should_be_flagged_as_text():
    """The other half of the same fix. `process.commandline` is analyzed in practice
    and is a common cause of fielddata pressure, but no hint covered it while the
    over-eager 'log' hint was firing on keyword fields. Note this must hold for all
    three spellings — see the de-separated-token case below."""
    assert _is_likely_text_field("process.commandline") is True
