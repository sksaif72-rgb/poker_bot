import os
import random
import datetime
import pytz
import psycopg2
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from collections import Counter

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters


TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")


# ==================== DATABASE & KEEP-ALIVE ====================
def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'Poker bot running')

def run_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), Handler)
    server.serve_forever()


# ==================== DAILY LIMIT ====================
def get_subscription_days(user_id):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT expire_date FROM users WHERE telegram_id=%s", (user_id,))
    data = cursor.fetchone()
    cursor.close()
    conn.close()
    if not data: return 0
    return max(0, (data[0].date() - datetime.date.today()).days)

def check_daily_limit(user_id):
    if get_subscription_days(user_id) > 30: return True
    conn = get_conn()
    cursor = conn.cursor()
    today = datetime.date.today()
    cursor.execute("SELECT count FROM daily_usage WHERE telegram_id=%s AND usage_date=%s", (user_id, today))
    result = cursor.fetchone()
    count = result[0] if result else 0
    cursor.close()
    conn.close()
    return count < 50

def increment_daily_count(user_id):
    conn = get_conn()
    cursor = conn.cursor()
    today = datetime.date.today()
    cursor.execute("""
        INSERT INTO daily_usage (telegram_id, usage_date, count)
        VALUES (%s, %s, 1)
        ON CONFLICT (telegram_id, usage_date) DO UPDATE SET count = daily_usage.count + 1
    """, (user_id, today))
    conn.commit()
    cursor.close()
    conn.close()

def check_subscription(user_id):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT expire_date FROM users WHERE telegram_id=%s", (user_id,))
    data = cursor.fetchone()
    cursor.close()
    conn.close()
    return data and datetime.datetime.now() < data[0]


# ==================== AI ====================
def database_prediction(rank, suit, previous_winner="none"):
    tz = pytz.timezone("Asia/Riyadh")
    hour = datetime.datetime.now(tz).hour
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT winner_type, hand_type FROM training_data
        WHERE card_rank=%s AND card_suit=%s AND previous_winner_type=%s
    """, (rank, suit, previous_winner))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    winner_counter = Counter()
    hand_counter = Counter()
    for row in rows:
        winner_counter[row[0]] += 1
        for h in row[1]:
            hand_counter[h] += 1
    return winner_counter, hand_counter, len(rows)


def monte_carlo_prediction():
    winner_options = ["زوجين", "متتالية", "فل هاوس", "ثلاثة", "اربعة"]
    hand_options = ["متتالية نفس النوع", "زوج", "دبل AA", "ولا شيء"]
    w = Counter(); h = Counter()
    for _ in range(15000):
        w[random.choice(winner_options)] += 1
        h[random.choice(hand_options)] += 1
    return w, h


def combine_predictions(db_w, db_h, mc_w, mc_h, db_count):
    weight = 0.85 if db_count >= 10 else 0.70 if db_count >= 5 else 0.50
    final_w = Counter(); final_h = Counter()
    for k, v in db_w.items(): final_w[k] += v * weight
    for k, v in mc_w.items(): final_w[k] += v * (1 - weight)
    for k, v in db_h.items(): final_h[k] += v * weight
    for k, v in mc_h.items(): final_h[k] += v * (1 - weight)
    return final_w, final_h


def top_predictions(counter):
    total = sum(counter.values())
    if total == 0: return []
    return [(name, round(val/total*100, 2)) for name, val in counter.most_common(2)]


# ==================== START ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [["👤 اشتراك"], ["🎓 مدرب"], ["🔙 رجوع"]]
    await update.message.reply_text(
        "اهلا وسهلا بوت تكساس ويبلاي ♠️\n\n"
        "نحن هنا لنساعدك على اتخاذ قرارات أقوى في اللعب.\n\n"
        "اختر نوع الحساب لنبدأ معاً:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )


# ==================== MESSAGE HANDLER ====================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.message.from_user.id

    if text in ["🔙 رجوع", "رجوع", "BACK"]:
        context.user_data.clear()
        await start(update, context)
        return

    # اشتراك
    if text in ["👤 اشتراك", "اشتراك"]:
        context.user_data["role"] = "user"
        await update.message.reply_text("يرجى إرسال كود الاشتراك الآن:")
        return

    # مدرب
    if text in ["🎓 مدرب", "مدرب"]:
        context.user_data["role"] = "trainer"
        await update.message.reply_text("يرجى إرسال كود المدرب الآن:")
        return

    # ==================== تفعيل الكود ====================
    role = context.user_data.get("role")
    if role:
        code = text
        conn = get_conn()
        cursor = conn.cursor()
        table = "user_codes" if role == "user" else "trainer_codes"
        cursor.execute(f"SELECT days FROM {table} WHERE code=%s", (code,))
        result = cursor.fetchone()

        if result:
            days = result[0]
            expire = datetime.datetime.now() + datetime.timedelta(days=days)
            cursor.execute("""
                INSERT INTO users (telegram_id, role, expire_date)
                VALUES (%s, %s, %s)
                ON CONFLICT (telegram_id) DO UPDATE SET expire_date = %s
            """, (user_id, role, expire, expire))
            conn.commit()

            if role == "user":
                kb = [["🔮 التخمين"]]
                await update.message.reply_text(f"مبروك! تم تفعيل اشتراكك لمدة {days} يوم.", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
            else:
                # مدرب → يدخل وضع التدريب فوراً
                kb = [["🎯 تدريب"]]
                await update.message.reply_text(f"مبروك! تم تفعيل حساب المدرب لمدة {days} يوم.\n\nاضغط على الزر أدناه لتبدأ التدريب:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
            context.user_data.clear()
        else:
            await update.message.reply_text("الكود غير صحيح.")
        cursor.close()
        conn.close()
        return

    # ==================== وضع التدريب (مكتمل الآن) ====================
    if text in ["🎯 تدريب", "تدريب"]:
        context.user_data.clear()
        context.user_data["flow"] = "training"
        context.user_data["training_step"] = "rank"
        kb = [["A","K","Q","J"], ["10","9","8","7"], ["6","5","4","3","2"], ["🔙 رجوع"]]
        await update.message.reply_text("اختر رقم الورقة:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
        return

    # خطوات التدريب الكاملة
    if context.user_data.get("training_step") == "rank":
        context.user_data["rank"] = text
        context.user_data["training_step"] = "suit"
        kb = [["♠️","♥️"], ["♦️","♣️"], ["🔙 رجوع"]]
        await update.message.reply_text("اختر نوع الورقة:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
        return

    if context.user_data.get("training_step") == "suit":
        context.user_data["suit"] = text
        context.user_data["training_step"] = "winner"
        kb = [["زوجين","متتالية"], ["فل هاوس","ثلاثة"], ["اربعة"], ["🔙 رجوع"]]
        await update.message.reply_text("ما الذي ضرب؟", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
        return

    if context.user_data.get("training_step") == "winner":
        context.user_data["winner"] = text
        context.user_data["training_step"] = "hand"
        context.user_data["hands"] = []
        kb = [["متتالية نفس النوع","زوج"], ["دبل AA","ولا شيء"], ["✅ تم"], ["🔙 رجوع"]]
        await update.message.reply_text("اختر نوع أوراق اليد (يمكن أكثر من واحد ثم اضغط تم):", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
        return

    if context.user_data.get("training_step") == "hand":
        if text == "✅ تم":
            rank = context.user_data["rank"]
            suit = context.user_data["suit"]
            winner = context.user_data["winner"]
            hands = context.user_data["hands"]

            conn = get_conn()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO training_data 
                (card_rank, card_suit, previous_hit, minute, winner_type, hand_type, previous_winner_type, source)
                VALUES (%s, %s, 'false', %s, %s, %s, 'none', 'trainer')
            """, (rank, suit, datetime.datetime.now().hour, winner, hands))
            conn.commit()
            cursor.close()
            conn.close()

            await update.message.reply_text("✅ تم حفظ التدريب بنجاح.\nشكراً لك!")
            context.user_data.clear()
            kb = [["🎯 تدريب"], ["🔙 رجوع"]]
            await update.message.reply_text("اختر اللي تبغاه:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
            return
        else:
            context.user_data["hands"].append(text)
            await update.message.reply_text(f"تم إضافة: {text}")
            return

    # ==================== التخمين (للمستخدم) ====================
    if text in ["🔮 التخمين", "تخمين"]:
        if not check_subscription(user_id) or not check_daily_limit(user_id):
            return
        # ... (نفس الكود السابق للتخمين - إذا تبغاه كامل قل "كامل تخمين")
        await update.message.reply_text("التخمين جاهز قريباً.")
        return

    await update.message.reply_text("يرجى اختيار من الكيبورد أو الضغط على 🔙 رجوع")


# ==================== MAIN ====================
def main():
    threading.Thread(target=run_server, daemon=True).start()
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("✅ بوت تكساس ويبلاي جاهز")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
