import asyncio
import json
import logging
from aio_pika import Message, connect_robust
from aio_pika.abc import AbstractIncomingMessage
from db_client import DBClient
import pika

db_client = DBClient()

async def main() -> None:
    try:
        # Perform connection
        connection_params = pika.ConnectionParameters(
            host='localhost',  # Замените на адрес вашего RabbitMQ сервера
            port=5672,          # Порт по умолчанию для RabbitMQ
            virtual_host='/',   # Виртуальный хост (обычно '/')
            credentials=pika.PlainCredentials(
                username='guest',  # Имя пользователя по умолчанию
                password='guest'   # Пароль по умолчанию
            )
        )

        # Установка соединения
        connection = pika.BlockingConnection(connection_params)

        # Создание канала
        channel = connection.channel()


        # Declaring queue
        queue = channel.queue_declare("catalog_store")

        print(" [x] Awaiting RPC requests")

        # Start listening the queue with name 'hello'
        async with queue.iterator() as qiterator:
            message: AbstractIncomingMessage
            async for message in qiterator:
                print("ok")
                try:
                    async with message.process(requeue=False):
                        assert message.reply_to is not None

                        message_d = json.loads(message.body.decode())
                        if "extract_bikes" in message_d:
                            response = db_client.extract_bikes(message_d["extract_bikes"])
                        elif "new_dialog" in message_d:
                            response = db_client.new_dialog(**message_d["new_dialog"])
                        elif "add_message" in message_d:
                            response = db_client.add_message(**message_d["add_message"])
                        response_enc = json.dumps(response).encode()

                        await exchange.publish(
                            Message(
                                body=response_enc,
                                correlation_id=message.correlation_id,
                            ),
                            routing_key=message.reply_to,
                        )
                        print("Request complete")
                except Exception:
                    logging.exception("Processing error for message %r", message)
    except Exception as e:
        print(f'Ошибка подключения: {e}')
    else:
        print('Подключение успешное')
if __name__ == "__main__":
    asyncio.run(main())