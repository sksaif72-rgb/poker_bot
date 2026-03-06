import os
import random
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


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'Poker bot running')


def run_server():
    port=int(os.environ.get("PORT",10000))
    server=HTTPServer(("0.0.0.0",port),Handler)
    server.serve_forever()


# ================= SUBSCRIPTION =================

def get_subscription_days(user_id):

    conn=get_conn()
    cur=conn.cursor()

    cur.execute("SELECT expire_date FROM users WHERE telegram_id=%s",(user_id,))
    data=cur.fetchone()

    cur.close()
    conn.close()

    if not data:
        return 0

    days=(data[0].date()-datetime.date.today()).days
    return max(days,0)


def check_subscription(user_id):
    return get_subscription_days(user_id)>0


# ================= LOAD DATA =================

def load_recent(limit=800):

    conn=get_conn()
    cur=conn.cursor()

    cur.execute("""
    SELECT card_rank,card_suit,previous_winner_type,winner_type,minute
    FROM training_data
    ORDER BY id DESC
    LIMIT %s
    """,(limit,))

    rows=cur.fetchall()

    cur.close()
    conn.close()

    return rows


# ================= AI =================

def pattern_ai(rank,suit,previous,minute):

    data=load_recent()

    c=Counter()

    for r in data:
        if r[0]==rank and r[1]==suit and r[2]==previous and r[4]==minute:
            c[r[3]]+=1

    return c


def frequency_ai(rank,suit):

    data=load_recent()

    c=Counter()

    for r in data:
        if r[0]==rank and r[1]==suit:
            c[r[3]]+=1

    return c


def minute_ai(minute):

    data=load_recent()

    c=Counter()

    for r in data:
        if r[4]==minute:
            c[r[3]]+=1

    return c


def recency_ai(rank,suit,previous,minute):

    data=load_recent()

    c=Counter()

    weight=len(data)

    for r in data:

        if r[0]==rank and r[1]==suit and r[2]==previous and r[4]==minute:
            c[r[3]]+=weight

        weight-=1

    return c


def markov_ai(previous):

    data=load_recent()

    c=Counter()

    for r in data:
        if r[2]==previous:
            c[r[3]]+=1

    return c


def combine_ai(rank,suit,previous,minute):

    p=pattern_ai(rank,suit,previous,minute)
    f=frequency_ai(rank,suit)
    r=recency_ai(rank,suit,previous,minute)
    m=markov_ai(previous)
    t=minute_ai(minute)

    final=Counter()

    for k,v in p.items():
        final[k]+=v*4

    for k,v in f.items():
        final[k]+=v*2

    for k,v in r.items():
        final[k]+=v*4

    for k,v in m.items():
        final[k]+=v*2

    for k,v in t.items():
        final[k]+=v*3

    return final


def top3(counter):

    total=sum(counter.values())

    if total==0:
        return []

    res=[]

    for k,v in counter.most_common(3):
        res.append((k,round(v/total*100,2)))

    return res


# ================= SAVE =================

def save_round(rank,suit,previous,winner):

    minute=datetime.datetime.now().minute

    conn=get_conn()
    cur=conn.cursor()

    cur.execute("""
    INSERT INTO training_data
    (card_rank,card_suit,previous_winner_type,winner_type,minute)
    VALUES (%s,%s,%s,%s,%s)
    """,(rank,suit,previous,winner,minute))

    conn.commit()

    cur.close()
    conn.close()


# ================= START =================

async def start(update:Update,context:ContextTypes.DEFAULT_TYPE):

    kb=[["👤 اشتراك"],["🎓 مدرب"]]

    await update.message.reply_text(
        "اهلا وسهلا بوت تكساس ويبلاي ♠️",
        reply_markup=ReplyKeyboardMarkup(kb,resize_keyboard=True)
    )


# ================= HANDLER =================

async def handle(update:Update,context:ContextTypes.DEFAULT_TYPE):

    text=update.message.text.strip()
    user_id=update.message.from_user.id


    if text=="🔮 التخمين":

        if not check_subscription(user_id):
            await update.message.reply_text("انتهى الاشتراك")
            return

        minute=datetime.datetime.now().minute

        context.user_data["minute"]=minute
        context.user_data["step"]="previous"

        kb=[["زوجين","متتالية"],["ثلاثة","فل هاوس"],["اربعة"]]

        await update.message.reply_text(
            f"الدقيقة الحالية {minute}\nما آخر ضربة؟",
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

        result=combine_ai(rank,suit,previous,minute)

        top=top3(result)

        if not top:
            await update.message.reply_text("لا توجد بيانات كافية")
            return

        labels=["🔥 افضل تخمين","⚖️ تخمين وسط","⚠️ تخمين ضعيف"]

        msg="🔮 تحليل الطاولة\n\n"

        for i,(name,prob) in enumerate(top):
            msg+=f"{labels[i]}\n{name} — {prob}%\n\n"

        await update.message.reply_text(msg)

        context.user_data["step"]="result"

        kb=[["زوجين","متتالية"],["ثلاثة","فل هاوس"],["اربعة"]]

        await update.message.reply_text(
            "⚠️ اختر الضربة الصحيحة",
            reply_markup=ReplyKeyboardMarkup(kb,resize_keyboard=True)
        )

        return


    if context.user_data.get("step")=="result":

        rank=context.user_data["rank"]
        suit=context.user_data["suit"]
        previous=context.user_data["previous"]

        winner=text

        save_round(rank,suit,previous,winner)

        context.user_data["previous"]=winner
        context.user_data["step"]="rank"

        kb=[["A","K","Q","J"],["10","9","8","7"],["6","5","4","3","2"]]

        await update.message.reply_text(
            "الجولة التالية اختر رقم الورقة",
            reply_markup=ReplyKeyboardMarkup(kb,resize_keyboard=True)
        )


# ================= MAIN =================

def main():

    threading.Thread(target=run_server,daemon=True).start()

    app=ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start",start))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,handle))

    print("Bot Running")

    app.run_polling(drop_pending_updates=True)


if __name__=="__main__":
    main()
