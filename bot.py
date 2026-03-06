import os
import random
import datetime
import pytz
import psycopg2
import threading

from flask import Flask
from collections import Counter

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters


TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")


conn = psycopg2.connect(DATABASE_URL)
cursor = conn.cursor()


# ==============================
# WEB SERVER FOR RENDER
# ==============================

web_app = Flask(__name__)

@web_app.route("/")
def home():
    return "Poker bot running"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port)


# ==============================
# LIMIT SYSTEM (2 per minute)
# ==============================

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


# ==============================
# START
# ==============================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    keyboard = [
        ["👤 اشتراك"],
        ["🎓 مدرب"]
    ]

    await update.message.reply_text(
        "♠️ بوت تخمين البوكر\n\nاختر نوع الحساب:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )


# ==============================
# DATABASE AI
# ==============================

def database_prediction(rank, suit, previous):

    tz = pytz.timezone("Asia/Riyadh")
    minute = datetime.datetime.now(tz).minute

    cursor.execute("""
    SELECT winner_type, hand_type
    FROM training_data
    WHERE card_rank=%s
    AND card_suit=%s
    AND previous_hit=%s
    AND minute=%s
    """,(rank,suit,previous,minute))

    rows = cursor.fetchall()

    winner_counter = Counter()
    hand_counter = Counter()

    for row in rows:

        winner_counter[row[0]] += 1

        for h in row[1]:
            hand_counter[h] += 1

    return winner_counter, hand_counter


# ==============================
# MONTE CARLO
# ==============================

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


# ==============================
# COMBINE
# ==============================

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


# ==============================
# TOP RESULTS
# ==============================

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


# ==============================
# MAIN HANDLER
# ==============================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = update.message.text
    user_id = update.message.from_user.id


# SUBSCRIBE

    if text == "👤 اشتراك":

        context.user_data["role"] = "user"

        await update.message.reply_text("ارسل كود الاشتراك")
        return


# TRAINER

    if text == "🎓 مدرب":

        context.user_data["role"] = "trainer"

        await update.message.reply_text("ارسل كود المدرب")
        return


# CODE CHECK

    role = context.user_data.get("role")

    if role:

        code = text

        if role == "user":

            cursor.execute(
                "SELECT used FROM user_codes WHERE code=%s",
                (code,)
            )

        else:

            cursor.execute(
                "SELECT used FROM trainer_codes WHERE code=%s",
                (code,)
            )

        result = cursor.fetchone()

        if result and result[0] == False:

            if role == "user":

                cursor.execute(
                    "UPDATE user_codes SET used=true WHERE code=%s",
                    (code,)
                )

            else:

                cursor.execute(
                    "UPDATE trainer_codes SET used=true WHERE code=%s",
                    (code,)
                )

            cursor.execute(
                "INSERT INTO users (telegram_id, role) VALUES (%s,%s)",
                (user_id, role)
            )

            conn.commit()

            if role == "user":
                keyboard = [["🔮 التخمين"]]
            else:
                keyboard = [["🎯 تدريب"]]

            await update.message.reply_text(
                "تم التفعيل بنجاح",
                reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
            )

            context.user_data.clear()
            return

        else:

            await update.message.reply_text("الكود غير صحيح")
            return


# START PREDICTION

    if text == "🔮 التخمين":

        if not check_limit(user_id):

            await update.message.reply_text("وصلت الحد الاقصى للتخمين في هذه الدقيقة")
            return

        context.user_data["step"] = "rank"

        keyboard = [
            ["A","K","Q","J"],
            ["10","9","8","7"],
            ["6","5","4","3","2"]
        ]

        await update.message.reply_text(
            "اختر رقم الورقة",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )

        return


# RANK

    if context.user_data.get("step") == "rank":

        context.user_data["rank"] = text
        context.user_data["step"] = "suit"

        keyboard = [
            ["❤️","♦️"],
            ["♠️","♣️"]
        ]

        await update.message.reply_text(
            "اختر نوع الورقة",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )

        return


# SUIT

    if context.user_data.get("step") == "suit":

        context.user_data["suit"] = text
        context.user_data["step"] = "previous"

        keyboard = [
            ["زوجين","متتالية"],
            ["فل هاوس","ثلاثة"],
            ["اربعة"]
        ]

        await update.message.reply_text(
            "ما هي الضربة السابقة",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )

        return


# FINAL PREDICTION

    if context.user_data.get("step") == "previous":

        context.user_data["previous"] = text

        rank = context.user_data["rank"]
        suit = context.user_data["suit"]
        previous = context.user_data["previous"]

        await update.message.reply_text("جاري الحساب...")

        db_winner, db_hand = database_prediction(rank,suit,previous)
        mc_winner, mc_hand = monte_carlo_prediction()

        final_winner, final_hand = combine_predictions(
            db_winner, db_hand,
            mc_winner, mc_hand
        )

        winner = top_predictions(final_winner)
        hand = top_predictions(final_hand)

        message = "🔮 توقع نوع اوراق الفائز\n\n"

        for w in winner:
            message += f"{w[0]} : {w[1]}%\n"

        message += "\n🃏 توقع اوراق اليد\n\n"

        for h in hand:
            message += f"{h[0]} : {h[1]}%\n"

        keyboard = [["🔮 التخمين"]]

        await update.message.reply_text(
            message,
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )

        context.user_data.clear()
        return


# ==============================
# RUN BOT
# ==============================

def main():

    threading.Thread(target=run_web).start()

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT, handle_message))

    print("BOT STARTED")

    app.run_polling()


if __name__ == "__main__":
    main()
