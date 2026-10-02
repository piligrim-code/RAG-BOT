import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aio_pika.exceptions import ChannelClosed, ChannelInvalidStateError

from rabbitmq import _handle_delivery


class Process:
    def __init__(self, exit_error=None):
        self.exit_error = exit_error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        if self.exit_error:
            raise self.exit_error


def delivery(exit_error=None):
    return SimpleNamespace(
        body=b'{"extract_catalog":{}}', reply_to="amq.synthetic.callback",
        correlation_id="synthetic-id", channel=SimpleNamespace(is_closed=False),
        process=Mock(return_value=Process(exit_error)),
    )


def test_successful_delivery_retains_nonmandatory_reply_contract():
    message = delivery()
    channel = SimpleNamespace(default_exchange=SimpleNamespace(publish=AsyncMock()))
    executor = SimpleNamespace(dispatch=AsyncMock(return_value=[]))
    assert asyncio.run(_handle_delivery(message, channel, executor)) is True
    executor.dispatch.assert_awaited_once_with({"extract_catalog": {}})
    assert channel.default_exchange.publish.call_args.kwargs == {
        "routing_key": message.reply_to, "mandatory": False}


@pytest.mark.parametrize("error", [ChannelInvalidStateError(), ChannelClosed(504, "synthetic"), ConnectionError()])
def test_publish_interruption_is_not_retried(error):
    message = delivery()
    channel = SimpleNamespace(default_exchange=SimpleNamespace(publish=AsyncMock(side_effect=error)))
    executor = SimpleNamespace(dispatch=AsyncMock(return_value=[]))
    assert asyncio.run(_handle_delivery(message, channel, executor)) is False
    channel.default_exchange.publish.assert_awaited_once()
    executor.dispatch.assert_awaited_once()


def test_closed_delivery_is_not_evaluated():
    message = delivery()
    message.channel.is_closed = True
    executor = SimpleNamespace(dispatch=AsyncMock())
    channel = SimpleNamespace(default_exchange=SimpleNamespace(publish=AsyncMock()))
    assert asyncio.run(_handle_delivery(message, channel, executor)) is False
    executor.dispatch.assert_not_awaited()
    channel.default_exchange.publish.assert_not_awaited()


def test_lost_original_channel_after_sql_does_not_publish_on_restored_channel():
    message = delivery()
    async def dispatch(request):
        message.channel.is_closed = True
        return []
    channel = SimpleNamespace(default_exchange=SimpleNamespace(publish=AsyncMock()))
    assert asyncio.run(_handle_delivery(message, channel, SimpleNamespace(dispatch=dispatch))) is False
    channel.default_exchange.publish.assert_not_awaited()


def test_cancellation_is_not_masked_by_closed_channel_during_context_exit():
    async def scenario():
        entered = asyncio.Event()
        async def dispatch(request):
            entered.set()
            await asyncio.sleep(10)
        message = delivery(exit_error=ChannelInvalidStateError())
        channel = SimpleNamespace(default_exchange=SimpleNamespace(publish=AsyncMock()))
        task = asyncio.create_task(_handle_delivery(message, channel, SimpleNamespace(dispatch=dispatch)))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        channel.default_exchange.publish.assert_not_awaited()
    asyncio.run(scenario())


def test_unrelated_programming_error_is_not_swallowed_as_transport_recovery():
    message = delivery()
    channel = SimpleNamespace(default_exchange=SimpleNamespace(publish=AsyncMock(side_effect=TypeError("synthetic"))))
    with pytest.raises(TypeError):
        asyncio.run(_handle_delivery(message, channel, SimpleNamespace(dispatch=AsyncMock(return_value=[]))))
