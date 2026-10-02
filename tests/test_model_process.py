import asyncio
import multiprocessing
from threading import Event

import pytest

from model_process import ModelProcess, ModelProcessError
from tests.model_fixtures import SyntheticBackend, startup_failure, startup_stall


@pytest.mark.parametrize("value", [0, -1, True, float("inf"), float("nan"), 601])
def test_invalid_deadlines(value):
    with pytest.raises(ValueError):
        ModelProcess(SyntheticBackend, timeout=value)


def test_spawned_backend_success_failure_and_shutdown():
    async def scenario():
        runtime = await ModelProcess(SyntheticBackend, startup_timeout=15).start()
        pid = runtime._process.pid
        try:
            assert await runtime.invoke("generate", "query") == {"res_content": '{"sku":"synthetic-a"}'}
            for content in ("fail", "oversize"):
                with pytest.raises(ModelProcessError, match="backend_failure") as caught:
                    await runtime.invoke("generate", content)
                assert "synthetic-private" not in str(caught.value)
            assert await runtime.invoke("retrieve", "query") == {"response": ["Synthetic product"]}
        finally:
            await runtime.close()
        await runtime.close()
        assert not runtime.ready and pid not in {process.pid for process in multiprocessing.active_children()}
        with pytest.raises(ModelProcessError, match="unavailable"):
            await runtime.invoke("generate", "query")
    asyncio.run(scenario())


def test_startup_error_is_sanitized_and_closes_resources():
    async def scenario():
        runtime = ModelProcess(startup_failure, startup_timeout=15)
        with pytest.raises(ModelProcessError, match="startup_failure") as caught:
            await runtime.start()
        assert "synthetic-private" not in str(caught.value)
        assert runtime._closing.done() and not runtime.ready
    asyncio.run(scenario())


@pytest.mark.parametrize("content,code", [("sleep", "timeout"), ("crash", "transport_failure")])
def test_native_stall_or_exit_retires_owned_process(content, code):
    async def scenario():
        runtime = await ModelProcess(SyntheticBackend, timeout=0.2, startup_timeout=15).start()
        pid = runtime._process.pid
        with pytest.raises(ModelProcessError, match=code):
            await runtime.invoke("generate", content)
        assert not runtime.ready and pid not in {process.pid for process in multiprocessing.active_children()}
        with pytest.raises(ModelProcessError, match="unavailable"):
            await runtime.invoke("generate", "next")
    asyncio.run(scenario())


def test_busy_is_rejected_without_enqueueing_or_replay():
    async def scenario():
        runtime = await ModelProcess(SyntheticBackend, timeout=5, startup_timeout=15).start()
        try:
            first = asyncio.create_task(runtime.invoke("generate", "brief"))
            await asyncio.sleep(0)
            with pytest.raises(ModelProcessError, match="busy"):
                await runtime.invoke("generate", "next")
            assert "res_content" in await first
        finally:
            await runtime.close()
    asyncio.run(scenario())


def test_cancelled_request_cannot_leave_native_work_running():
    async def scenario():
        runtime = await ModelProcess(SyntheticBackend, startup_timeout=15).start()
        pid = runtime._process.pid
        task = asyncio.create_task(runtime.invoke("generate", "sleep"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not runtime.ready and pid not in {process.pid for process in multiprocessing.active_children()}
    asyncio.run(scenario())


def test_stalled_startup_is_terminated():
    async def scenario():
        before = {process.pid for process in multiprocessing.active_children()}
        runtime = ModelProcess(startup_stall, startup_timeout=0.2)
        with pytest.raises(ModelProcessError, match="timeout"):
            await runtime.start()
        assert {process.pid for process in multiprocessing.active_children()} == before
        assert runtime._closing.done() and not runtime.ready
    asyncio.run(scenario())


def test_repeated_cancellation_cannot_interrupt_process_cleanup(monkeypatch):
    async def scenario():
        runtime = await ModelProcess(SyntheticBackend, startup_timeout=15).start()
        pid = runtime._process.pid
        entered, release = Event(), Event()
        original = runtime._stop_process
        def held_stop(interrupt):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Synthetic stop was not released")
            original(interrupt)
        monkeypatch.setattr(runtime, "_stop_process", held_stop)
        task = asyncio.create_task(runtime.invoke("generate", "sleep"))
        await asyncio.sleep(0)
        task.cancel()
        try:
            async with asyncio.timeout(3):
                while not entered.is_set():
                    await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert pid not in {process.pid for process in multiprocessing.active_children()}
        assert not runtime.ready
    asyncio.run(scenario())
