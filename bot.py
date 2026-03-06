import psycopg2
import os

DATABASE_URL = os.getenv("DATABASE_URL")

conn = psycopg2.connect(DATABASE_URL)
cursor = conn.cursor()
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TOKEN = os.getenv("BOT_TOKEN")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    keyboard = [
        ["اشتراك"],
        ["مدرب"]
    ]

    reply = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    await update.message.reply_text(
        "اهلا بك في بوت التخمين",
        reply_markup=reply
    )

app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))

app.run_polling()
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import psycopg2
import os

TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

conn = psycopg2.connect(DATABASE_URL)
cursor = conn.cursor()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    keyboard = [
        ["👤 اشتراك"],
        ["🎓 مدرب"]
    ]

    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    await update.message.reply_text(
        "🤖 مرحباً بك في بوت تخمين البوكر\n\nاختر نوع الحساب:",
        reply_markup=reply_markup
    )
    async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = update.message.text

    if text == "👤 اشتراك":
        context.user_data["role"] = "user"
        await update.message.reply_text("🔑 ارسل كود الاشتراك")

    elif text == "🎓 مدرب":
        context.user_data["role"] = "trainer"
        await update.message.reply_text("🔑 ارسل كود المدرب")

    else:
        role = context.user_data.get("role")

        if role:
            code = text

            cursor.execute(
                "SELECT role FROM codes WHERE code=%s AND role=%s AND used=false",
                (code, role)
            )

            result = cursor.fetchone()

            if result:

                cursor.execute(
                    "UPDATE codes SET used=true WHERE code=%s",
                    (code,)
                )

                cursor.execute(
                    "INSERT INTO users (telegram_id, role) VALUES (%s,%s)",
                    (update.message.from_user.id, role)
                )

                conn.commit()

                if role == "user":

                    keyboard = [["🔮 التخمين"]]

                else:

                    keyboard = [["🎯 تدريب"]]

                reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

                await update.message.reply_text(
                    "✅ تم التفعيل بنجاح",
                    reply_markup=reply_markup
                )

            else:

                await update.message.reply_text("❌ الكود غير صحيح")
                app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.TEXT, handle_message))

app.run_polling()
elif text == "🔮 التخمين":

    keyboard = [
        ["A","K","Q","J"],
        ["10","9","8","7"],
        ["6","5","4","3","2"],
        ["⬅ رجوع"]
    ]

    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    context.user_data["step"] = "rank"

    await update.message.reply_text(
        "🃏 اختر رقم الورقة",
        reply_markup=reply_markup
    )
if context.user_data.get("step") == "rank":

    context.user_data["card_rank"] = text
    context.user_data["step"] = "suit"

    keyboard = [
        ["❤️","♦️"],
        ["♠️","♣️"],
        ["⬅ رجوع"]
    ]

    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    await update.message.reply_text(
        "اختر نوع الورقة",
        reply_markup=reply_markup
    )

    return
    if context.user_data.get("step") == "suit":

    context.user_data["card_suit"] = text
    context.user_data["step"] = "previous"

    keyboard = [
        ["زوجين","متتالية"],
        ["فل هاوس","ثلاثة"],
        ["اربعة"],
        ["⬅ رجوع"]
    ]

    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    await update.message.reply_text(
        "ما هي ضربة الجولة السابقة؟",
        reply_markup=reply_markup
    )

    return
    if context.user_data.get("step") == "previous":

    context.user_data["previous_hit"] = text

    rank = context.user_data["card_rank"]
    suit = context.user_data["card_suit"]
    previous = context.user_data["previous_hit"]

    await update.message.reply_text("🔮 جاري حساب التوقع...")

    # هنا لاحقاً سنضع الذكاء الصناعي

    result = """
🔮 توقع نوع أوراق الفائز

ثلاثة : 32%
زوجين : 28%

🃏 توقع أوراق اليد

زوج : 35%
ولا شيء : 22%
"""

    keyboard = [["➡️ التخمين التالي"]]

    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    await update.message.reply_text(result, reply_markup=reply_markup)

    context.user_data.clear()

    return
    if text == "⬅ رجوع":

    context.user_data.clear()

    keyboard = [
        ["🔮 التخمين"],
        ["📊 احصائيات"]
    ]

    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    await update.message.reply_text(
        "رجعت للقائمة الرئيسية",
        reply_markup=reply_markup
    )

    return
    elif text == "📊 احصائيات":

    cursor.execute("SELECT COUNT(*) FROM training_data")
    total = cursor.fetchone()[0]

    message = f"""
📊 احصائيات البوت

عدد التدريبات:
{total}

الخوارزمية:
80٪ تحليل قاعدة البيانات
20٪ Monte Carlo
"""

    await update.message.reply_text(message)
import datetime
import pytz
import random
from collections import Counter
def database_prediction(rank, suit, previous_hit):

    tz = pytz.timezone("Asia/Riyadh")
    minute = datetime.datetime.now(tz).minute

    cursor.execute("""
    SELECT winner_type, hand_type
    FROM training_data
    WHERE card_rank=%s
    AND card_suit=%s
    AND previous_hit=%s
    AND minute=%s
    """, (rank, suit, previous_hit, minute))

    rows = cursor.fetchall()

    winner_counter = Counter()
    hand_counter = Counter()

    for r in rows:

        winner_counter[r[0]] += 1

        for h in r[1]:
            hand_counter[h] += 1

    return winner_counter, hand_counter
def monte_carlo_prediction():

    winner_options = ["زوجين","متتالية","فل هاوس","ثلاثة","اربعة"]
    hand_options = ["متتالية نفس النوع","زوج","دبل AA","ولا شيء"]

    winner_counter = Counter()
    hand_counter = Counter()

    for i in range(5000):

        winner = random.choice(winner_options)
        winner_counter[winner] += 1

        hand = random.choice(hand_options)
        hand_counter[hand] += 1

    return winner_counter, hand_counter
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
def top_predictions(counter):

    total = sum(counter.values())

    result = counter.most_common(2)

    formatted = []

    for name,value in result:

        percent = round((value / total) * 100,2)

        formatted.append((name,percent))

    return formatted
db_winner, db_hand = database_prediction(rank, suit, previous)

mc_winner, mc_hand = monte_carlo_prediction()

final_winner, final_hand = combine_predictions(
    db_winner,
    db_hand,
    mc_winner,
    mc_hand
)

winner_result = top_predictions(final_winner)
hand_result = top_predictions(final_hand)
message = "🔮 توقع نوع أوراق الفائز\n\n"

for w in winner_result:
    message += f"{w[0]} : {w[1]}%\n"

message += "\n🃏 توقع أوراق اليد\n\n"

for h in hand_result:
    message += f"{h[0]} : {h[1]}%\n"
elif text == "🎯 تدريب":

    keyboard = [
        ["A","K","Q","J"],
        ["10","9","8","7"],
        ["6","5","4","3","2"],
        ["⬅ رجوع"]
    ]

    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    context.user_data["trainer_step"] = "rank"

    await update.message.reply_text(
        "🃏 اختر رقم الورقة للتدريب",
        reply_markup=reply_markup
    )
if context.user_data.get("trainer_step") == "rank":

    context.user_data["rank"] = text
    context.user_data["trainer_step"] = "suit"

    keyboard = [
        ["❤️","♦️"],
        ["♠️","♣️"],
        ["⬅ رجوع"]
    ]

    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    await update.message.reply_text(
        "اختر نوع الورقة",
        reply_markup=reply_markup
    )

    return
if context.user_data.get("trainer_step") == "suit":

    context.user_data["suit"] = text
    context.user_data["trainer_step"] = "previous"

    keyboard = [
        ["زوجين","متتالية"],
        ["فل هاوس","ثلاثة"],
        ["اربعة"],
        ["⬅ رجوع"]
    ]

    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    await update.message.reply_text(
        "ما هي الضربة السابقة؟",
        reply_markup=reply_markup
    )

    return
if context.user_data.get("trainer_step") == "previous":

    context.user_data["previous"] = text
    context.user_data["trainer_step"] = "winner"

    keyboard = [
        ["زوجين","متتالية"],
        ["فل هاوس","ثلاثة"],
        ["اربعة"],
        ["⬅ رجوع"]
    ]

    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    await update.message.reply_text(
        "🏆 اختر نوع أوراق الفائز الحقيقي",
        reply_markup=reply_markup
    )

    return
if context.user_data.get("trainer_step") == "winner":

    context.user_data["winner"] = text
    context.user_data["trainer_step"] = "hand"

    context.user_data["hands"] = []

    keyboard = [
        ["متتالية نفس النوع"],
        ["زوج"],
        ["دبل AA"],
        ["ولا شيء"],
        ["✅ تم"]
    ]

    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    await update.message.reply_text(
        "اختر أوراق اليد (يمكن اختيار اكثر من خيار)",
        reply_markup=reply_markup
    )

    return
if context.user_data.get("trainer_step") == "hand":

    if text != "✅ تم":

        context.user_data["hands"].append(text)

        await update.message.reply_text(
            f"تم إضافة: {text}"
        )

        return
            else:

        import datetime
        import pytz

        tz = pytz.timezone("Asia/Riyadh")
        minute = datetime.datetime.now(tz).minute

        cursor.execute("""
        INSERT INTO training_data
        (card_rank, card_suit, previous_hit, winner_type, hand_type, minute)
        VALUES (%s,%s,%s,%s,%s,%s)
        """, (

            context.user_data["rank"],
            context.user_data["suit"],
            context.user_data["previous"],
            context.user_data["winner"],
            context.user_data["hands"],
            minute

        ))

        conn.commit()

        context.user_data.clear()

        keyboard = [["🎯 تدريب"]]

        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

        await update.message.reply_text(
            "✅ تم حفظ التدريب بنجاح",
            reply_markup=reply_markup
        )

        return

