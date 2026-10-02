import asyncio
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from catalog_service import CatalogRequestError, catalog_reply, dispatch_catalog_request
from db_calls import extract_gk
from catalog_filters import SKU


def test_empty_catalog_is_a_result_not_an_exception():
    rpc = SimpleNamespace(call=AsyncMock(return_value=[]))
    assert asyncio.run(extract_gk({}, rpc)) == ("", None)
    assert "No matching products" in asyncio.run(catalog_reply({}, rpc))


def test_first_product_is_consistent_and_only_three_are_shown():
    rows = [{"sku": f"example-{i}", "Фото": "not_in_response"} for i in range(4)]
    rpc = SimpleNamespace(call=AsyncMock(return_value=rows))
    context, first = asyncio.run(extract_gk({"category": "не имеет значения"}, rpc))
    assert first == rows[0]
    assert "example-2" in context and "example-3" not in context
    assert "not_in_response" not in context
    rpc.call.assert_awaited_once_with({"extract_catalog": {}})


@pytest.mark.parametrize("response", [None, {}, [1], [None]])
def test_invalid_catalog_response(response):
    with pytest.raises(ValueError):
        asyncio.run(extract_gk({}, SimpleNamespace(call=AsyncMock(return_value=response))))


def test_request_matches_worker_contract():
    database = SimpleNamespace(extract_catalog=Mock(return_value=[{"sku": "synthetic"}]))
    result = dispatch_catalog_request({"extract_catalog": {"sku": "synthetic"}}, database)
    assert result == [{"sku": "synthetic"}]
    database.extract_catalog.assert_called_once_with({SKU: "synthetic"})


@pytest.mark.parametrize("payload", [{}, {"extract_bikes": {}}, {"extract_catalog": []},
                                       {"extract_catalog": {}, "new_dialog": {}}, None])
def test_worker_rejects_unsupported_contract(payload):
    db = SimpleNamespace(extract_catalog=Mock())
    with pytest.raises(CatalogRequestError):
        dispatch_catalog_request(payload, db)
    db.extract_catalog.assert_not_called()


def test_message_length_is_bounded():
    rpc = SimpleNamespace(call=AsyncMock(return_value=[{"text": "x" * 10000}]))
    assert len(asyncio.run(catalog_reply({}, rpc))) < 4096


def test_non_bmp_reply_is_bounded_in_utf16_units():
    rpc = SimpleNamespace(call=AsyncMock(return_value=[{"text": "\U0001f600" * 10000}]))
    reply = asyncio.run(catalog_reply({}, rpc))
    assert len(reply.encode("utf-16-le")) <= 7000


def test_offline_demo_outputs_both_cases():
    root = Path(__file__).resolve().parents[1]
    p = subprocess.run([sys.executable, str(root / "demo.py")], cwd=root, capture_output=True,
                       text=True, timeout=10, check=True)
    result = json.loads(p.stdout)
    assert "sample-001" in result["matching"]
    assert "No matching products" in result["empty"]
