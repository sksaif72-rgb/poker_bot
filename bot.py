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


# ==================== AI (ذكي جداً) ====================
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


# ==================== START (ترحيب تجاري دافئ) ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [["👤 اشتراك"], ["🎓 مدرب"], ["🔙 رجوع"]]
    await update.message.reply_text(
        "مرحباً بك عزيزي العميل ♠️\n\n"
        "أهلاً وسهلاً في **بوت تخمين البوكر الاحترافي**.\n"
        "نحن هنا لنساعدك على اتخاذ قرارات أقوى في اللعب.\n\n"
        "اختر نوع الحساب لنبدأ معاً:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )


# ==================== MESSAGE HANDLER (كامل جداً) ====================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.message.from_user.id

    # ───── زر الرجوع في أي خطوة ─────
    if text in ["🔙 رجوع", "رجوع", "BACK"]:
        context.user_data.clear()
        await start(update, context)
        return

    # ───── اشتراك ─────
    if text in ["👤 اشتراك", "اشتراك"]:
        context.user_data["role"] = "user"
        await update.message.reply_text(
            "شكراً لثقتك عزيزي العميل ❤️\n\n"
            "يرجى إرسال كود الاشتراك الآن:"
        )
        return

    # ───── مدرب ─────
    if text in ["🎓 مدرب", "مدرب"]:
        context.user_data["role"] = "trainer"
        await update.message.reply_text(
            "مرحباً بك أستاذنا المدرب 👨‍🏫\n\n"
            "يرجى إرسال كود المدرب الآن:"
        )
        return

    # ───── تفعيل الكود ─────
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

            kb = [["🔮 التخمين"]] if role == "user" else [["🎯 تدريب"]]
            await update.message.reply_text(
                f"🎉 مبروك! تم تفعيل اشتراكك بنجاح لمدة {days} يوم.\n"
                "نحن سعداء بخدمتك ونتمنى لك تجربة ممتعة ومربحة.",
                reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True)
            )
            context.user_data.clear()
        else:
            await update.message.reply_text("عذراً، الكود غير صحيح ❌\nيرجى المحاولة مرة أخرى أو التواصل مع الدعم.")
        cursor.close()
        conn.close()
        return

    # ───── التدريب (للمدرب) ─────
    if text in ["🎯 تدريب", "تدريب"]:
        context.user_data.clear()
        context.user_data["flow"] = "training"
        context.user_data["training_step"] = "rank"
        kb = [["A","K","Q","J"], ["10","9","8","7"], ["6","5","4","3","2"], ["🔙 رجوع"]]
        await update.message.reply_text("اختر رقم الورقة عزيزي المدرب:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
        return

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
        await update.message.reply_text("ما الذي ضرب في هذه الجولة؟", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
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

            await update.message.reply_text("✅ تم حفظ التدريب بنجاح.\nشكراً لمساهمتك في تطوير البوت!")
            context.user_data.clear()
            kb = [["🎯 تدريب"], ["🔙 رجوع"]]
            await update.message.reply_text("اختر اللي تبغاه:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
            return
        else:
            context.user_data["hands"].append(text)
            await update.message.reply_text(f"تم إضافة: {text}")
            return

    # ───── التخمين (للمستخدم) ─────
    if text in ["🔮 التخمين", "تخمين"]:
        if not check_subscription(user_id):
            await update.message.reply_text("عذراً، اشتراكك منتهي.\nيرجى تجديد الاشتراك للاستمرار.")
            return
        if not check_daily_limit(user_id):
            await update.message.reply_text("عذراً، وصلت الحد اليومي (50 تخمين).\nحاول غداً أو اشترك في الباقة غير المحدودة.")
            return

        context.user_data.clear()
        context.user_data["flow"] = "predict"
        context.user_data["predict_step"] = "winner_type"
        kb = [["زوجين", "متتالية"], ["فل هاوس", "ثلاثة"], ["اربعة"], ["🔙 رجوع"]]
        await update.message.reply_text("اختر نوع الضربة المتوقعة عزيزي العميل:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
        return

    step = context.user_data.get("predict_step")

    if step == "winner_type":
        context.user_data["winner_type"] = text
        context.user_data["predict_step"] = "rank"
        kb = [["A","K","Q","J"], ["10","9","8","7"], ["6","5","4","3","2"], ["🔙 رجوع"]]
        await update.message.reply_text("اختر رقم الورقة:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
        return

    if step == "rank":
        context.user_data["rank"] = text
        context.user_data["predict_step"] = "suit"
        kb = [["♠️","♥️"], ["♦️","♣️"], ["🔙 رجوع"]]
        await update.message.reply_text("اختر نوع الورقة:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
        return

    if step == "suit":
        rank = context.user_data["rank"]
        suit = text
        prev = context.user_data.get("winner_type", "none")

        db_w, db_h, db_count = database_prediction(rank, suit, prev)
        mc_w, mc_h = monte_carlo_prediction()
        final_w, final_h = combine_predictions(db_w, db_h, mc_w, mc_h, db_count)

        winners = top_predictions(final_w)
        hands = top_predictions(final_h)

        msg = f"🔮 **نتيجة التخمين**\n\n**نوع الضربة:**\n"
        for w, p in winners: msg += f"• {w} : {p}%\n"
        msg += "\n**نوع اليد:**\n"
        for h, p in hands: msg += f"• {h} : {p}%\n"

        await update.message.reply_text(msg, parse_mode="Markdown")

        # التنبيه القوي التجاري
        warning = (
            "⚠️ **تحذير مهم جداً** ⚠️\n\n"
            "إذا أدخلت معلومة غير صحيحة الآن، قد يؤثر ذلك على دقة التخمينات المستقبلية.\n"
            "يرجى التأكد من الإجابة بكل صدق ودقة 100%.\n\n"
            "شنو كانت الضربة الحقيقية في هذه الجولة؟"
        )
        kb = [["زوجين","متتالية"], ["فل هاوس","ثلاثة"], ["اربعة"], ["🔙 رجوع"]]
        await update.message.reply_text(warning, reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
        context.user_data["predict_step"] = "real_winner"
        context.user_data["rank"] = rank
        context.user_data["suit"] = suit
        return

    if context.user_data.get("predict_step") == "real_winner":
        real_winner = text
        rank = context.user_data["rank"]
        suit = context.user_data["suit"]
        prev = context.user_data.get("winner_type", "none")

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

        await update.message.reply_text(
            "✅ شكراً لك عزيزي العميل!\n"
            "تم حفظ النتيجة الحقيقية بنجاح.\n"
            "البوت أصبح أذكى بفضلك ❤️"
        )
        context.user_data.clear()
        kb = [["🔮 التخمين"], ["🔙 رجوع"]]
        await update.message.reply_text("اختر اللي تبغاه:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
        return

    # إذا ما فهم أي شيء
    await update.message.reply_text("عذراً، لم أفهم طلبك.\nيرجى اختيار من الكيبورد أو الضغط على 🔙 رجوع")


# ==================== MAIN ====================
def main():
    threading.Thread(target=run_server, daemon=True).start()
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("✅ بوت تخمين البوكر الاحترافي جاهز للخدمة")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
