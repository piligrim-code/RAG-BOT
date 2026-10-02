"""Strict in-memory Telegram transport; unsupported methods fail, never use HTTP."""
import asyncio
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, GetMe, GetUpdates, SendMessage
from aiogram.types import Chat, Message, Update, User


class SyntheticTelegram(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent = []
        self.closed = False
        self.send_error = None
        self.poll_started = asyncio.Event()
        self.poll_tasks = set()

    async def close(self):
        self.closed = True

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, GetMe):
            return User(id=123456, is_bot=True, first_name="Synthetic", username="synthetic_bot")
        if isinstance(method, GetUpdates):
            task = asyncio.current_task()
            self.poll_tasks.add(task)
            self.poll_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.poll_tasks.discard(task)
        if isinstance(method, AnswerCallbackQuery):
            self.sent.append(method)
            return True
        if not isinstance(method, SendMessage):
            raise AssertionError(f"Unsupported synthetic Telegram method: {type(method).__name__}")
        if self.send_error:
            raise self.send_error
        self.sent.append(method)
        return Message(message_id=len(self.sent), date=datetime.now(timezone.utc),
                       chat=Chat(id=method.chat_id, type="private"), text=method.text)

    async def stream_content(self, *args, **kwargs):
        raise AssertionError("Synthetic Telegram never downloads files")
        yield b""  # Implements the async-generator interface without opening a socket.


def synthetic_bot():
    session = SyntheticTelegram()
    return Bot("123456:synthetic-only", session=session), session


def update(text="Synthetic query", *, user=101, update_id=1, chat_type="private"):
    message = {"message_id": update_id, "date": 0,
               "chat": {"id": user, "type": chat_type},
               "from": {"id": user, "is_bot": False, "first_name": "Synthetic"}}
    if text is not None:
        message["text"] = text
    return Update.model_validate({"update_id": update_id, "message": message})
