import asyncio
import json
import logging
import os

from aio_pika import Message, connect_robust
from aio_pika.exceptions import AMQPConnectionError, ChannelClosed, ChannelInvalidStateError
from catalog_filters import load_json_object
from catalog_worker import CatalogExecutor


async def _handle_delivery(message, channel, executor):
    try:
        async with message.process(requeue=False):
            if message.channel.is_closed:
                raise ChannelInvalidStateError("Delivery channel closed")
            if not message.reply_to or not message.correlation_id:
                return True
            try:
                request = load_json_object(message.body)
                response = await executor.dispatch(request)
            except ValueError:
                response = {"error": {"code": "invalid_request"}}
            except Exception as error:
                logging.error("Catalog request failed (%s)", type(error).__name__)
                response = {"error": {"code": "internal_error"}}
            # The message belongs to its original channel, not a restored one.
            if message.channel.is_closed:
                raise ChannelInvalidStateError("Delivery channel closed")
            await channel.default_exchange.publish(
                Message(json.dumps(response).encode("utf-8"),
                        content_type="application/json", correlation_id=message.correlation_id),
                routing_key=message.reply_to, mandatory=False,
            )
        return True
    except (AMQPConnectionError, ChannelClosed, ChannelInvalidStateError, ConnectionError):
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise asyncio.CancelledError() from None
        logging.warning("Catalog delivery interrupted; response not retried")
        return False


async def serve_catalog(database, url, request_queue="catalog_store", ready=None):
    """Serve the catalog contract; the caller owns the database lifecycle."""
    connection = await connect_robust(url, timeout=15)
    async with connection, CatalogExecutor(database) as executor:
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=1)
        queue = await channel.declare_queue(request_queue, durable=True)
        async with queue.iterator() as requests:
            if ready is not None:
                ready.set()
            async for message in requests:
                delivered = await _handle_delivery(message, channel, executor)
                if not delivered and connection.is_closed:
                    raise ConnectionError("Catalog connection closed")
            raise RuntimeError("Catalog consumer stopped unexpectedly")


async def main():
    from dotenv import load_dotenv
    load_dotenv()
    url = os.environ.get("RABBITMQ_URL")
    if not url:
        raise ValueError("Set RABBITMQ_URL before starting the worker")
    # Importing this module for contract tests must not open a database.
    from db_client import DBClient
    database = DBClient()
    try:
        await serve_catalog(database, url)
    finally:
        database.close()


if __name__ == "__main__":
    asyncio.run(main())
