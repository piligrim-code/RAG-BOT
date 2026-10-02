import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot_sessions import BotSessions
from catalog_filters import CATEGORY, PRICE, SKU
from main import create_dispatcher, run_polling
from tests.bot_transport import synthetic_bot, update


def parts(*, extract=None, **kwargs):
    rpc = SimpleNamespace(call=AsyncMock(return_value=[{SKU: "synthetic-a", PRICE: 10}]),
                          close=AsyncMock())
    extract = extract if extract is not None else AsyncMock(return_value={"sku": "synthetic-a"})
    dp = create_dispatcher(rpc, extract=extract, **kwargs)
    bot, transport = synthetic_bot()
    return dp, bot, transport, rpc, extract


def context(dp, bot, user=101):
    return dp.fsm.get_context(bot=bot, chat_id=user, user_id=user)


def test_dispatch_followup_reset_and_no_retained_transcript():
    async def scenario():
        extract = AsyncMock(side_effect=[{"sku": "synthetic-a"}, {"price": {"<=": 20}}])
        dp, bot, transport, rpc, _ = parts(extract=extract)
        try:
            await dp.feed_update(bot, update("/start"))
            await dp.feed_update(bot, update())
            await dp.feed_update(bot, update("Synthetic follow-up", update_id=2))
            assert extract.await_args_list[1].args[1] == {SKU: "synthetic-a"}
            assert await context(dp, bot).get_data() == {
                "catalog_params": {SKU: "synthetic-a", PRICE: {"<=": 20}}}
            assert "synthetic-a" in transport.sent[-1].text
            await dp.feed_update(bot, update("/forget", update_id=3))
            assert not dp.storage.sessions
            assert rpc.call.await_count == 2
        finally:
            await dp.emit_shutdown(bot=bot)
            await bot.session.close()
    asyncio.run(scenario())


def test_same_session_serializes_while_another_user_can_continue():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        observed = []
        async def extract(query, filters):
            observed.append((query, filters))
            if query == "first":
                entered.set()
                await release.wait()
                return {"sku": "synthetic-a"}
            return {"category": "Alpha"}
        dp, bot, _, _, _ = parts(extract=extract)
        first = asyncio.create_task(dp.feed_update(bot, update("first")))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            second = asyncio.create_task(dp.feed_update(bot, update("second", update_id=2)))
            await dp.feed_update(bot, update("other", user=202, update_id=3))
            assert [query for query, _ in observed] == ["first", "other"]
            release.set()
            await asyncio.gather(first, second)
            assert observed[-1] == ("second", {SKU: "synthetic-a"})
            assert (await context(dp, bot).get_data())["catalog_params"] == {
                SKU: "synthetic-a", CATEGORY: "Alpha"}
            assert (await context(dp, bot, 202).get_data())["catalog_params"] == {CATEGORY: "Alpha"}
        finally:
            release.set()
            await dp.emit_shutdown(bot=bot)
            await bot.session.close()
    asyncio.run(scenario())


def test_failed_reply_keeps_previous_state_and_releases_lock():
    async def scenario():
        dp, bot, transport, _, extract = parts()
        try:
            await dp.feed_update(bot, update())
            extract.return_value = {"category": "Alpha"}
            transport.send_error = RuntimeError("Synthetic delivery failure")
            with pytest.raises(RuntimeError, match="delivery"):
                await dp.feed_update(bot, update("follow-up"))
            assert (await context(dp, bot).get_data())["catalog_params"] == {SKU: "synthetic-a"}
            transport.send_error = None
            await dp.feed_update(bot, update("follow-up"))
            assert CATEGORY in (await context(dp, bot).get_data())["catalog_params"]
        finally:
            await dp.emit_shutdown(bot=bot)
            await bot.session.close()
    asyncio.run(scenario())


def test_forget_clears_state_even_when_confirmation_cannot_be_sent():
    async def scenario():
        dp, bot, transport, _, _ = parts()
        try:
            await dp.feed_update(bot, update())
            transport.send_error = RuntimeError("Synthetic delivery failure")
            with pytest.raises(RuntimeError, match="delivery"):
                await dp.feed_update(bot, update("/forget"))
            assert not dp.storage.sessions
        finally:
            await dp.emit_shutdown(bot=bot)
            await bot.session.close()
    asyncio.run(scenario())


def test_capacity_declines_new_sessions_without_losing_existing_state():
    async def scenario():
        dp, bot, transport, rpc, _ = parts(sessions=BotSessions(capacity=1))
        try:
            await dp.feed_update(bot, update())
            await dp.feed_update(bot, update(user=202))
            assert rpc.call.await_count == 1 and len(dp.storage.sessions) == 1
            assert transport.sent[-1].chat_id == 202
            await dp.feed_update(bot, update("/forget"))
            await dp.feed_update(bot, update(user=202))
            assert rpc.call.await_count == 2
        finally:
            await dp.emit_shutdown(bot=bot)
            await bot.session.close()
    asyncio.run(scenario())


def test_shutdown_cancels_inflight_turn_before_state_and_clients_close():
    async def scenario():
        entered = asyncio.Event()
        stopped = []
        async def extract(*args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.append(True)
        dp, bot, transport, rpc, _ = parts(extract=extract)
        task = asyncio.create_task(dp.feed_update(bot, update()))
        await asyncio.wait_for(entered.wait(), 2)
        await dp.emit_shutdown(bot=bot)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped == [True] and not dp.active_updates and not dp.storage.sessions
        assert dp.storage.closed and not transport.sent
        await dp.feed_update(bot, update())
        rpc.call.assert_not_awaited()
        await bot.session.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("error", [RuntimeError("Synthetic startup failure"), asyncio.CancelledError()])
def test_polling_failure_always_closes_owned_resources(error):
    async def scenario():
        dp, bot, transport, rpc, _ = parts()
        dp.start_polling = AsyncMock(side_effect=error)
        with pytest.raises(type(error)):
            await run_polling(bot, dp, rpc)
        assert dp.storage.closed and transport.closed
        rpc.close.assert_awaited_once()
        assert dp.start_polling.call_args.kwargs["tasks_concurrency_limit"] == 32
    asyncio.run(scenario())


def test_operator_is_opt_in_and_question_is_not_stored():
    async def scenario():
        dp, bot, transport, rpc, extract = parts(admin_id=900)
        try:
            await dp.feed_update(bot, update("/operator"))
            await dp.feed_update(bot, update(None))
            assert all(message.chat_id == 101 for message in transport.sent)
            await dp.feed_update(bot, update("Synthetic operator question"))
            forwarded = [message for message in transport.sent if message.chat_id == 900]
            assert len(forwarded) == 1 and "101" in forwarded[0].text
            assert not dp.storage.sessions
            extract.assert_not_awaited()
            rpc.call.assert_not_awaited()
        finally:
            await dp.emit_shutdown(bot=bot)
            await bot.session.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_real_polling_lifecycle_leaves_no_poll_task(cancel):
    async def scenario():
        dp, bot, transport, rpc, _ = parts()
        task = asyncio.create_task(run_polling(bot, dp, rpc))
        try:
            await asyncio.wait_for(transport.poll_started.wait(), 2)
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                await dp.stop_polling()
                await task
            assert not transport.poll_tasks
            assert transport.closed and dp.storage.closed
            rpc.close.assert_awaited_once()
        finally:
            for pending in tuple(transport.poll_tasks):
                pending.cancel()
            if transport.poll_tasks:
                await asyncio.gather(*tuple(transport.poll_tasks), return_exceptions=True)
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


def test_repeated_cancellation_still_finishes_cleanup():
    async def scenario():
        dp, bot, transport, rpc, _ = parts()
        closing, release = asyncio.Event(), asyncio.Event()
        original_close = transport.close
        async def close():
            closing.set()
            await release.wait()
            await original_close()
        transport.close = close
        task = asyncio.create_task(run_polling(bot, dp, rpc))
        await asyncio.wait_for(transport.poll_started.wait(), 2)
        task.cancel()
        await asyncio.wait_for(closing.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not transport.poll_tasks
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert transport.closed and dp.storage.closed
        rpc.close.assert_awaited_once()
    asyncio.run(scenario())


@pytest.mark.parametrize("text", ["/unknown", "x" * 3501, "\U0001f600" * 1800])
def test_operator_refuses_commands_and_oversize_unicode(text):
    async def scenario():
        dp, bot, transport, _, _ = parts(admin_id=900)
        try:
            await dp.feed_update(bot, update("/operator"))
            await dp.feed_update(bot, update(text))
            assert all(message.chat_id == 101 for message in transport.sent)
        finally:
            await dp.emit_shutdown(bot=bot)
            await bot.session.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("text,chat_type", [("/unknown", "private"), ("/operator", "private"),
                                           (None, "private"), ("Synthetic query", "group")])
def test_unsupported_input_never_reaches_model_or_rpc(text, chat_type):
    async def scenario():
        dp, bot, _, rpc, extract = parts()
        try:
            await dp.feed_update(bot, update(text, chat_type=chat_type))
            extract.assert_not_awaited()
            rpc.call.assert_not_awaited()
            assert not dp.storage.sessions
        finally:
            await dp.emit_shutdown(bot=bot)
            await bot.session.close()
    asyncio.run(scenario())
