import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from catalog_filters import (
    ANY_VALUE, CATEGORY, DESCRIPTION, PRICE, SKU, FilterValidationError,
    load_json_object, merge_filter_patch, normalize_filters, parse_model_patch,
)
from catalog_service import CatalogRequestError, dispatch_catalog_request


def test_aliases_case_and_whitespace_are_normalized():
    assert normalize_filters({" SKU ": " synthetic-a ", "CATEGORY": "Alpha",
                              DESCRIPTION.lower(): "example", "price": {"<=": 10}}) == {
        SKU: "synthetic-a", CATEGORY: "Alpha", DESCRIPTION: "example", PRICE: {"<=": 10}}


@pytest.mark.parametrize("filters", [
    None, [], {"brand": "example"}, {"unknown": None}, {"sku": ["a", "b"]},
    {"sku": 1}, {"sku": ""}, {"sku": " "}, {"sku": "x" * 101}, {"sku": "bad\x00"},
    {"sku": "\ud800"}, {"sku": "a", SKU: "b"}, {"sku": None, SKU: "a"}, {1: "value"},
])
def test_bad_fields_never_reach_database(filters):
    database = SimpleNamespace(extract_catalog=Mock())
    with pytest.raises(CatalogRequestError):
        dispatch_catalog_request({"extract_catalog": filters}, database)
    database.extract_catalog.assert_not_called()


@pytest.mark.parametrize("price", [
    {}, [], "20", {"<": "20"}, {"<": True}, {"<": 2.0}, {"<": -1},
    {"<": 2 ** 31}, {"between": [1, 2]}, {">": 20, "<=": 20},
    {">=": 20, "<": 20}, {"=": 10, ">": 10}, {"<": 0},
    {">": 2 ** 31 - 1},
])
def test_bad_price_ranges(price):
    with pytest.raises(FilterValidationError):
        normalize_filters({"price": price})


@pytest.mark.parametrize("price", [
    {"=": 0}, {"<=": 0}, {">=": 0}, {"<": 1}, {">": 0},
    {">=": 10, "<=": 10}, {"=": 10, ">=": 0, "<": 20},
])
def test_integer_price_boundaries(price):
    assert normalize_filters({"price": price}) == {PRICE: price}


def test_patch_is_nonmutating_and_replaces_complete_price_range():
    previous = {"sku": "synthetic-a", "price": {">": 10}}
    patch = {"category": "Alpha", "price": {"<=": 20}, "sku": None}
    originals = deepcopy((previous, patch))
    result = merge_filter_patch(previous, patch)
    assert result == {CATEGORY: "Alpha", PRICE: {"<=": 20}}
    result[PRICE]["<="] = 99
    assert (previous, patch) == originals


def test_empty_patch_preserves_filters_and_removal_is_explicit():
    assert merge_filter_patch({SKU: "synthetic-a"}, {}) == {SKU: "synthetic-a"}
    assert merge_filter_patch({SKU: "synthetic-a"}, {SKU: ANY_VALUE}) == {}
    assert normalize_filters({"category": None}) == {}


@pytest.mark.parametrize("raw", [
    '[]', 'null', '{"sku":"a","sku":"b"}', '{"price":{"<":10,"<":20}}',
    '{"price":{"<":NaN}}', '{"price":{"<":Infinity}}', b"\xff", "x" * 16385,
    '{"sku":"\\ud800"}', 'prefix {"sku":"a"}',
])
def test_bad_model_json_cannot_silently_become_empty_filter(raw):
    with pytest.raises(FilterValidationError):
        parse_model_patch(raw)


def test_plain_and_single_fenced_json_are_supported():
    raw = json.dumps({"sku": "synthetic-a", "price": {"<=": 20}})
    fence = chr(96) * 3
    assert parse_model_patch(raw) == parse_model_patch(f"{fence}json\n{raw}\n{fence}")
    with pytest.raises(FilterValidationError):
        parse_model_patch(f"{fence}json\n{raw}\n{fence}\nExtra prose")


def test_wire_limit_counts_bytes_and_rejects_duplicate_operation():
    with pytest.raises(FilterValidationError):
        load_json_object('{"extract_catalog":{},"extract_catalog":{}}')
    with pytest.raises(FilterValidationError):
        load_json_object(json.dumps({"text": "\u044f" * 100}, ensure_ascii=False), limit=150)
    assert load_json_object(b'{"extract_catalog":{}}') == {"extract_catalog": {}}
