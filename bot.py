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
# PREDICTION ENGINE v5.8 (المحسن الجديد)
# ────────────────────────────────────────────────
def predict_sequence(sequence):
    if len(sequence) < 1:
        selected = FRUITS[:3] + MEATS[:2]
        percents = [24, 21, 19, 20, 16]
        return selected, percents

    cache_key = tuple(sequence)
    if cache_key in prediction_cache:
        return prediction_cache[cache_key]

    # تحميل أكثر بيانات (زيادة الحد)
    rows = db_execute(
        "SELECT sequence, next_hit FROM training_data ORDER BY id DESC LIMIT 10000"
    )

    scores = {item: 0.0 for item in ITEMS}

    # ── Multi n-gram 1→6 ──
    for order in [1, 2, 3, 4, 5, 6]:
        if len(sequence) < order:
            continue
        trans = defaultdict(Counter)
        for seq_json, next_hit in rows:
            try:
                seq = json.loads(seq_json) if isinstance(seq_json, str) else seq_json
                if len(seq) < order: continue
                key = tuple(seq[-order:])
                trans[key][next_hit] += 1
            except:
                continue

        weight_map = {1: 300, 2: 250, 3: 200, 4: 150, 5: 100, 6: 50}
        weight = weight_map.get(order, 80)

        key = tuple(sequence[-order:])
        if key in trans:
            total = sum(trans[key].values()) + len(ITEMS) * 6
            for item in ITEMS:
                count = trans[key].get(item, 0) + 6
                scores[item] += (count / total) * weight
        else:
            for item in ITEMS:
                scores[item] += 8.0

    # ── Exact 6-match boost (الأقوى) ──
    if len(sequence) >= 6:
        exact_key = tuple(sequence[-6:])
        for seq_json, next_hit in rows:
            try:
                seq = json.loads(seq_json) if isinstance(seq_json, str) else seq_json
                if len(seq) >= 6 and tuple(seq[-6:]) == exact_key:
                    scores[next_hit] += 650   # boost خرافي
            except:
                continue

    # ── Last hit boost قوي جداً ──
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
            tot = sum(trans_last.values()) + len(ITEMS) * 4
            for item in ITEMS:
                scores[item] += ((trans_last.get(item, 0) + 4) / tot) * 420

    # ── Recency boost (آخر 5) ──
    if len(sequence) >= 5:
        for i, item in enumerate(sequence[-5:]):
            pos_weight = (5 - i) / 5 * 2.2
            scores[item] += pos_weight * 95

    # ── اختيار 3 فواكه + 2 لحوم (كما في التصميم الأصلي) ──
    sorted_fruits = sorted(FRUITS, key=lambda x: scores[x], reverse=True)[:3]
    sorted_meats  = sorted(MEATS,  key=lambda x: scores[x], reverse=True)[:2]
    selected_items = sorted_fruits + sorted_meats

    selected_scores = [max(scores[item], 1) for item in selected_items]
    total_sel = sum(selected_scores) or 1
    percents = [round((s / total_sel) * 100) for s in selected_scores]

    diff = 100 - sum(percents)
    if diff != 0:
        max_idx = percents.index(max(percents))
        percents[max_idx] += diff

    prediction_cache[cache_key] = (selected_items, percents)
    return selected_items, percents

# ────────────────────────────────────────────────
# HANDLERS (نفسها بدون تغيير)
# ────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    create_user(user_id)
    remaining = get_remaining_time(user_id)
    await update.message.reply_text(
        f"""🎯 بوت COWBOY **v5.8** – )

**حالة اشتراكك:** {remaining}

اختر من الأزرار أدناه 👇""",
        reply_markup=main_keyboard()
    )

# باقي الـ handlers نفسها بالضبط (للتوفير في المساحة)
# (copy-paste من الكود القديم: show_profile, ask_code, handle_text, guess_warning, ... إلخ)

async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    role = user[0] if user and user[0] else "👤 مستخدم عادي"
    remaining = get_remaining_time(user_id)
    results = db_execute("SELECT COUNT(*) FROM user_results WHERE telegram_id = %s", (user_id,), fetchone=True)[0] or 0
    text = f"""👤 **حسابك**

🆔 ID: <code>{user_id}</code>
👑 الرتبة: {role}
📊 نتائجك: {results}
💎 الاشتراك: {remaining}"""
    await update.message.reply_text(text, parse_mode="HTML")

async def ask_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔑 أرسل كود الاشتراك:")
    sessions[update.effective_user.id] = {"mode": "code"}

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    if user_id == ADMIN_ID and text.startswith("/createcode "):
        try:
            _, code, days, maxu = text.split()
            db_execute("INSERT INTO codes (code, days, max_use) VALUES (%s,%s,%s)", (code, int(days), int(maxu)), commit=True)
            await update.message.reply_text(f"✅ كود جديد: {code}")
        except:
            await update.message.reply_text("❌ الاستخدام: /createcode الكود الأيام الحد")
        return
    if user_id in sessions and sessions[user_id].get("mode") == "code":
        success, msg = activate_code(user_id, text)
        await update.message.reply_text(msg)
        if success:
            sessions.pop(user_id, None)
            await update.message.reply_text(get_remaining_time(user_id), reply_markup=main_keyboard())

# (كل الـ handlers الأخرى مثل guess_warning, start_guess, hit_selected ... نفسها 100% من الكود القديم)
# فقط استبدل def predict_sequence بالنسخة الجديدة أعلاه

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

    print("✅ بوت COWBOY v5.8 شغال – محسن بقوة!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
