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


# ==================== DATABASE CONNECTION ====================
def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")


# ==================== KEEP-ALIVE SERVER ====================
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'Poker bot running')


def run_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), Handler)
    server.serve_forever()


# ==================== RATE LIMIT (2 رسالة كل دقيقة) ====================
user_limits = {}

def check_limit(user_id):
    tz = pytz.timezone("Asia/Riyadh")
    minute = datetime.datetime.now(tz).minute

    if user_id not in user_limits:
        user_limits[user_id] = {"minute": minute, "count": 0}

    if user_limits[user_id]["minute"] != minute:
        user_limits[user_id] = {"minute": minute, "count": 0}

    if user_limits[user_id]["count"] >= 2:
        return False

    user_limits[user_id]["count"] += 1
    return True


# ==================== DAILY LIMIT (حسب الاشتراك) ====================
def get_subscription_days(user_id):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT expire_date FROM users WHERE telegram_id=%s", (user_id,))
    data = cursor.fetchone()
    cursor.close()
    conn.close()

    if not data:
        return 0
    expire = data[0].date()
    today = datetime.date.today()
    return max(0, (expire - today).days)


def check_daily_limit(user_id):
    days_left = get_subscription_days(user_id)
    if days_left > 30:
        return True  # غير محدود

    # اشتراك أسبوعي أو أقل → 50 مرة يومياً
    conn = get_conn()
    cursor = conn.cursor()
    today = datetime.date.today()
    cursor.execute("""
        SELECT count FROM daily_usage 
        WHERE telegram_id=%s AND usage_date=%s
    """, (user_id, today))
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
        ON CONFLICT (telegram_id, usage_date)
        DO UPDATE SET count = daily_usage.count + 1
    """, (user_id, today))
    conn.commit()
    cursor.close()
    conn.close()


# ==================== CHECK SUBSCRIPTION ====================
def check_subscription(user_id):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT expire_date FROM users WHERE telegram_id=%s", (user_id,))
    data = cursor.fetchone()
    cursor.close()
    conn.close()
    if not data:
        return False
    return datetime.datetime.now() < data[0]


# ==================== AI FUNCTIONS ====================
def database_prediction(rank, suit):
    tz = pytz.timezone("Asia/Riyadh")
    minute = datetime.datetime.now(tz).minute

    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT winner_type, hand_type
        FROM training_data
        WHERE card_rank=%s AND card_suit=%s AND previous_hit='false' AND minute=%s
    """, (rank, suit, minute))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    winner_counter = Counter()
    hand_counter = Counter()
    for row in rows:
        winner_counter[row[0]] += 1
        for h in row[1]:
            hand_counter[h] += 1
    return winner_counter, hand_counter


def monte_carlo_prediction():
    winner_options = ["زوجين", "متتالية", "فل هاوس", "ثلاثة", "اربعة"]
    hand_options = ["متتالية نفس النوع", "زوج", "دبل AA", "ولا شيء"]

    winner_counter = Counter()
    hand_counter = Counter()
    for _ in range(10000):
        winner_counter[random.choice(winner_options)] += 1
        hand_counter[random.choice(hand_options)] += 1
    return winner_counter, hand_counter


def combine_predictions(db_w, db_h, mc_w, mc_h):
    final_w = Counter()
    final_h = Counter()
    for k, v in db_w.items(): final_w[k] += v * 0.8
    for k, v in mc_w.items(): final_w[k] += v * 0.2
    for k, v in db_h.items(): final_h[k] += v * 0.8
    for k, v in mc_h.items(): final_h[k] += v * 0.2
    return final_w, final_h


def top_predictions(counter):
    total = sum(counter.values())
    if total == 0:
        return []
    return [(name, round((val / total) * 100, 2)) for name, val in counter.most_common(2)]


# ==================== START ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [["👤 اشتراك"], ["🎓 مدرب"]]
    await update.message.reply_text(
        "♠️ بوت تخمين البوكر\n\nاختر نوع الحساب:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )


# ==================== MESSAGE HANDLER (كل شيء هنا) ====================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.message.from_user.id

    if not check_limit(user_id):
        await update.message.reply_text("⏳ انتظر دقيقة قبل إرسال رسالة جديدة.")
        return

    # ------------------ اشتراك / مدرب ------------------
    if text == "👤 اشتراك":
        context.user_data["role"] = "user"
        await update.message.reply_text("ارسل كود الاشتراك")
        return

    if text == "🎓 مدرب":
        context.user_data["role"] = "trainer"
        await update.message.reply_text("ارسل كود المدرب")
        return

    # ------------------ تفعيل الكود ------------------
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

            keyboard = [["🔮 التخمين"]] if role == "user" else [["🎯 تدريب"]]
            await update.message.reply_text(
                f"✅ تم التفعيل لمدة {days} يوم",
                reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
            )
            context.user_data.clear()
        else:
            await update.message.reply_text("❌ الكود غير صحيح")
        cursor.close()
        conn.close()
        return

    # ------------------ التدريب (نفس النسخة القديمة) ------------------
    if text == "🎯 تدريب":
        context.user_data.clear()
        context.user_data["training_step"] = "rank"
        keyboard = [["A","K","Q","J"], ["10","9","8","7"], ["6","5","4","3","2"]]
        await update.message.reply_text("اختر رقم الورقة للتدريب", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
        return

    if context.user_data.get("training_step") == "rank":
        context.user_data["rank"] = text
        context.user_data["training_step"] = "suit"
        keyboard = [["♠️","♥️"], ["♦️","♣️"]]
        await update.message.reply_text("اختر نوع الورقة", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
        return

    if context.user_data.get("training_step") == "suit":
        context.user_data["suit"] = text
        rank = context.user_data["rank"]
        suit = context.user_data["suit"]

        db_w, db_h = database_prediction(rank, suit)
        mc_w, mc_h = monte_carlo_prediction()
        final_w, final_h = combine_predictions(db_w, db_h, mc_w, mc_h)
        winners = top_predictions(final_w)

        msg = "🔮 التخمين:\n\n"
        for w, p in winners:
            msg += f"{w} : {p}%\n"
        await update.message.reply_text(msg)

        context.user_data["training_step"] = "winner"
        keyboard = [["زوجين","متتالية"], ["فل هاوس","ثلاثة"], ["اربعة"]]
        await update.message.reply_text("ما الذي ضرب؟", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
        return

    if context.user_data.get("training_step") == "winner":
        context.user_data["winner"] = text
        context.user_data["training_step"] = "hand"
        context.user_data["hands"] = []
        keyboard = [["متتالية نفس النوع","زوج"], ["دبل AA","ولا شيء"], ["✅ تم"]]
        await update.message.reply_text("اختر نوع أوراق اليد (يمكن أكثر من واحد)", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
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
                INSERT INTO training_data (card_rank, card_suit, previous_hit, minute, winner_type, hand_type)
                VALUES (%s, %s, 'false', %s, %s, %s)
            """, (rank, suit, datetime.datetime.now().minute, winner, hands))
            conn.commit()
            cursor.close()
            conn.close()

            await update.message.reply_text("✅ تم حفظ التدريب", reply_markup=ReplyKeyboardMarkup([["🎯 تدريب"]], resize_keyboard=True))
            context.user_data.clear()
            return
        else:
            context.user_data["hands"].append(text)
            await update.message.reply_text(f"تم اختيار: {text}")
            return

    # ------------------ التخمين الجديد (حسب طلبك بالضبط) ------------------
    if text == "🔮 التخمين":
        if not check_subscription(user_id):
            await update.message.reply_text("❌ اشتراكك منتهي، جدده أولاً")
            return
        if not check_daily_limit(user_id):
            await update.message.reply_text("❌ وصلت الحد اليومي (50 تخمين). حاول غداً")
            return

        context.user_data.clear()
        context.user_data["predict_step"] = "winner_type"

        keyboard = [["زوجين", "متتالية"], ["فل هاوس", "ثلاثة"], ["اربعة"]]
        await update.message.reply_text(
            "🔮 اختر نوع الضربة المتوقعة:",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )
        return

    # خطوات التخمين
    step = context.user_data.get("predict_step")

    if step == "winner_type":
        context.user_data["winner_type"] = text  # نحفظه لكن ما نستخدمه في الحساب (حسب النسخة الأصلية)
        context.user_data["predict_step"] = "rank"
        keyboard = [["A","K","Q","J"], ["10","9","8","7"], ["6","5","4","3","2"]]
        await update.message.reply_text("اختر رقم الورقة:", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
        return

    if step == "rank":
        context.user_data["rank"] = text
        context.user_data["predict_step"] = "suit"
        keyboard = [["♠️","♥️"], ["♦️","♣️"]]
        await update.message.reply_text("اختر نوع الورقة:", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
        return

    if step == "suit":
        context.user_data["suit"] = text
        rank = context.user_data["rank"]
        suit = context.user_data["suit"]

        db_w, db_h = database_prediction(rank, suit)
        mc_w, mc_h = monte_carlo_prediction()
        final_w, final_h = combine_predictions(db_w, db_h, mc_w, mc_h)

        winners = top_predictions(final_w)
        hands = top_predictions(final_h)

        msg = "🔮 **نتيجة التخمين**\n\n**نوع الضربة:**\n"
        for w, p in winners:
            msg += f"• {w} : {p}%\n"
        msg += "\n**نوع اليد:**\n"
        for h, p in hands:
            msg += f"• {h} : {p}%\n"

        await update.message.reply_text(msg, parse_mode="Markdown")

        # زيادة العداد اليومي
        increment_daily_count(user_id)

        # زر خمن ثاني + العودة
        keyboard = [["🔄 خمن ثاني"], ["🔙 القائمة الرئيسية"]]
        await update.message.reply_text(
            "تم! اختر اللي تبغاه:",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )
        context.user_data.clear()
        return

    if text == "🔄 خمن ثاني":
        context.user_data.clear()
        context.user_data["predict_step"] = "winner_type"
        keyboard = [["زوجين", "متتالية"], ["فل هاوس", "ثلاثة"], ["اربعة"]]
        await update.message.reply_text(
            "🔮 اختر نوع الضربة المتوقعة:",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )
        return

    if text == "🔙 القائمة الرئيسية":
        keyboard = [["🔮 التخمين"]]
        await update.message.reply_text("القائمة الرئيسية:", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
        context.user_data.clear()
        return


# ==================== MAIN ====================
def main():
    threading.Thread(target=run_server, daemon=True).start()

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("✅ Poker Bot Started Successfully")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
