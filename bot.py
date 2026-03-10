import logging
import psycopg2
import json
from datetime import datetime, timedelta, timezone
import os
from collections import Counter, defaultdict

from flask import Flask
from threading import Thread

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton
)

from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
    CallbackQueryHandler, MessageHandler, filters
)

# ────────────────────────────────────────────────
# CONFIG
# ────────────────────────────────────────────────
TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# ────────────────────────────────────────────────
# WEB SERVER
# ────────────────────────────────────────────────
app_web = Flask(__name__)
@app_web.route("/")
def home(): return "Bot is running"
def run_web():
    port = int(os.environ.get("PORT", 10000))
    app_web.run(host="0.0.0.0", port=port)
def keep_alive():
    Thread(target=run_web, daemon=True).start()

# ────────────────────────────────────────────────
# LOGGING + DB
# ────────────────────────────────────────────────
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
conn = psycopg2.connect(DATABASE_URL, sslmode="require")

# ────────────────────────────────────────────────
# GAME ITEMS
# ────────────────────────────────────────────────
ITEMS = ["🍎", "🍊", "🥬", "🍉", "🐟", "🍔", "🍤", "🍗"]
FRUITS = ["🍎", "🍊", "🥬", "🍉"]
MEATS  = ["🐟", "🍔", "🍤", "🍗"]

# ────────────────────────────────────────────────
# SESSIONS + CACHE
# ────────────────────────────────────────────────
sessions = {}
prediction_cache = {}

# ────────────────────────────────────────────────
# DB HELPERS
# ────────────────────────────────────────────────
def db_execute(query, params=None, fetchone=False, commit=False):
    try:
        with conn.cursor() as cur:
            cur.execute(query, params or ())
            if fetchone: return cur.fetchone()
            if commit:
                conn.commit()
                return True
            return cur.fetchall()
    except Exception as e:
        logger.error(f"DB error: {e}")
        if commit: conn.rollback()
        return None if fetchone else []

def get_user(telegram_id):
    return db_execute("SELECT role, subscription_end FROM users WHERE telegram_id = %s", (telegram_id,), fetchone=True)

def create_user(telegram_id):
    db_execute("INSERT INTO users (telegram_id) VALUES (%s) ON CONFLICT DO NOTHING", (telegram_id,), commit=True)

def check_subscription(telegram_id):
    row = db_execute("SELECT subscription_end FROM users WHERE telegram_id = %s", (telegram_id,), fetchone=True)
    return row and row[0] and row[0] > datetime.now(timezone.utc)

def get_remaining_time(telegram_id):
    row = db_execute("SELECT subscription_end FROM users WHERE telegram_id = %s", (telegram_id,), fetchone=True)
    if not row or not row[0] or row[0] <= datetime.now(timezone.utc):
        return "❌ منتهي أو غير نشط"
    delta = row[0] - datetime.now(timezone.utc)
    return f"✅ نشط | متبقي {delta.days} يوم و {delta.seconds//3600} ساعة"

def activate_code(telegram_id, code):
    data = db_execute("SELECT days, used, max_use FROM codes WHERE code = %s", (code,), fetchone=True)
    if not data: return False, "❌ الكود غير صحيح"
    days, used, max_use = data
    if used >= max_use: return False, "❌ الكود منتهي"
    end_date = datetime.now(timezone.utc) + timedelta(days=days)
    db_execute("UPDATE users SET subscription_end = %s WHERE telegram_id = %s", (end_date, telegram_id), commit=True)
    db_execute("UPDATE codes SET used = used + 1 WHERE code = %s", (code,), commit=True)
    return True, f"✅ تم التفعيل!\nمتبقي: {days} يوم"

# ────────────────────────────────────────────────
# KEYBOARDS + VISUAL
# ────────────────────────────────────────────────
def main_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("🎯 توقع الجولة")],
        [KeyboardButton("👤 حسابي")],
        [KeyboardButton("🎟 تفعيل كود")],
        [KeyboardButton("📊 إحصائيات")]
    ], resize_keyboard=True, one_time_keyboard=False)

def build_result_keyboard():
    keyboard = []
    row = []
    for item in ITEMS:
        row.append(InlineKeyboardButton(item, callback_data=f"result_{item}"))
        if len(row) == 4:
            keyboard.append(row)
            row = []
    if row: keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🏠 رجوع للقائمة", callback_data="back_to_main")])
    return InlineKeyboardMarkup(keyboard)

def format_sequence_visual(sequence):
    if not sequence:
        return "📭 لا يوجد تسلسل بعد"
    return f"🎰 **التسلسل الحالي** (آخر 6 مرئية)\n{'  •  '.join(sequence[-6:])}"

# ────────────────────────────────────────────────
# PREDICTION ENGINE v5.7 – تركيز على 1 + 4 + 6 فقط
# ────────────────────────────────────────────────
def predict_sequence(sequence):
    if len(sequence) < 1:
        selected = FRUITS[:3] + MEATS[:2]
        percents = [24, 21, 19, 20, 16]
        return selected, percents

    cache_key = tuple(sequence)
    if cache_key in prediction_cache:
        return prediction_cache[cache_key]

    rows = db_execute(
        "SELECT sequence, next_hit FROM training_data ORDER BY id DESC LIMIT 4000"
    )

    scores = {item: 0.0 for item in ITEMS}

    # ─── Markov على النوافذ المحددة فقط (1 + 4 + 6) ───
    for order in [1, 4, 6]:
        if len(sequence) < order:
            continue

        trans = defaultdict(Counter)
        for seq_json, next_hit in rows:
            try:
                seq = json.loads(seq_json) if isinstance(seq_json, str) else seq_json
                if len(seq) < order:
                    continue
                key = tuple(seq[-order:])
                trans[key][next_hit] += 1
            except:
                continue

        # أوزان مقترحة (يمكن تعديلها)
        weight_map = {1: 340, 4: 190, 6: 95}
        weight = weight_map.get(order, 100)

        key = tuple(sequence[-order:])
        if key in trans:
            total = sum(trans[key].values()) + len(ITEMS) * 6   # smoothing أقوى
            for item in ITEMS:
                count = trans[key].get(item, 0) + 6
                scores[item] += (count / total) * weight
        else:
            # fallback خفيف
            for item in ITEMS:
                scores[item] += 10.0

    # ─── تعزيز إضافي للانتقال من آخر ضربة (دائمًا الأهم) ───
    if sequence:
        last = sequence[-1]
        trans_last = Counter()
        for seq_json, next_hit in rows:
            try:
                seq = json.loads(seq_json) if isinstance(seq_json, str) else seq_json
                if seq and seq[-1] == last:
                    trans_last[next_hit] += 1
            except:
                continue

        if trans_last:
            tot = sum(trans_last.values()) + len(ITEMS) * 5
            for item in ITEMS:
                scores[item] += ((trans_last.get(item, 0) + 5) / tot) * 380

    # ─── recency boost خفيف لآخر 3 عناصر ───
    if len(sequence) >= 3:
        for i, item in enumerate(sequence[-3:]):
            pos_weight = (3 - i) / 3 * 1.6   # الأحدث أقوى
            scores[item] += pos_weight * 120

    # ─── اختيار 3 فواكه + 2 لحوم ───
    sorted_fruits = sorted(FRUITS, key=lambda x: scores[x], reverse=True)[:3]
    sorted_meats  = sorted(MEATS,  key=lambda x: scores[x], reverse=True)[:2]
    selected_items = sorted_fruits + sorted_meats

    # ─── نسب مئوية ديناميكية ───
    selected_scores = [max(scores[item], 1) for item in selected_items]
    total_sel = sum(selected_scores) or 1
    percents = [round((s / total_sel) * 100) for s in selected_scores]

    # تصحيح لـ 100%
    diff = 100 - sum(percents)
    if diff != 0:
        max_idx = percents.index(max(percents))
        percents[max_idx] += diff

    prediction_cache[cache_key] = (selected_items, percents)
    return selected_items, percents

# ────────────────────────────────────────────────
# عرض النتيجة
# ────────────────────────────────────────────────
async def show_prediction(message, user_id):
    sequence = sessions[user_id]["hits"]
    selected_items, percents = predict_sequence(sequence)
    visual = format_sequence_visual(sequence)

    fruits_part = selected_items[:3]
    meats_part  = selected_items[3:]
    fruits_perc = percents[:3]
    meats_perc  = percents[3:]

    fruit_line = " • ".join([f"{item} {p}%" for item, p in zip(fruits_part, fruits_perc)])
    meat_line  = " • ".join([f"{item} {p}%" for item, p in zip(meats_part, meats_perc)])

    text = f"""{visual}

**الجولة {sessions[user_id]['round_number']}**

**🍏 أفضل 3 فواكه:**
{fruit_line}

**🍖 أفضل 2 لحوم:**
{meat_line}

اختر النتيجة الفعلية 👇"""

    await message.reply_text(text, reply_markup=build_result_keyboard())

async def save_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    result = query.data.replace("result_", "")
    user_id = query.from_user.id
    sequence = sessions[user_id]["hits"]

    user = get_user(user_id)
    if user and user[0] == "CP":
        db_execute("INSERT INTO user_results (telegram_id, last_hit, real_result) VALUES (%s,%s,%s)",
                   (user_id, sequence[-1], result), commit=True)
        db_execute("INSERT INTO training_data (last_hit, sequence, next_hit, trainer_id) VALUES (%s,%s,%s,%s)",
                   (sequence[-1], json.dumps(sequence), result, user_id), commit=True)

    new_seq = sequence[1:] + [result]
    sessions[user_id]["hits"] = new_seq
    sessions[user_id]["round_number"] += 1

    selected_items, percents = predict_sequence(new_seq)
    visual = format_sequence_visual(new_seq)

    fruits_part = selected_items[:3]
    meats_part  = selected_items[3:]
    fruits_perc = percents[:3]
    meats_perc  = percents[3:]

    fruit_line = " • ".join([f"{item} {p}%" for item, p in zip(fruits_part, fruits_perc)])
    meat_line  = " • ".join([f"{item} {p}%" for item, p in zip(meats_part, meats_perc)])

    text = f"""{visual}

**الجولة {sessions[user_id]['round_number']}**

**🍏 أفضل 3 فواكه:**
{fruit_line}

**🍖 أفضل 2 لحوم:**
{meat_line}

اختر النتيجة الفعلية 👇"""

    await query.message.reply_text(text, reply_markup=build_result_keyboard())

# ────────────────────────────────────────────────
# باقي الدوال (start, profile, code, guess_warning, tutorial, start_guess, ask_hit, ...)
# ────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    create_user(user_id)
    remaining = get_remaining_time(user_id)
    await update.message.reply_text(
        f"""🎯 بوت COWBOY v5.7 – تركيز على آخر 1 + 4 + 6 ضربات

**حالة اشتراكك:** {remaining}

اختر من الأزرار أدناه 👇""",
        reply_markup=main_keyboard()
    )

# ────────────────────────────────────────────────
# باقي الـ handlers (كما في النسخ السابقة)
# ────────────────────────────────────────────────

# ... (انسخ باقي الدوال من النسخة السابقة: show_profile, ask_code, handle_text, guess_warning,
# tutorial_next, start_guess, ask_hit, hit_selected, confirm_hit, back_hit, back_to_main, show_statistics)

# ────────────────────────────────────────────────
# MAIN
# ────────────────────────────────────────────────
def main():
    keep_alive()
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("^(🎯 توقع الجولة)$"), guess_warning))
    app.add_handler(MessageHandler(filters.Regex("^(👤 حسابي)$"), show_profile))
    app.add_handler(MessageHandler(filters.Regex("^(🎟 تفعيل كود)$"), ask_code))
    app.add_handler(MessageHandler(filters.Regex("^(📊 إحصائيات)$"), show_statistics))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.add_handler(CallbackQueryHandler(start_guess, pattern="^start_guess$"))
    app.add_handler(CallbackQueryHandler(tutorial_next, pattern="^tutorial_next$"))
    app.add_handler(CallbackQueryHandler(hit_selected, pattern="^hit_"))
    app.add_handler(CallbackQueryHandler(confirm_hit, pattern="^confirm_hit_"))
    app.add_handler(CallbackQueryHandler(back_hit, pattern="^back_hit$"))
    app.add_handler(CallbackQueryHandler(save_result, pattern="^result_"))
    app.add_handler(CallbackQueryHandler(back_to_main, pattern="^back_to_main$"))

    print("✅ بوت COWBOY v5.7 شغال – تركيز على 1 + 4 + 6 ضربات")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
