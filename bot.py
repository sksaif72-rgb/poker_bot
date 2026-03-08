import logging
import psycopg2
import json
from datetime import datetime, timedelta

from flask import Flask
from threading import Thread
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton
)

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters
)

# =========================
# CONFIG
# =========================

import os

TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_ID = os.getenv("ADMIN_ID")
# =========================
# WEB SERVER (RENDER)
# =========================

app_web = Flask(__name__)

@app_web.route("/")
def home():
    return "Bot is running"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app_web.run(host="0.0.0.0", port=port)

def keep_alive():
    t = Thread(target=run_web)
    t.start()
# =========================
# LOGGING
# =========================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# =========================
# DATABASE
# =========================

conn = psycopg2.connect(DATABASE_URL, sslmode="require")
# =========================
# GAME OPTIONS
# =========================

ITEMS = [
    "🍎",
    "🍊",
    "🥬",
    "🍉",
    "🐟",
    "🍔",
    "🍤",
    "🍗"
]

# =========================
# USER SESSION
# =========================

sessions = {}

# =========================
# DATABASE FUNCTIONS
# =========================

def get_user(telegram_id):

    cur = conn.cursor()

    cur.execute(
        "SELECT role, subscription_end FROM users WHERE telegram_id=%s",
        (telegram_id,)
    )

    user = cur.fetchone()

    cur.close()

    return user


def create_user(telegram_id):

    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO users (telegram_id)
        VALUES (%s)
        ON CONFLICT (telegram_id) DO NOTHING
        """,
        (telegram_id,)
    )

    conn.commit()

    cur.close()


def check_subscription(telegram_id):

    cur = conn.cursor()

    cur.execute(
        "SELECT subscription_end FROM users WHERE telegram_id=%s",
        (telegram_id,)
    )

    r = cur.fetchone()

    cur.close()

    if not r:
        return False

    if r[0] is None:
        return False

    return r[0] > datetime.now()


def activate_code(telegram_id, code):

    cur = conn.cursor()

    cur.execute(
        "SELECT days, used, max_use FROM codes WHERE code=%s",
        (code,)
    )

    data = cur.fetchone()

    if not data:
        cur.close()
        return False, "❌ الكود غير صحيح"

    days, used, max_use = data

    if used >= max_use:
        cur.close()
        return False, "❌ الكود منتهي"

    end_date = datetime.now() + timedelta(days=days)

    cur.execute(
        """
        UPDATE users
        SET subscription_end=%s
        WHERE telegram_id=%s
        """,
        (end_date, telegram_id)
    )

    cur.execute(
        "UPDATE codes SET used = used + 1 WHERE code=%s",
        (code,)
    )

    conn.commit()

    cur.close()

    return True, f"✅ تم تفعيل الاشتراك {days} يوم"


# =========================
# START COMMAND
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.message.from_user.id

    create_user(user_id)

    keyboard = [

        [KeyboardButton("🎯 توقع الجولة")],
        [KeyboardButton("🎟 تفعيل كود")],
        [KeyboardButton("👨‍🏫 لوحة المدرب")]

    ]

    await update.message.reply_text(

        "مرحبا بك في بوت التوقعات الذكي",

        reply_markup=ReplyKeyboardMarkup(
            keyboard,
            resize_keyboard=True
        )
    )


# =========================
# SUBSCRIPTION CODE INPUT
# =========================

async def ask_code(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "ادخل كود الاشتراك"
    )

    sessions[update.message.from_user.id] = {
        "mode": "code"
    }


async def receive_code(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.message.from_user.id

    if user_id not in sessions:
        return

    if sessions[user_id]["mode"] != "code":
        return

    code = update.message.text.strip()

    ok, msg = activate_code(user_id, code)

    await update.message.reply_text(msg)

    sessions.pop(user_id)


# =========================
# WARNING BEFORE GUESS
# =========================

async def guess_warning(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.message.from_user.id

    if not check_subscription(user_id):

        await update.message.reply_text(
            "❌ يجب الاشتراك أولاً"
        )

        return

    keyboard = [

        [InlineKeyboardButton(
            "ابدأ",
            callback_data="start_guess"
        )]

    ]

    await update.message.reply_text(

        "⚠️ تحذير\n\n"
        "تأكد من إدخال الضربات بشكل صحيح.\n"
        "إذا أخطأت استخدم زر الرجوع.",

        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    # =========================
# START GUESS
# =========================

async def start_guess(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    sessions[user_id] = {
        "mode": "guess",
        "hits": []
    }

    await ask_hit(query.message, user_id)


# =========================
# ASK FOR HIT
# =========================

async def ask_hit(message, user_id):

    step = len(sessions[user_id]["hits"]) + 1

    keyboard = []

    row = []

    for item in ITEMS:

        row.append(
            InlineKeyboardButton(
                item,
                callback_data=f"hit_{item}"
            )
        )

        if len(row) == 4:
            keyboard.append(row)
            row = []

    keyboard.append(
        [InlineKeyboardButton("🔙 رجوع", callback_data="back_hit")]
    )

    await message.reply_text(

        f"اختر الضربة رقم {step}",

        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# =========================
# HIT SELECTED
# =========================

async def hit_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    fruit = query.data.split("_")[1]

    keyboard = [

        [InlineKeyboardButton(
            "✅ تم",
            callback_data=f"confirm_hit_{fruit}"
        )],

        [InlineKeyboardButton(
            "🔙 رجوع",
            callback_data="back_hit"
        )]

    ]

    await query.edit_message_text(

        f"اخترت {fruit}\n\nهل أنت متأكد؟",

        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# =========================
# CONFIRM HIT
# =========================

async def confirm_hit(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    fruit = query.data.replace("confirm_hit_", "")

    sessions[user_id]["hits"].append(fruit)

    if len(sessions[user_id]["hits"]) < 6:

        await ask_hit(query.message, user_id)

    else:

        await show_prediction(query.message, user_id)


# =========================
# BACK BUTTON
# =========================

async def back_hit(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    if user_id not in sessions:
        return

    hits = sessions[user_id]["hits"]

    if hits:
        hits.pop()

    await ask_hit(query.message, user_id)
    # =========================
# PREDICTION ENGINE
# =========================

def predict_sequence(sequence):

    scores = {}

    for item in ITEMS:
        scores[item] = 0

    cur = conn.cursor()

    # قراءة بيانات التدريب
    cur.execute("SELECT sequence, next_hit FROM training_data")

    rows = cur.fetchall()

    for seq_json, next_hit in rows:

    if isinstance(seq_json, str):
        seq = json.loads(seq_json)
    else:
        seq = seq_json

        seq = json.loads(seq_json)

        if seq == sequence:
            scores[next_hit] += 120

        if len(seq) >= 5 and seq[-5:] == sequence[-5:]:
            scores[next_hit] += 90

        if len(seq) >= 4 and seq[-4:] == sequence[-4:]:
            scores[next_hit] += 70

        if len(seq) >= 3 and seq[-3:] == sequence[-3:]:
            scores[next_hit] += 40

        if len(seq) >= 2 and seq[-2:] == sequence[-2:]:
            scores[next_hit] += 20

        if seq[-1] == sequence[-1]:
            scores[next_hit] += 10

    # قراءة نتائج المستخدمين
    cur.execute("SELECT last_hit, real_result FROM user_results")

    rows = cur.fetchall()

    for last_hit, result in rows:

        if last_hit == sequence[-1]:
            scores[result] += 15

    cur.close()

    # ترتيب النتائج
    sorted_items = sorted(
        scores.items(),
        key=lambda x: x[1],
        reverse=True
    )

    return [item[0] for item in sorted_items]


# =========================
# SHOW PREDICTION
# =========================

async def show_prediction(message, user_id):

    sequence = sessions[user_id]["hits"]

    predictions = predict_sequence(sequence)

    text = "🎯 التوقعات الأقوى\n\n"

    for i, p in enumerate(predictions[:4]):

        text += f"{i+1}️⃣ {p}\n"

    text += "\nاختر النتيجة الحقيقية"

    keyboard = []

    row = []

    for item in ITEMS:

        row.append(
            InlineKeyboardButton(
                item,
                callback_data=f"result_{item}"
            )
        )

        if len(row) == 4:
            keyboard.append(row)
            row = []

    await message.reply_text(

        text,

        reply_markup=InlineKeyboardMarkup(keyboard)
    )
# =========================
# SAVE RESULT
# =========================
async def save_result(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    result = query.data.replace("result_", "")

    if user_id not in sessions:
        return

    sequence = sessions[user_id]["hits"]

    cur = conn.cursor()

    # حفظ نتيجة المستخدم
    cur.execute(
        """
        INSERT INTO user_results
        (telegram_id, last_hit, real_result)
        VALUES (%s,%s,%s)
        """,
        (
            user_id,
            sequence[-1],
            result
        )
    )

    # تحويل نتيجة المستخدم إلى تدريب
    cur.execute(
        """
        INSERT INTO training_data
        (last_hit, sequence, next_hit, trainer_id)
        VALUES (%s,%s,%s,%s)
        """,
        (
            sequence[-1],
            json.dumps(sequence),
            result,
            user_id
        )
    )

    conn.commit()

    cur.close()

    # تحديث التسلسل للجولة القادمة

    sequence.pop(0)
    sequence.append(result)

    sessions[user_id]["hits"] = sequence

    # حساب توقعات جديدة

    predictions = predict_sequence(sequence)

    text = "🎯 الجولة الجديدة\n\n"

    for i, p in enumerate(predictions[:4]):

        text += f"{i+1}️⃣ {p}\n"

    text += "\nاختر النتيجة الحقيقية"

    keyboard = []

    row = []

    for item in ITEMS:

        row.append(
            InlineKeyboardButton(
                item,
                callback_data=f"result_{item}"
            )
        )

        if len(row) == 4:
            keyboard.append(row)
            row = []

    await query.message.reply_text(

        text,

        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    # =========================
# TRAINER PANEL
# =========================

async def trainer_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.message.from_user.id

    user = get_user(user_id)

    if not user:
        await update.message.reply_text("❌ غير مسموح")
        return

    role = user[0]

    if role != "trainer":
        await update.message.reply_text("❌ هذه اللوحة للمدربين فقط")
        return

    keyboard = [
        [InlineKeyboardButton("🧠 تدريب البوت", callback_data="trainer_start_training")]
    ]

    await update.message.reply_text(
        "👨‍🏫 لوحة المدرب",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# =========================
# START TRAINING
# =========================

async def trainer_start_training(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    sessions[user_id] = {
        "mode": "training",
        "hits": []
    }

    await trainer_ask_hit(query.message, user_id)


# =========================
# ASK TRAIN HIT
# =========================

async def trainer_ask_hit(message, user_id):

    step = len(sessions[user_id]["hits"]) + 1

    keyboard = []
    row = []

    for item in ITEMS:

        row.append(
            InlineKeyboardButton(
                item,
                callback_data=f"train_hit_{item}"
            )
        )

        if len(row) == 4:
            keyboard.append(row)
            row = []

    keyboard.append(
        [InlineKeyboardButton("🔙 رجوع", callback_data="train_back")]
    )

    await message.reply_text(
        f"ادخل الضربة التدريبية رقم {step}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# =========================
# TRAIN HIT SELECT
# =========================

async def trainer_hit_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    fruit = query.data.replace("train_hit_", "")

    keyboard = [

        [InlineKeyboardButton("✅ تم", callback_data=f"train_confirm_{fruit}")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="train_back")]

    ]

    await query.edit_message_text(
        f"اخترت {fruit}\nهل أنت متأكد؟",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# =========================
# CONFIRM TRAIN HIT
# =========================
async def trainer_confirm_hit(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    fruit = query.data.replace("train_confirm_", "")

    sessions[user_id]["hits"].append(fruit)

    if len(sessions[user_id]["hits"]) == 6:

        sequence = sessions[user_id]["hits"]
        seq_text = " ".join(sequence)

        keyboard = []
        row = []

        for item in ITEMS:

            row.append(
                InlineKeyboardButton(
                    item,
                    callback_data=f"train_result_{item}"
                )
            )

            if len(row) == 4:
                keyboard.append(row)
                row = []

        await query.message.reply_text(
            f"📊 بيانات التدريب\n\n"
            f"التسلسل:\n{seq_text}\n\n"
            f"اختر الضربة رقم 7",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    else:

        await trainer_ask_hit(query.message, user_id)
# =========================
# TRAIN BACK
# =========================

async def trainer_back(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    if user_id not in sessions:
        return

    hits = sessions[user_id]["hits"]

    if hits:
        hits.pop()

    await trainer_ask_hit(query.message, user_id)


# =========================
# SAVE TRAINING DATA
# =========================

async def trainer_save(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    result = query.data.replace("train_result_", "")

    sequence = sessions[user_id]["hits"]

    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO training_data
        (last_hit, sequence, next_hit, trainer_id)
        VALUES (%s,%s,%s,%s)
        """,
        (
            sequence[-1],
            json.dumps(sequence),
            result,
            user_id
        )
    )

    conn.commit()
    cur.close()

    sessions.pop(user_id)

    await query.message.reply_text(
        "✅ تم حفظ التدريب بنجاح"
    )
    # =========================
# MAIN FUNCTION
# =========================

def main():

    keep_alive()

    app = ApplicationBuilder().token(TOKEN).build()
    # أوامر

    app.add_handler(CommandHandler("start", start))

    # أزرار القائمة

    app.add_handler(MessageHandler(
        filters.TEXT & filters.Regex("🎟 تفعيل كود"),
        ask_code
    ))

    app.add_handler(MessageHandler(
        filters.TEXT & filters.Regex("🎯 توقع الجولة"),
        guess_warning
    ))

    app.add_handler(MessageHandler(
        filters.TEXT & filters.Regex("👨‍🏫 لوحة المدرب"),
        trainer_panel
    ))

    # استقبال كود الاشتراك

    app.add_handler(MessageHandler(
        filters.TEXT,
        receive_code
    ))

    # التخمين

    app.add_handler(CallbackQueryHandler(
        start_guess,
        pattern="start_guess"
    ))

    app.add_handler(CallbackQueryHandler(
        hit_selected,
        pattern="^hit_"
    ))

    app.add_handler(CallbackQueryHandler(
        confirm_hit,
        pattern="^confirm_hit_"
    ))

    app.add_handler(CallbackQueryHandler(
        back_hit,
        pattern="back_hit"
    ))

    app.add_handler(CallbackQueryHandler(
        save_result,
        pattern="^result_"
    ))

    # التدريب

    app.add_handler(CallbackQueryHandler(
        trainer_start_training,
        pattern="trainer_start_training"
    ))

    app.add_handler(CallbackQueryHandler(
        trainer_hit_selected,
        pattern="^train_hit_"
    ))

    app.add_handler(CallbackQueryHandler(
        trainer_confirm_hit,
        pattern="^train_confirm_"
    ))

    app.add_handler(CallbackQueryHandler(
        trainer_back,
        pattern="train_back"
    ))

    app.add_handler(CallbackQueryHandler(
        trainer_save,
        pattern="^train_result_"
    ))

    print("BOT STARTED")

    app.run_polling()


# =========================
# RUN
# =========================

if __name__ == "__main__":
    main()
    
