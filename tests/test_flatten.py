"""_flatten_doc and _flatten_mapping — pure, no I/O.

Spec (step 1):
  _flatten_doc(doc)     -> {dotted_field_name: python_type_name}
  _flatten_mapping(prop) -> {dotted_field_name: opensearch_type}

Partitions explored (step 3): empty input; single scalar; nested one level;
nested two levels; every scalar leaf type; a dict leaf that is itself empty.
"""

import pytest

from lib.client import _flatten_doc

# ── _flatten_doc ──────────────────────────────────────────────────────────────


def test_flatten_doc_empty_dict_yields_empty_dict():
    assert _flatten_doc({}) == {}


def test_flatten_doc_flat_scalars_report_type_names():
    doc = {"a": 1, "b": "s", "c": 1.5, "d": True, "e": None, "f": [1, 2]}
    assert _flatten_doc(doc) == {
        "a": "int",
        "b": "str",
        "c": "float",
        "d": "bool",
        "e": "NoneType",
        "f": "list",
    }


def test_flatten_doc_nests_with_dot_notation():
    doc = {"agent": {"name": "web01", "id": 3}}
    assert _flatten_doc(doc) == {"agent.name": "str", "agent.id": "int"}


def test_flatten_doc_recurses_more_than_one_level():
    doc = {"data": {"win": {"eventdata": {"logonType": "3"}}}}
    assert _flatten_doc(doc) == {"data.win.eventdata.logonType": "str"}


def test_flatten_doc_empty_nested_dict_disappears_entirely():
    """A dict leaf is recursed into, never typed — so `{}` contributes nothing.

    This is the interesting boundary: the field exists in the document but is
    absent from the output, so discover_fields cannot report it.
    """
    assert _flatten_doc({"a": {}, "b": 1}) == {"b": "int"}


def test_flatten_doc_recurses_into_a_list_of_dicts():
    """An array of objects must expose its element fields, not just its own type.

    Reporting only `rule.mitre: list` told the agent that `rule.mitre.id` did not
    exist — and arrays of objects are exactly where SOC data lives (`rule.mitre.*`,
    Windows eventdata). The array itself is still typed, so the shape is additive:
    the caller learns both that it is a list and what is inside it."""
    doc = {"rule": {"mitre": [{"id": "T1059"}]}}
    assert _flatten_doc(doc) == {"rule.mitre": "list", "rule.mitre.id": "str"}


@pytest.mark.parametrize("value", [[1, 2], []])
def test_flatten_doc_list_of_non_dicts_stays_a_leaf(value):
    """Only object elements carry sub-fields worth discovering. A list of scalars —
    and the empty list, where there is nothing to inspect — must not invent any."""
    assert _flatten_doc({"a": value}) == {"a": "list"}


def test_flatten_doc_prefix_argument_is_honoured():
    assert _flatten_doc({"x": 1}, prefix="root") == {"root.x": "int"}


# ── _flatten_mapping ──────────────────────────────────────────────────────────


def test_flatten_mapping_empty_properties(client):
    assert client._flatten_mapping({}) == {}


def test_flatten_mapping_leaf_types(client):
    props = {"level": {"type": "long"}, "id": {"type": "keyword"}}
    assert client._flatten_mapping(props) == {"level": "long", "id": "keyword"}


def test_flatten_mapping_field_with_no_type_defaults_to_object(client):
    """A pure container node carries `properties` but no `type` key."""
    props = {"agent": {"properties": {"name": {"type": "keyword"}}}}
    assert client._flatten_mapping(props) == {
        "agent": "object",
        "agent.name": "keyword",
    }


def test_flatten_mapping_node_with_both_type_and_properties_keeps_its_type(client):
    props = {"alerts": {"type": "nested", "properties": {"id": {"type": "long"}}}}
    assert client._flatten_mapping(props) == {"alerts": "nested", "alerts.id": "long"}


def test_flatten_mapping_recurses_two_levels(client):
    props = {
        "data": {
            "properties": {
                "win": {"properties": {"eventdata": {"type": "object"}}},
            }
        }
    }
    assert client._flatten_mapping(props) == {
        "data": "object",
        "data.win": "object",
        "data.win.eventdata": "object",
    }


def test_flatten_mapping_reveals_multifields_under_fields_key(client):
    """`fields` sub-mappings — the `.keyword` companions — must be listed.

    This closes a loop that was otherwise unclosable: `terms()` warns "try
    'rule.description.keyword'", the agent calls `get_mapping` to confirm it exists,
    and while only `properties` was recursed the answer was "no such field". The
    server was advising a field it could not show you.
    """
    props = {
        "rule": {
            "properties": {
                "description": {
                    "type": "text",
                    "fields": {"keyword": {"type": "keyword", "ignore_above": 256}},
                }
            }
        }
    }
    assert client._flatten_mapping(props) == {
        "rule": "object",
        "rule.description": "text",
        "rule.description.keyword": "keyword",
    }


def test_flatten_mapping_prefix_argument_is_honoured(client):
    assert client._flatten_mapping({"x": {"type": "long"}}, prefix="root") == {
        "root.x": "long"
    }


@pytest.mark.parametrize("bad_type", ["", "unknown_type"])
def test_flatten_mapping_passes_through_whatever_type_string_it_finds(client, bad_type):
    """No validation of the type vocabulary — it is a pass-through."""
    assert client._flatten_mapping({"f": {"type": bad_type}}) == {"f": bad_type}
