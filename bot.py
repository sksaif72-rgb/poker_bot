import logging
import psycopg2
import json
from datetime import datetime, timedelta
import os

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

# ────────────────────────────────────────────────
# CONFIG
# ────────────────────────────────────────────────

TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # للأمان

# ────────────────────────────────────────────────
# WEB SERVER (لـ Render / keep-alive)
# ────────────────────────────────────────────────

app_web = Flask(__name__)

@app_web.route("/")
def home():
    return "Bot is running"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app_web.run(host="0.0.0.0", port=port)

def keep_alive():
    Thread(target=run_web, daemon=True).start()

# ────────────────────────────────────────────────
# LOGGING
# ────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────
# DATABASE CONNECTION
# ────────────────────────────────────────────────

conn = psycopg2.connect(DATABASE_URL, sslmode="require")

# ────────────────────────────────────────────────
# GAME ITEMS
# ────────────────────────────────────────────────

ITEMS = ["🍎", "🍊", "🥬", "🍉", "🐟", "🍔", "🍤", "🍗"]

# ────────────────────────────────────────────────
# SESSIONS
# ────────────────────────────────────────────────

sessions = {}

# ────────────────────────────────────────────────
# DATABASE HELPERS
# ────────────────────────────────────────────────

def db_execute(query, params=None, fetchone=False, commit=False):
    try:
        with conn.cursor() as cur:
            cur.execute(query, params or ())
            if fetchone:
                return cur.fetchone()
            if commit:
                conn.commit()
                return True
            return cur.fetchall()
    except psycopg2.Error as e:
        logger.error(f"Database error: {e}")
        if commit:
            conn.rollback()
        return None if fetchone else []

def get_user(telegram_id):
    row = db_execute(
        "SELECT role, subscription_end FROM users WHERE telegram_id = %s",
        (telegram_id,), fetchone=True
    )
    return row

def create_user(telegram_id):
    db_execute(
        "INSERT INTO users (telegram_id) VALUES (%s) ON CONFLICT (telegram_id) DO NOTHING",
        (telegram_id,), commit=True
    )

def check_subscription(telegram_id):
    row = db_execute(
        "SELECT subscription_end FROM users WHERE telegram_id = %s",
        (telegram_id,), fetchone=True
    )
    if not row or not row[0]:
        return False
    return row[0] > datetime.now()

def activate_code(telegram_id, code):
    data = db_execute(
        "SELECT days, used, max_use FROM codes WHERE code = %s",
        (code,), fetchone=True
    )
    if not data:
        return False, "❌ الكود غير صحيح"

    days, used, max_use = data
    if used >= max_use:
        return False, "❌ الكود منتهي"

    end_date = datetime.now() + timedelta(days=days)

    db_execute(
        "UPDATE users SET subscription_end = %s WHERE telegram_id = %s",
        (end_date, telegram_id), commit=True
    )
    db_execute(
        "UPDATE codes SET used = used + 1 WHERE code = %s",
        (code,), commit=True
    )
    return True, f"✅ تم تفعيل الاشتراك {days} يوم"

# ────────────────────────────────────────────────
# START COMMAND
# ────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    create_user(user_id)

    keyboard = [
        [KeyboardButton("🎯 توقع الجولة")],
        [KeyboardButton("🎟 تفعيل كود")],
        [KeyboardButton("👨‍🏫 لوحة المدرب")]
    ]

    await update.message.reply_text(
        "مرحبا بك في بوت التوقعات الذكي",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )

# ────────────────────────────────────────────────
# CODE ACTIVATION FLOW
# ────────────────────────────────────────────────

async def ask_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("ادخل كود الاشتراك")
    sessions[update.effective_user.id] = {"mode": "code"}

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id not in sessions:
        return

    mode = sessions[user_id].get("mode")

    if mode == "code":
        code = update.message.text.strip()
        success, msg = activate_code(user_id, code)
        await update.message.reply_text(msg)
        if success:
            sessions.pop(user_id, None)

# ────────────────────────────────────────────────
# GUESS FLOW
# ────────────────────────────────────────────────

async def guess_warning(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not check_subscription(user_id):
        await update.message.reply_text("❌ يجب الاشتراك أولاً")
        return

    keyboard = [[InlineKeyboardButton("ابدأ", callback_data="start_guess")]]
    await update.message.reply_text(
        "⚠️ تحذير\n\nتأكد من إدخال الضربات بشكل صحيح.\nإذا أخطأت استخدم زر الرجوع.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def start_guess(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    sessions[user_id] = {"mode": "guess", "hits": []}
    await ask_hit(query.message, user_id)

async def ask_hit(message, user_id):
    if user_id not in sessions or "hits" not in sessions[user_id]:
        return

    step = len(sessions[user_id]["hits"]) + 1

    keyboard = []
    row = []
    for item in ITEMS:
        row.append(InlineKeyboardButton(item, callback_data=f"hit_{item}"))
        if len(row) == 4:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="back_hit")])

    await message.reply_text(
        f"اختر الضربة رقم {step}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def hit_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id not in sessions or "hits" not in sessions[user_id]:
        return

    fruit = query.data.split("_", 1)[1]

    keyboard = [
        [InlineKeyboardButton("✅ تم", callback_data=f"confirm_hit_{fruit}")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="back_hit")]
    ]

    await query.edit_message_text(
        f"اخترت {fruit}\n\nهل أنت متأكد؟",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def confirm_hit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id not in sessions or "hits" not in sessions[user_id]:
        return

    fruit = query.data.replace("confirm_hit_", "")
    sessions[user_id]["hits"].append(fruit)

    if len(sessions[user_id]["hits"]) < 6:
        await ask_hit(query.message, user_id)
    else:
        await show_prediction(query.message, user_id)

async def back_hit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id not in sessions or "hits" not in sessions[user_id]:
        return

    if sessions[user_id]["hits"]:
        sessions[user_id]["hits"].pop()

    await ask_hit(query.message, user_id)

# ────────────────────────────────────────────────
# PREDICTION LOGIC
# ────────────────────────────────────────────────

def predict_sequence(sequence):
    scores = {item: 0 for item in ITEMS}

    rows = db_execute("SELECT sequence, next_hit FROM training_data")
    for seq_json, next_hit in rows:
        try:
            seq = json.loads(seq_json) if isinstance(seq_json, str) else seq_json
        except:
            continue

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
        if seq and seq[-1] == sequence[-1]:
            scores[next_hit] += 10

    sorted_items = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [item[0] for item in sorted_items]

async def show_prediction(message, user_id):
    if user_id not in sessions or "hits" not in sessions[user_id]:
        return

    sequence = sessions[user_id]["hits"]
    predictions = predict_sequence(sequence)

    text = "🎯 التوقعات الأقوى\n\n" + "\n".join(f"{i+1}️⃣ {p}" for i, p in enumerate(predictions[:4]))
    text += "\n\nاختر النتيجة الحقيقية"

    keyboard = []
    row = []
    for item in ITEMS:
        row.append(InlineKeyboardButton(item, callback_data=f"result_{item}"))
        if len(row) == 4:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    await message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def save_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id not in sessions or "hits" not in sessions[user_id]:
        return

    result = query.data.replace("result_", "")
    sequence = sessions[user_id]["hits"]

    # حفظ نتيجة المستخدم
    db_execute(
        "INSERT INTO user_results (telegram_id, last_hit, real_result) VALUES (%s, %s, %s)",
        (user_id, sequence[-1], result),
        commit=True
    )

    # حفظ للتدريب
    db_execute(
        "INSERT INTO training_data (last_hit, sequence, next_hit, trainer_id) VALUES (%s, %s, %s, %s)",
        (sequence[-1], json.dumps(sequence), result, user_id),
        commit=True
    )

    # تحديث التسلسل للجولة التالية (sliding window)
    new_sequence = sequence[1:] + [result]
    sessions[user_id]["hits"] = new_sequence

    # عرض التوقع الجديد
    predictions = predict_sequence(new_sequence)
    text = "🎯 الجولة الجديدة\n\n" + "\n".join(f"{i+1}️⃣ {p}" for i, p in enumerate(predictions[:4]))
    text += "\n\nاختر النتيجة الحقيقية"

    keyboard = []
    row = []
    for item in ITEMS:
        row.append(InlineKeyboardButton(item, callback_data=f"result_{item}"))
        if len(row) == 4:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

# ────────────────────────────────────────────────
# TRAINER PANEL
# ────────────────────────────────────────────────

async def trainer_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)

    if not user or user[0] != "trainer":
        await update.message.reply_text("❌ هذه اللوحة للمدربين فقط")
        return

    keyboard = [[InlineKeyboardButton("🧠 تدريب البوت", callback_data="trainer_start_training")]]
    await update.message.reply_text(
        "👨‍🏫 لوحة المدرب",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ────────────────────────────────────────────────
# TRAINING FLOW
# ────────────────────────────────────────────────

async def trainer_start_training(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    sessions[user_id] = {"mode": "training", "hits": []}
    await trainer_ask_hit(query.message, user_id)

async def trainer_ask_hit(message, user_id):
    if user_id not in sessions or "hits" not in sessions[user_id]:
        return

    step = len(sessions[user_id]["hits"]) + 1

    keyboard = []
    row = []
    for item in ITEMS:
        row.append(InlineKeyboardButton(item, callback_data=f"train_hit_{item}"))
        if len(row) == 4:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="train_back")])

    await message.reply_text(
        f"ادخل الضربة التدريبية رقم {step}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def trainer_hit_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id not in sessions:
        return

    fruit = query.data.replace("train_hit_", "")

    keyboard = [
        [InlineKeyboardButton("✅ تم", callback_data=f"train_confirm_{fruit}")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="train_back")]
    ]

    await query.edit_message_text(
        f"اخترت {fruit}\nهل أنت متأكد؟",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def trainer_confirm_hit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id not in sessions or "hits" not in sessions[user_id]:
        return

    fruit = query.data.replace("train_confirm_", "")
    sessions[user_id]["hits"].append(fruit)

    if len(sessions[user_id]["hits"]) == 6:
        seq_text = " ".join(sessions[user_id]["hits"])
        keyboard = []
        row = []
        for item in ITEMS:
            row.append(InlineKeyboardButton(item, callback_data=f"train_result_{item}"))
            if len(row) == 4:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)

        await query.message.reply_text(
            f"📊 بيانات التدريب\n\nالتسلسل:\n{seq_text}\n\nاختر الضربة رقم 7",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await trainer_ask_hit(query.message, user_id)

async def trainer_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id not in sessions or "hits" not in sessions[user_id]:
        return

    if sessions[user_id]["hits"]:
        sessions[user_id]["hits"].pop()

    await trainer_ask_hit(query.message, user_id)

async def trainer_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id not in sessions or "hits" not in sessions[user_id]:
        return

    result = query.data.replace("train_result_", "")
    sequence = sessions[user_id]["hits"]

    db_execute(
        "INSERT INTO training_data (last_hit, sequence, next_hit, trainer_id) VALUES (%s, %s, %s, %s)",
        (sequence[-1], json.dumps(sequence), result, user_id),
        commit=True
    )

    sessions.pop(user_id, None)
    await query.message.reply_text("✅ تم حفظ التدريب بنجاح")

# ────────────────────────────────────────────────
# MAIN
# ────────────────────────────────────────────────

def main():
    keep_alive()

    app = ApplicationBuilder().token(TOKEN).build()

    # Commands & menu buttons
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("^(🎯 توقع الجولة)$"), guess_warning))
    app.add_handler(MessageHandler(filters.Regex("^(🎟 تفعيل كود)$"), ask_code))
    app.add_handler(MessageHandler(filters.Regex("^(👨‍🏫 لوحة المدرب)$"), trainer_panel))

    # Text handler (only for code input now)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Callback queries
    app.add_handler(CallbackQueryHandler(start_guess, pattern="^start_guess$"))
    app.add_handler(CallbackQueryHandler(hit_selected, pattern="^hit_"))
    app.add_handler(CallbackQueryHandler(confirm_hit, pattern="^confirm_hit_"))
    app.add_handler(CallbackQueryHandler(back_hit, pattern="^back_hit$"))
    app.add_handler(CallbackQueryHandler(save_result, pattern="^result_"))

    app.add_handler(CallbackQueryHandler(trainer_start_training, pattern="^trainer_start_training$"))
    app.add_handler(CallbackQueryHandler(trainer_hit_selected, pattern="^train_hit_"))
    app.add_handler(CallbackQueryHandler(trainer_confirm_hit, pattern="^train_confirm_"))
    app.add_handler(CallbackQueryHandler(trainer_back, pattern="^train_back$"))
    app.add_handler(CallbackQueryHandler(trainer_save, pattern="^train_result_"))

    print("BOT STARTED")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
