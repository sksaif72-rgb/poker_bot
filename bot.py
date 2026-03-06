import os
import psycopg2
import random
import datetime
import pytz
from collections import Counter

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes


# ================================
# BOT TOKEN
# ================================

TOKEN = os.getenv("BOT_TOKEN")

# ================================
# DATABASE CONNECTION
# ================================

DATABASE_URL = os.getenv("DATABASE_URL")

conn = psycopg2.connect(DATABASE_URL)
cursor = conn.cursor()

# ================================
# TIMEZONE
# ================================

timezone = pytz.timezone("UTC")

def get_current_minute():
    now = datetime.datetime.now(timezone)
    return now.minute


# ================================
# MONTE CARLO
# ================================

winner_types = [
    "زوجين",
    "متتالية",
    "فل هاوس",
    "ثلاثة",
    "اربعة"
]

hand_types = [
    "متتالية نفس النوع",
    "زوج",
    "دبلAA",
    "لاشيء"
]


def monte_carlo_prediction():

    winner = random.choice(winner_types)
    hand = random.choice(hand_types)

    return winner, hand


# ================================
# DATABASE PREDICTION
# ================================

def database_prediction(rank, suit, previous_hit, minute):

    cursor.execute("""
        SELECT winner_type, hand_type
        FROM training_data
        WHERE rank=%s
        AND suit=%s
        AND previous_hit=%s
        AND minute=%s
    """, (rank, suit, previous_hit, minute))

    rows = cursor.fetchall()

    if not rows:
        return None, None

    winners = []
    hands = []

    for row in rows:
        winners.append(row[0])
        hands.extend(row[1])

    winner_counter = Counter(winners)
    hand_counter = Counter(hands)

    return winner_counter, hand_counter
