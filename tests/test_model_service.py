"""Real loopback HTTP fixtures; no public service, key or model is used."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from aiohttp import web
from aiohttp.test_utils import TestServer as LocalServer
import pytest

from catalog_filters import CATEGORY, PRICE, SKU
from conversation import CatalogTurnError, run_catalog_turn
from llm import MAX_MODEL_BYTES, ModelServiceError, extract_filter_patch
import llm


async def with_server(handler, operation):
    app = web.Application()
    app.router.add_post("/generate", handler)
    async with LocalServer(app) as server:
        return await operation(str(server.make_url("/generate")))


def test_real_loopback_request_uses_bounded_structured_context(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    async def handle(request):
        body = await request.json()
        assert set(body) == {"content"}
        prompt = body["content"]
        context = json.loads(prompt.splitlines()[-1])
        assert context == {"previous_filters": {SKU: "synthetic-a"}, "query": "Synthetic follow-up"}
        assert request.headers["Accept-Encoding"] == "identity"
        return web.json_response({"res_content": json.dumps({"category": "Alpha", "price": {"<=": 20}})})
    async def operation(url):
        return await extract_filter_patch("Synthetic follow-up", {"sku": "synthetic-a"}, url=url)
    assert asyncio.run(with_server(handle, operation)) == {CATEGORY: "Alpha", PRICE: {"<=": 20}}


@pytest.mark.parametrize("status", [301, 307, 401, 429, 500])
def test_http_failure_is_not_retried_or_followed(status):
    calls = []
    async def handle(request):
        calls.append(True)
        return web.Response(status=status, headers={"Location": "/generate"},
                            text="synthetic-private-detail")
    async def operation(url):
        with pytest.raises(ModelServiceError) as caught:
            await extract_filter_patch("Synthetic", {}, url=url)
        assert caught.value.code == "http_error" and caught.value.status == status
        assert "synthetic-private-detail" not in str(caught.value)
    asyncio.run(with_server(handle, operation))
    assert calls == [True]


@pytest.mark.parametrize("body", [
    b"invalid JSON", b"[]", b'{"other":"value"}',
    b'{"res_content":"{}","res_content":"{}"}',
    b'{"res_content":"not JSON"}', b'{"res_content":"{\\"brand\\":\\"unsupported\\"}"}',
])
def test_bad_model_response_does_not_fall_back_to_catalog(body):
    async def handle(request):
        return web.Response(body=body, content_type="application/json")
    async def operation(url):
        async def extract(query, filters):
            return await extract_filter_patch(query, filters, url=url)
        rpc = SimpleNamespace(call=AsyncMock())
        with pytest.raises(CatalogTurnError, match="extraction"):
            await run_catalog_turn("Synthetic", {}, extract=extract, rpc_client=rpc)
        rpc.call.assert_not_awaited()
    asyncio.run(with_server(handle, operation))


def test_oversize_content_length_is_rejected():
    async def handle(request):
        return web.Response(body=b"x" * (MAX_MODEL_BYTES + 1))
    async def operation(url):
        with pytest.raises(ModelServiceError, match="response_too_large"):
            await extract_filter_patch("Synthetic", {}, url=url)
    asyncio.run(with_server(handle, operation))


def test_oversize_chunked_response_is_rejected():
    async def handle(request):
        response = web.StreamResponse()
        response.enable_chunked_encoding()
        await response.prepare(request)
        await response.write(b"x" * (MAX_MODEL_BYTES + 1))
        await response.write_eof()
        return response
    async def operation(url):
        with pytest.raises(ModelServiceError, match="response_too_large"):
            await extract_filter_patch("Synthetic", {}, url=url)
    asyncio.run(with_server(handle, operation))


def test_compressed_response_is_not_decompressed():
    async def handle(request):
        return web.Response(body=b"not valid gzip", headers={"Content-Encoding": "gzip"})
    async def operation(url):
        with pytest.raises(ModelServiceError, match="unsupported_encoding"):
            await extract_filter_patch("Synthetic", {}, url=url)
    asyncio.run(with_server(handle, operation))


def test_real_response_delay_hits_total_timeout():
    async def handle(request):
        await asyncio.sleep(0.1)
        return web.json_response({"res_content": "{}"})
    async def operation(url):
        with pytest.raises(ModelServiceError, match="timeout"):
            await extract_filter_patch("Synthetic", {}, url=url, timeout=0.01)
    asyncio.run(with_server(handle, operation))


@pytest.mark.parametrize("url", [None, "", "file:///synthetic", "http://user:pass@localhost/generate",
                              "http://localhost/generate?key=synthetic", "http://localhost/#fragment",
                              "http://localhost:bad/generate"])
def test_bad_configuration_never_creates_session(monkeypatch, url):
    monkeypatch.delenv("LLM_URL", raising=False)
    session = Mock()
    monkeypatch.setattr(llm.aiohttp, "ClientSession", session)
    with pytest.raises(ModelServiceError, match="configuration"):
        asyncio.run(extract_filter_patch("Synthetic", {}, url=url))
    session.assert_not_called()


def test_invalid_model_input_never_creates_session(monkeypatch):
    session = Mock()
    monkeypatch.setattr(llm.aiohttp, "ClientSession", session)
    with pytest.raises(ValueError):
        asyncio.run(extract_filter_patch("", {}, url="http://127.0.0.1/generate"))
    with pytest.raises(ValueError):
        asyncio.run(extract_filter_patch("Synthetic", {}, url="http://127.0.0.1/generate", timeout=True))
    session.assert_not_called()
