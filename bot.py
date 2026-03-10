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
# WEB SERVER (لـ Render / Railway / etc)
# ────────────────────────────────────────────────
app_web = Flask(__name__)

@app_web.route("/")
def home():
    return "Bot is alive"

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

try:
    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
except Exception as e:
    logger.error(f"Cannot connect to database: {e}")
    conn = None

# ────────────────────────────────────────────────
# GAME ITEMS
# ────────────────────────────────────────────────
ITEMS = ["🍎", "🍊", "🥬", "🍉", "🐟", "🍔", "🍤", "🍗"]
FRUITS = ["🍎", "🍊", "🥬", "🍉"]
MEATS  = ["🐟", "🍔", "🍤", "🍗"]
ALL_ITEMS_SET = set(ITEMS)

# ────────────────────────────────────────────────
# SESSIONS + CACHE
# ────────────────────────────────────────────────
sessions = {}
prediction_cache = {}

# ────────────────────────────────────────────────
# DB HELPERS
# ────────────────────────────────────────────────
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
        logger.error(f"DB error: {e}")
        if commit and conn:
            conn.rollback()
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
    if not data:
        return False, "❌ الكود غير موجود"
    days, used, max_use = data
    if used >= max_use:
        return False, "❌ الكود مستنفد"
    end_date = datetime.now(timezone.utc) + timedelta(days=days)
    db_execute("UPDATE users SET subscription_end = %s WHERE telegram_id = %s", (end_date, telegram_id), commit=True)
    db_execute("UPDATE codes SET used = used + 1 WHERE code = %s", (code,), commit=True)
    return True, f"✅ تم التفعيل!\nالاشتراك صالح لـ {days} يوم"

# ────────────────────────────────────────────────
# KEYBOARDS
# ────────────────────────────────────────────────
def main_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("🎯 توقع الجولة")],
        [KeyboardButton("👤 حسابي")],
        [KeyboardButton("🎟 تفعيل كود")],
        [KeyboardButton("📊 إحصائيات")]
    ], resize_keyboard=True)

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
    keyboard.append([InlineKeyboardButton("🏠 رجوع للقائمة", callback_data="back_to_main")])
    return InlineKeyboardMarkup(keyboard)

def format_sequence_visual(sequence):
    if not sequence:
        return "📭 لا توجد ضربات بعد"
    return f"🎰 **التسلسل الحالي** (آخر 8)\n{'  •  '.join(sequence[-8:])}"

# ────────────────────────────────────────────────
# PREDICTION ENGINE v6.0 – Comprehensive
# ────────────────────────────────────────────────
def predict_sequence(sequence):
    if len(sequence) == 0:
        return FRUITS[:3] + MEATS[:2], [26, 22, 20, 18, 14]

    cache_key = tuple(sequence[-12:])  # cache أطول
    if cache_key in prediction_cache:
        return prediction_cache[cache_key]

    rows = db_execute(
        "SELECT sequence, next_hit FROM training_data ORDER BY id DESC LIMIT 15000"
    ) or []

    scores = {item: 0.0 for item in ITEMS}

    # 1. Global frequency (weak prior)
    global_count = Counter()
    for _, next_hit in rows:
        global_count[next_hit] += 1
    total_global = sum(global_count.values()) or 1
    for item in ITEMS:
        scores[item] += (global_count.get(item, 0) / total_global) * 40

    # 2. Exact long matches (very strong)
    for match_len in [8, 7, 6, 5]:
        if len(sequence) < match_len:
            continue
        exact_key = tuple(sequence[-match_len:])
        weight = {8: 1800, 7: 1200, 6: 800, 5: 400}[match_len]
        for seq_json, next_hit in rows:
            try:
                seq = json.loads(seq_json) if isinstance(seq_json, str) else seq_json
                if len(seq) >= match_len and tuple(seq[-match_len:]) == exact_key:
                    scores[next_hit] += weight
            except:
                pass

    # 3. Markov chains – multiple orders
    for order in [1, 2, 3, 4, 5, 6]:
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

        weight = {1: 180, 2: 260, 3: 340, 4: 280, 5: 220, 6: 160}.get(order, 100)
        key = tuple(sequence[-order:])
        if key in trans:
            tot = sum(trans[key].values()) + len(ITEMS) * 5
            for item in ITEMS:
                scores[item] += ((trans[key].get(item, 0) + 5) / tot) * weight

    # 4. After specific last item (strong)
    if sequence:
        last = sequence[-1]
        after_last = Counter()
        for seq_json, next_hit in rows:
            try:
                seq = json.loads(seq_json) if isinstance(seq_json, str) else seq_json
                if seq and seq[-1] == last:
                    after_last[next_hit] += 1
            except:
                pass
        if after_last:
            tot = sum(after_last.values()) + len(ITEMS) * 3
            for item in ITEMS:
                scores[item] += ((after_last.get(item, 0) + 3) / tot) * 520

    # 5. Anti-recency / "due" items (items not seen recently)
    if len(sequence) >= 4:
        recent = set(sequence[-6:])
        for item in ITEMS:
            if item not in recent:
                scores[item] += 180
            elif item == sequence[-1]:
                scores[item] -= 90   # تقليل احتمال التكرار المباشر

    # ─── Final selection ────────────────────────────────
    sorted_fruits = sorted(FRUITS, key=lambda x: scores[x], reverse=True)[:3]
    sorted_meats  = sorted(MEATS,  key=lambda x: scores[x], reverse=True)[:2]
    selected = sorted_fruits + sorted_meats

    raw_scores = [max(scores.get(item, 1), 1) for item in selected]
    total = sum(raw_scores) or 1
    percents = [round(s / total * 100) for s in raw_scores]

    # تصحيح النسب لتصبح 100%
    diff = 100 - sum(percents)
    if diff != 0 and percents:
        idx = percents.index(max(percents))
        percents[idx] += diff

    prediction_cache[cache_key] = (selected, percents)
    return selected, percents

# ────────────────────────────────────────────────
# HANDLERS  (باقي الكود بدون تغيير كبير)
# ────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    create_user(user_id)
    remaining = get_remaining_time(user_id)
    await update.message.reply_text(
        f"""🎯 **COWBOY v6.0** – تنبؤ شامل (طويل الأمد + إحصائيات عامة)

**اشتراكك:** {remaining}

اختر من الأزرار 👇""",
        reply_markup=main_keyboard()
    )

# ── باقي الـ handlers نفسها مع تغييرات بسيطة في النصوص ──

async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    role = user[0] if user and user[0] else "مستخدم عادي"
    remaining = get_remaining_time(user_id)
    count = db_execute("SELECT COUNT(*) FROM user_results WHERE telegram_id = %s", (user_id,), fetchone=True)
    count = count[0] if count else 0

    text = f"""👤 **حسابك**

🆔 <code>{user_id}</code>
👑 الرتبة: {role}
📊 نتائج مسجلة: {count}
💎 الاشتراك: {remaining}"""
    await update.message.reply_text(text, parse_mode="HTML")


async def guess_warning(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not check_subscription(user_id):
        await update.message.reply_text("❌ الاشتراك منتهي")
        return

    keyboard = [
        [InlineKeyboardButton("🚀 ابدأ الآن", callback_data="start_guess")],
        [InlineKeyboardButton("🏠 رجوع", callback_data="back_to_main")]
    ]

    await update.message.reply_text(
        f"""🎲 **توقع الجولة**

أدخل آخر 6–8 ضربات من يسار إلى يمين
مثال: 🍎 🍊 🥬 🍉 🐟 🍔

**اشتراكك:** {get_remaining_time(user_id)}""",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ... (باقي الـ handlers كما هي مع تعديلات طفيفة في النصوص إن أردت)


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
    app.add_handler(CallbackQueryHandler(hit_selected, pattern="^hit_"))
    app.add_handler(CallbackQueryHandler(confirm_hit, pattern="^confirm_hit_"))
    app.add_handler(CallbackQueryHandler(back_hit, pattern="^back_hit$"))
    app.add_handler(CallbackQueryHandler(save_result, pattern="^result_"))
    app.add_handler(CallbackQueryHandler(back_to_main, pattern="^back_to_main$"))

    print("🚀 Cowboy v6.0 – Comprehensive Prediction Engine")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
