"""timeline — entity-centric event view built on top of search_string.

Spec (step 1): match `entity` against any of `fields` (OR), oldest-first, and
return {"total", "entity", "fields", "events"} plus any warning search_string
raised.

The interesting logic is string construction: an OR of quoted phrase clauses,
with the entity's own quotes escaped. Partitions across `fields` length
(0 / 1 / many) and across entity content (plain / quoted / backslashed).
"""

import pytest

from lib.client import _NO_TIME_RANGE_WARNING

from .conftest import search_response

BOUNDED = {"from_ts": "2024-01-01", "to_ts": "2024-01-02"}


def sent_query(rec):
    """The Lucene string that ended up in the request."""
    q = rec.body["query"]
    if "bool" in q:
        return q["bool"]["must"]["query_string"]["query"]
    return q["query_string"]["query"]


# ── OR-clause construction ────────────────────────────────────────────────────


def test_single_field_produces_one_parenthesised_clause(client, stub):
    rec = stub(search_response())
    client.timeline("idx", "1.2.3.4", ["data.srcip"], **BOUNDED)
    assert sent_query(rec) == '(data.srcip:"1.2.3.4")'


def test_multiple_fields_are_joined_with_or_inside_one_group(client, stub):
    rec = stub(search_response())
    client.timeline("idx", "1.2.3.4", ["data.srcip", "data.dstip"], **BOUNDED)
    assert sent_query(rec) == '(data.srcip:"1.2.3.4" OR data.dstip:"1.2.3.4")'


def test_field_order_is_preserved(client, stub):
    rec = stub(search_response())
    client.timeline("idx", "x", ["a", "b", "c"], **BOUNDED)
    assert sent_query(rec) == '(a:"x" OR b:"x" OR c:"x")'


@pytest.mark.parametrize("empty", [[], None])
def test_empty_fields_raises_value_error(client, stub, empty):
    stub(search_response())
    with pytest.raises(ValueError, match="fields must be a non-empty list"):
        client.timeline("idx", "1.2.3.4", empty, **BOUNDED)


def test_empty_fields_raises_before_any_request(client, stub):
    rec = stub(search_response())
    with pytest.raises(ValueError):
        client.timeline("idx", "1.2.3.4", [], **BOUNDED)
    assert rec.call_count == 0


def test_extra_query_is_anded_onto_the_entity_group(client, stub):
    rec = stub(search_response())
    client.timeline(
        "idx", "1.2.3.4", ["data.srcip"], extra_query="rule.level:>=10", **BOUNDED
    )
    assert sent_query(rec) == '(data.srcip:"1.2.3.4") AND (rule.level:>=10)'


@pytest.mark.parametrize("empty", [None, ""])
def test_falsy_extra_query_adds_no_and_clause(client, stub, empty):
    rec = stub(search_response())
    client.timeline("idx", "1.2.3.4", ["data.srcip"], extra_query=empty, **BOUNDED)
    assert sent_query(rec) == '(data.srcip:"1.2.3.4")'


# ── Entity quoting and escaping ───────────────────────────────────────────────


def test_double_quotes_in_the_entity_are_escaped(client, stub):
    rec = stub(search_response())
    client.timeline("idx", 'say "hi"', ["rule.description"], **BOUNDED)
    assert sent_query(rec) == '(rule.description:"say \\"hi\\"")'


def test_non_string_entity_is_stringified_before_escaping(client, stub):
    rec = stub(search_response())
    client.timeline("idx", 4625, ["data.win.system.eventID"], **BOUNDED)
    assert sent_query(rec) == '(data.win.system.eventID:"4625")'


def test_entity_with_spaces_stays_a_single_phrase(client, stub):
    """The surrounding quotes are what make a multi-word entity one term rather
    than an implicit OR of tokens."""
    rec = stub(search_response())
    client.timeline("idx", "Local System Account", ["user.name"], **BOUNDED)
    assert sent_query(rec) == '(user.name:"Local System Account")'


def test_backslashes_in_the_entity_are_escaped(client, stub):
    """Backslashes must be doubled before the quote escaping runs.

    Inside a Lucene phrase the backslash is itself the escape character, so an
    unescaped Windows path or DOMAIN\\user value is silently corrupted: `C:\\Windows`
    would arrive with `\\W` consumed as an escape, and the term would stop matching
    the very document it was copied from. That is a silent wrong answer on this
    tool's primary input, which is worse than an error.

    Regression guard for the fix in `timeline`: `.replace("\\\\", "\\\\\\\\")` must run
    BEFORE `.replace('"', '\\\\"')`, or the quote escape's own backslash gets doubled.
    """
    rec = stub(search_response())
    client.timeline("idx", r"C:\Windows\System32", ["process.path"], **BOUNDED)
    assert sent_query(rec) == r'(process.path:"C:\\Windows\\System32")'


def test_entity_ending_in_a_backslash_keeps_the_closing_quote_intact(client, stub):
    """The severe form of the same bug: an unescaped trailing backslash escapes the
    closing quote, leaving the phrase unterminated so OpenSearch rejects the whole
    query with a 400 — reaching the analyst as the unrelated-sounding "Bad request:
    check your query syntax or field names". Doubling it keeps the phrase closed."""
    rec = stub(search_response())
    client.timeline("idx", "C:\\Users\\admin\\", ["process.path"], **BOUNDED)
    assert sent_query(rec) == r'(process.path:"C:\\Users\\admin\\")'


def test_lucene_operators_in_the_entity_are_neutralised_by_the_quotes(client, stub):
    """Quoting does hold for the common special characters, so this is *not* a
    general query-injection hole — the backslash is the one that escapes."""
    rec = stub(search_response())
    client.timeline("idx", "a AND b OR *", ["f"], **BOUNDED)
    assert sent_query(rec) == '(f:"a AND b OR *")'


# ── Search parameters timeline forces ─────────────────────────────────────────


def test_timeline_sorts_ascending_on_the_timestamp_field(client, stub):
    """"Chronological" means oldest-first, the opposite of search_string's
    default — an accidental flip would silently reverse every timeline."""
    rec = stub(search_response())
    client.timeline("idx", "x", ["f"], ts_field="data.timestamp", **BOUNDED)
    assert rec.body["sort"] == [
        {"data.timestamp": {"order": "asc", "unmapped_type": "date"}}
    ]


def test_timeline_default_limit_is_one_hundred(client, stub):
    rec = stub(search_response())
    client.timeline("idx", "x", ["f"], **BOUNDED)
    assert rec.body["size"] == 100


def test_timeline_forwards_source_fields(client, stub):
    rec = stub(search_response())
    client.timeline("idx", "x", ["f"], source_fields=["a", "b"], **BOUNDED)
    assert rec.body["_source"] == ["a", "b"]


def test_timeline_forwards_the_time_range(client, stub):
    rec = stub(search_response())
    client.timeline("idx", "x", ["f"], **BOUNDED)
    assert rec.body["query"]["bool"]["filter"] == [
        {"range": {"@timestamp": {"gte": "2024-01-01", "lte": "2024-01-02"}}}
    ]


# ── Result shaping ────────────────────────────────────────────────────────────


def test_timeline_result_shape(client, stub):
    stub(search_response(hits=[{"a": 1}, {"a": 2}], total=2))
    result = client.timeline("idx", "1.2.3.4", ["data.srcip"], **BOUNDED)
    assert result == {
        "total": 2,
        "entity": "1.2.3.4",
        "fields": ["data.srcip"],
        "events": [{"a": 1}, {"a": 2}],
    }


def test_timeline_normalises_an_integer_total(client, stub):
    """Inherited from search_string, but worth pinning at this level too since
    `total` is re-read out of the intermediate dict."""
    stub(search_response(hits=[{"a": 1}], total=99, total_as_int=True))
    assert client.timeline("idx", "x", ["f"], **BOUNDED)["total"] == 99


def test_timeline_no_hits_yields_empty_events_list(client, stub):
    stub(search_response(hits=[], total=0))
    result = client.timeline("idx", "x", ["f"], **BOUNDED)
    assert result["events"] == []
    assert result["total"] == 0


def test_timeline_echoes_the_entity_unescaped(client, stub):
    """The caller sees what they asked for, not the Lucene-escaped form."""
    stub(search_response())
    result = client.timeline("idx", 'a"b', ["f"], **BOUNDED)
    assert result["entity"] == 'a"b'


def test_timeline_propagates_the_no_time_range_warning(client, stub):
    stub(search_response())
    result = client.timeline("idx", "x", ["f"])  # deliberately unbounded
    assert result["warning"] == _NO_TIME_RANGE_WARNING


def test_timeline_propagates_the_limit_cap_warning(client, stub):
    stub(search_response())
    result = client.timeline("idx", "x", ["f"], limit=100_000, **BOUNDED)
    assert "limit capped at 200" in result["warning"]


def test_timeline_omits_the_warning_key_when_there_is_nothing_to_warn_about(
    client, stub
):
    stub(search_response())
    assert "warning" not in client.timeline("idx", "x", ["f"], **BOUNDED)
