"""Single-job SQL offload with graceful draining before database disposal."""
import asyncio
from concurrent.futures import ThreadPoolExecutor

from catalog_service import dispatch_catalog_request


async def _settle(future):
    interrupted = False
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            interrupted = True
        except Exception:
            break
    if not future.cancelled():
        future.exception()
    return interrupted


class CatalogExecutor:
    """Own the execution thread, not the database. Do not enqueue unbounded work."""

    def __init__(self, database):
        self.database = database
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rag-catalog")
        self._pending = None
        self._closing = False
        self._closed = False

    async def __aenter__(self):
        if self._closing:
            raise RuntimeError("Catalog executor is closing")
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.close()

    async def dispatch(self, request):
        if self._closing:
            raise RuntimeError("Catalog executor is closing")
        if self._pending is not None:
            raise RuntimeError("Catalog executor already has an active request")
        future = asyncio.get_running_loop().run_in_executor(
            self._pool, dispatch_catalog_request, request, self.database)
        self._pending = future
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # Task cancellation cannot stop a running psycopg2 call. Drain it so
            # caller cleanup cannot dispose the database underneath that call.
            await _settle(future)
            raise
        finally:
            self._pending = None

    async def close(self):
        if self._closed:
            return
        self._closing = True
        interrupted = False
        if self._pending is not None:
            interrupted = await _settle(self._pending)
        self._pool.shutdown(wait=True, cancel_futures=True)
        self._closed = True
        if interrupted:
            raise asyncio.CancelledError()
