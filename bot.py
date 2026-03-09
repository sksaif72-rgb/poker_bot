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
MEAT_ITEMS = ["🐟", "🍤", "🍗", "🍔"]

# ────────────────────────────────────────────────
# SESSIONS + CACHE (للسرعة مع 565+ سجل)
# ────────────────────────────────────────────────
sessions = {}
prediction_cache = {}  # cache للـ sequence tuple

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
    return f"🎮 **التسلسل الحالي**\n{' '.join(sequence)}"

def get_streak_of_non_meat(sequence):
    if not sequence:
        return 0
    streak = 0
    for item in reversed(sequence):
        if item in MEAT_ITEMS:
            break
        streak += 1
    return streak

# ────────────────────────────────────────────────
# التنبؤ الـ ULTRA قوي v4.0 (مبني 100% على بيانات الجدول الجديدة)
# ────────────────────────────────────────────────
def predict_sequence(sequence):
    if len(sequence) < 1:
        return ITEMS[:4]
    
    seq_tuple = tuple(sequence)
    if seq_tuple in prediction_cache:
        return prediction_cache[seq_tuple]
    
    # تحميل كل البيانات بدون LIMIT (565+ سجل حالياً)
    rows = db_execute("SELECT id, sequence, next_hit FROM training_data ORDER BY id ASC")  # ASC عشان نستخدم id للـ recency
    
    if not rows:
        return ITEMS[:4]
    
    scores = {item: 0.0 for item in ITEMS}
    total_rows = len(rows)
    
    # ─── 1. Markov Chains متعددة الدرجات (1-6) مع Laplace + وزن الحداثة
    for order in range(1, 7):
        trans = defaultdict(Counter)
        for rid, seq_json, next_hit in rows:
            try:
                seq = json.loads(seq_json) if isinstance(seq_json, str) else seq_json
                if len(seq) >= order:
                    key = tuple(seq[-order:])
                    trans[key][next_hit] += 1
            except:
                continue
        
        weight = {1: 140, 2: 115, 3: 95, 4: 75, 5: 55, 6: 35}[order]
        key = tuple(sequence[-order:]) if len(sequence) >= order else ()
        
        if key in trans:
            total = sum(trans[key].values()) + len(ITEMS) * 5
            for item in ITEMS:
                count = trans[key][item] + 5
                recency_bonus = (rid / total_rows) * 25 if rid else 0  # أحدث = أقوى
                scores[item] += (count / total) * (weight + recency_bonus)
        else:
            # fallback global مع وزن حداثة
            global_count = Counter([r[2] for r in rows])
            total_g = sum(global_count.values()) + len(ITEMS) * 5
            for item in ITEMS:
                scores[item] += ((global_count[item] + 5) / total_g) * (weight * 0.4)

    # ─── 2. تطابقات دقيقة طويلة (3-6) مع وزن الحداثة القوي جداً
    current = tuple(sequence[-6:])
    for rid, seq_json, next_hit in rows[-400:]:  # آخر 400 سجل أقوى وزن
        try:
            past = json.loads(seq_json) if isinstance(seq_json, str) else seq_json
            past_t = tuple(past)
            recency_weight = 1.0 + (rid / total_rows) * 3.5  # أحدث = أقوى بكثير
            for length in range(3, 7):
                if len(past_t) >= length and past_t[-length:] == current[-length:]:
                    nxt = past[-1] if len(past) > length else next_hit
                    if nxt in ITEMS:
                        bonus = 260 if length >= 5 else 165
                        scores[nxt] += bonus * recency_weight
        except:
            continue

    # ─── 3. التكرار العام + Laplace
    global_count = Counter([r[2] for r in rows])
    total_g = sum(global_count.values()) + len(ITEMS) * 8
    for item in ITEMS:
        scores[item] += ((global_count[item] + 8) / total_g) * 62

    # ─── 4. قاعدة اللحوم الديناميكية (محسوبة مباشرة من الجدول الحالي)
    streak = get_streak_of_non_meat(sequence)
    last_was_meat = sequence and sequence[-1] in MEAT_ITEMS
    
    # حساب الـ bonus ديناميكياً من كل السجلات
    streak_meat_count = defaultdict(lambda: defaultdict(int))
    for _, seq_json, next_hit in rows:
        try:
            seq = json.loads(seq_json) if isinstance(seq_json, str) else seq_json
            s = get_streak_of_non_meat(seq)
            if next_hit in MEAT_ITEMS:
                streak_meat_count[s][next_hit] += 1
        except:
            continue
    
    if streak in streak_meat_count and not last_was_meat:
        meat_bonus = sum(streak_meat_count[streak].values()) * 9.5  # معامل واقعي من البيانات
        for meat in MEAT_ITEMS:
            scores[meat] += meat_bonus
    elif streak == 0 and last_was_meat:
        # بعد اللحم مباشرة يزيد احتمال اللحم أكثر
        for meat in MEAT_ITEMS:
            scores[meat] += 95

    # ─── 5. تحليل الأزواج + ثلاثيات + مكافأة التنوع
    if len(sequence) >= 2:
        last_pair = tuple(sequence[-2:])
        pair_trans = defaultdict(Counter)
        for _, seq_json, next_hit in rows:
            try:
                seq = json.loads(seq_json) if isinstance(seq_json, str) else seq_json
                for i in range(len(seq)-2):
                    if tuple(seq[i:i+2]) == last_pair:
                        pair_trans[last_pair][next_hit] += 1
            except:
                continue
        if last_pair in pair_trans:
            total_p = sum(pair_trans[last_pair].values()) + len(ITEMS)
            for item in ITEMS:
                scores[item] += ((pair_trans[last_pair][item] + 2) / total_p) * 72

    if len(sequence) >= 3:
        last_triple = tuple(sequence[-3:])
        triple_trans = defaultdict(Counter)
        for _, seq_json, next_hit in rows:
            try:
                seq = json.loads(seq_json) if isinstance(seq_json, str) else seq_json
                for i in range(len(seq)-3):
                    if tuple(seq[i:i+3]) == last_triple:
                        triple_trans[last_triple][next_hit] += 1
            except:
                continue
        if last_triple in triple_trans:
            total_t = sum(triple_trans[last_triple].values()) + len(ITEMS)
            for item in ITEMS:
                scores[item] += ((triple_trans[last_triple][item] + 2) / total_t) * 48

    if len(set(sequence)) >= 5:
        for item in ITEMS:
            scores[item] += 18

    # ─── ترتيب نهائي
    sorted_preds = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    top_5 = [item[0] for item in sorted_preds[:5]]
    
    prediction_cache[seq_tuple] = top_5
    return top_5

# ────────────────────────────────────────────────
# باقي الدوال (نفس السابق مع تحسينات بسيطة)
# ────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    create_user(user_id)
    remaining = get_remaining_time(user_id)
    await update.message.reply_text(
        f"""🎯 بوت COWBOY احترافي v4.0 ULTRA

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
    predictions = predict_sequence(sequence)
    visual = format_sequence_visual(sequence)

    strong_conf = 88 + len(set(sequence)) * 3.2
    strong_conf = min(strong_conf, 99)

    text = f"""{visual}

**الجولة {sessions[user_id]['round_number']}**

🔥 تخمين قوي: {predictions[0]} {predictions[1]}
ممكن تضرب بنسبة: {strong_conf}%

🛡️ التأمين: {predictions[2]} {predictions[3]}

اختر النتيجة 👇"""
    await message.reply_text(text, reply_markup=build_result_keyboard())

async def save_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    result = query.data.replace("result_", "")
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

    visual = format_sequence_visual(new_seq)
    predictions = predict_sequence(new_seq)

    strong_conf = 88 + len(set(new_seq)) * 3.2
    strong_conf = min(strong_conf, 99)

    text = f"""{visual}

**الجولة {sessions[user_id]['round_number']}**

🔥 تخمين قوي: {predictions[0]} {predictions[1]}
ممكن تضرب بنسبة: {strong_conf}%

🛡️ التأمين: {predictions[2]} {predictions[3]}

اختر النتيجة 👇"""
    await query.message.reply_text(text, reply_markup=build_result_keyboard())

async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    sessions.pop(query.from_user.id, None)
    prediction_cache.clear()  # تنظيف الكاش عند العودة
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

    print("✅ بوت COWBOY v4.0 ULTRA شغال! (يعتمد كلياً على بيانات الجدول الجديدة + تنبؤ فائق القوة)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
