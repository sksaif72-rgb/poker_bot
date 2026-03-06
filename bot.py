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
# ================================
# COMBINE AI (80% DB + 20% MONTE CARLO)
# ================================

def combine_predictions(rank, suit, previous_hit, minute):

    db_winner, db_hand = database_prediction(rank, suit, previous_hit, minute)

    monte_winner, monte_hand = monte_carlo_prediction()

    winner_scores = Counter()
    hand_scores = Counter()

    # 80% Database
    if db_winner:
        for k, v in db_winner.items():
            winner_scores[k] += v * 0.8

    if db_hand:
        for k, v in db_hand.items():
            hand_scores[k] += v * 0.8

    # 20% Monte Carlo
    winner_scores[monte_winner] += 0.2
    hand_scores[monte_hand] += 0.2

    return winner_scores, hand_scores


# ================================
# TOP 2 PREDICTIONS
# ================================

def top_predictions(counter):

    if not counter:
        return []

    total = sum(counter.values())

    results = []

    for k, v in counter.most_common(2):

        percent = round((v / total) * 100)

        results.append((k, percent))

    return results
