"""_coerce_bool — env-var / config truthiness.

Spec (step 1): `_coerce_bool(value, default=True) -> bool`. Used to turn
`OPENSEARCH_VERIFY_SSL` (always a string when it comes from the environment) or
a JSON config value (a real bool) into a bool.

The rule the code actually implements — three disjoint branches, verified by
execution:

  1. `isinstance(value, str)`  -> `value.lower() != "false"`
  2. `isinstance(value, bool)` -> `value`
  3. anything else            -> `default`, and **the value is discarded**

Two things worth flagging, both asserted below:

  * Branch 1 is an *equality* boundary on the single literal "false". Every other
    string is True — including "0", "no", "off", "" and "false " with a trailing
    space. For a flag that disables TLS verification this fails *open* (secure
    direction) which is the right way round, but "no"/"0" silently not working
    is a foot-gun.
  * Branch 3 ignores the value entirely, so `_coerce_bool(0)` is **True** and
    `_coerce_bool([])` is **True** under the default. Python truthiness is not
    consulted anywhere.
"""

import pytest

from lib.client import _coerce_bool

# ── Branch 1: strings. Equality boundary on the literal "false". ──────────────


@pytest.mark.parametrize("value", ["false", "FALSE", "False", "FaLsE"])
def test_string_false_is_the_only_falsey_string_case_insensitive(value):
    """On point of the equality: `value.lower() == "false"`."""
    assert _coerce_bool(value) is False


@pytest.mark.parametrize(
    "value",
    [
        "fals",  # off point: one char short
        "falsee",  # off point: one char long
        "false ",  # off point: trailing whitespace, NOT trimmed
        " false",  # off point: leading whitespace, NOT trimmed
    ],
)
def test_strings_adjacent_to_false_are_true(value):
    """Two off points either side of an equality boundary, plus the whitespace
    variants that a hand-edited .env file actually produces."""
    assert _coerce_bool(value) is True


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on", "anything"])
def test_ordinary_truthy_strings_are_true(value):
    assert _coerce_bool(value) is True


@pytest.mark.parametrize("value", ["0", "no", "off", "", "null", "none"])
def test_conventional_falsey_spellings_are_nonetheless_true(value):
    """Recorded, not endorsed: only the exact word "false" turns the flag off."""
    assert _coerce_bool(value) is True


@pytest.mark.parametrize("default", [True, False])
def test_default_is_ignored_for_any_string(default):
    """A string never reaches the default — branch 1 always returns."""
    assert _coerce_bool("false", default=default) is False
    assert _coerce_bool("anything", default=default) is True


# ── Branch 2: real bools pass through. ────────────────────────────────────────


@pytest.mark.parametrize("default", [True, False])
def test_bool_passes_through_regardless_of_default(default):
    assert _coerce_bool(True, default=default) is True
    assert _coerce_bool(False, default=default) is False


# ── Branch 3: everything else returns the default, value discarded. ───────────


@pytest.mark.parametrize("value", [None, 0, 1, -1, 0.0, [], {}, object()])
def test_non_str_non_bool_returns_the_default_ignoring_truthiness(value):
    """`0`, `[]` and `None` are True under the default. Python truthiness is
    never consulted, so these are all indistinguishable to the function."""
    assert _coerce_bool(value, default=True) is True
    assert _coerce_bool(value, default=False) is False


def test_default_default_is_true():
    """The declared default matters: an absent config key means verify_ssl=True,
    i.e. TLS verification stays on unless explicitly disabled."""
    assert _coerce_bool(None) is True


def test_bool_is_checked_after_str_but_bool_is_not_a_str_so_order_is_harmless():
    """`bool` subclasses `int`, not `str`, so branch 1 cannot shadow branch 2.

    Guard against a future reorder that puts an `int` check before the `bool`
    check, which would silently reroute True/False into branch 3.
    """
    assert _coerce_bool(True) is True
    assert _coerce_bool(False) is False
