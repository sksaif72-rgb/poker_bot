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


# ==================== DATABASE ====================
def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")


# ==================== KEEP-ALIVE ====================
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'Poker bot running')


def run_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), Handler)
    server.serve_forever()


# ==================== DAILY LIMIT + SUBSCRIPTION ====================
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


# ==================== AI (يتعلم التسلسل) ====================
def database_prediction(rank, suit, previous_winner):
    tz = pytz.timezone("Asia/Riyadh")
    hour = datetime.datetime.now(tz).hour

    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT winner_type, hand_type
        FROM training_data
        WHERE card_rank=%s AND card_suit=%s 
          AND previous_winner_type=%s 
          AND minute BETWEEN %s AND %s
    """, (rank, suit, previous_winner, hour-1, hour+1))
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
    for k, v in mc_w.items(): final_w[k] += v * (1-weight)
    for k, v in db_h.items(): final_h[k] += v * weight
    for k, v in mc_h.items(): final_h[k] += v * (1-weight)
    return final_w, final_h


def top_predictions(counter):
    total = sum(counter.values())
    if total == 0: return []
    return [(name, round(val/total*100, 2)) for name, val in counter.most_common(2)]


# ==================== START ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [["👤 اشتراك"], ["🎓 مدرب"], ["🔙 رجوع"]]  # حتى في البداية
    await update.message.reply_text(
        "♠️ بوت تخمين البوكر\nاختر نوع الحساب:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )


# ==================== MESSAGE HANDLER (مع زر رجوع في كل خطوة) ====================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.message.from_user.id

    # ==================== زر الرجوع في أي خطوة ====================
    if text == "🔙 رجوع":
        if context.user_data.get("flow") == "predict":
            context.user_data["predict_step"] = "winner_type"
            keyboard = [["زوجين", "متتالية"], ["فل هاوس", "ثلاثة"], ["اربعة"], ["🔙 رجوع"]]
            await update.message.reply_text("رجعت لاختيار نوع الضربة المتوقعة", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
            return
        elif context.user_data.get("flow") == "training":
            context.user_data["training_step"] = "rank"
            keyboard = [["A","K","Q","J"], ["10","9","8","7"], ["6","5","4","3","2"], ["🔙 رجوع"]]
            await update.message.reply_text("رجعت لاختيار رقم الورقة", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
            return
        else:
            await update.message.reply_text("رجعت للقائمة الرئيسية")
            context.user_data.clear()
            await start(update, context)
            return

    # ==================== باقي الكود (اشتراك، تدريب، تخمين) ====================
    # ... (الاشتراك والتدريب نفس السابق مع إضافة زر "🔙 رجوع" في كل كيبورد)

    # ==================== التخمين المطور ====================
    if text == "🔮 التخمين":
        if not check_subscription(user_id) or not check_daily_limit(user_id):
            return
        context.user_data.clear()
        context.user_data["flow"] = "predict"
        context.user_data["predict_step"] = "winner_type"
        keyboard = [["زوجين", "متتالية"], ["فل هاوس", "ثلاثة"], ["اربعة"], ["🔙 رجوع"]]
        await update.message.reply_text("اختر نوع الضربة المتوقعة:", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
        return

    # ... (خطوات winner_type → rank → suit نفسها مع زر رجوع في كل كيبورد)

    if context.user_data.get("predict_step") == "suit":
        # عرض النتيجة
        # ... (نفس الكود السابق)

        # بعد عرض النتيجة مباشرة يطلب الضربة الحقيقية مع التنبيه
        warning = "⚠️ **تحذير مهم جداً**:\nإذا حطيت معلومة غلط، البوت راح يعطيك تخمينات غير صحيحة في المستقبل!\nتأكد من الإجابة الصحيحة 100%"
        keyboard = [["زوجين","متتالية"], ["فل هاوس","ثلاثة"], ["اربعة"], ["🔙 رجوع"]]
        await update.message.reply_text(warning + "\n\nشنو كانت الضربة الحقيقية؟", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
        context.user_data["predict_step"] = "real_winner"
        return

    if context.user_data.get("predict_step") == "real_winner":
        real_winner = text
        rank = context.user_data["rank"]
        suit = context.user_data["suit"]
        prev = context.user_data.get("previous_winner_type", "none")

        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO training_data 
            (card_rank, card_suit, previous_hit, minute, winner_type, hand_type, previous_winner_type, source)
            VALUES (%s, %s, 'false', %s, %s, %s, %s, 'user')
        """, (rank, suit, datetime.datetime.now().hour, real_winner, [], prev))
        conn.commit()
        cursor.close()
        conn.close()

        increment_daily_count(user_id)
        await update.message.reply_text("✅ شكراً! تم حفظ البيانات الحقيقية والبوت صار أذكى")
        context.user_data.clear()
        keyboard = [["🔮 التخمين"], ["🔙 رجوع"]]
        await update.message.reply_text("اختر اللي تبغاه:", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
        return

    # (التدريب أيضاً فيه زر رجوع في كل خطوة)

# ==================== MAIN ====================
def main():
    threading.Thread(target=run_server, daemon=True).start()
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("✅ Poker Bot Started (مع زر رجوع + تنبيه قوي)")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
