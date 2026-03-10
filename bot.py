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

WINDOW_SIZE = 5   # يعتمد فقط على آخر 5 ضربات (كما طلبت)

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
    return f"🎰 **آخر {min(len(sequence), WINDOW_SIZE)} ضربة**\n{'  •  '.join(sequence[-WINDOW_SIZE:])}"

# ────────────────────────────────────────────────
# التنبؤ المتطور (يعتمد على آخر 5 ضربات + قاعدة البيانات)
# ────────────────────────────────────────────────
def predict_sequence(sequence):
    if len(sequence) < 1:
        selected = FRUITS + MEATS
        percents = [18, 16, 15, 14, 13, 9, 8, 7]
        prediction_cache[()] = (selected, percents)
        return selected, percents

    # نأخذ فقط آخر 5 ضربات (كما طلبت)
    recent = sequence[-WINDOW_SIZE:]
    cache_key = tuple(recent)
    if cache_key in prediction_cache:
        return prediction_cache[cache_key]

    # جلب البيانات من قاعدة البيانات
    rows = db_execute(
        "SELECT sequence, next_hit FROM training_data ORDER BY id DESC LIMIT 3000"
    )

    scores = {item: 0.0 for item in ITEMS}

    # Markov Chains (تركيز قوي على آخر 5)
    for order in [1, 2, 3, 4]:
        trans = defaultdict(Counter)
        for seq_json, next_hit in rows:
            try:
                seq = json.loads(seq_json) if isinstance(seq_json, str) else seq_json
                if len(seq) < order: continue
                key = tuple(seq[-order:])
                trans[key][next_hit] += 1
            except:
                continue
        weight = {1: 220, 2: 140, 3: 70, 4: 30}[order]
        k = min(order, len(recent))
        key = tuple(recent[-k:])
        if key in trans:
            total = sum(trans[key].values()) + len(ITEMS) * 4
            for item in ITEMS:
                scores[item] += ((trans[key][item] + 4) / total) * weight

    # Recency Bias + Hot/Cold داخل آخر 5
    if len(recent) >= 2:
        window_count = Counter(recent)
        for item in ITEMS:
            freq = window_count[item] / len(recent)
            scores[item] += freq * 180
            if freq == 0:
                scores[item] += 25

    # Streak & Alternation
    if len(recent) >= 3:
        last_three = recent[-3:]
        if last_three[0] == last_three[1] == last_three[2]:
            for item in ITEMS:
                if item != recent[-1]:
                    scores[item] += 90
        elif last_three[0] != last_three[1] and last_three[1] != last_three[2]:
            for item in [i for i in ITEMS if i != recent[-1]]:
                scores[item] += 65

    # انتقال مباشر من آخر عنصر (قوي جداً)
    if recent:
        last = recent[-1]
        trans_last = Counter()
        for seq_json, next_hit in rows:
            try:
                seq = json.loads(seq_json) if isinstance(seq_json, str) else seq_json
                if seq and seq[-1] == last:
                    trans_last[next_hit] += 1
            except:
                continue
        if trans_last:
            tot = sum(trans_last.values()) + len(ITEMS) * 3
            for item in ITEMS:
                scores[item] += ((trans_last[item] + 3) / tot) * 260

    # Global bias خفيف
    global_count = Counter([r[1] for r in rows])
    total_g = sum(global_count.values()) or 1
    for item in ITEMS:
        scores[item] += (global_count[item] / total_g) * 15

    # ───── ترتيب الفواكه واللحوم بشكل منفصل ─────
    sorted_fruits = sorted(FRUITS, key=lambda x: scores[x], reverse=True)
    sorted_meats  = sorted(MEATS,  key=lambda x: scores[x], reverse=True)
    selected_items = sorted_fruits + sorted_meats

    # ───── نسب مئوية ديناميكية حقيقية لكل الـ8 عناصر (مجموع 100%) ─────
    selected_scores = [max(scores[item], 1) for item in selected_items]
    total_sel = sum(selected_scores) or 1
    percents = [round((s / total_sel) * 100) for s in selected_scores]

    # تصحيح دقيق ليكون المجموع بالضبط 100
    diff = 100 - sum(percents)
    if diff != 0:
        max_idx = percents.index(max(percents))
        percents[max_idx] += diff

    prediction_cache[cache_key] = (selected_items, percents)
    return selected_items, percents

# ────────────────────────────────────────────────
# باقي الدوال
# ────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    create_user(user_id)
    remaining = get_remaining_time(user_id)
    await update.message.reply_text(
        f"""🎯 بوت COWBOY v5.4 ENSEMBLE (كل الـ8 مع نسب حقيقية)

**حالة اشتراكك:** {remaining}

اختر من الأزرار أدناه 👇""",
        reply_markup=main_keyboard()
    )

async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    role = user[0] if user and user[0] else "👤 مستخدم عادي"
    remaining = get_remaining_time(user_id)
    results = db_execute("SELECT COUNT(*) FROM user_results WHERE telegram_id = %s", (user_id,), fetchone=True)[0] or 0

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
    if not check_subscription(update.effective_user.id):
        await update.message.reply_text("❌ اشتراكك منتهي")
        return
    
    example = "مثال: 🍎 🍊 🥬 🍉 🐟 🍔"

    keyboard = [
        [InlineKeyboardButton("📖 التالي (فهمت)", callback_data="tutorial_next")],
        [InlineKeyboardButton("🚀 ابدأ الجولة الآن", callback_data="start_guess")]
    ]
    
    await update.message.reply_text(
        f"""⚠️ **اختر التسلسل من يسار إلى يمين**\n
{example}

**حالة اشتراكك:** {get_remaining_time(update.effective_user.id)}

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
    step = len(sessions[user_id]["hits"]) + 1
    keyboard = []
    row = []
    for item in ITEMS:
        row.append(InlineKeyboardButton(item, callback_data=f"hit_{item}"))
        if len(row) == 4:
            keyboard.append(row)
            row = []
    if row: keyboard.append(row)
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
    sessions[user_id]["hits"].append(fruit)
    if len(sessions[user_id]["hits"]) < 6:
        await ask_hit(query.message, user_id)
    else:
        await show_prediction(query.message, user_id)

async def back_hit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if sessions[user_id]["hits"]:
        sessions[user_id]["hits"].pop()
    await ask_hit(query.message, user_id)

async def show_prediction(message, user_id):
    sequence = sessions[user_id]["hits"]
    selected_items, percents = predict_sequence(sequence)
    visual = format_sequence_visual(sequence)

    fruits_part = selected_items[:4]
    meats_part  = selected_items[4:]
    fruits_perc = percents[:4]
    meats_perc  = percents[4:]

    fruit_line = " • ".join([f"{item} {p}%" for item, p in zip(fruits_part, fruits_perc)])
    meat_line  = " • ".join([f"{item} {p}%" for item, p in zip(meats_part, meats_perc)])

    text = f"""{visual}

**الجولة {sessions[user_id]['round_number']}**

**🍏 الفواكه:**
{fruit_line}

**🍖 اللحوم:**
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

    fruits_part = selected_items[:4]
    meats_part  = selected_items[4:]
    fruits_perc = percents[:4]
    meats_perc  = percents[4:]

    fruit_line = " • ".join([f"{item} {p}%" for item, p in zip(fruits_part, fruits_perc)])
    meat_line  = " • ".join([f"{item} {p}%" for item, p in zip(meats_part, meats_perc)])

    text = f"""{visual}

**الجولة {sessions[user_id]['round_number']}**

**🍏 الفواكه:**
{fruit_line}

**🍖 اللحوم:**
{meat_line}

اختر النتيجة الفعلية 👇"""

    await query.message.reply_text(text, reply_markup=build_result_keyboard())

async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    sessions.pop(query.from_user.id, None)
    prediction_cache.clear()
    await query.message.reply_text("🏠 العودة للقائمة", reply_markup=main_keyboard())

async def show_statistics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total_data = db_execute("SELECT COUNT(*) FROM training_data", fetchone=True)[0]
    active = db_execute("SELECT COUNT(*) FROM users WHERE subscription_end > %s", (datetime.now(timezone.utc),), fetchone=True)[0]
    await update.message.reply_text(f"""📊 إحصائيات
البيانات التدريبية: {total_data}
المشتركين النشطين: {active}
اشتراكك: {get_remaining_time(update.effective_user.id)}""")

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

    print("✅ بوت COWBOY v5.4 ENSEMBLE ")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
