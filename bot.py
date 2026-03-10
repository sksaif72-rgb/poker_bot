import logging import psycopg2 import json from datetime import
datetime, timedelta, timezone import os from collections import Counter,
defaultdict from flask import Flask from threading import Thread

from telegram import ( Update, InlineKeyboardButton,
InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton )

from telegram.ext import ( ApplicationBuilder, CommandHandler,
ContextTypes, CallbackQueryHandler, MessageHandler, filters )

───────────────────────── CONFIG ─────────────────────────

TOKEN = os.getenv(“BOT_TOKEN”) DATABASE_URL = os.getenv(“DATABASE_URL”)
ADMIN_ID = int(os.getenv(“ADMIN_ID”, “0”))

───────────────────────── WEB SERVER ─────────────────────

app_web = Flask(name)

@app_web.route(“/”) def home(): return “Bot is alive”

def run_web(): port = int(os.environ.get(“PORT”, 10000))
app_web.run(host=“0.0.0.0”, port=port)

def keep_alive(): Thread(target=run_web, daemon=True).start()

───────────────────────── LOGGING ────────────────────────

logging.basicConfig( format=“%(asctime)s - %(name)s - %(levelname)s -
%(message)s”, level=logging.INFO ) logger = logging.getLogger(name)

───────────────────────── DATABASE ───────────────────────

try: conn = psycopg2.connect(DATABASE_URL, sslmode=“require”) except
Exception as e: logger.error(f”Database connection error: {e}“) conn =
None

def db_execute(query, params=None, fetchone=False, commit=False): if
conn is None: return None if fetchone else [] try: with conn.cursor() as
cur: cur.execute(query, params or ()) if fetchone: return cur.fetchone()
if commit: conn.commit() return True return cur.fetchall() except
Exception as e: logger.error(f”DB error: {e}“) if commit and conn:
conn.rollback() return None if fetchone else []

───────────────────────── GAME ITEMS ─────────────────────

ITEMS = [“🍎”,“🍊”,“🥬”,“🍉”,“🐟”,“🍔”,“🍤”,“🍗”] FRUITS =
[“🍎”,“🍊”,“🥬”,“🍉”] MEATS = [“🐟”,“🍔”,“🍤”,“🍗”] ALL_ITEMS_SET =
set(ITEMS)

───────────────────────── USERS ──────────────────────────

def create_user(telegram_id): db_execute( “INSERT INTO users
(telegram_id) VALUES (%s) ON CONFLICT DO NOTHING”, (telegram_id,),
commit=True )

def get_user(telegram_id): return db_execute( “SELECT role,
subscription_end FROM users WHERE telegram_id=%s”, (telegram_id,),
fetchone=True )

def check_subscription(telegram_id): row = db_execute( “SELECT
subscription_end FROM users WHERE telegram_id=%s”, (telegram_id,),
fetchone=True ) return row and row[0] and row[0] >
datetime.now(timezone.utc)

def get_remaining_time(telegram_id): row = db_execute( “SELECT
subscription_end FROM users WHERE telegram_id=%s”, (telegram_id,),
fetchone=True ) if not row or not row[0] or row[0] <=
datetime.now(timezone.utc): return “❌ منتهي” delta = row[0] -
datetime.now(timezone.utc) return f”✅ متبقي {delta.days} يوم”

───────────────────────── CODES ──────────────────────────

def activate_code(telegram_id, code):

    data = db_execute(
        "SELECT days,used,max_use FROM codes WHERE code=%s",
        (code,), fetchone=True
    )

    if not data:
        return False,"❌ الكود غير موجود"

    days,used,max_use = data

    if used >= max_use:
        return False,"❌ الكود مستنفد"

    end = datetime.now(timezone.utc) + timedelta(days=days)

    db_execute(
        "UPDATE users SET subscription_end=%s WHERE telegram_id=%s",
        (end,telegram_id),commit=True
    )

    db_execute(
        "UPDATE codes SET used=used+1 WHERE code=%s",
        (code,),commit=True
    )

    return True,f"✅ تم التفعيل لمدة {days} يوم"

───────────────────────── KEYBOARD ───────────────────────

def main_keyboard(): return ReplyKeyboardMarkup( [ [KeyboardButton(“🎯
توقع الجولة”)], [KeyboardButton(“👤 حسابي”)], [KeyboardButton(“🎟 تفعيل
كود”)], [KeyboardButton(“📊 إحصائيات”)] ], resize_keyboard=True )

def build_result_keyboard(): keyboard=[] row=[] for item in ITEMS:
row.append(InlineKeyboardButton(item,callback_data=f”result_{item}“)) if
len(row)==4: keyboard.append(row) row=[] if row: keyboard.append(row)

    keyboard.append(
        [InlineKeyboardButton("🏠 القائمة",callback_data="back_to_main")]
    )

    return InlineKeyboardMarkup(keyboard)

───────────────────────── AI ENGINE ──────────────────────

prediction_cache={}

def predict_sequence(sequence):

    if len(sequence)==0:
        return FRUITS[:3]+MEATS[:2],[25,25,20,15,15]

    cache_key=tuple(sequence[-8:])
    if cache_key in prediction_cache:
        return prediction_cache[cache_key]

    rows=db_execute(
        "SELECT sequence,next_hit FROM training_data ORDER BY id DESC LIMIT 10000"
    ) or []

    scores={item:0 for item in ITEMS}

    global_count=Counter()
    for _,n in rows:
        global_count[n]+=1

    total=sum(global_count.values()) or 1

    for item in ITEMS:
        scores[item]+=global_count.get(item,0)/total*50

    if sequence:
        last=sequence[-1]

        after_last=Counter()

        for seq_json,next_hit in rows:
            try:
                seq=json.loads(seq_json)
                if seq and seq[-1]==last:
                    after_last[next_hit]+=1
            except:
                pass

        tot=sum(after_last.values())+len(ITEMS)

        for item in ITEMS:
            scores[item]+=((after_last.get(item,0)+1)/tot)*200

    sorted_fruits=sorted(FRUITS,key=lambda x:scores[x],reverse=True)[:3]
    sorted_meats=sorted(MEATS,key=lambda x:scores[x],reverse=True)[:2]

    selected=sorted_fruits+sorted_meats

    raw=[scores[x] for x in selected]
    s=sum(raw) or 1

    perc=[round(i/s*100) for i in raw]

    prediction_cache[cache_key]=(selected,perc)

    return selected,perc

───────────────────────── HANDLERS ───────────────────────

async def start(update:Update,context:ContextTypes.DEFAULT_TYPE):

    user_id=update.effective_user.id
    create_user(user_id)

    await update.message.reply_text(
        f"🤖 Cowboy Prediction Bot\n\nاشتراكك: {get_remaining_time(user_id)}",
        reply_markup=main_keyboard()
    )

async def show_profile(update:Update,context:ContextTypes.DEFAULT_TYPE):

    user_id=update.effective_user.id
    user=get_user(user_id)

    role=user[0] if user else "مستخدم"

    text=f"""

👤 حسابك

ID: {user_id} الرتبة: {role} الاشتراك: {get_remaining_time(user_id)} ““”

    await update.message.reply_text(text)

async def ask_code(update:Update,context:ContextTypes.DEFAULT_TYPE):

    context.user_data["wait_code"]=True

    await update.message.reply_text("🎟 أرسل كود التفعيل")

async def
show_statistics(update:Update,context:ContextTypes.DEFAULT_TYPE):

    rows=db_execute("SELECT next_hit FROM training_data") or []

    counter=Counter([r[0] for r in rows])

    text="📊 الإحصائيات\n\n"

    total=sum(counter.values())

    for item in ITEMS:
        c=counter.get(item,0)
        p=round(c/total*100) if total else 0
        text+=f"{item} : {p}%\n"

    await update.message.reply_text(text)

async def handle_text(update:Update,context:ContextTypes.DEFAULT_TYPE):

    user_id=update.effective_user.id
    text=update.message.text.strip()

    if context.user_data.get("wait_code"):

        context.user_data["wait_code"]=False

        success,msg=activate_code(user_id,text)

        await update.message.reply_text(msg)
        return

    sequence=text.split()

    if not set(sequence).issubset(ALL_ITEMS_SET):

        await update.message.reply_text(
            "❌ أرسل رموز اللعبة فقط\nمثال:\n🍎 🍊 🥬 🍉 🐟 🍔"
        )
        return

    pred,perc=predict_sequence(sequence)

    msg="🤖 التوقع\n\n"

    for i,p in zip(pred,perc):
        msg+=f"{i} {p}%\n"

    await update.message.reply_text(
        msg,
        reply_markup=build_result_keyboard()
    )

async def back_to_main(update:Update,context:ContextTypes.DEFAULT_TYPE):

    query=update.callback_query
    await query.answer()

    await query.message.reply_text(
        "🏠 القائمة الرئيسية",
        reply_markup=main_keyboard()
    )

───────────────────────── MAIN ───────────────────────────

def main():

    keep_alive()

    app=ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start",start))

    app.add_handler(MessageHandler(filters.Regex("^(🎯 توقع الجولة)$"),handle_text))
    app.add_handler(MessageHandler(filters.Regex("^(👤 حسابي)$"),show_profile))
    app.add_handler(MessageHandler(filters.Regex("^(🎟 تفعيل كود)$"),ask_code))
    app.add_handler(MessageHandler(filters.Regex("^(📊 إحصائيات)$"),show_statistics))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,handle_text))

    app.add_handler(CallbackQueryHandler(back_to_main,pattern="back_to_main"))

    print("Bot Started")

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if name==“main”: main()
