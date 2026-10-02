"""Import-safe Telegram adapter for the tested catalog workflow."""
import asyncio
from contextlib import AsyncExitStack
import logging
import os

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from dotenv import load_dotenv

from bot_sessions import BotSessions, SessionCapacityError
from catalog_filters import FilterValidationError, validate_text
from conversation import run_catalog_turn
from llm import extract_filter_patch
from rabbitclient import RpcClient


class AskQuestion(StatesGroup):
    question = State()


class CatalogDispatcher(Dispatcher):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.active_updates = set()
        self.closing = False

    async def feed_update(self, bot, update, **kwargs):
        if self.closing:
            return UNHANDLED
        task = asyncio.current_task()
        self.active_updates.add(task)
        try:
            return await super().feed_update(bot, update, **kwargs)
        finally:
            self.active_updates.discard(task)

    async def emit_shutdown(self, *args, **kwargs):
        self.closing = True
        pending = tuple(task for task in self.active_updates if task is not asyncio.current_task())
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await super().emit_shutdown(*args, **kwargs)


def menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Посмотреть каталог")],
        [KeyboardButton(text="Связь с оператором")],
    ], resize_keyboard=True)


async def catalog_reply(message: types.Message, state: FSMContext, rpc_client, extract):
    user_data = await state.get_data()
    try:
        turn = await run_catalog_turn(
            message.text, user_data.get("catalog_params", {}),
            extract=extract, rpc_client=rpc_client)
    except FilterValidationError:
        await message.answer("Укажите артикул, категорию, описание или целочисленный диапазон цены.")
        return
    except Exception as error:
        logging.warning("Catalog flow failed (%s)", type(error).__name__)
        await message.answer("Каталог временно недоступен. Попробуйте позже.")
        return
    await message.answer(turn.reply)
    await state.update_data(catalog_params=turn.filters)


def create_dispatcher(rpc_client, *, extract=extract_filter_patch, admin_id=None, sessions=None):
    if admin_id is not None and (type(admin_id) is not int or admin_id == 0):
        raise ValueError("admin_id must be a nonzero integer or None")
    sessions = sessions if sessions is not None else BotSessions()
    dp = CatalogDispatcher(storage=sessions, events_isolation=sessions,
                           rpc_client=rpc_client, extract=extract)
    # The public adapter supports private chats only; group state is not qualified.
    dp.message.filter(F.chat.type == "private", F.from_user)

    @dp.errors()
    async def capacity_error(event: types.ErrorEvent):
        if not isinstance(event.exception, SessionCapacityError):
            raise event.exception
        if event.update.message is not None and event.update.message.chat.type == "private":
            await event.update.message.answer("Сервис занят. Попробуйте позже.")
        return True

    @dp.message(Command("start", "forget", "cancel"))
    async def reset(message: types.Message, state: FSMContext):
        await state.clear()
        await message.answer("Фильтры сброшены. Напишите, какой товар вы ищете.", reply_markup=menu())

    @dp.message(Command("help"))
    @dp.message(F.text == "Посмотреть каталог")
    async def catalog_help(message: types.Message):
        await message.answer("Укажите артикул, категорию или цену. /forget сбрасывает текущие фильтры.")

    @dp.message(Command("operator"))
    @dp.message(F.text == "Связь с оператором")
    async def operator(message: types.Message, state: FSMContext):
        if admin_id is None:
            await message.answer("Связь с оператором не настроена.")
            return
        await message.answer(
            "Следующее текстовое сообщение и ваш Telegram ID будут переданы оператору. "
            "Не отправляйте пароли и платёжные данные. /cancel отменяет отправку.",
            reply_markup=ReplyKeyboardRemove())
        await state.set_state(AskQuestion.question)

    @dp.message(AskQuestion.question, F.text, ~F.text.startswith("/"))
    async def question(message: types.Message, state: FSMContext, bot: Bot):
        try:
            validate_text(message.text, 3500)
            if len(message.text.encode("utf-16-le")) > 7000:
                raise FilterValidationError("Question exceeds Telegram text limit")
        except FilterValidationError:
            await message.answer("Вопрос должен содержать от 1 до 3500 символов.")
            return
        await bot.send_message(admin_id, f"Telegram ID: {message.from_user.id}\n{message.text}")
        # Do not retain the question or auto-forward it again after reply failure.
        await state.set_state(None)
        await message.answer("Вопрос передан оператору.", reply_markup=menu())

    @dp.message(AskQuestion.question)
    async def question_needs_text(message: types.Message):
        await message.answer("Отправьте вопрос текстом или используйте /cancel.")

    @dp.message(F.text.startswith("/"))
    async def unknown_command(message: types.Message):
        await message.answer("Доступные команды: /start, /forget, /operator, /cancel, /help.")

    dp.message.register(catalog_reply, F.text)

    @dp.message()
    async def needs_text(message: types.Message):
        await message.answer("Поиск по каталогу принимает текстовые сообщения.")

    @dp.callback_query()
    async def old_menu(callback: types.CallbackQuery):
        await callback.answer("Напишите запрос текстом или используйте /start.")

    return dp


async def _stop_and_close(bot, dp, rpc_client, polling):
    # Cleanup also covers startup failure, before aiogram's shutdown hooks run.
    async with AsyncExitStack() as cleanup:
        cleanup.push_async_callback(bot.session.close)
        cleanup.push_async_callback(rpc_client.close)
        cleanup.push_async_callback(dp.emit_shutdown, bot=bot)
        if not polling.done():
            stopper = asyncio.create_task(dp.stop_polling())
            try:
                done, _ = await asyncio.wait((polling, stopper), return_when=asyncio.FIRST_COMPLETED)
                if stopper in done:
                    try:
                        await stopper
                    except RuntimeError:
                        # Cancellation may precede the polling task's first instruction.
                        polling.cancel()
                await asyncio.gather(polling, return_exceptions=True)
            finally:
                if not stopper.done():
                    stopper.cancel()
                await asyncio.gather(stopper, return_exceptions=True)


async def run_polling(bot, dp, rpc_client):
    polling = asyncio.create_task(dp.start_polling(
        bot, close_bot_session=False, tasks_concurrency_limit=32))
    try:
        await asyncio.shield(polling)
    finally:
        closing = asyncio.create_task(_stop_and_close(bot, dp, rpc_client, polling))
        cancelled = False
        while not closing.done():
            try:
                await asyncio.shield(closing)
            except asyncio.CancelledError:
                cancelled = True
        closing.result()
        if cancelled:
            raise asyncio.CancelledError()


async def main():
    load_dotenv()
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise ValueError("Set BOT_TOKEN before starting the Telegram adapter")
    raw_admin = os.environ.get("ADMIN_ID")
    try:
        admin_id = None if not raw_admin else int(raw_admin)
        if admin_id == 0:
            raise ValueError()
    except ValueError:
        raise ValueError("ADMIN_ID must be a nonzero integer when configured") from None
    rpc_client = RpcClient()
    dp = create_dispatcher(rpc_client, admin_id=admin_id)
    bot = Bot(token, session=AiohttpSession(timeout=15))
    await run_polling(bot, dp, rpc_client)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
