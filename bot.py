import os
import json
import asyncio
import datetime
import random
import asyncpg

from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils import executor

TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

bot = Bot(token=TOKEN)
dp = Dispatcher(bot)

OPTIONS = [
"🍉 بطيخ",
"🍎 تفاح",
"🍊 برتقال",
"🐟 سمك",
"🍤 روبيان",
"🍔 برغر",
"🥬 خضار",
"🍗 دجاج"
]

pool = None

# ================= DATABASE =================

async def connect_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL)

# ================= KEYBOARDS =================

def main_menu():
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("🎯 توقع الجولة")
    kb.add("🎟 تفعيل كود")
    kb.add("👨‍🏫 لوحة المدرب")
    return kb

def hits_keyboard():
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    for h in OPTIONS:
        kb.add(h)
    kb.add("↩️ رجوع")
    return kb

# ================= START =================

@dp.message_handler(commands=['start'])
async def start(msg: types.Message):

    async with pool.acquire() as conn:
        await conn.execute(
        "INSERT INTO users (telegram_id) VALUES($1) ON CONFLICT DO NOTHING",
        msg.from_user.id)

    await msg.answer(
    "🎮 مرحباً بك في بوت التوقعات",
    reply_markup=main_menu()
    )

# ================= SUBSCRIPTION =================

@dp.message_handler(lambda m: m.text == "🎟 تفعيل كود")
async def redeem_code(msg: types.Message):

    await msg.answer("اكتب كود الاشتراك")

    @dp.message_handler()
    async def process_code(message: types.Message):

        code = message.text.strip()

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
            "SELECT * FROM codes WHERE code=$1", code)

            if not row:
                await message.answer("❌ الكود غير صحيح")
                return

            days = row["days"]

            end = datetime.datetime.now() + datetime.timedelta(days=days)

            await conn.execute(
            "UPDATE users SET subscription_end=$1 WHERE telegram_id=$2",
            end, message.from_user.id)

            await message.answer("✅ تم تفعيل الاشتراك")

# ================= PREDICTION =================

@dp.message_handler(lambda m: m.text == "🎯 توقع الجولة")
async def predict_start(msg: types.Message):

    await msg.answer(
    "اختر آخر ضربة ظهرت",
    reply_markup=hits_keyboard()
    )

@dp.message_handler(lambda m: m.text in OPTIONS)
async def predict(msg: types.Message):

    last_hit = msg.text
    minute = datetime.datetime.now().minute

    async with pool.acquire() as conn:

        rows = await conn.fetch(
        "SELECT * FROM training_data WHERE last_hit=$1",
        last_hit)

    scores = {o:0 for o in OPTIONS}

    for r in rows:
        next_hit = r["next_hit"]
        scores[next_hit] += 3

    for o in OPTIONS:
        scores[o] += random.random()

    result = sorted(scores.items(), key=lambda x:x[1], reverse=True)

    best = [x[0] for x in result[:4]]

    text = "🎯 توقع الجولة القادمة\n\n"

    for i,b in enumerate(best,1):
        text += f"{i}️⃣ {b}\n"

    text += "\n⚠️ التوقعات تحليل احتمالي وليست مضمونة."

    await msg.answer(text, reply_markup=main_menu())

# ================= TRAINER =================

trainer_state = {}

@dp.message_handler(lambda m: m.text == "👨‍🏫 لوحة المدرب")
async def trainer_panel(msg: types.Message):

    async with pool.acquire() as conn:
        user = await conn.fetchrow(
        "SELECT role FROM users WHERE telegram_id=$1",
        msg.from_user.id)

    if user["role"] != "trainer":
        await msg.answer("❌ هذه القائمة للمدربين فقط")
        return

    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("🧠 تدريب جديد")
    kb.add("↩️ رجوع")

    await msg.answer("لوحة المدرب", reply_markup=kb)

@dp.message_handler(lambda m: m.text == "🧠 تدريب جديد")
async def trainer_start(msg: types.Message):

    trainer_state[msg.from_user.id] = {
        "sequence": []
    }

    await msg.answer(
    "اختر آخر ضربة قبل التسلسل",
    reply_markup=hits_keyboard()
    )

@dp.message_handler(lambda m: m.text in OPTIONS)
async def trainer_sequence(msg: types.Message):

    state = trainer_state.get(msg.from_user.id)

    if not state:
        return

    if "last_hit" not in state:

        state["last_hit"] = msg.text

        await msg.answer("ابدأ إدخال التسلسل (6 ضربات)")
        return

    if len(state["sequence"]) < 6:

        state["sequence"].append(msg.text)

        if len(state["sequence"]) == 6:
            await msg.answer("اختر الضربة التالية")
        else:
            await msg.answer(f"الضربة رقم {len(state['sequence'])+1}")
        return

    next_hit = msg.text

    async with pool.acquire() as conn:
        await conn.execute(
        "INSERT INTO training_data(last_hit,sequence,next_hit) VALUES($1,$2,$3)",
        state["last_hit"],
        json.dumps(state["sequence"]),
        next_hit
        )

    del trainer_state[msg.from_user.id]

    await msg.answer("✅ تم حفظ التدريب", reply_markup=main_menu())

# ================= RUN =================

async def on_startup(dp):
    await connect_db()

if __name__ == "__main__":
    executor.start_polling(dp, on_startup=on_startup)
