import os
import random
import datetime
import pytz
import psycopg2
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from collections import Counter, defaultdict

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


# ==================== AI SYSTEM ====================

def load_recent_data(limit=500):
    conn = get_conn()
    cursor = conn.cursor()

    cursor.execute("""
    SELECT card_rank, card_suit, winner_type, previous_winner_type
    FROM training_data
    ORDER BY id DESC
    LIMIT %s
    """,(limit,))

    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows


# -------- Pattern AI --------
def pattern_ai(rank,suit,previous):
    data = load_recent_data()

    counter = Counter()

    for r in data:
        if r[0]==rank and r[1]==suit and r[3]==previous:
            counter[r[2]]+=1

    return counter


# -------- Frequency AI --------
def frequency_ai(rank,suit):
    data = load_recent_data()

    counter = Counter()

    for r in data:
        if r[0]==rank and r[1]==suit:
            counter[r[2]]+=1

    return counter


# -------- Recency Weight AI --------
def recency_ai(rank,suit,previous):

    data = load_recent_data()

    counter = Counter()

    weight = len(data)

    for r in data:
        if r[0]==rank and r[1]==suit and r[3]==previous:
            counter[r[2]]+=weight

        weight-=1

    return counter


# -------- Markov Chain --------
def markov_chain(previous):

    data = load_recent_data()

    counter = Counter()

    for r in data:
        if r[3]==previous:
            counter[r[2]]+=1

    return counter


# -------- Monte Carlo --------
def monte_carlo_prediction():
    options = ["زوجين","متتالية","فل هاوس","ثلاثة","اربعة"]

    counter = Counter()

    for _ in range(10000):
        counter[random.choice(options)]+=1

    return counter


# -------- Combine All --------
def ai_prediction(rank,suit,previous):

    p = pattern_ai(rank,suit,previous)
    f = frequency_ai(rank,suit)
    r = recency_ai(rank,suit,previous)
    m = markov_chain(previous)
    mc = monte_carlo_prediction()

    final = Counter()

    for k,v in p.items():
        final[k]+=v*3

    for k,v in f.items():
        final[k]+=v*2

    for k,v in r.items():
        final[k]+=v*4

    for k,v in m.items():
        final[k]+=v*2

    for k,v in mc.items():
        final[k]+=v*1

    return final


def top_predictions(counter):
    total = sum(counter.values())

    if total==0:
        return []

    return [(k,round(v/total*100,2)) for k,v in counter.most_common(2)]


# ==================== START ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    keyboard=[["👤 اشتراك"],["🎓 مدرب"],["🔙 رجوع"]]

    await update.message.reply_text(
        "اهلا وسهلا بوت تكساس ويبلاي ♠️\n\n"
        "نحن هنا لنساعدك على اتخاذ قرارات أقوى في اللعب.\n\n"
        "اختر نوع الحساب لنبدأ معاً:",
        reply_markup=ReplyKeyboardMarkup(keyboard,resize_keyboard=True)
    )


# ==================== MESSAGE HANDLER ====================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = update.message.text.strip()
    user_id = update.message.from_user.id


    if text in ["🔙 رجوع","رجوع","BACK"]:
        context.user_data.clear()
        await start(update,context)
        return


    if text in ["👤 اشتراك","اشتراك"]:
        context.user_data["role"]="user"
        await update.message.reply_text("يرجى إرسال كود الاشتراك الآن:")
        return


    if text in ["🎓 مدرب","مدرب"]:
        context.user_data["role"]="trainer"
        await update.message.reply_text("يرجى إرسال كود المدرب الآن:")
        return


# ==================== CODE ACTIVATION ====================

    role=context.user_data.get("role")

    if role:

        code=text

        conn=get_conn()
        cursor=conn.cursor()

        table="user_codes" if role=="user" else "trainer_codes"

        cursor.execute(f"SELECT days FROM {table} WHERE code=%s",(code,))

        result=cursor.fetchone()

        if result:

            days=result[0]

            expire=datetime.datetime.now()+datetime.timedelta(days=days)

            cursor.execute("""
            INSERT INTO users (telegram_id,role,expire_date)
            VALUES (%s,%s,%s)
            ON CONFLICT (telegram_id)
            DO UPDATE SET expire_date=%s
            """,(user_id,role,expire,expire))

            conn.commit()

            if role=="user":

                kb=[["🔮 التخمين"]]

                await update.message.reply_text(
                    f"مبروك! تم تفعيل اشتراكك لمدة {days} يوم.",
                    reply_markup=ReplyKeyboardMarkup(kb,resize_keyboard=True)
                )

            else:

                kb=[["🎯 تدريب"]]

                await update.message.reply_text(
                    f"مبروك! تم تفعيل حساب المدرب لمدة {days} يوم.\n\nاضغط على الزر أدناه لتبدأ التدريب:",
                    reply_markup=ReplyKeyboardMarkup(kb,resize_keyboard=True)
                )

            context.user_data.clear()

        else:

            await update.message.reply_text("الكود غير صحيح.")

        cursor.close()
        conn.close()

        return


# ==================== TRAINING ====================

    if text in ["🎯 تدريب","تدريب"]:

        context.user_data.clear()
        context.user_data["training_step"]="rank"

        kb=[["A","K","Q","J"],["10","9","8","7"],["6","5","4","3","2"],["🔙 رجوع"]]

        await update.message.reply_text(
            "اختر رقم الورقة:",
            reply_markup=ReplyKeyboardMarkup(kb,resize_keyboard=True)
        )

        return


    if context.user_data.get("training_step")=="rank":

        context.user_data["rank"]=text
        context.user_data["training_step"]="suit"

        kb=[["♠️","♥️"],["♦️","♣️"],["🔙 رجوع"]]

        await update.message.reply_text(
            "اختر نوع الورقة:",
            reply_markup=ReplyKeyboardMarkup(kb,resize_keyboard=True)
        )

        return


    if context.user_data.get("training_step")=="suit":

        context.user_data["suit"]=text
        context.user_data["training_step"]="winner"

        kb=[["زوجين","متتالية"],["فل هاوس","ثلاثة"],["اربعة"],["🔙 رجوع"]]

        await update.message.reply_text(
            "ما الذي ضرب؟",
            reply_markup=ReplyKeyboardMarkup(kb,resize_keyboard=True)
        )

        return


# ==================== USER PREDICTION ====================

    if text in ["🔮 التخمين","تخمين"]:

        if not check_subscription(user_id):
            return

        if not check_daily_limit(user_id):
            return

        context.user_data["predict_step"]="rank"

        kb=[["A","K","Q","J"],["10","9","8","7"],["6","5","4","3","2"],["🔙 رجوع"]]

        await update.message.reply_text(
            "اختر رقم الورقة:",
            reply_markup=ReplyKeyboardMarkup(kb,resize_keyboard=True)
        )

        return


    if context.user_data.get("predict_step")=="rank":

        context.user_data["rank"]=text
        context.user_data["predict_step"]="suit"

        kb=[["♠️","♥️"],["♦️","♣️"],["🔙 رجوع"]]

        await update.message.reply_text(
            "اختر نوع الورقة:",
            reply_markup=ReplyKeyboardMarkup(kb,resize_keyboard=True)
        )

        return


    if context.user_data.get("predict_step")=="suit":

        context.user_data["suit"]=text
        context.user_data["predict_step"]="previous"

        kb=[["زوجين","متتالية"],["فل هاوس","ثلاثة"],["اربعة"],["ولا شيء"],["🔙 رجوع"]]

        await update.message.reply_text(
            "ما آخر ضربة؟",
            reply_markup=ReplyKeyboardMarkup(kb,resize_keyboard=True)
        )

        return


    if context.user_data.get("predict_step")=="previous":

        rank=context.user_data["rank"]
        suit=context.user_data["suit"]
        previous=text

        result=ai_prediction(rank,suit,previous)

        top=top_predictions(result)

        increment_daily_count(user_id)

        if not top:

            await update.message.reply_text("لا توجد بيانات كافية حالياً.")
            return

        msg="🔮 التوقع الأقرب:\n\n"

        for name,prob in top:

            msg+=f"{name} — {prob}%\n"

        await update.message.reply_text(msg)

        context.user_data.clear()

        kb=[["🔮 التخمين"],["🔙 رجوع"]]

        await update.message.reply_text(
            "يمكنك التخمين مرة أخرى:",
            reply_markup=ReplyKeyboardMarkup(kb,resize_keyboard=True)
        )

        return


    await update.message.reply_text("يرجى اختيار من الكيبورد أو الضغط على 🔙 رجوع")


# ==================== MAIN ====================

def main():

    threading.Thread(target=run_server,daemon=True).start()

    app=ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start",start))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,handle_message))

    print("✅ بوت تكساس ويبلاي جاهز")

    app.run_polling(drop_pending_updates=True)


if __name__=="__main__":
    main()
