import os
import json
import datetime
import random
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
last_hits = {}

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

    user_state[msg.from_user.id] = "menu"

    await msg.answer(
    "🎮 مرحباً بك في بوت التوقعات",
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

# ================= PREDICTION START =================

@dp.message_handler(lambda m: m.text == "🎯 توقع الجولة")
async def predict_start(msg: types.Message):

    sub = await check_subscription(msg.from_user.id)

    if not sub:
        await msg.answer(
        "❌ يجب تفعيل الاشتراك أولاً",
        reply_markup=main_menu()
        )
        return

    user_state[msg.from_user.id] = "choose_last"

    await msg.answer(
    "اختر آخر ضربة ظهرت",
    reply_markup=hits_keyboard()
    )

# ================= PREDICT =================

@dp.message_handler(lambda m: user_state.get(m.from_user.id) == "choose_last" and m.text in OPTIONS)
async def predict(msg: types.Message):

    last_hit = msg.text
    last_hits[msg.from_user.id] = last_hit

    scores = {o:0 for o in OPTIONS}

    async with pool.acquire() as conn:

        # بيانات التدريب
        rows = await conn.fetch(
        "SELECT next_hit FROM training_data WHERE last_hit=$1",
        last_hit
        )

        for r in rows:
            scores[r["next_hit"]] += 5

        # نتائج المستخدمين
        rows2 = await conn.fetch(
        "SELECT real_result FROM user_results WHERE last_hit=$1",
        last_hit
        )

        for r in rows2:
            scores[r["real_result"]] += 2

    # عشوائية بسيطة
    for o in OPTIONS:
        scores[o] += random.random()

    result = sorted(scores.items(), key=lambda x:x[1], reverse=True)

    best = [x[0] for x in result[:4]]

    text = "🎯 توقع الجولة القادمة\n\n"

    for i,b in enumerate(best,1):
        text += f"{i}️⃣ {b}\n"

    text += "\nبعد ظهور النتيجة اختر الضربة الحقيقية"

    user_state[msg.from_user.id] = "waiting_result"

    await msg.answer(text, reply_markup=hits_keyboard())

# ================= SAVE RESULT =================

@dp.message_handler(lambda m: user_state.get(m.from_user.id) == "waiting_result" and m.text in OPTIONS)
async def save_result(msg: types.Message):

    last_hit = last_hits.get(msg.from_user.id)

    async with pool.acquire() as conn:

        await conn.execute(
        "INSERT INTO user_results(telegram_id,last_hit,real_result) VALUES($1,$2,$3)",
        msg.from_user.id,
        last_hit,
        msg.text
        )

    user_state[msg.from_user.id] = "choose_last"

    await msg.answer(
    "✅ تم تسجيل النتيجة\n\nاختر آخر ضربة للجولة الجديدة",
    reply_markup=hits_keyboard()
    )

# ================= TRAINER =================

trainer_state = {}

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
    kb.add("🧠 تدريب جديد")
    kb.add("↩️ رجوع")

    await msg.answer("لوحة المدرب", reply_markup=kb)

@dp.message_handler(lambda m: m.text == "🧠 تدريب جديد")
async def trainer_start(msg: types.Message):

    trainer_state[msg.from_user.id] = {"sequence": []}

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
        "INSERT INTO training_data(last_hit,sequence,next_hit,trainer_id) VALUES($1,$2,$3,$4)",
        state["last_hit"],
        json.dumps(state["sequence"]),
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
