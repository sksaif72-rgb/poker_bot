import logging
import psycopg2
import json
import os
from datetime import datetime, timedelta, timezone
from collections import Counter
from flask import Flask
from threading import Thread

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters


# ---------------- CONFIG ----------------

TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))


# ---------------- WEB SERVER ----------------

app_web = Flask(__name__)

@app_web.route("/")
def home():
    return "Bot is alive"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app_web.run(host="0.0.0.0", port=port)

def keep_alive():
    Thread(target=run_web, daemon=True).start()


# ---------------- LOGGING ----------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ---------------- DATABASE ----------------

try:
    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
except Exception as e:
    logger.error(f"Database error: {e}")
    conn = None


def db_execute(query, params=None, fetchone=False, commit=False):
    if conn is None:
        return None if fetchone else []

    try:
        with conn.cursor() as cur:
            cur.execute(query, params or ())
            if fetchone:
                return cur.fetchone()
            if commit:
                conn.commit()
                return True
            return cur.fetchall()

    except Exception as e:
        logger.error(f"DB error {e}")
        if commit:
            conn.rollback()
        return None if fetchone else []


# ---------------- GAME ITEMS ----------------

ITEMS = ["🍎","🍊","🥬","🍉","🐟","🍔","🍤","🍗"]
ALL_ITEMS_SET = set(ITEMS)


# ---------------- USERS ----------------

def create_user(user_id):

    db_execute(
        "INSERT INTO users (telegram_id) VALUES (%s) ON CONFLICT DO NOTHING",
        (user_id,),
        commit=True
    )


def get_subscription(user_id):

    row = db_execute(
        "SELECT subscription_end FROM users WHERE telegram_id=%s",
        (user_id,),
        fetchone=True
    )

    if not row or not row[0]:
        return "❌ لا يوجد اشتراك"

    if row[0] < datetime.now(timezone.utc):
        return "❌ منتهي"

    delta = row[0] - datetime.now(timezone.utc)

    return f"✅ متبقي {delta.days} يوم"


# ---------------- CODE ACTIVATION ----------------

def activate_code(user_id, code):

    data = db_execute(
        "SELECT days,used,max_use FROM codes WHERE code=%s",
        (code,),
        fetchone=True
    )

    if not data:
        return False,"❌ الكود غير موجود"

    days,used,max_use = data

    if used >= max_use:
        return False,"❌ الكود مستنفد"

    end_date = datetime.now(timezone.utc) + timedelta(days=days)

    db_execute(
        "UPDATE users SET subscription_end=%s WHERE telegram_id=%s",
        (end_date,user_id),
        commit=True
    )

    db_execute(
        "UPDATE codes SET used=used+1 WHERE code=%s",
        (code,),
        commit=True
    )

    return True,f"✅ تم التفعيل لمدة {days} يوم"


# ---------------- KEYBOARD ----------------

def main_keyboard():

    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🎯 توقع الجولة")],
            [KeyboardButton("👤 حسابي")],
            [KeyboardButton("🎟 تفعيل كود")],
            [KeyboardButton("📊 إحصائيات")]
        ],
        resize_keyboard=True
    )


# ---------------- PREDICTION ----------------

def predict(sequence):

    rows = db_execute("SELECT next_hit FROM training_data") or []

    counter = Counter(rows)

    scores = {i:counter.get(i,0) for i in ITEMS}

    sorted_items = sorted(scores.items(),key=lambda x:x[1],reverse=True)[:5]

    total = sum(scores.values()) or 1

    result=[]
    perc=[]

    for item,val in sorted_items:
        result.append(item)
        perc.append(round(val/total*100))

    return result,perc


# ---------------- HANDLERS ----------------

async def start(update:Update,context:ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id

    create_user(user_id)

    await update.message.reply_text(
        f"🤖 Cowboy Bot\n\nاشتراكك: {get_subscription(user_id)}",
        reply_markup=main_keyboard()
    )


async def profile(update:Update,context:ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id

    await update.message.reply_text(
        f"👤 حسابك\n\nID: {user_id}\nالاشتراك: {get_subscription(user_id)}"
    )


async def ask_code(update:Update,context:ContextTypes.DEFAULT_TYPE):

    context.user_data["wait_code"]=True

    await update.message.reply_text("🎟 أرسل كود التفعيل")


async def stats(update:Update,context:ContextTypes.DEFAULT_TYPE):

    rows = db_execute("SELECT next_hit FROM training_data") or []

    counter = Counter(rows)

    total = sum(counter.values())

    text="📊 الإحصائيات\n\n"

    for item in ITEMS:

        count = counter.get(item,0)

        percent = round(count/total*100) if total else 0

        text+=f"{item} : {percent}%\n"

    await update.message.reply_text(text)


async def handle_text(update:Update,context:ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id
    text = update.message.text.strip()

    if context.user_data.get("wait_code"):

        context.user_data["wait_code"]=False

        ok,msg = activate_code(user_id,text)

        await update.message.reply_text(msg)

        return

    sequence = text.split()

    if not set(sequence).issubset(ALL_ITEMS_SET):

        await update.message.reply_text(
            "❌ أرسل الرموز فقط\n\nمثال\n🍎 🍊 🥬 🍉 🐟 🍔"
        )

        return

    pred,perc = predict(sequence)

    msg="🤖 التوقع\n\n"

    for i,p in zip(pred,perc):

        msg+=f"{i} {p}%\n"

    await update.message.reply_text(msg)


# ---------------- MAIN ----------------

def main():

    keep_alive()

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start",start))

    app.add_handler(MessageHandler(filters.Regex("^(👤 حسابي)$"),profile))
    app.add_handler(MessageHandler(filters.Regex("^(🎟 تفعيل كود)$"),ask_code))
    app.add_handler(MessageHandler(filters.Regex("^(📊 إحصائيات)$"),stats))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,handle_text))

    print("Bot started")

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__=="__main__":
    main()
