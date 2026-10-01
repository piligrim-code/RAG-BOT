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


def test_sql_error_and_unsupported_request_do_not_poison_worker(database):
    from rabbitclient import RpcRemoteError

    async def scenario():
        async with stack(database) as client:
            for payload in [{"unsupported": {}}, {"extract_catalog": {"\u0426\u0435\u043d\u0430": {"<": "not-an-integer"}}}]:
                with pytest.raises(RpcRemoteError, match="rejected"):
                    await client.call(payload)
                assert len(await client.call({"extract_catalog": {}})) == 2
            assert not client.futures
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
