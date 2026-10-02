import asyncio
import json
import logging
import os

from aio_pika import Message, connect_robust
from catalog_service import dispatch_catalog_request
from catalog_filters import load_json_object


async def serve_catalog(database, url, request_queue="catalog_store", ready=None):
    """Serve the catalog contract; the caller owns the database lifecycle."""
    connection = await connect_robust(url, timeout=15)
    async with connection:
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=1)
        queue = await channel.declare_queue(request_queue, durable=True)
        async with queue.iterator() as requests:
            if ready is not None:
                ready.set()
            async for message in requests:
                async with message.process(requeue=False):
                    if not message.reply_to or not message.correlation_id:
                        continue
                    try:
                        request = load_json_object(message.body)
                        response = dispatch_catalog_request(request, database)
                    except ValueError:
                        response = {"error": {"code": "invalid_request"}}
                    except Exception as error:
                        logging.error("Catalog request failed (%s)", type(error).__name__)
                        response = {"error": {"code": "internal_error"}}
                    await channel.default_exchange.publish(
                        Message(json.dumps(response).encode("utf-8"),
                                content_type="application/json", correlation_id=message.correlation_id),
                        routing_key=message.reply_to,
                    )


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
