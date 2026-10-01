"""Bounded request/reply RPC with a dedicated callback queue per connection."""
import asyncio
import json
import math
import os
import uuid

from aio_pika import Message, connect


class RpcRemoteError(RuntimeError):
    pass


class RpcClient:
    def __init__(self, url=None, timeout=15.0, request_queue="catalog_store"):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        self.url = url
        self.timeout = timeout
        self.request_queue = request_queue
        self.futures = {}
        self.connection = None
        self.channel = None
        self.callback_queue = None
        self._connect_lock = asyncio.Lock()

    async def connect(self):
        async with self._connect_lock:
            if self.connection is not None and not self.connection.is_closed:
                return self
            url = self.url or os.environ.get("RABBITMQ_URL")
            if not url:
                raise ValueError("Set RABBITMQ_URL before using live RPC")
            connection = await connect(url, timeout=self.timeout)
            try:
                channel = await connection.channel()
                queue = await channel.declare_queue("", exclusive=True, auto_delete=True)
                await queue.consume(self.on_response, no_ack=True)
            except BaseException:
                await connection.close()
                raise
            self.connection, self.channel, self.callback_queue = connection, channel, queue
        return self

    async def on_response(self, message):
        future = self.futures.pop(message.correlation_id, None)
        if future is None or future.done():
            return
        future.set_result(message.body)

    async def call(self, message):
        if not isinstance(message, dict):
            raise ValueError("RPC request must be an object")
        body = json.dumps(message).encode("utf-8")
        correlation_id = str(uuid.uuid4())
        future = None
        try:
            async with asyncio.timeout(self.timeout):
                await self.connect()
                if len(self.futures) >= 128:
                    raise RuntimeError("Too many pending RPC calls")
                future = asyncio.get_running_loop().create_future()
                self.futures[correlation_id] = future
                await self.channel.default_exchange.publish(
                    Message(body, content_type="application/json",
                            correlation_id=correlation_id, reply_to=self.callback_queue.name),
                    routing_key=self.request_queue,
                )
                response = await future
                try:
                    payload = json.loads(response.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise RpcRemoteError("Invalid JSON response") from None
                if isinstance(payload, dict) and "error" in payload:
                    raise RpcRemoteError("Catalog service rejected the request")
                return payload
        finally:
            self.futures.pop(correlation_id, None)
            if future is not None and not future.done():
                future.cancel()

    async def close(self):
        async with self._connect_lock:
            for future in self.futures.values():
                if not future.done():
                    future.cancel()
            self.futures.clear()
            connection, self.connection = self.connection, None
            self.channel = self.callback_queue = None
            if connection is not None:
                await connection.close()
