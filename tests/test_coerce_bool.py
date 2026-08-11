"""_coerce_bool — env-var / config truthiness.

Spec: `_coerce_bool(value, default=True, *, name="value") -> bool`. Turns
`OPENSEARCH_VERIFY_SSL` (always a string when it comes from the environment),
`OPENSEARCH_ALLOW_INSECURE_CONFIG`, or a JSON config value (a real bool) into a
bool.

**This file previously pinned a defect** (backlog T9), which is why it changed.
The old implementation was three disjoint branches:

  1. `isinstance(value, str)`  -> `value.lower() != "false"`
  2. `isinstance(value, bool)` -> `value`
  3. anything else            -> `default`, value discarded

so branch 1 was an *equality* boundary on the single literal "false" — `"0"`,
`"no"`, `"off"`, `""` and `"false "` (the trailing space a hand-edited .env
produces) were all **True** — and branch 3 ignored the value, making
`_coerce_bool(0)` True as well. `OPENSEARCH_VERIFY_SSL=0` therefore did not
disable TLS verification and said nothing about it. Failing in the secure
direction is the right way round; failing *silently* is the foot-gun.

The rule now, and the three boundaries it is tested on:

  * **membership, not equality** — the conventional falsey set is accepted, so
    "0"/"no"/"off"/"n"/"f"/"false" all disable, and "1"/"yes"/"on"/"y"/"t"/"true"
    all enable;
  * **whitespace and case are stripped** before the lookup;
  * **ambiguity is refused, not guessed** — anything outside both sets raises
    ValueError naming the variable and the accepted spellings, rather than
    resolving to `default` and leaving the operator with a flag that appears to
    be ignored.

"Unset" is spelled two ways and both yield `default`: `None`, and an empty or
whitespace-only string — because `OPENSEARCH_VERIFY_SSL=` is how every shell
writes "unset", and turning TLS verification off must be deliberate.
"""

import pytest

from lib.client import _coerce_bool

# ── Falsey spellings: a set, no longer the single literal "false". ─────────────


@pytest.mark.parametrize(
    "value", ["false", "FALSE", "False", "FaLsE", "0", "no", "NO", "off", "n", "f"]
)
def test_conventional_falsey_spellings_are_false(value):
    """The T9 fix. "0", "no" and "off" used to be True."""
    assert _coerce_bool(value) is False


@pytest.mark.parametrize("value", ["true", "TRUE", "True", "1", "yes", "on", "y", "t"])
def test_conventional_truthy_spellings_are_true(value):
    assert _coerce_bool(value) is True


# ── Whitespace is stripped on both sides of the boundary. ─────────────────────


@pytest.mark.parametrize("value", ["false ", " false", "\tfalse\n", " off ", " 0 "])
def test_surrounding_whitespace_is_stripped(value):
    """What a hand-edited .env actually produces. These used to be True."""
    assert _coerce_bool(value) is False


@pytest.mark.parametrize("value", [" true", "true\n", " yes "])
def test_surrounding_whitespace_is_stripped_on_the_truthy_side_too(value):
    assert _coerce_bool(value) is True


# ── Ambiguity is refused rather than silently defaulted. ─────────────────────


@pytest.mark.parametrize("value", ["fals", "falsee", "maybe", "null", "none", "2", "-1", "truthy"])
def test_ambiguous_strings_raise_instead_of_defaulting(value):
    """Two off points either side of the old equality boundary, plus the words a
    user might reasonably try. Returning `default` here is what made a mistyped
    flag look like a flag that does not work."""
    with pytest.raises(ValueError, match="as a boolean"):
        _coerce_bool(value)


def test_the_error_names_the_variable_and_the_accepted_spellings():
    """The message is the whole point of raising: it has to be fixable without
    reading the source."""
    with pytest.raises(ValueError) as raised:
        _coerce_bool("maybe", name="OPENSEARCH_VERIFY_SSL")
    message = str(raised.value)
    assert "OPENSEARCH_VERIFY_SSL" in message
    assert "'maybe'" in message
    assert "true/false" in message


@pytest.mark.parametrize("default", [True, False])
def test_an_ambiguous_string_raises_whatever_the_default_is(default):
    """The default is for *absent* input, never for uninterpretable input."""
    with pytest.raises(ValueError):
        _coerce_bool("maybe", default=default)


@pytest.mark.parametrize("value", [-1, 2, 0.0, 1.0, [], {}, object(), ("a",)])
def test_non_boolean_types_and_out_of_range_numbers_raise(value):
    """Branch 3 used to discard these and return the default, so `_coerce_bool(0)`
    was True and `_coerce_bool([])` was True. Python truthiness is still not
    consulted — `[]` is not "false", it is "not a boolean"."""
    with pytest.raises(ValueError):
        _coerce_bool(value)


# ── Recognised non-string forms. ──────────────────────────────────────────────


@pytest.mark.parametrize("default", [True, False])
def test_bool_passes_through_regardless_of_default(default):
    assert _coerce_bool(True, default=default) is True
    assert _coerce_bool(False, default=default) is False


def test_the_integers_0_and_1_are_accepted():
    """A JSON config file can carry 0/1 where a bool was meant; both are
    unambiguous, so they are read rather than refused."""
    assert _coerce_bool(0) is False
    assert _coerce_bool(1) is True


def test_bool_is_checked_before_int_so_true_does_not_take_the_int_path():
    """`bool` subclasses `int`, so the order of those two checks is load-bearing:
    an `int` check placed first would still return the right answer here, but a
    future `value in (0, 1)` guard would reject `True` outright."""
    assert _coerce_bool(True) is True
    assert _coerce_bool(False) is False
    assert isinstance(_coerce_bool(1), bool)


# ── "Unset" — the only route to the default. ──────────────────────────────────


@pytest.mark.parametrize("value", [None, "", "   ", "\n"])
@pytest.mark.parametrize("default", [True, False])
def test_absent_input_yields_the_default(value, default):
    """`OPENSEARCH_VERIFY_SSL=` is how a shell spells "unset", and an unset flag
    must not disable TLS verification."""
    assert _coerce_bool(value, default=default) is default


def test_default_default_is_true():
    """The declared default matters: an absent config key means verify_ssl=True,
    i.e. TLS verification stays on unless explicitly disabled."""
    assert _coerce_bool(None) is True
    assert _coerce_bool("") is True
