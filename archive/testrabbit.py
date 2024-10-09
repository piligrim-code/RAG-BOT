import asyncio
import json
from aio_pika import connect_robust, Message
from aio_pika.abc import AbstractChannel
from uuid import uuid4

async def send_request(channel: AbstractChannel, message: dict):
    exchange = channel.default_exchange
    queue_name = "catalog_store"
    await exchange.publish(
        Message(
            body=json.dumps(message).encode(),
            reply_to=f"amq.rabbitmq.reply-to",  #  Используйте стандартный  reply_to 
            correlation_id=str(uuid4()),
        ),
        routing_key=queue_name,
    )

async def main():
    connection = await connect_robust("amqp://admin:admin@localhost:5672/")
    async with connection:
        channel = await connection.channel()
        await send_request(channel, {"extract_bikes": {"param1": "value1", "param2": "value2"}})

if __name__ == "__main__":
    asyncio.run(main())