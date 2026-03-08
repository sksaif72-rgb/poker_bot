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
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# ────────────────────────────────────────────────
# WEB SERVER (Render keep-alive)
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
# DATABASE
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
    return db_execute(
        "SELECT role, subscription_end FROM users WHERE telegram_id = %s",
        (telegram_id,), fetchone=True
    )

def create_user(telegram_id):
    db_execute(
        "INSERT INTO users (telegram_id) VALUES (%s) ON CONFLICT DO NOTHING",
        (telegram_id,), commit=True
    )

def check_subscription(telegram_id):
    row = db_execute(
        "SELECT subscription_end FROM users WHERE telegram_id = %s",
        (telegram_id,), fetchone=True
    )
    return row and row[0] and row[0] > datetime.now()

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
    db_execute("UPDATE users SET subscription_end = %s WHERE telegram_id = %s",
               (end_date, telegram_id), commit=True)
    db_execute("UPDATE codes SET used = used + 1 WHERE code = %s",
               (code,), commit=True)
    return True, f"✅ تم تفعيل الاشتراك {days} يوم"

# ────────────────────────────────────────────────
# RESULT KEYBOARD (مع زر الرجوع)
# ────────────────────────────────────────────────

def build_result_keyboard():
    keyboard = []
    row = []
    for item in ITEMS:
        row.append(InlineKeyboardButton(item, callback_data=f"result_{item}"))
        if len(row) == 4:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🏠 رجوع إلى القائمة الرئيسية", callback_data="back_to_main")])
    return InlineKeyboardMarkup(keyboard)

# ────────────────────────────────────────────────
# START
# ────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    create_user(user_id)
    keyboard = [
        [KeyboardButton("🎯 توقع الجولة")],
        [KeyboardButton("🎟 تفعيل كود")],
        [KeyboardButton("👨‍🏫 لوحة المدرب")],
        [KeyboardButton("📊 احصائيات")]
    ]
    await update.message.reply_text(
        "مرحبا بك في بوت التوقعات الذكي",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )

# ────────────────────────────────────────────────
# CODE ACTIVATION
# ────────────────────────────────────────────────

async def ask_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("ادخل كود الاشتراك")
    sessions[update.effective_user.id] = {"mode": "code"}

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in sessions:
        return
    if sessions[user_id].get("mode") != "code":
        return

    code = update.message.text.strip()
    success, msg = activate_code(user_id, code)
    await update.message.reply_text(msg)
    if success:
        sessions.pop(user_id, None)

# ────────────────────────────────────────────────
# GUESS FLOW  ← تمت إضافة رقم الجولة هنا
# ────────────────────────────────────────────────

async def guess_warning(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_subscription(update.effective_user.id):
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
    sessions[user_id] = {
        "mode": "guess",
        "hits": [],
        "round_number": 1
    }
    await ask_hit(query.message, user_id)

async def ask_hit(message, user_id):
    if user_id not in sessions or "hits" not in sessions[user_id]:
        return
    step = len(sessions[user_id]["hits"]) + 1
    round_num = sessions[user_id]["round_number"]
    
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
        f"**الجولة {round_num}**  🎲\n\nاختر الضربة رقم {step}",
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
# PREDICTION
# ────────────────────────────────────────────────

def predict_sequence(sequence):
    if not sequence:
        return ITEMS[:]

    scores = {item: 0 for item in ITEMS}
    last_hit = sequence[-1]

    rows = db_execute("SELECT sequence, next_hit FROM training_data")
    for seq_json, next_hit in rows:
        try:
            seq = json.loads(seq_json) if isinstance(seq_json, str) else seq_json
        except:
            continue
        if not seq:
            continue

        if seq[-1] == last_hit:
            scores[next_hit] += 140

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

    sorted_items = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [item[0] for item in sorted_items]

async def show_prediction(message, user_id):
    if user_id not in sessions or "hits" not in sessions[user_id]:
        return
    sequence = sessions[user_id]["hits"]
    round_num = sessions[user_id]["round_number"]
    predictions = predict_sequence(sequence)
    
    text = f"**الجولة {round_num}**  🎯 التوقعات الأقوى\n\n"
    text += f"التسلسل الحالي: {' '.join(sequence)}\n\n"
    text += "\n".join(f"{i+1}️⃣ {p}" for i, p in enumerate(predictions[:4]))
    text += "\n\nاختر النتيجة الحقيقية لهذه الجولة"

    await message.reply_text(text, reply_markup=build_result_keyboard())

async def save_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if user_id not in sessions or "hits" not in sessions[user_id]:
        return
    result = query.data.replace("result_", "")
    sequence = sessions[user_id]["hits"]

    db_execute(
        "INSERT INTO user_results (telegram_id, last_hit, real_result) VALUES (%s,%s,%s)",
        (user_id, sequence[-1], result), commit=True
    )
    db_execute(
        "INSERT INTO training_data (last_hit, sequence, next_hit, trainer_id) VALUES (%s,%s,%s,%s)",
        (sequence[-1], json.dumps(sequence), result, user_id), commit=True
    )

    # تحديث التسلسل + زيادة رقم الجولة
    new_sequence = sequence[1:] + [result]
    sessions[user_id]["hits"] = new_sequence
    sessions[user_id]["round_number"] += 1

    round_num = sessions[user_id]["round_number"]
    predictions = predict_sequence(new_sequence)
    
    text = f"**الجولة {round_num}**  🎯 الجولة الجديدة\n\n"
    text += f"التسلسل الحالي: {' '.join(new_sequence)}\n\n"
    text += "\n".join(f"{i+1}️⃣ {p}" for i, p in enumerate(predictions[:4]))
    text += "\n\nاختر النتيجة الحقيقية"

    await query.message.reply_text(text, reply_markup=build_result_keyboard())

# ────────────────────────────────────────────────
# زر الرجوع إلى القائمة الرئيسية
# ────────────────────────────────────────────────

async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    sessions.pop(user_id, None)
    
    keyboard = [
        [KeyboardButton("🎯 توقع الجولة")],
        [KeyboardButton("🎟 تفعيل كود")],
        [KeyboardButton("👨‍🏫 لوحة المدرب")],
        [KeyboardButton("📊 احصائيات")]
    ]
    await query.message.reply_text(
        "🏠 تم العودة إلى القائمة الرئيسية",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )

# ────────────────────────────────────────────────
# إحصائيات
# ────────────────────────────────────────────────

async def show_statistics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    total_trainings_row = db_execute("SELECT COUNT(*) FROM training_data", fetchone=True)
    total_trainings = total_trainings_row[0] if total_trainings_row else 0
    
    total_users_row = db_execute("SELECT COUNT(*) FROM users", fetchone=True)
    total_users = total_users_row[0] if total_users_row else 0
    
    active_subs_row = db_execute(
        "SELECT COUNT(*) FROM users WHERE subscription_end > %s",
        (datetime.now(),), fetchone=True
    )
    active_subs = active_subs_row[0] if active_subs_row else 0
    
    user_results_row = db_execute(
        "SELECT COUNT(*) FROM user_results WHERE telegram_id = %s",
        (user_id,), fetchone=True
    )
    user_results_count = user_results_row[0] if user_results_row else 0
    
    user_info = get_user(user_id)
    if user_info and user_info[1] and user_info[1] > datetime.now():
        sub_text = f"✅ نشط حتى {user_info[1].strftime('%Y-%m-%d')}"
    else:
        sub_text = "❌ غير نشط (اشترك بكود)"

    text = f"""📊 إحصائيات البوت

إجمالي البيانات التدريبية: {total_trainings}
عدد المستخدمين: {total_users}
المشتركين النشطين: {active_subs}
نتائجك المسجلة: {user_results_count}
حالة اشتراكك: {sub_text}

شكراً لاستخدامك البوت! 🚀"""

    await update.message.reply_text(text)

# ────────────────────────────────────────────────
# TRAINER PANEL & TRAINING FLOW
# ────────────────────────────────────────────────

async def trainer_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if not user or user[0] != "trainer":
        await update.message.reply_text("❌ هذه اللوحة للمدربين فقط")
        return
    keyboard = [[InlineKeyboardButton("🧠 تدريب البوت", callback_data="trainer_start_training")]]
    await update.message.reply_text("👨‍🏫 لوحة المدرب", reply_markup=InlineKeyboardMarkup(keyboard))

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
    await message.reply_text(f"ادخل الضربة التدريبية رقم {step}", reply_markup=InlineKeyboardMarkup(keyboard))

async def trainer_hit_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    fruit = query.data.replace("train_hit_", "")
    keyboard = [
        [InlineKeyboardButton("✅ تم", callback_data=f"train_confirm_{fruit}")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="train_back")]
    ]
    await query.edit_message_text(f"اخترت {fruit}\nهل أنت متأكد؟", reply_markup=InlineKeyboardMarkup(keyboard))

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
        "INSERT INTO training_data (last_hit, sequence, next_hit, trainer_id) VALUES (%s,%s,%s,%s)",
        (sequence[-1], json.dumps(sequence), result, user_id), commit=True
    )
    sessions.pop(user_id, None)
    await query.message.reply_text("✅ تم حفظ التدريب بنجاح")

# ────────────────────────────────────────────────
# MAIN
# ────────────────────────────────────────────────

def main():
    keep_alive()
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("^(🎯 توقع الجولة)$"), guess_warning))
    app.add_handler(MessageHandler(filters.Regex("^(🎟 تفعيل كود)$"), ask_code))
    app.add_handler(MessageHandler(filters.Regex("^(👨‍🏫 لوحة المدرب)$"), trainer_panel))
    app.add_handler(MessageHandler(filters.Regex("^(📊 احصائيات)$"), show_statistics))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.add_handler(CallbackQueryHandler(start_guess, pattern="^start_guess$"))
    app.add_handler(CallbackQueryHandler(hit_selected, pattern="^hit_"))
    app.add_handler(CallbackQueryHandler(confirm_hit, pattern="^confirm_hit_"))
    app.add_handler(CallbackQueryHandler(back_hit, pattern="^back_hit$"))
    app.add_handler(CallbackQueryHandler(save_result, pattern="^result_"))
    app.add_handler(CallbackQueryHandler(back_to_main, pattern="^back_to_main$"))
    app.add_handler(CallbackQueryHandler(trainer_start_training, pattern="^trainer_start_training$"))
    app.add_handler(CallbackQueryHandler(trainer_hit_selected, pattern="^train_hit_"))
    app.add_handler(CallbackQueryHandler(trainer_confirm_hit, pattern="^train_confirm_"))
    app.add_handler(CallbackQueryHandler(trainer_back, pattern="^train_back$"))
    app.add_handler(CallbackQueryHandler(trainer_save, pattern="^train_result_"))

    print("BOT STARTED")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
