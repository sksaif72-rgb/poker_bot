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


# DATABASE CONNECTION
def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")


# RENDER PORT SERVER
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'Poker bot running')


def run_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), Handler)
    server.serve_forever()


# LIMIT SYSTEM
user_limits = {}

def check_limit(user_id):

    tz = pytz.timezone("Asia/Riyadh")
    minute = datetime.datetime.now(tz).minute

    if user_id not in user_limits:
        user_limits[user_id] = {"minute": minute, "count": 0}

    if user_limits[user_id]["minute"] != minute:
        user_limits[user_id]["minute"] = minute
        user_limits[user_id]["count"] = 0

    if user_limits[user_id]["count"] >= 2:
        return False

    user_limits[user_id]["count"] += 1
    return True


# START
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    keyboard = [
        ["👤 اشتراك"],
        ["🎓 مدرب"]
    ]

    await update.message.reply_text(
        "♠️ بوت تخمين البوكر\n\nاختر نوع الحساب:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )


# CHECK SUBSCRIPTION
def check_subscription(user_id):

    conn = get_conn()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT expire_date FROM users WHERE telegram_id=%s",
        (user_id,)
    )

    data = cursor.fetchone()

    cursor.close()
    conn.close()

    if not data:
        return False

    expire = data[0]

    if datetime.datetime.now() > expire:
        return False

    return True


# DATABASE AI
def database_prediction(rank, suit, previous):

    tz = pytz.timezone("Asia/Riyadh")
    minute = datetime.datetime.now(tz).minute

    conn = get_conn()
    cursor = conn.cursor()

    cursor.execute("""
    SELECT winner_type, hand_type
    FROM training_data
    WHERE card_rank=%s
    AND card_suit=%s
    AND previous_hit=%s
    AND minute=%s
    """,(rank,suit,previous,minute))

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


# MONTE CARLO
def monte_carlo_prediction():

    winner_options = ["زوجين","متتالية","فل هاوس","ثلاثة","اربعة"]
    hand_options = ["متتالية نفس النوع","زوج","دبل AA","ولا شيء"]

    winner_counter = Counter()
    hand_counter = Counter()

    for i in range(10000):

        winner = random.choice(winner_options)
        winner_counter[winner] += 1

        hand = random.choice(hand_options)
        hand_counter[hand] += 1

    return winner_counter, hand_counter


# COMBINE AI
def combine_predictions(db_winner, db_hand, mc_winner, mc_hand):

    final_winner = Counter()
    final_hand = Counter()

    for k,v in db_winner.items():
        final_winner[k] += v * 0.8

    for k,v in mc_winner.items():
        final_winner[k] += v * 0.2

    for k,v in db_hand.items():
        final_hand[k] += v * 0.8

    for k,v in mc_hand.items():
        final_hand[k] += v * 0.2

    return final_winner, final_hand


# TOP RESULTS
def top_predictions(counter):

    total = sum(counter.values())

    if total == 0:
        return []

    top = counter.most_common(2)

    results = []

    for name,val in top:

        percent = round((val/total)*100,2)
        results.append((name,percent))

    return results


# MESSAGE HANDLER
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = update.message.text
    user_id = update.message.from_user.id


# ACCOUNT TYPE
    if text == "👤 اشتراك":

        context.user_data["role"] = "user"
        await update.message.reply_text("ارسل كود الاشتراك")
        return

    if text == "🎓 مدرب":

        context.user_data["role"] = "trainer"
        await update.message.reply_text("ارسل كود المدرب")
        return


# CODE CHECK
    role = context.user_data.get("role")

    if role:

        code = text

        conn = get_conn()
        cursor = conn.cursor()

        if role == "user":

            cursor.execute(
                "SELECT days FROM user_codes WHERE code=%s",
                (code,)
            )

        else:

            cursor.execute(
                "SELECT days FROM trainer_codes WHERE code=%s",
                (code,)
            )

        result = cursor.fetchone()

        if result:

            days = result[0]

            expire = datetime.datetime.now() + datetime.timedelta(days=days)

            cursor.execute(
                """
                INSERT INTO users (telegram_id,role,expire_date)
                VALUES (%s,%s,%s)
                ON CONFLICT (telegram_id)
                DO UPDATE SET expire_date=%s
                """,
                (user_id,role,expire,expire)
            )

            conn.commit()

            cursor.close()
            conn.close()

            if role == "user":
                keyboard = [["🔮 التخمين"]]
            else:
                keyboard = [["🎯 تدريب"]]

            await update.message.reply_text(
                f"تم التفعيل لمدة {days} يوم",
                reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
            )

            context.user_data.clear()
            return

        else:

            await update.message.reply_text("الكود غير صحيح")
            cursor.close()
            conn.close()
            return


# ---------------- TRAINING SYSTEM ----------------

    if text == "🎯 تدريب":

        context.user_data.clear()
        context.user_data["training_step"] = "rank"

        keyboard = [
            ["A","K","Q","J"],
            ["10","9","8","7"],
            ["6","5","4","3","2"]
        ]

        await update.message.reply_text(
            "اختر رقم الورقة للتدريب",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )
        return


# اختيار الرقم
    if context.user_data.get("training_step") == "rank":

        context.user_data["rank"] = text
        context.user_data["training_step"] = "suit"

        keyboard = [
            ["♠️","♥️"],
            ["♦️","♣️"]
        ]

        await update.message.reply_text(
            "اختر نوع الورقة",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )
        return


# اختيار النوع + إعطاء التخمين
    if context.user_data.get("training_step") == "suit":

        context.user_data["suit"] = text

        rank = context.user_data["rank"]
        suit = context.user_data["suit"]

db_winner, db_hand = database_prediction(rank,suit,"false")
        mc_winner, mc_hand = monte_carlo_prediction()

        final_winner, final_hand = combine_predictions(
            db_winner,db_hand,mc_winner,mc_hand
        )

        winners = top_predictions(final_winner)

        msg = "🔮 التخمين:\n\n"

        for w in winners:
            msg += f"{w[0]} : {w[1]}%\n"

        await update.message.reply_text(msg)

        context.user_data["training_step"] = "winner"

        keyboard = [
            ["زوجين","متتالية"],
            ["فل هاوس","ثلاثة"],
            ["اربعة"]
        ]

        await update.message.reply_text(
            "ما الذي ضرب؟",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )
        return


# اختيار الفائز
    if context.user_data.get("training_step") == "winner":

        context.user_data["winner"] = text
        context.user_data["training_step"] = "hand"

        context.user_data["hands"] = []

        keyboard = [
            ["متتالية نفس النوع","زوج"],
            ["دبل AA","ولا شيء"],
            ["✅ تم"]
        ]

        await update.message.reply_text(
            "اختر نوع أوراق اليد (يمكن اختيار أكثر من خيار ثم اضغط تم)",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )
        return


# اختيار أوراق اليد
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
            (card_rank,card_suit,previous_hit,minute,winner_type,hand_type)
            VALUES(%s,%s,%s,%s,%s,%s)
            """,(
                rank,
                suit,
                "false",
                datetime.datetime.now().minute,
                winner,
                hands
            ))

            conn.commit()

            cursor.close()
            conn.close()

            keyboard = [["🎯 تدريب"]]

            await update.message.reply_text(
                "✅ تم حفظ التدريب",
                reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
            )

            context.user_data.clear()
            return

        else:

            context.user_data["hands"].append(text)

            await update.message.reply_text(
                f"تم اختيار: {text}"
            )
            return
# MAIN
def main():

    threading.Thread(target=run_server).start()

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT, handle_message))

    print("BOT STARTED")

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
