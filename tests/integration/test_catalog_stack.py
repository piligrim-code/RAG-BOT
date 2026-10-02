"""Opt-in real broker/database tests. Use tools/run_integration.py."""
import asyncio
from contextlib import asynccontextmanager, suppress
import json
import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RAG_INTEGRATION") != "1", reason="requires owned disposable services")


@pytest.fixture(scope="module")
def database():
    from db_client import Catalog, DBClient

    name = os.environ["RAG_TEST_DB"]
    if not name.startswith("rag_probe_"):
        raise ValueError("Refusing non-test database")
    db = DBClient(username="rag_probe", password=os.environ["RAG_TEST_PASSWORD"],
                  host="127.0.0.1", port=int(os.environ["RAG_TEST_PG_PORT"]), database=name)
    try:
        # Refuse an existing catalog rather than clearing unknown records.
        if db.extract_catalog():
            raise ValueError("Integration database must be empty")
        with db.Session.begin() as session:
            session.add_all([
                Catalog(art="synthetic-a", cat="Alpha", descr="test fixture", price=10),
                Catalog(art="synthetic-b", cat="Beta", descr="test fixture", price=20),
            ])
        yield db
    finally:
        db.close()


@asynccontextmanager
async def stack(database, timeout=5):
    from rabbitclient import RpcClient
    from rabbitmq import serve_catalog

    queue = "probe_" + uuid.uuid4().hex
    ready = asyncio.Event()
    worker = asyncio.create_task(serve_catalog(database, os.environ["RAG_TEST_AMQP_URL"], queue, ready))
    startup = asyncio.create_task(ready.wait())
    client = RpcClient(os.environ["RAG_TEST_AMQP_URL"], timeout=timeout, request_queue=queue)
    try:
        done, _ = await asyncio.wait([startup, worker], timeout=15, return_when=asyncio.FIRST_COMPLETED)
        if worker in done:
            await worker
            raise RuntimeError("Worker exited before readiness")
        if startup not in done:
            raise TimeoutError("Worker did not become ready")
        yield client
    finally:
        startup.cancel()
        with suppress(asyncio.CancelledError):
            await startup
        try:
            await client.close()
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
        assert not client.futures
        assert database.engine.pool.checkedout() == 0


def test_roundtrip_filters_empty_and_formatter(database):
    from catalog_service import catalog_reply

    async def scenario():
        async with stack(database) as client:
            assert len(await client.call({"extract_catalog": {}})) == 2
            sku = "\u0410\u0440\u0442\u0438\u043a\u0443\u043b"
            assert (await client.call({"extract_catalog": {sku: "SYNTHETIC-A"}}))[0][sku] == "synthetic-a"
            category = "\u041a\u0430\u0442\u0435\u0433\u043e\u0440\u0438\u044f"
            description = "\u041e\u043f\u0438\u0441\u0430\u043d\u0438\u0435"
            price = "\u0426\u0435\u043d\u0430"
            assert (await client.call({"extract_catalog": {category: "BETA"}}))[0][sku] == "synthetic-b"
            assert len(await client.call({"extract_catalog": {description: "TEST FIXTURE"}})) == 2
            assert (await client.call({"extract_catalog": {price: {">": 15, "<": 25}}}))[0][sku] == "synthetic-b"
            assert await client.call({"extract_catalog": {category: "Beta", price: {"<": 15}}}) == []
            assert await client.call({"extract_catalog": {sku: "absent"}}) == []
            assert "No matching products" in await catalog_reply({sku: "absent"}, client)
            assert "synthetic-a" in await catalog_reply({sku: "synthetic-a"}, client)
    asyncio.run(scenario())


def test_concurrent_clients_keep_correlation_isolated(database):
    from rabbitclient import RpcClient

    async def scenario():
        async with stack(database) as first:
            second = RpcClient(first.url, timeout=10, request_queue=first.request_queue)
            try:
                sku = "\u0410\u0440\u0442\u0438\u043a\u0443\u043b"
                expected = ["synthetic-a" if i % 2 == 0 else "synthetic-b" for i in range(24)]
                result = await asyncio.gather(*[(first if i % 3 else second).call({"extract_catalog": {sku: value}}) for i, value in enumerate(expected)])
                assert [rows[0][sku] for rows in result] == expected
                assert first.callback_queue.name != second.callback_queue.name
                assert not first.futures and not second.futures
            finally:
                await second.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("sku_first", [True, False])
def test_sku_combines_with_other_filters_regardless_of_json_order(database, sku_first):
    async def scenario():
        async with stack(database) as client:
            sku = "\u0410\u0440\u0442\u0438\u043a\u0443\u043b"
            category = "\u041a\u0430\u0442\u0435\u0433\u043e\u0440\u0438\u044f"
            price = "\u0426\u0435\u043d\u0430"
            for filters, expected in [([(sku, "synthetic-a"), (category, "Beta")], []),
                                      ([(sku, "synthetic-a"), (price, {">": 15})], []),
                                      ([(sku, "synthetic-a"), (category, "Alpha"), (price, {"<": 15})], ["synthetic-a"])]:
                request = dict(filters if sku_first else reversed(filters))
                rows = await client.call({"extract_catalog": request})
                assert [row[sku] for row in rows] == expected
    asyncio.run(scenario())


def test_bad_filters_and_unsupported_request_do_not_poison_worker(database):
    from rabbitclient import RpcRemoteError

    async def scenario():
        async with stack(database) as client:
            for payload in [{"unsupported": {}}, {"extract_catalog": {"\u0426\u0435\u043d\u0430": {"<": "not-an-integer"}}}]:
                with pytest.raises(RpcRemoteError, match="rejected"):
                    await client.call(payload)
                assert len(await client.call({"extract_catalog": {}})) == 2
            assert not client.futures
    asyncio.run(scenario())


def test_actual_sql_error_is_isolated_from_next_request(database):
    from rabbitclient import RpcRemoteError
    from sqlalchemy import text

    async def scenario():
        async with stack(database) as client:
            with database.engine.begin() as connection:
                connection.execute(text("ALTER TABLE products_trio RENAME COLUMN price TO hidden_price"))
            try:
                with pytest.raises(RpcRemoteError, match="rejected"):
                    await client.call({"extract_catalog": {}})
            finally:
                with database.engine.begin() as connection:
                    connection.execute(text("ALTER TABLE products_trio RENAME COLUMN hidden_price TO price"))
            assert len(await client.call({"extract_catalog": {}})) == 2
    asyncio.run(scenario())


def test_aliases_inclusive_prices_and_unknown_filters_over_real_rpc(database):
    from catalog_filters import SKU
    from rabbitclient import RpcRemoteError

    async def scenario():
        async with stack(database) as client:
            for filters, expected in [
                ({"sku": "SYNTHETIC-A", "price": {"<=": 10}}, ["synthetic-a"]),
                ({"category": "Beta", "price": {">=": 20}}, ["synthetic-b"]),
                ({"price": {"=": 10}}, ["synthetic-a"]),
            ]:
                result = await client.call({"extract_catalog": filters})
                assert [row[SKU] for row in result] == expected
            for filters in [{"brand": "unsupported"}, {"price": {">": 20, "<=": 20}},
                            {"sku": "synthetic-a", SKU: "synthetic-b"}]:
                with pytest.raises(RpcRemoteError, match="rejected"):
                    await client.call({"extract_catalog": filters})
            assert len(await client.call({"extract_catalog": {}})) == 2
    asyncio.run(scenario())


@pytest.mark.parametrize("body", [b'{"extract_catalog":{},"extract_catalog":{"sku":"synthetic-b"}}',
                                b"x" * 16385])
def test_duplicate_or_oversized_wire_request_is_rejected(database, body):
    from aio_pika import Message

    async def scenario():
        async with stack(database) as client:
            await client.connect()
            correlation_id = uuid.uuid4().hex
            future = asyncio.get_running_loop().create_future()
            client.futures[correlation_id] = future
            await client.channel.default_exchange.publish(
                Message(body, correlation_id=correlation_id, reply_to=client.callback_queue.name),
                routing_key=client.request_queue)
            assert json.loads(await asyncio.wait_for(future, 5)) == {"error": {"code": "invalid_request"}}
            assert len(await client.call({"extract_catalog": {}})) == 2
    asyncio.run(scenario())


def test_http_model_to_amqp_sql_and_followup_state(database):
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from catalog_filters import CATEGORY, PRICE, SKU
    from conversation import run_catalog_turn
    from llm import extract_filter_patch

    async def scenario():
        patches = {
            "first": {"category": "Alpha", "price": {"<=": 10}},
            "conflict": {"sku": "synthetic-b"},
            "revise": {"sku": None, "category": "Beta", "price": {">=": 20}},
        }
        received = []
        async def generate(request):
            context = json.loads((await request.json())["content"].splitlines()[-1])
            received.append(context)
            return web.json_response({"res_content": json.dumps(patches[context["query"]])})
        app = web.Application()
        app.router.add_post("/generate", generate)
        async with TestServer(app) as server:
            async def extract(query, filters):
                return await extract_filter_patch(query, filters, url=str(server.make_url("/generate")))
            async with stack(database) as client:
                first = await run_catalog_turn("first", {}, extract=extract, rpc_client=client)
                assert "synthetic-a" in first.reply and "synthetic-b" not in first.reply
                second = await run_catalog_turn("conflict", first.filters, extract=extract, rpc_client=client)
                assert "No matching products" in second.reply
                third = await run_catalog_turn("revise", second.filters, extract=extract, rpc_client=client)
                assert "synthetic-b" in third.reply and "synthetic-a" not in third.reply
                assert first.filters == {CATEGORY: "Alpha", PRICE: {"<=": 10}}
                assert second.filters[SKU] == "synthetic-b" and SKU not in third.filters
                assert received[1]["previous_filters"] == first.filters
    asyncio.run(scenario())


def test_malformed_wire_json_returns_generic_error(database):
    from aio_pika import Message

    async def scenario():
        async with stack(database) as client:
            await client.connect()
            correlation_id = uuid.uuid4().hex
            future = asyncio.get_running_loop().create_future()
            client.futures[correlation_id] = future
            await client.channel.default_exchange.publish(
                Message(b"invalid json", correlation_id=correlation_id, reply_to=client.callback_queue.name),
                routing_key=client.request_queue)
            result = json.loads(await asyncio.wait_for(future, 5))
            assert result == {"error": {"code": "invalid_request"}}
            assert len(await client.call({"extract_catalog": {}})) == 2
    asyncio.run(scenario())


def test_timeout_and_cancellation_clear_pending_calls(database):
    from rabbitclient import RpcClient

    async def scenario():
        async with stack(database) as observer:
            await observer.connect()
            blackhole = await observer.channel.declare_queue("", exclusive=True, auto_delete=True)
            client = RpcClient(observer.url, timeout=0.5, request_queue=blackhole.name)
            try:
                await client.connect()
                with pytest.raises(TimeoutError):
                    await client.call({"extract_catalog": {}})
                assert not client.futures
                client.timeout = 5
                pending = asyncio.create_task(client.call({"extract_catalog": {}}))
                async with asyncio.timeout(2):
                    while not client.futures:
                        await asyncio.sleep(0.005)
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
                assert not client.futures
            finally:
                await client.close()
    asyncio.run(scenario())


def test_next_call_reconnects_after_connection_is_closed(database):
    async def scenario():
        async with stack(database) as client:
            assert len(await client.call({"extract_catalog": {}})) == 2
            previous_queue = client.callback_queue.name
            await client.connection.close()
            assert len(await client.call({"extract_catalog": {}})) == 2
            assert client.callback_queue.name != previous_queue
    asyncio.run(scenario())
