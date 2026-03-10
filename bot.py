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
# WEB SERVER (لـ Render / Heroku / etc)
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
        return False, "❌ الكود غير صحيح"
    days, used, max_use = data
    if used >= max_use:
        return False, "❌ الكود منتهي"
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
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🏠 رجوع للقائمة", callback_data="back_to_main")])
    return InlineKeyboardMarkup(keyboard)

def format_sequence_visual(sequence):
    if not sequence:
        return "📭 لا يوجد تسلسل بعد"
    return f"🎰 **التسلسل الحالي** (آخر 6 مرئية)\n{'  •  '.join(sequence[-6:])}"

# ────────────────────────────────────────────────
# PREDICTION ENGINE v5.8
# ────────────────────────────────────────────────
def predict_sequence(sequence):
    if len(sequence) < 1:
        return FRUITS[:3] + MEATS[:2], [24, 21, 19, 20, 16]

    cache_key = tuple(sequence)
    if cache_key in prediction_cache:
        return prediction_cache[cache_key]

    rows = db_execute(
        "SELECT sequence, next_hit FROM training_data ORDER BY id DESC LIMIT 10000"
    ) or []

    scores = {item: 0.0 for item in ITEMS}

    # Multi-order Markov
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

    # Exact 6-match super boost
    if len(sequence) >= 6:
        exact_key = tuple(sequence[-6:])
        for seq_json, next_hit in rows:
            try:
                seq = json.loads(seq_json) if isinstance(seq_json, str) else seq_json
                if len(seq) >= 6 and tuple(seq[-6:]) == exact_key:
                    scores[next_hit] += 650
            except:
                continue

    # Last hit strong boost
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

    # Recency boost (last 5)
    if len(sequence) >= 5:
        for i, item in enumerate(sequence[-5:]):
            pos_weight = (5 - i) / 5 * 2.2
            scores[item] += pos_weight * 95

    # Select 3 fruits + 2 meats
    sorted_fruits = sorted(FRUITS, key=lambda x: scores[x], reverse=True)[:3]
    sorted_meats  = sorted(MEATS,  key=lambda x: scores[x], reverse=True)[:2]
    selected_items = sorted_fruits + sorted_meats

    selected_scores = [max(scores.get(item, 1), 1) for item in selected_items]
    total_sel = sum(selected_scores) or 1
    percents = [round((s / total_sel) * 100) for s in selected_scores]

    diff = 100 - sum(percents)
    if diff != 0 and percents:
        max_idx = percents.index(max(percents))
        percents[max_idx] += diff

    prediction_cache[cache_key] = (selected_items, percents)
    return selected_items, percents

# ────────────────────────────────────────────────
# HANDLERS
# ────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    create_user(user_id)
    remaining = get_remaining_time(user_id)
    await update.message.reply_text(
        f"""🎯 بوت COWBOY v5.8 – محسن (1→6 + exact match)

**حالة اشتراكك:** {remaining}

اختر من الأزرار أدناه 👇""",
        reply_markup=main_keyboard()
    )

async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    role = user[0] if user and user[0] else "👤 مستخدم عادي"
    remaining = get_remaining_time(user_id)
    results = db_execute("SELECT COUNT(*) FROM user_results WHERE telegram_id = %s", (user_id,), fetchone=True)
    results = results[0] if results else 0

    text = f"""👤 **حسابك**

🆔 ID: <code>{user_id}</code>
👑 الرتبة: {role}
📊 نتائجك المسجلة: {results}
💎 الاشتراك: {remaining}

🚀 جاهز للتوقع؟"""
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

async def guess_warning(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not check_subscription(user_id):
        await update.message.reply_text("❌ اشتراكك منتهي")
        return

    example = "مثال: 🍎 🍊 🥬 🍉 🐟 🍔 🍤"

    keyboard = [
        [InlineKeyboardButton("📖 التالي (فهمت)", callback_data="tutorial_next")],
        [InlineKeyboardButton("🚀 ابدأ الجولة الآن", callback_data="start_guess")]
    ]

    await update.message.reply_text(
        f"""⚠️ **اختر التسلسل من يسار إلى يمين**\n
{example}

**حالة اشتراكك:** {get_remaining_time(user_id)}

جاهز؟""",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def tutorial_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "✅ تم فهم التعليمات!\n\nاضغط لبدء الجولة",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚀 ابدأ الجولة الآن", callback_data="start_guess")]])
    )

async def start_guess(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    sessions[user_id] = {"mode": "guess", "hits": [], "round_number": 1}
    await ask_hit(query.message, user_id)

async def ask_hit(message, user_id):
    if user_id not in sessions:
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
        f"**الجولة {sessions[user_id]['round_number']}** 🎲\nاختر الضربة رقم {step}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def hit_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    fruit = query.data.split("_", 1)[1]
    kb = [
        [InlineKeyboardButton("✅ تأكيد", callback_data=f"confirm_hit_{fruit}")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="back_hit")]
    ]
    await query.edit_message_text(f"اخترت {fruit}\nمتأكد؟", reply_markup=InlineKeyboardMarkup(kb))

async def confirm_hit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    fruit = query.data.replace("confirm_hit_", "")
    if user_id not in sessions:
        return
    sessions[user_id]["hits"].append(fruit)
    if len(sessions[user_id]["hits"]) < 6:
        await ask_hit(query.message, user_id)
    else:
        await show_prediction(query.message, user_id)

async def back_hit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if user_id not in sessions:
        return
    if sessions[user_id]["hits"]:
        sessions[user_id]["hits"].pop()
    await ask_hit(query.message, user_id)

async def show_prediction(message, user_id):
    if user_id not in sessions:
        return
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
    if user_id not in sessions:
        return

    sequence = sessions[user_id]["hits"]

    user = get_user(user_id)
    if user and user[0] == "CP":
        db_execute(
            "INSERT INTO user_results (telegram_id, last_hit, real_result) VALUES (%s,%s,%s)",
            (user_id, sequence[-1], result), commit=True
        )
        db_execute(
            "INSERT INTO training_data (last_hit, sequence, next_hit, trainer_id) VALUES (%s,%s,%s,%s)",
            (sequence[-1], json.dumps(sequence), result, user_id), commit=True
        )

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

async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    sessions.pop(user_id, None)
    prediction_cache.clear()
    await query.message.reply_text("🏠 العودة للقائمة", reply_markup=main_keyboard())

async def show_statistics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total_data = db_execute("SELECT COUNT(*) FROM training_data", fetchone=True)
    total_data = total_data[0] if total_data else 0

    active = db_execute(
        "SELECT COUNT(*) FROM users WHERE subscription_end > %s",
        (datetime.now(timezone.utc),), fetchone=True
    )
    active = active[0] if active else 0

    await update.message.reply_text(
        f"""📊 إحصائيات
البيانات التدريبية: {total_data}
المشتركين النشطين: {active}
اشتراكك: {get_remaining_time(update.effective_user.id)}"""
    )

# ────────────────────────────────────────────────
# MAIN
# ────────────────────────────────────────────────
def main():
    keep_alive()
    application = ApplicationBuilder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.Regex("^(🎯 توقع الجولة)$"), guess_warning))
    application.add_handler(MessageHandler(filters.Regex("^(👤 حسابي)$"), show_profile))
    application.add_handler(MessageHandler(filters.Regex("^(🎟 تفعيل كود)$"), ask_code))
    application.add_handler(MessageHandler(filters.Regex("^(📊 إحصائيات)$"), show_statistics))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    application.add_handler(CallbackQueryHandler(tutorial_next, pattern="^tutorial_next$"))
    application.add_handler(CallbackQueryHandler(start_guess, pattern="^start_guess$"))
    application.add_handler(CallbackQueryHandler(hit_selected, pattern="^hit_"))
    application.add_handler(CallbackQueryHandler(confirm_hit, pattern="^confirm_hit_"))
    application.add_handler(CallbackQueryHandler(back_hit, pattern="^back_hit$"))
    application.add_handler(CallbackQueryHandler(save_result, pattern="^result_"))
    application.add_handler(CallbackQueryHandler(back_to_main, pattern="^back_to_main$"))

    print("✅ بوت COWBOY v5.8 شغال")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
