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

# WEB SERVER

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot Running"

def run_web():
    port = int(os.environ.get("PORT",10000))
    app.run(host="0.0.0.0",port=port)

# OPTIONS

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

pool=None
user_state={}
user_sequences={}
trainer_state={}

# DATABASE

async def connect_db():
    global pool
    pool=await asyncpg.create_pool(DATABASE_URL)

# KEYBOARDS

def main_menu():
    kb=ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("🎯 توقع الجولة")
    kb.add("🎟 تفعيل كود")
    kb.add("👨‍🏫 لوحة المدرب")
    return kb

def hits_keyboard():
    kb=ReplyKeyboardMarkup(resize_keyboard=True)
    for h in OPTIONS:
        kb.add(h)
    kb.add("↩️ رجوع")
    return kb

# START

@dp.message_handler(commands=['start'])
async def start(msg:types.Message):

    async with pool.acquire() as conn:
        await conn.execute(
        "INSERT INTO users (telegram_id) VALUES($1) ON CONFLICT DO NOTHING",
        msg.from_user.id)

    await msg.answer(
    "🎮 مرحبا بك في بوت التوقعات",
    reply_markup=main_menu()
    )

# CHECK SUB

async def check_subscription(user_id):

    async with pool.acquire() as conn:

        row=await conn.fetchrow(
        "SELECT subscription_end FROM users WHERE telegram_id=$1",
        user_id)

    if not row:
        return False

    if not row["subscription_end"]:
        return False

    return row["subscription_end"]>datetime.datetime.now()

# START PREDICTION

@dp.message_handler(lambda m:m.text=="🎯 توقع الجولة")
async def start_prediction(msg:types.Message):

    sub=await check_subscription(msg.from_user.id)

    if not sub:
        await msg.answer("❌ يجب الاشتراك اولا")
        return

    user_sequences[msg.from_user.id]=[]

    user_state[msg.from_user.id]="sequence"

    await msg.answer(
    "ادخل اخر 6 ضربات\nاختر الضربة 1",
    reply_markup=hits_keyboard()
    )

# SEQUENCE INPUT

@dp.message_handler(lambda m:m.text in OPTIONS and user_state.get(m.from_user.id)=="sequence")
async def sequence_input(msg:types.Message):

    seq=user_sequences[msg.from_user.id]
    seq.append(msg.text)

    if len(seq)<6:

        await msg.answer(f"الضربة {len(seq)+1}")

        return

    user_state[msg.from_user.id]="predict"

    await predict(msg)

# PREDICTION ENGINE

async def predict(msg):

    seq=user_sequences[msg.from_user.id]

    scores={o:0 for o in OPTIONS}

    async with pool.acquire() as conn:

        rows=await conn.fetch("SELECT sequence,next_hit FROM training_data")

        for r in rows:

            db_seq=r["sequence"]
            next_hit=r["next_hit"]

            # FULL MATCH

            if db_seq==seq:
                scores[next_hit]+=20

            # PARTIAL MATCH

            match=sum([1 for i in range(6) if db_seq[i]==seq[i]])

            if match>=4:
                scores[next_hit]+=10

            # LAST HIT

            if db_seq[-1]==seq[-1]:
                scores[next_hit]+=5

        # USER RESULTS

        rows2=await conn.fetch("SELECT sequence,real_result FROM user_results")

        for r in rows2:

            if r["sequence"]==seq:

                scores[r["real_result"]]+=6

    for o in OPTIONS:
        scores[o]+=random.random()

    result=sorted(scores.items(),key=lambda x:x[1],reverse=True)

    best=[x[0] for x in result[:4]]

    text="🎯 التوقعات الأقوى\n\n"

    for i,b in enumerate(best,1):
        text+=f"{i}️⃣ {b}\n"

    text+="\nبعد ظهور النتيجة اختر الضربة الحقيقية"

    user_state[msg.from_user.id]="result"

    await msg.answer(text,reply_markup=hits_keyboard())

# SAVE RESULT

@dp.message_handler(lambda m:m.text in OPTIONS and user_state.get(m.from_user.id)=="result")
async def save_result(msg:types.Message):

    seq=user_sequences[msg.from_user.id]
    result=msg.text

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

# TRAINER

@dp.message_handler(lambda m:m.text=="👨‍🏫 لوحة المدرب")
async def trainer_panel(msg:types.Message):

    async with pool.acquire() as conn:

        user=await conn.fetchrow(
        "SELECT role FROM users WHERE telegram_id=$1",
        msg.from_user.id)

    if not user or user["role"]!="trainer":

        await msg.answer("❌ هذه القائمة للمدربين فقط")
        return

    kb=ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("🧠 تدريب")
    kb.add("↩️ رجوع")

    await msg.answer("لوحة المدرب",reply_markup=kb)

@dp.message_handler(lambda m:m.text=="🧠 تدريب")
async def trainer_start(msg:types.Message):

    trainer_state[msg.from_user.id]={"sequence":[]}

    await msg.answer(
    "ادخل 6 ضربات للتسلسل",
    reply_markup=hits_keyboard()
    )

@dp.message_handler(lambda m:m.text in OPTIONS)
async def trainer_sequence(msg:types.Message):

    state=trainer_state.get(msg.from_user.id)

    if not state:
        return

    seq=state["sequence"]

    if len(seq)<6:

        seq.append(msg.text)

        if len(seq)==6:
            await msg.answer("اختر الضربة التالية")
        else:
            await msg.answer(f"الضربة {len(seq)+1}")

        return

    next_hit=msg.text

    async with pool.acquire() as conn:

        await conn.execute(
        "INSERT INTO training_data(sequence,next_hit,trainer_id) VALUES($1,$2,$3)",
        json.dumps(seq),
        next_hit,
        msg.from_user.id
        )

    del trainer_state[msg.from_user.id]

    await msg.answer("✅ تم حفظ التدريب")

# RUN

async def on_startup(dp):

    await connect_db()

    await bot.delete_webhook(drop_pending_updates=True)

    print("Bot Running")

if __name__=="__main__":

    t=threading.Thread(target=run_web)
    t.start()

    executor.start_polling(dp,on_startup=on_startup)
