import os
import datetime
import psycopg2
import threading

from http.server import HTTPServer, BaseHTTPRequestHandler
from collections import Counter

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters


TOKEN=os.getenv("BOT_TOKEN")
DATABASE_URL=os.getenv("DATABASE_URL")


# ================= DATABASE =================

def get_conn():
    return psycopg2.connect(DATABASE_URL,sslmode="require")


# ================= SERVER =================

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'Bot running')


def run_server():
    port=int(os.environ.get("PORT",10000))
    server=HTTPServer(("0.0.0.0",port),Handler)
    server.serve_forever()


# ================= SUBSCRIPTION =================

def activate_user(user_id,days):

    conn=get_conn()
    cur=conn.cursor()

    expire=datetime.datetime.now()+datetime.timedelta(days=days)

    cur.execute("""
    INSERT INTO users (telegram_id,expire_date)
    VALUES (%s,%s)
    ON CONFLICT (telegram_id)
    DO UPDATE SET expire_date=%s
    """,(user_id,expire,expire))

    conn.commit()
    cur.close()
    conn.close()


def check_user_code(code):

    conn=get_conn()
    cur=conn.cursor()

    cur.execute("SELECT days FROM user_codes WHERE code=%s",(code,))
    r=cur.fetchone()

    cur.close()
    conn.close()

    return r


# ================= AI =================

def load_data():

    conn=get_conn()
    cur=conn.cursor()

    cur.execute("""
    SELECT card_rank,card_suit,previous_winner_type,winner_type,minute
    FROM training_data
    ORDER BY id DESC
    LIMIT 500
    """)

    rows=cur.fetchall()

    cur.close()
    conn.close()

    return rows


def predict(rank,suit,previous,minute):

    data=load_data()

    c=Counter()

    for r in data:

        score=0

        if r[0]==rank and r[1]==suit:
            score+=2

        if r[2]==previous:
            score+=2

        if r[4]==minute:
            score+=3

        if score>0:
            c[r[3]]+=score

    return c


# ================= START =================

async def start(update:Update,context:ContextTypes.DEFAULT_TYPE):

    kb=[
        ["👤 اشتراك"],
        ["🎓 مدرب"]
    ]

    await update.message.reply_text(
        "اهلا وسهلا بوت تكساس ♠️",
        reply_markup=ReplyKeyboardMarkup(kb,resize_keyboard=True)
    )


# ================= HANDLER =================

async def handle(update:Update,context:ContextTypes.DEFAULT_TYPE):

    text=update.message.text
    user_id=update.message.from_user.id


# -------- اشتراك --------

    if text=="👤 اشتراك":

        context.user_data["step"]="user_code"

        await update.message.reply_text("ادخل كود الاشتراك")

        return


    if context.user_data.get("step")=="user_code":

        code=text

        r=check_user_code(code)

        if not r:

            await update.message.reply_text("الكود غير صحيح")
            return

        activate_user(user_id,r[0])

        kb=[["🔮 التخمين"]]

        await update.message.reply_text(
            "تم تفعيل الاشتراك",
            reply_markup=ReplyKeyboardMarkup(kb,resize_keyboard=True)
        )

        context.user_data["step"]=None

        return


# -------- التخمين --------

    if text=="🔮 التخمين":

        minute=datetime.datetime.now().minute

        context.user_data["minute"]=minute
        context.user_data["step"]="previous"

        kb=[["زوجين","متتالية"],["ثلاثة","فل هاوس"],["اربعة"]]

        await update.message.reply_text(
            f"الدقيقة {minute}\nما اخر ضربة؟",
            reply_markup=ReplyKeyboardMarkup(kb,resize_keyboard=True)
        )

        return


    if context.user_data.get("step")=="previous":

        context.user_data["previous"]=text
        context.user_data["step"]="rank"

        kb=[["A","K","Q","J"],["10","9","8","7"],["6","5","4","3","2"]]

        await update.message.reply_text(
            "اختر رقم الورقة",
            reply_markup=ReplyKeyboardMarkup(kb,resize_keyboard=True)
        )

        return


    if context.user_data.get("step")=="rank":

        context.user_data["rank"]=text
        context.user_data["step"]="suit"

        kb=[["♠️","♥️"],["♦️","♣️"]]

        await update.message.reply_text(
            "اختر نوع الورقة",
            reply_markup=ReplyKeyboardMarkup(kb,resize_keyboard=True)
        )

        return


    if context.user_data.get("step")=="suit":

        rank=context.user_data["rank"]
        suit=text
        previous=context.user_data["previous"]
        minute=context.user_data["minute"]

        context.user_data["suit"]=suit

        result=predict(rank,suit,previous,minute)

        total=sum(result.values())

        if total==0:

            await update.message.reply_text("لا يوجد بيانات كافية")
            return

        msg="🔮 التحليل\n\n"

        labels=["🔥 افضل تخمين","⚖️ تخمين وسط","⚠️ تخمين ضعيف"]

        for i,(k,v) in enumerate(result.most_common(3)):

            p=round(v/total*100,2)

            msg+=f"{labels[i]}\n{k} — {p}%\n\n"

        await update.message.reply_text(msg)

        context.user_data["step"]="result"

        kb=[["زوجين","متتالية"],["ثلاثة","فل هاوس"],["اربعة"]]

        await update.message.reply_text(
            "اختر الضربة الصحيحة",
            reply_markup=ReplyKeyboardMarkup(kb,resize_keyboard=True)
        )

        return


# ================= MAIN =================

def main():

    threading.Thread(target=run_server,daemon=True).start()

    app=ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start",start))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,handle))

    print("Bot Running")

    app.run_polling()


if __name__=="__main__":
    main()
