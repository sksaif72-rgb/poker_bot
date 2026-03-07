import os
import json
import random
import datetime
import asyncpg
import threading

from flask import Flask
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup
from aiogram.utils import executor

TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

bot = Bot(token=TOKEN)
dp = Dispatcher(bot)

# ================= WEB SERVER =================

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot Running"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# ================= OPTIONS =================

OPTIONS = [
"🍉 بطيخ",
"🍎 تفاح",
"🍊 برتقال",
"🐟 سمك",
"🍤 روبيان",
"🍔 برغر",
"🥬 خس",
"🍗 دجاج"
]

pool = None
user_state = {}
user_sequences = {}
trainer_state = {}

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
    "🎮 مرحباً بك\nاختر من القائمة",
    reply_markup=main_menu()
    )

# ================= BACK =================

@dp.message_handler(lambda m: m.text == "↩️ رجوع")
async def back(msg: types.Message):

    user_state[msg.from_user.id] = "menu"

    await msg.answer(
    "القائمة الرئيسية",
    reply_markup=main_menu()
    )

# ================= SUBSCRIPTION =================

@dp.message_handler(lambda m: m.text == "🎟 تفعيل كود")
async def redeem_code(msg: types.Message):

    user_state[msg.from_user.id] = "waiting_code"

    await msg.answer("اكتب كود الاشتراك")

@dp.message_handler(lambda m: user_state.get(m.from_user.id) == "waiting_code")
async def process_code(message: types.Message):

    code = message.text.strip()

    async with pool.acquire() as conn:

        row = await conn.fetchrow(
        "SELECT * FROM codes WHERE code=$1",
        code
        )

        if not row:
            await message.answer("❌ الكود غير صحيح")
            return

        end = datetime.datetime.now() + datetime.timedelta(days=row["days"])

        await conn.execute(
        "UPDATE users SET subscription_end=$1 WHERE telegram_id=$2",
        end,
        message.from_user.id
        )

    user_state[message.from_user.id] = "menu"

    await message.answer(
    "✅ تم تفعيل الاشتراك",
    reply_markup=main_menu()
    )

# ================= CHECK SUB =================

async def check_subscription(user_id):

    async with pool.acquire() as conn:

        row = await conn.fetchrow(
        "SELECT subscription_end FROM users WHERE telegram_id=$1",
        user_id
        )

    if not row:
        return False

    if not row["subscription_end"]:
        return False

    return row["subscription_end"] > datetime.datetime.now()

# ================= START PREDICTION =================

@dp.message_handler(lambda m: m.text == "🎯 توقع الجولة")
async def start_prediction(msg: types.Message):

    sub = await check_subscription(msg.from_user.id)

    if not sub:
        await msg.answer(
        "❌ يجب الاشتراك أولاً",
        reply_markup=main_menu()
        )
        return

    user_sequences[msg.from_user.id] = []

    user_state[msg.from_user.id] = "sequence_input"

    await msg.answer(
    "ادخل آخر 6 ضربات\nاختر الضربة رقم 1",
    reply_markup=hits_keyboard()
    )

# ================= SEQUENCE INPUT =================

@dp.message_handler(lambda m: user_state.get(m.from_user.id) == "sequence_input" and m.text in OPTIONS)
async def sequence_input(msg: types.Message):

    seq = user_sequences[msg.from_user.id]
    seq.append(msg.text)

    if len(seq) < 6:

        await msg.answer(f"الضربة رقم {len(seq)+1}")

        return

    user_state[msg.from_user.id] = "predicting"

    await predict(msg)

# ================= PREDICT FUNCTION =================

async def predict(msg):

    seq = user_sequences[msg.from_user.id]

    scores = {o:0 for o in OPTIONS}

    async with pool.acquire() as conn:

        rows = await conn.fetch(
        "SELECT next_hit FROM training_data WHERE sequence=$1",
        json.dumps(seq)
        )

        for r in rows:
            scores[r["next_hit"]] += 15

        rows2 = await conn.fetch(
        "SELECT real_result FROM user_results WHERE sequence=$1",
        json.dumps(seq)
        )

        for r in rows2:
            scores[r["real_result"]] += 5

    for o in OPTIONS:
        scores[o] += random.random()

    result = sorted(scores.items(), key=lambda x:x[1], reverse=True)

    best = [x[0] for x in result[:4]]

    text = "🎯 التوقعات الأقوى:\n\n"

    for i,b in enumerate(best,1):
        text += f"{i}️⃣ {b}\n"

    text += "\nبعد ظهور النتيجة اختر الضربة الحقيقية"

    user_state[msg.from_user.id] = "waiting_result"

    await msg.answer(text, reply_markup=hits_keyboard())

# ================= SAVE RESULT =================

@dp.message_handler(lambda m: user_state.get(m.from_user.id) == "waiting_result" and m.text in OPTIONS)
async def save_result(msg: types.Message):

    seq = user_sequences[msg.from_user.id]
    result = msg.text

    async with pool.acquire() as conn:

        await conn.execute(
        "INSERT INTO user_results(telegram_id,sequence,real_result) VALUES($1,$2,$3)",
        msg.from_user.id,
        json.dumps(seq),
        result
        )

    seq.pop(0)
    seq.append(result)

    await predict(msg)

# ================= TRAINER PANEL =================

@dp.message_handler(lambda m: m.text == "👨‍🏫 لوحة المدرب")
async def trainer_panel(msg: types.Message):

    async with pool.acquire() as conn:
        user = await conn.fetchrow(
        "SELECT role FROM users WHERE telegram_id=$1",
        msg.from_user.id
        )

    if not user or user["role"] != "trainer":
        await msg.answer("❌ هذه القائمة للمدربين فقط")
        return

    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("🧠 تدريب")
    kb.add("↩️ رجوع")

    await msg.answer("لوحة المدرب", reply_markup=kb)

# ================= TRAINER START =================

@dp.message_handler(lambda m: m.text == "🧠 تدريب")
async def trainer_start(msg: types.Message):

    trainer_state[msg.from_user.id] = {"sequence":[]}

    await msg.answer(
    "ادخل 6 ضربات للتسلسل",
    reply_markup=hits_keyboard()
    )

@dp.message_handler(lambda m: m.text in OPTIONS)
async def trainer_sequence(msg: types.Message):

    state = trainer_state.get(msg.from_user.id)

    if not state:
        return

    seq = state["sequence"]

    if len(seq) < 6:

        seq.append(msg.text)

        if len(seq) == 6:

            await msg.answer("اختر الضربة التالية")

        else:

            await msg.answer(f"الضربة رقم {len(seq)+1}")

        return

    next_hit = msg.text

    async with pool.acquire() as conn:

        await conn.execute(
        "INSERT INTO training_data(sequence,next_hit,trainer_id) VALUES($1,$2,$3)",
        json.dumps(seq),
        next_hit,
        msg.from_user.id
        )

    del trainer_state[msg.from_user.id]

    await msg.answer("✅ تم حفظ التدريب", reply_markup=main_menu())

# ================= RUN =================

async def on_startup(dp):
    await connect_db()
    await bot.delete_webhook(drop_pending_updates=True)
    print("Bot Running")

if __name__ == "__main__":

    t = threading.Thread(target=run_web)
    t.start()

    executor.start_polling(dp, on_startup=on_startup)
