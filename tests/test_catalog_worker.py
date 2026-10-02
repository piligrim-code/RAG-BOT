import asyncio
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from catalog_worker import CatalogExecutor


async def wait_started(event):
    async with asyncio.timeout(2):
        while not event.is_set():
            await asyncio.sleep(0.005)


def blocked_database():
    entered, release = Event(), Event()
    threads = []
    def extract(filters):
        threads.append(get_ident())
        entered.set()
        if not release.wait(3):
            raise RuntimeError("Synthetic operation was not released")
        return [{"sku": "synthetic-a"}]
    return SimpleNamespace(extract_catalog=extract, close=Mock()), entered, release, threads


def test_sql_runs_off_loop_and_close_does_not_own_database():
    async def scenario():
        database, entered, release, threads = blocked_database()
        async with CatalogExecutor(database) as executor:
            task = asyncio.create_task(executor.dispatch({"extract_catalog": {}}))
            try:
                await wait_started(entered)
                await asyncio.sleep(0.01)
                assert not task.done() and threads == [threads[0]]
                assert threads[0] != get_ident()
            finally:
                release.set()
            assert await task == [{"sku": "synthetic-a"}]
        database.close.assert_not_called()
        assert executor._closed and executor._pending is None
    asyncio.run(scenario())


def test_cancelled_request_drains_sql_before_propagating():
    async def scenario():
        database, entered, release, _ = blocked_database()
        async with CatalogExecutor(database) as executor:
            task = asyncio.create_task(executor.dispatch({"extract_catalog": {}}))
            try:
                await wait_started(entered)
                task.cancel()
                await asyncio.sleep(0.01)
                assert not task.done()
                task.cancel()
                await asyncio.sleep(0.01)
                assert not task.done()
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert executor._pending is None
            assert await executor.dispatch({"extract_catalog": {}}) == [{"sku": "synthetic-a"}]
    asyncio.run(scenario())


def test_cancelled_close_finishes_cleanup_and_rejects_new_work():
    async def scenario():
        database, entered, release, _ = blocked_database()
        executor = CatalogExecutor(database)
        task = asyncio.create_task(executor.dispatch({"extract_catalog": {}}))
        closing = None
        try:
            await wait_started(entered)
            closing = asyncio.create_task(executor.close())
            await asyncio.sleep(0.01)
            closing.cancel()
            await asyncio.sleep(0.01)
            assert not closing.done()
            with pytest.raises(RuntimeError, match="closing"):
                await executor.dispatch({"extract_catalog": {}})
        finally:
            release.set()
        assert await task == [{"sku": "synthetic-a"}]
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert executor._closed
        await executor.close()
    asyncio.run(scenario())


def test_busy_executor_does_not_queue_more_work():
    async def scenario():
        database, entered, release, threads = blocked_database()
        async with CatalogExecutor(database) as executor:
            task = asyncio.create_task(executor.dispatch({"extract_catalog": {}}))
            try:
                await wait_started(entered)
                with pytest.raises(RuntimeError, match="active request"):
                    await executor.dispatch({"extract_catalog": {}})
                assert len(threads) == 1
            finally:
                release.set()
            await task
    asyncio.run(scenario())


def test_operation_exception_releases_slot():
    async def scenario():
        database = SimpleNamespace(extract_catalog=Mock(side_effect=[RuntimeError("Synthetic failure"), []]))
        async with CatalogExecutor(database) as executor:
            with pytest.raises(RuntimeError, match="Synthetic failure"):
                await executor.dispatch({"extract_catalog": {}})
            assert executor._pending is None
            assert await executor.dispatch({"extract_catalog": {}}) == []
    asyncio.run(scenario())


def test_closed_executor_refuses_reentry():
    async def scenario():
        executor = CatalogExecutor(SimpleNamespace(extract_catalog=Mock()))
        await executor.close()
        with pytest.raises(RuntimeError, match="closing"):
            async with executor:
                pass
        with pytest.raises(RuntimeError, match="closing"):
            await executor.dispatch({"extract_catalog": {}})
    asyncio.run(scenario())
