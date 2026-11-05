import pytest

from mailroom.providers.base import ProviderError, parse_json


def test_parse_plain_json():
    assert parse_json('{"a": 1}') == {"a": 1}


def test_parse_fenced_json():
    assert parse_json('```json\n{"a": 2}\n```') == {"a": 2}


def test_parse_json_with_preamble():
    assert parse_json('Sure! Here it is:\n{"a": 3}\nHope that helps.') == {"a": 3}


def test_parse_failure_is_explicit():
    with pytest.raises(ProviderError):
        parse_json("no json here at all")
