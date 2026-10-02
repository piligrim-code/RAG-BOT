"""One owned native-model process, bounded IPC and no automatic request replay."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import math
import multiprocessing

MAX_REQUEST_BYTES = 65536
MAX_RESPONSE_BYTES = 16384


class ModelProcessError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(f"Model process: {code}")


def _packet(value, maximum):
    data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(data) > maximum:
        raise ValueError("IPC size limit")
    return data


def _worker(connection, factory):
    backend = None
    try:
        backend = factory()
        connection.send_bytes(b'{"ready":true}')
        while True:
            request = json.loads(connection.recv_bytes(MAX_REQUEST_BYTES))
            if request is None:
                break
            try:
                result = backend.invoke(request["operation"], request["content"])
                encoded = _packet({"result": result}, MAX_RESPONSE_BYTES)
            except Exception:
                encoded = b'{"error":"backend_failure"}'
            connection.send_bytes(encoded)
    except (EOFError, BrokenPipeError, OSError):
        pass
    except Exception:
        try:
            connection.send_bytes(b'{"error":"startup_failure"}')
        except (EOFError, BrokenPipeError, OSError):
            pass
    finally:
        try:
            if backend is not None:
                try:
                    backend.close()
                except Exception:
                    pass
        finally:
            connection.close()


class ModelProcess:
    def __init__(self, factory, *, timeout=30, startup_timeout=120):
        for value in (timeout, startup_timeout):
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 600:
                raise ValueError("Model deadlines must be finite numbers in (0, 600]")
        self.factory, self.timeout, self.startup_timeout = factory, timeout, startup_timeout
        self._pool = None
        self._process = None
        self._connection = None
        self._pending = None
        self._closing = None
        self.ready = False

    async def start(self):
        if self._process is not None or self._closing is not None:
            raise ModelProcessError("already_started_or_closed")
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        self._connection = parent
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rag-model-ipc")
        self._process = context.Process(target=_worker, args=(child, self.factory), daemon=True)
        try:
            self._process.start()
            child.close()
            response = await self._exchange(None, self.startup_timeout)
            if response != {"ready": True}:
                raise ModelProcessError("startup_failure")
            if self._closing is not None:
                raise ModelProcessError("unavailable")
            self.ready = True
        except BaseException:
            child.close()
            await self.close()
            raise
        return self

    def _roundtrip(self, payload):
        if payload is not None:
            self._connection.send_bytes(payload)
        return json.loads(self._connection.recv_bytes(MAX_RESPONSE_BYTES))

    async def _exchange(self, payload, timeout):
        future = asyncio.get_running_loop().run_in_executor(self._pool, self._roundtrip, payload)
        self._pending = future
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        except (TimeoutError, asyncio.CancelledError, EOFError, OSError) as error:
            await self.close()
            if isinstance(error, asyncio.CancelledError):
                raise
            code = "timeout" if isinstance(error, TimeoutError) else "transport_failure"
            raise ModelProcessError(code) from None
        finally:
            self._pending = None

    async def invoke(self, operation, content):
        if not self.ready or self._closing is not None:
            raise ModelProcessError("unavailable")
        if self._pending is not None:
            raise ModelProcessError("busy")
        payload = _packet({"operation": operation, "content": content}, MAX_REQUEST_BYTES)
        response = await self._exchange(payload, self.timeout)
        if set(response) != {"result"}:
            raise ModelProcessError("backend_failure")
        return response["result"]

    def _stop_process(self, interrupt):
        process = self._process
        if process is not None and process.pid is not None:
            if process.is_alive() and not interrupt:
                try:
                    self._connection.send_bytes(b"null")
                except (BrokenPipeError, EOFError, OSError):
                    pass
                process.join(2)
            if process.is_alive():
                process.terminate()
                process.join(2)
            if process.is_alive():
                process.kill()
                process.join(2)
            if process.is_alive():
                raise ModelProcessError("shutdown_failure")
            process.close()
        if self._connection is not None:
            self._connection.close()

    async def _close(self):
        self.ready = False
        await asyncio.to_thread(self._stop_process, self._pending is not None)
        if self._pending is not None:
            await asyncio.gather(self._pending, return_exceptions=True)
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)

    async def close(self):
        if self._closing is None:
            self._closing = asyncio.create_task(self._close())
        interrupted = False
        while not self._closing.done():
            try:
                await asyncio.shield(self._closing)
            except asyncio.CancelledError:
                interrupted = True
        self._closing.result()
        if interrupted:
            raise asyncio.CancelledError()
