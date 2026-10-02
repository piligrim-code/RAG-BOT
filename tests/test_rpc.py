import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import rabbitclient
from rabbitclient import RpcClient, RpcRemoteError


def wire(monkeypatch, client, body=b'[]'):
    queue = SimpleNamespace(name="amq.synthetic.callback", consume=AsyncMock())
    async def publish(message, routing_key):
        assert routing_key == "catalog_store"
        assert message.reply_to == queue.name
        assert message.content_type == "application/json"
        if body is not None:
            await client.on_response(SimpleNamespace(correlation_id=message.correlation_id, body=body))
    channel = SimpleNamespace(is_closed=False, declare_queue=AsyncMock(return_value=queue),
                              default_exchange=SimpleNamespace(publish=AsyncMock(side_effect=publish)))
    connected = asyncio.Event()
    connected.set()
    connection = SimpleNamespace(is_closed=False, connected=connected, close_callbacks=set(),
                                 channel=AsyncMock(return_value=channel), close=AsyncMock())
    connect = AsyncMock(return_value=connection)
    monkeypatch.setattr(rabbitclient, "connect", connect)
    return connection, channel, queue, connect


def test_reuses_connection_and_separates_request_and_reply_queues(monkeypatch):
    async def run():
        client = RpcClient(url="amqp://localhost/synthetic")
        conn, channel, _, connect = wire(monkeypatch, client)
        assert await client.call({"extract_catalog": {}}) == []
        assert await client.call({"extract_catalog": {}}) == []
        connect.assert_awaited_once()
        channel.declare_queue.assert_awaited_once_with("", exclusive=True, auto_delete=True)
        assert not client.futures
        await client.close()
        conn.close.assert_awaited_once()
    asyncio.run(run())


def test_timeout_cleans_pending_future_and_late_reply_is_ignored(monkeypatch):
    async def run():
        client = RpcClient(url="amqp://localhost/synthetic", timeout=0.02)
        _, channel, _, _ = wire(monkeypatch, client, body=None)
        with pytest.raises(TimeoutError):
            await client.call({"extract_catalog": {}})
        assert not client.futures
        message = channel.default_exchange.publish.call_args.args[0]
        await client.on_response(SimpleNamespace(correlation_id=message.correlation_id, body=b'[]'))
        await client.on_response(SimpleNamespace(correlation_id=None, body=b'[]'))
        await client.close()
    asyncio.run(run())


def test_publish_failure_cleans_future(monkeypatch):
    async def run():
        client = RpcClient(url="amqp://localhost/synthetic")
        _, channel, _, _ = wire(monkeypatch, client)
        channel.default_exchange.publish.side_effect = ConnectionError("synthetic")
        with pytest.raises(ConnectionError):
            await client.call({})
        assert not client.futures
        await client.close()
    asyncio.run(run())


@pytest.mark.parametrize('body', [b'not-json', b'{"error":{"code":"invalid_request"}}', b'\xff'])
def test_invalid_and_remote_error_responses_are_explicit(monkeypatch, body):
    async def run():
        client = RpcClient(url="amqp://localhost/synthetic")
        wire(monkeypatch, client, body)
        with pytest.raises(RpcRemoteError):
            await client.call({})
        assert not client.futures
        await client.close()
    asyncio.run(run())


def test_cancellation_cleans_pending_state(monkeypatch):
    async def run():
        client = RpcClient(url="amqp://localhost/synthetic")
        wire(monkeypatch, client, body=None)
        task = asyncio.create_task(client.call({}))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not client.futures
        await client.close()
    asyncio.run(run())


def test_missing_url_never_connects(monkeypatch):
    monkeypatch.delenv("RABBITMQ_URL", raising=False)
    connect = AsyncMock()
    monkeypatch.setattr(rabbitclient, "connect", connect)
    with pytest.raises(ValueError, match="RABBITMQ_URL"):
        asyncio.run(RpcClient().call({}))
    connect.assert_not_called()


def test_partial_connection_setup_is_closed(monkeypatch):
    async def run():
        client = RpcClient(url="amqp://localhost/synthetic")
        connection, channel, _, _ = wire(monkeypatch, client)
        channel.declare_queue.side_effect = ConnectionError("synthetic")
        with pytest.raises(ConnectionError):
            await client.call({})
        connection.close.assert_awaited_once()
        assert not client.futures and client.connection is None
    asyncio.run(run())


@pytest.mark.parametrize("timeout", [0, -1, float('inf'), float('nan')])
def test_invalid_timeout(timeout):
    with pytest.raises(ValueError):
        RpcClient(timeout=timeout)


def test_concurrent_responses_are_matched_by_correlation(monkeypatch):
    async def run():
        client = RpcClient(url="amqp://localhost/synthetic")
        _, channel, _, connect = wire(monkeypatch, client, body=None)
        sent = []
        async def publish(message, routing_key):
            sent.append(message)
            if len(sent) == 2:
                for reply in reversed(sent):
                    payload = json.loads(reply.body)
                    await client.on_response(SimpleNamespace(correlation_id=reply.correlation_id,
                                                             body=json.dumps(payload).encode()))
        channel.default_exchange.publish.side_effect = publish
        result = await asyncio.gather(client.call({"id": 1}), client.call({"id": 2}))
        assert result == [{"id": 1}, {"id": 2}]
        assert not client.futures
        connect.assert_awaited_once()
        await client.close()
    asyncio.run(run())


def test_deadline_includes_connect(monkeypatch):
    async def run():
        async def never_connect(*args, **kwargs):
            await asyncio.Event().wait()
        monkeypatch.setattr(rabbitclient, 'connect', never_connect)
        client = RpcClient(url="amqp://localhost/synthetic", timeout=0.02)
        with pytest.raises(TimeoutError):
            await client.call({})
        assert not client.futures
    asyncio.run(run())


def test_close_cancels_pending_calls(monkeypatch):
    async def run():
        client = RpcClient(url="amqp://localhost/synthetic")
        connection, _, _, _ = wire(monkeypatch, client, body=None)
        task = asyncio.create_task(client.call({}))
        await asyncio.sleep(0)
        await client.close()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not client.futures
        connection.close.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize("broken", ["channel", "transport"])
def test_next_call_replaces_broken_channel_even_if_connection_not_marked_closed(monkeypatch, broken):
    async def scenario():
        client = RpcClient(url="amqp://localhost/synthetic")
        first, channel, _, _ = wire(monkeypatch, client)
        assert await client.call({}) == []
        if broken == "channel":
            channel.is_closed = True
        else:
            first.connected.clear()
        second, _, _, connect = wire(monkeypatch, client)
        assert await client.call({}) == []
        assert client.connection is second
        first.close.assert_awaited_once()
        connect.assert_awaited_once()
        await client.close()
    asyncio.run(scenario())


def test_disconnect_fails_pending_without_republishing(monkeypatch):
    async def scenario():
        client = RpcClient(url="amqp://localhost/synthetic")
        connection, channel, _, _ = wire(monkeypatch, client, body=None)
        task = asyncio.create_task(client.call({}))
        await asyncio.sleep(0)
        connection.connected.clear()
        await client._on_disconnect(connection)
        with pytest.raises(RpcRemoteError, match="not retried"):
            await task
        assert not client.futures
        channel.default_exchange.publish.assert_awaited_once()
        await client.close()
    asyncio.run(scenario())


def test_old_connection_callback_cannot_fail_new_pending_request(monkeypatch):
    async def scenario():
        client = RpcClient(url="amqp://localhost/synthetic")
        old, channel, _, _ = wire(monkeypatch, client)
        assert await client.call({}) == []
        channel.is_closed = True
        new, _, _, _ = wire(monkeypatch, client, body=None)
        task = asyncio.create_task(client.call({}))
        await asyncio.sleep(0)
        await client._on_disconnect(old)
        assert not task.done() and client.connection is new
        await client.close()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
