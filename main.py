from dotenv import load_dotenv
import os
import asyncio
import logging
import json
from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode
from aiogram.filters import BaseFilter
from aiogram.filters.command import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove, ContentType, ReplyKeyboardMarkup, BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from rabbitclient import RpcClient
from db_calls import extract_gk
from llm import slot_fill

load_dotenv() 

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")

rpc_client = RpcClient()

# Включаем логирование, чтобы не пропустить важные сообщения
logging.basicConfig(level=logging.INFO)

# Объект бота
bot = Bot(TOKEN)
# Диспетчер
dp = Dispatcher()
def make_reply_keyboard(button_name_rows):
    button_rows = []
    for button_name_row in button_name_rows:
        button_rows.append([KeyboardButton(text=text) for text in button_name_row])
    keyboard = ReplyKeyboardMarkup(keyboard=button_rows, resize_keyboard=True)
    return keyboard

class AskQuestion(StatesGroup):
    question = State()

@dp.message(Command("start"))
async def start_func(message: types.Message, state: FSMContext):
    await state.clear()
    user_data = await state.get_data()
    dialog_id = await rpc_client.call({"new_dialog": {"user_id": message.from_user.id}})
    messages = user_data.get("messages", [])
    messages.append({"role": "assistant", "content": "Добрый день! Что бы вы хотели??"})
    keyboard = make_reply_keyboard([["Посмотреть каталог"], ["Связь с оператором"]])
    await state.update_data(dialog_id=dialog_id)
    await state.update_data(messages=messages)
    await message.answer("Добрый день! Что бы вы хотели??", reply_markup=keyboard)


@dp.message(F.text == "Посмотреть каталог")
async def message_reply(message: types.Message):
    keyboard = make_reply_keyboard([["Диски"], ["Пороховое и газовое оборудование"], ["Наши СТМ"], ["Прочее"]])
    await message.answer("Хорошо, мы занимаемся продажей алмазного оборудования, выберите категорию из списка ниже", reply_markup=keyboard)

def make_inline_keyboard():
    button1 = InlineKeyboardButton(
        text="Резать металл",
        callback_data="metal"
    )
    button2 = InlineKeyboardButton(
        text="Резать дерево",
        callback_data="wood"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[button1, button2]])
    return keyboard

def make_metal_keyboard():
    button3 = InlineKeyboardButton(
        text="Отрезные круги",
        callback_data="cutting_circles"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[button3]])
    return keyboard

def make_STM_keyboard():
    button4 = InlineKeyboardButton(
        text="Насадки",
        callback_data="nozzle"
    )
    button5 = InlineKeyboardButton(
        text="Заусовщики",
        callback_data="zausovshchik"
    )
    button6 = InlineKeyboardButton(
        text="Пылеотвод",
        callback_data="dust_collector"
    )
    button7 = InlineKeyboardButton(
        text="Отрезные круги",
        callback_data="cutting_wheels"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[button4, button5, button6, button7 ]])
    return keyboard

@dp.message(F.text == "Диски")
async def message_reply(message: types.Message):
    keyboard = make_inline_keyboard()
    await message.answer("Какая категория дисков вас интересует?", reply_markup=keyboard)

@dp.callback_query(F.data == "metal")
async def process_metal_callback(callback_query: types.CallbackQuery):
    keyboard = make_metal_keyboard()
    await callback_query.message.edit_text("Вы выбрали 'Резать металл'. Что вас интересует?", reply_markup=keyboard)

@dp.callback_query(F.data == "wood")
async def process_wood_callback(callback_query: types.CallbackQuery):
    await callback_query.message.edit_text("Вы выбрали 'Резать дерево'.")

@dp.callback_query(F.data == "cutting_circles")
async def process_cutting_circles_callback(callback_query: types.CallbackQuery):
    await callback_query.message.edit_text("Вы выбрали 'Отрезные круги'.")

@dp.message(F.text == "Наши СТМ")
async def message_reply(message: types.Message):
    keyboard = make_STM_keyboard()
    await message.answer("Что из наших СТМ вас интересует?", reply_markup=keyboard)

@dp.callback_query(F.data == "nozzle")
async def process_metal_callback(callback_query: types.CallbackQuery):
    await callback_query.message.edit_text("Вы выбрали 'Насадки'. Что вас интересует?")

@dp.callback_query(F.data == "zausovshchik")
async def process_metal_callback(callback_query: types.CallbackQuery):
    await callback_query.message.edit_text("Вы выбрали 'Заусовщики'. Что вас интересует?")

@dp.callback_query(F.data == "dust_collector")
async def process_metal_callback(callback_query: types.CallbackQuery):
    await callback_query.message.edit_text("Вы выбрали 'Пылеоотводы'. Что вас интересует?")

@dp.callback_query(F.data == "cutting_wheels")
async def process_metal_callback(callback_query: types.CallbackQuery):
    await callback_query.message.edit_text("Вы выбрали 'Отрезные круги'. Что вас интересует?")


@dp.message(F.text =="Связь с оператором")
async def operator_reply(message: types.Message, state: FSMContext):
    await message.answer("Задайте ваш вопрос оператору:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(AskQuestion.question)

# Хэндлер для сбора вопроса
@dp.message(AskQuestion.question)
async def process_question(message: types.Message, state: FSMContext):
    question = message.text
    await state.update_data(question=question)

    # Отправляем сообщение в группу
    username = message.from_user.username
    user_mention = f"@{username}" 
    await bot.send_message(
        chat_id=ADMIN_ID,
        text=f"Пользователь {message.from_user.first_name} ({message.from_user.username}) хочет связаться с оператором. \nID пользователя: {user_mention}\nВопрос: {question}"
    )
    await message.answer("Оператор скоро вам напишет, ожидайте.", reply_markup=ReplyKeyboardRemove())
    await state.clear()

@dp.message(F.content_type == ContentType.TEXT)
async def message_reply(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    user_id = message.from_user.id
    dialog_id = user_data["dialog_id"]
    messages = user_data.get("messages", [])
    param_dict = await slot_fill(user_data, message)
    context, catalog = await extract_gk(param_dict, rpc_client)


# Запуск процесса поллинга новых апдейтов
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())