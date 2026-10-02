import asyncio

import pytest

from bot_sessions import BotSessions, SessionCapacityError


@pytest.mark.parametrize("kwargs", [{"capacity": 0}, {"capacity": True}, {"capacity": 100001},
                                    {"ttl": 0}, {"ttl": True}, {"ttl": float("nan")}])
def test_invalid_session_limits(kwargs):
    with pytest.raises(ValueError):
        BotSessions(**kwargs)


def test_idle_ttl_capacity_and_defensive_copies():
    async def scenario():
        now = [10.0]
        sessions = BotSessions(capacity=1, ttl=5, clock=lambda: now[0])
        async with sessions.lock("a"):
            data = {"filters": {"sku": "synthetic"}}
            await sessions.set_data("a", data)
            data["filters"]["sku"] = "mutated"
            fetched = await sessions.get_data("a")
            fetched["filters"]["sku"] = "also-mutated"
            assert (await sessions.get_data("a"))["filters"]["sku"] == "synthetic"
            now[0] = 100
            with pytest.raises(SessionCapacityError):
                async with sessions.lock("b"):
                    pass
        now[0] = 104
        with pytest.raises(SessionCapacityError):
            async with sessions.lock("b"):
                pass
        now[0] = 105
        async with sessions.lock("b"):
            assert await sessions.get_data("b") == {}
        assert not sessions.sessions
        await sessions.close()
        await sessions.close()
        with pytest.raises(RuntimeError, match="closed"):
            await sessions.get_data("a")
    asyncio.run(scenario())


def test_waiter_cancellation_does_not_drop_active_lock():
    async def scenario():
        sessions = BotSessions()
        async def waiting():
            async with sessions.lock("a"):
                pytest.fail("Cancelled waiter acquired lock")
        async with sessions.lock("a"):
            waiter = asyncio.create_task(waiting())
            await asyncio.sleep(0)
            assert sessions.sessions["a"].users == 2
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert sessions.sessions["a"].users == 1
            with pytest.raises(RuntimeError, match="Drain"):
                await sessions.close()
        assert not sessions.sessions
        await sessions.close()
    asyncio.run(scenario())
