"""index_settings — flatten the /_settings response to a per-index summary.

`_get` is stubbed at the seam. Under test: the five-key projection, the
per-index fan-out, and the default for refresh_interval.
"""



def settings_response(**index_settings):
    return {"my-index": {"settings": {"index": index_settings}}}


def test_projects_the_five_documented_keys(client, stub):
    stub(
        settings_response(
            number_of_shards="3",
            number_of_replicas="1",
            refresh_interval="30s",
            lifecycle={"name": "hot-warm"},
            creation_date="1704067200000",
            provided_name="my-index",
            uuid="dropped",
        ),
        on="_get",
    )
    assert client.index_settings("my-index") == {
        "my-index": {
            "number_of_shards": "3",
            "number_of_replicas": "1",
            "refresh_interval": "30s",
            "lifecycle_name": "hot-warm",
            "creation_date_ms": "1704067200000",
        }
    }


def test_requests_the_settings_endpoint_for_the_given_index(client, stub):
    rec = stub(settings_response(), on="_get")
    client.index_settings("wazuh-alerts-*")
    assert rec.path == "/wazuh-alerts-*/_settings"


def test_absent_refresh_interval_defaults_to_one_second(client, stub):
    """OpenSearch omits the key when the index uses the cluster default, which is
    1s — so the substituted default is factually right."""
    stub(settings_response(number_of_shards="1"), on="_get")
    assert client.index_settings("my-index")["my-index"]["refresh_interval"] == "1s"


def test_all_other_absent_keys_become_none(client, stub):
    stub(settings_response(), on="_get")
    assert client.index_settings("my-index") == {
        "my-index": {
            "number_of_shards": None,
            "number_of_replicas": None,
            "refresh_interval": "1s",
            "lifecycle_name": None,
            "creation_date_ms": None,
        }
    }


def test_absent_lifecycle_block_yields_a_none_name(client, stub):
    stub(settings_response(number_of_shards="1"), on="_get")
    assert client.index_settings("my-index")["my-index"]["lifecycle_name"] is None


def test_lifecycle_block_without_a_name_yields_none(client, stub):
    stub(settings_response(lifecycle={"rollover_alias": "x"}), on="_get")
    assert client.index_settings("my-index")["my-index"]["lifecycle_name"] is None


def test_fans_out_over_every_index_in_a_wildcard_response(client, stub):
    stub(
        {
            "idx-a": {"settings": {"index": {"number_of_shards": "1"}}},
            "idx-b": {"settings": {"index": {"number_of_shards": "5"}}},
        },
        on="_get",
    )
    result = client.index_settings("idx-*")
    assert set(result) == {"idx-a", "idx-b"}
    assert result["idx-a"]["number_of_shards"] == "1"
    assert result["idx-b"]["number_of_shards"] == "5"


def test_empty_response_yields_empty_dict(client, stub):
    stub({}, on="_get")
    assert client.index_settings("nope") == {}


def test_missing_settings_envelope_yields_the_all_none_summary(client, stub):
    stub({"my-index": {}}, on="_get")
    assert client.index_settings("my-index")["my-index"]["number_of_shards"] is None


def test_explicit_null_lifecycle_reports_no_policy(client, stub):
    """A detached ISM policy arrives as `"lifecycle": null`, not as an absent key.

    `s.get("lifecycle", {}).get("name")` defended only against absence, so a present
    null made `.get` return None and the chained `.get("name")` raise AttributeError —
    taking down the whole tool call instead of reporting "no policy". An index with a
    detached policy is a completely ordinary thing to ask about."""
    stub(settings_response(lifecycle=None), on="_get")
    assert client.index_settings("my-index")["my-index"]["lifecycle_name"] is None


def test_explicit_null_refresh_interval_still_applies_the_default(client, stub):
    """The non-fatal half of the same root cause: OpenSearch omits or nulls
    `refresh_interval` when the index runs at the server default, and the documented
    "1s" fallback has to apply in both cases, not just the absent one."""
    stub(settings_response(refresh_interval=None), on="_get")
    assert client.index_settings("my-index")["my-index"]["refresh_interval"] == "1s"
