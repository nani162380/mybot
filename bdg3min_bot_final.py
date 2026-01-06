# bdg3min_final_bot_v5.py
# BDG 3-min Telegram bot — V5 Market-Mode + Learning phase + Channel Manager
# Requirements: pip install pyTelegramBotAPI requests

import time
import threading
import logging
import sqlite3
import datetime
import requests
import telebot
import collections
import random

# ---------------- CONFIG ----------------
BOT_TOKEN = "8595314247:AAG_WqyrlWX0VxRljU2zLnVwJo1DR-uZCgs"   # put your token here
OWNER_ID = 5698239751                                         # your Telegram numeric id
BDG_HISTORY_API = "https://draw.ar-lottery01.com/WinGo/WinGo_3M/GetHistoryIssuePage.json"
PAGE_SIZE = 12
RESULT_STABLE_WAIT = 5
DB_FILE = "bdg3min_final_bot_v5.db"
REQUEST_TIMEOUT = 8

# GIFs (win / loss)
WIN_GIF = "https://media3.giphy.com/media/v1.Y2lkPTc5MGI3NjExdWQwaWl1cWJoZzR6MHR2cGx2dWdvOGFkNnlxYWs3dTUwZ21iNHJpbyZlcD12MV9pbnRlcm5hbF9naWZfYnlfaWQmY3Q9Zw/CFaGnXWf6GABHKKZcC/giphy.gif"
LOSS_GIF = "https://media0.giphy.com/media/v1.Y2lkPTc5MGI3NjExNzY4NXI5eWlrbWhmcnlrbXhnajZ0amZhZnJkbnJycnM3dHk5NWF5cSZlcD12MV9pbnRlcm5hbF9naWZfYnlfaWQmY3Q9Zw/3otPoU74ilAX3nyRPi/giphy.gif"

# martingale levels (amounts)
LEVEL_AMOUNTS = {1: 10, 2: 30, 3: 70, 4: 210, 5: 490}
FIXED_AMOUNT_AFTER_6 = 1470

LEARNING_RESULTS_N = 10
MAX_MEMORY = 80

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s: %(message)s")
logger = logging.getLogger("bdg3min_final_v5")

bot = telebot.TeleBot(BOT_TOKEN)

# ---------------- DB helpers ----------------
def db_exec(query, params=(), fetch=False):
    con = sqlite3.connect(DB_FILE, timeout=20)
    cur = con.cursor()
    try:
        cur.execute(query, params)
        if fetch:
            return cur.fetchall()
        con.commit()
    finally:
        cur.close()
        con.close()

# init tables
db_exec("""
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period TEXT,
    predicted TEXT,
    amount INTEGER,
    level INTEGER,
    actual TEXT,
    win INTEGER,
    ts DATETIME DEFAULT CURRENT_TIMESTAMP
)
""")
db_exec("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")

def set_meta(k, v):
    db_exec("INSERT OR REPLACE INTO meta(k,v) VALUES (?,?)", (k, str(v)))

def get_meta(k, default=None):
    r = db_exec("SELECT v FROM meta WHERE k=?", (k,), fetch=True)
    return r[0][0] if r else default

# ---------------- utilities ----------------
def amount_for_level(lvl):
    if lvl <= 0:
        return LEVEL_AMOUNTS[1]
    if lvl <= 5:
        return LEVEL_AMOUNTS.get(lvl, LEVEL_AMOUNTS[5])
    return FIXED_AMOUNT_AFTER_6

# ---------------- state ----------------
state = {
    "running": False,
    "channels": set((get_meta("channels") or "").split(",")) if get_meta("channels") else set(),
    "level": int(get_meta("level", "1")),
    "amount": int(get_meta("amount", str(LEVEL_AMOUNTS[1]))),
    "last_period": get_meta("last_period", None),
    "learning": get_meta("learning", "1") == "1",
    "collected": int(get_meta("collected", "0") or 0)
}

previous_results = collections.deque(maxlen=MAX_MEMORY)  # store recent actuals as "BIG"/"SMALL"

def persist_state():
    set_meta("channels", ",".join([str(x) for x in state["channels"]]))
    set_meta("level", state["level"])
    set_meta("amount", state["amount"])
    set_meta("last_period", state["last_period"])
    set_meta("learning", "1" if state.get("learning") else "0")
    set_meta("collected", state.get("collected", 0))

# ---------------- messaging helpers ----------------
def send_owner(text):
    try:
        bot.send_message(OWNER_ID, text)
    except Exception:
        logger.debug("Failed to send owner text")

def send_clients(text):
    # send only prediction text to channels
    for ch in list(state["channels"]):
        try:
            # channel id may be stored as string, bot accepts str or int
            bot.send_message(ch, text)
        except Exception:
            logger.debug("Failed to send text to channel %s", ch)

def send_clients_gif(url):
    for ch in list(state["channels"]):
        try:
            bot.send_animation(ch, url)
        except Exception:
            logger.debug("Failed to send gif to channel %s", ch)

# ---------------- API fetch ----------------
def fetch_history_from_api(page_no=1):
    try:
        params = {"ts": str(int(time.time()*1000)), "pageNo": page_no, "pageSize": PAGE_SIZE}
        r = requests.get(BDG_HISTORY_API, params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        j = r.json()
        if isinstance(j, dict) and j.get("data") and isinstance(j["data"].get("list"), list):
            return j["data"]["list"]
        if isinstance(j, dict) and isinstance(j.get("list"), list):
            return j["list"]
        return None
    except Exception as e:
        logger.debug("fetch_history error: %s", e)
        return None

# ---------------- market-mode logic (V5) ----------------
def detect_mode(recent):
    if not recent:
        return "UNKNOWN"
    last8 = recent[-8:]
    last6 = recent[-6:]
    # trap detection
    p = ",".join(last8)
    if "BIG,BIG,BIG,SMALL,BIG,SMALL" in p or "SMALL,SMALL,SMALL,BIG,SMALL,BIG" in p:
        return "TRAP"
    # strong trend
    if last8.count("BIG") >= 5:
        return "TREND_BIG"
    if last8.count("SMALL") >= 5:
        return "TREND_SMALL"
    # ranging/zig-zag
    if len(last6) >= 4:
        alt = True
        for i in range(1, len(last6)):
            if last6[i] == last6[i-1]:
                alt = False
                break
        if alt:
            return "RANGING"
    # breakout
    if len(last8) >= 5:
        if last8[-5:-1].count(last8[-5]) >= 3 and last8[-1] != last8[-2]:
            return "BREAKOUT"
    return "UNKNOWN"

def decide_prediction(recent):
    mode = detect_mode(recent)
    # send mode only to owner (not to channels)
    send_owner(f"🧠 Detected mode: {mode} (memory {len(recent)})")
    if mode == "TRAP":
        if not recent:
            return random.choice(["BIG", "SMALL"])
        return "SMALL" if recent[-1] == "BIG" else "BIG"
    if mode == "TREND_BIG":
        return "BIG"
    if mode == "TREND_SMALL":
        return "SMALL"
    if mode == "RANGING":
        return "SMALL" if recent[-1] == "BIG" else "BIG"
    if mode == "BREAKOUT":
        return recent[-1]
    # UNKNOWN -> weighted last 6
    seq = recent[-6:]
    if not seq:
        return random.choice(["BIG", "SMALL"])
    weights = [1,1,2,2,3,4][-len(seq):]
    score = 0
    for i, val in enumerate(seq):
        weight = weights[i]
        score += weight if val == "BIG" else -weight
    return "BIG" if score > 0 else "SMALL"

# ---------------- time helper ----------------
def next_3min_tick(now=None):
    if now is None:
        now = datetime.datetime.now()
    minute = now.minute
    second = now.second
    if minute % 3 == 0 and second < 2:
        next_min = minute
    else:
        next_min = minute + (3 - (minute % 3)) if (minute % 3) != 0 else minute + 3
    target = (now + datetime.timedelta(minutes=(next_min - minute))).replace(second=0, microsecond=0)
    if target <= now:
        target = target + datetime.timedelta(minutes=3)
    return target

# ---------------- core loop ----------------
def prediction_loop():
    logger.info("prediction loop started (API-based V5)")
    # initial read to know last_period and to prefill memory
    rows = fetch_history_from_api()
    if rows and len(rows) > 0:
        state["last_period"] = rows[0].get("issueNumber")
        # fill previous_results with recent history (older -> newer)
        for r in rows[::-1]:
            try:
                num = r.get("number")
                bs = "BIG" if int(num) > 4 else "SMALL"
            except:
                bs = "SMALL"
            previous_results.append(bs)
        persist_state()
        send_owner(f"🔁 Initialized last_period={state['last_period']}, primed memory={len(previous_results)}")
    else:
        send_owner("⚠️ No initial rows found; will sync to tick")

    # learning mode notify owner only
    if state.get("learning"):
        send_owner(f"⚠️ Learning mode ON: collecting {LEARNING_RESULTS_N} real results first (~{LEARNING_RESULTS_N*3} minutes).")

    while state["running"]:
        try:
            now = datetime.datetime.now()
            target = next_3min_tick(now)
            wait_seconds = (target - now).total_seconds()
            logger.info("Next tick at %s (in %.1f s)", target.strftime("%H:%M:%S"), wait_seconds)
            # sleep interruptible
            remaining = wait_seconds
            while remaining > 0 and state["running"]:
                time.sleep(min(1.0, remaining))
                remaining -= 1.0
            if not state["running"]:
                break

            # wait a bit then fetch
            time.sleep(RESULT_STABLE_WAIT)
            rows = fetch_history_from_api()
            if not rows or len(rows) == 0:
                # quick retries
                found = False
                for _ in range(6):
                    time.sleep(1)
                    rows = fetch_history_from_api()
                    if rows and len(rows) > 0:
                        found = True
                        break
                if not found:
                    send_owner("⚠️ No rows after tick; skipping this tick")
                    continue

            latest = rows[0]
            actual_period = latest.get("issueNumber")
            actual_num = latest.get("number")
            try:
                actual_bs = "BIG" if int(actual_num) > 4 else "SMALL"
            except:
                actual_bs = "SMALL"
            logger.info("Read latest: period=%s num=%s bs=%s", actual_period, actual_num, actual_bs)

            # initialization if missing
            if not state["last_period"]:
                state["last_period"] = actual_period
                previous_results.append(actual_bs)
                state["collected"] = min(LEARNING_RESULTS_N, state.get("collected", 0) + 1)
                persist_state()
                send_owner(f"Initialized last_period to {state['last_period']}")
                continue

            # skip duplicate period
            if actual_period == state["last_period"]:
                logger.info("same period as last (%s) — nothing new", actual_period)
                continue

            # push actual into memory
            previous_results.append(actual_bs)

            # increment learning counter if in learning mode
            if state.get("learning"):
                state["collected"] = min(LEARNING_RESULTS_N, state.get("collected", 0) + 1)
                persist_state()
                send_owner(f"📥 Learning collected: {state['collected']}/{LEARNING_RESULTS_N}")

            # Resolve pending prediction if exists
            pending_rows = db_exec("SELECT id, period, predicted, amount, level FROM history WHERE win IS NULL ORDER BY id DESC LIMIT 1", fetch=True)
            pending = pending_rows[0] if pending_rows else None
            if pending:
                pid, pred_period, pred_val, amt, lvl = pending
                win_flag = 1 if pred_val.upper() == actual_bs else 0
                db_exec("UPDATE history SET actual=?, win=? WHERE id=?", (actual_bs, win_flag, pid))

                # Send GIF only to clients (no extra text)
                gif = WIN_GIF if win_flag == 1 else LOSS_GIF
                send_clients_gif(gif)
                send_owner(f"Result for period {pred_period}: actual={actual_bs}, predicted={pred_val}, win={win_flag}")

                # update martingale
                if win_flag == 1:
                    state["level"] = 1
                    state["amount"] = amount_for_level(1)
                else:
                    state["level"] = state.get("level", 1) + 1
                    state["amount"] = amount_for_level(state["level"])
                persist_state()

            # update last period
            state["last_period"] = actual_period
            persist_state()

            # if still learning, do not send next prediction to clients until learning done
            if state.get("learning") and state.get("collected", 0) < LEARNING_RESULTS_N:
                logger.info("Learning phase: collected %d/%d", state.get("collected",0), LEARNING_RESULTS_N)
                continue

            # when learning finishes
            if state.get("learning") and state.get("collected", 0) >= LEARNING_RESULTS_N:
                state["learning"] = False
                persist_state()
                send_owner(f"✅ Learning complete ({LEARNING_RESULTS_N}). AI predictions activated.")

            # prepare next prediction
            try:
                next_period = str(int(actual_period) + 1)
            except:
                next_period = actual_period + "1"

            pred_val = decide_prediction(list(previous_results))

            # send prediction -> only to clients (and optionally to owner for logging)
            pred_msg = f"🔮 Prediction\nPeriod: {next_period}\n➡️ {pred_val}\nAmount: {state['amount']} | Level: {state['level']}"
            send_clients(pred_msg)       # clients see only predictions and GIFs
            send_owner(f"Sent prediction -> {pred_msg}")  # owner sees everything

            # record pending prediction
            db_exec("INSERT INTO history(period,predicted,amount,level,actual,win) VALUES (?,?,?,?,?,?)",
                    (next_period, pred_val, state["amount"], state["level"], None, None))

        except Exception as e:
            logger.exception("prediction_loop exception: %s", e)
            send_owner(f"🔥 Prediction loop error: {e}")
            time.sleep(3)

    logger.info("prediction loop stopped")
    send_owner("⛔ Prediction loop stopped")

# ---------------- owner-only decorator ----------------
def owner_only(func):
    def wrapper(message):
        try:
            uid = message.from_user.id
        except Exception:
            bot.reply_to(message, "Cannot verify user.")
            return
        if uid != OWNER_ID:
            bot.reply_to(message, "❌ You are not authorized.")
            return
        return func(message)
    return wrapper

# ---------------- Channel commands ----------------
@bot.message_handler(commands=['channels'])
@owner_only
def cmd_channels(message):
    if not state["channels"]:
        bot.reply_to(message, "No channels added yet.")
        return
    out = "📢 Connected Channels:\n"
    for ch in state["channels"]:
        out += f"- {ch}\n"
    bot.reply_to(message, out)

@bot.message_handler(commands=['add'])
@owner_only
def cmd_add(message):
    # Forward a message from channel -> then reply to it with /add
    if not message.reply_to_message:
        bot.reply_to(message, "Reply to a forwarded message from the channel you want to add.")
        return
    try:
        ch = message.reply_to_message.forward_from_chat
        if not ch:
            bot.reply_to(message, "Could not detect channel info. Make sure you forwarded a message directly from the channel.")
            return
        ch_id = ch.id
        state["channels"].add(str(ch_id))
        persist_state()
        bot.reply_to(message, f"✅ Added channel: {ch_id}")
        send_owner(f"➕ Channel added: {ch_id}")
    except Exception as e:
        logger.exception("add error: %s", e)
        bot.reply_to(message, "❌ Failed to add channel. Forward a channel message and reply /add.")

@bot.message_handler(commands=['remove'])
@owner_only
def cmd_remove(message):
    # Reply to a forwarded message from the channel to remove it
    if not message.reply_to_message:
        bot.reply_to(message, "Reply to a forwarded message from the channel you want to remove.")
        return
    try:
        ch = message.reply_to_message.forward_from_chat
        if not ch:
            bot.reply_to(message, "Could not detect channel info. Make sure you forwarded a message directly from the channel.")
            return
        ch_id = str(ch.id)
        if ch_id in state["channels"]:
            state["channels"].remove(ch_id)
            persist_state()
            bot.reply_to(message, f"➖ Removed channel: {ch_id}")
            send_owner(f"➖ Channel removed from sending list: {ch_id}")
        else:
            bot.reply_to(message, "Channel not in list.")
    except Exception as e:
        logger.exception("remove error: %s", e)
        bot.reply_to(message, "❌ Failed to remove channel.")

# manual add/remove by ID
@bot.message_handler(commands=['addchannel'])
@owner_only
def cmd_addchannel(message):
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /addchannel <chat_id or @channelusername>")
        return
    ch = parts[1].strip()
    state["channels"].add(ch)
    persist_state()
    bot.reply_to(message, f"➕ Added channel: {ch}")
    send_owner(f"➕ Channel manually added: {ch}")

@bot.message_handler(commands=['removechannel'])
@owner_only
def cmd_removechannel(message):
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /removechannel <chat_id or @channelusername>")
        return
    ch = parts[1].strip()
    if ch in state["channels"]:
        state["channels"].remove(ch)
        persist_state()
        bot.reply_to(message, f"➖ Removed channel: {ch}")
        send_owner(f"➖ Channel removed from sending list: {ch}")
    else:
        bot.reply_to(message, "Channel not found in list.")

# ---------------- other commands ----------------
@bot.message_handler(commands=['start'])
def cmd_start(message):
    bot.reply_to(message,
    "🤖 **BDG 3-Min Bot Online**\n\n"
    "📌 **Owner Commands:**\n"
    "/startbot – Start bot\n"
    "/stopbot – Stop bot\n"
    "/channels – List all channels\n"
    "/add – Add channel (reply to forwarded msg)\n"
    "/remove – Remove channel (reply to forwarded msg)\n"
    "/addchannel <id> – Add manually\n"
    "/removechannel <id> – Remove manually\n"
    "/stats – Show bot performance\n"
    "/history – Last 50 predictions\n"
    "/mode – Current market mode\n"
    )

@bot.message_handler(commands=['startbot'])
@owner_only
def cmd_startbot(message):
    if state["running"]:
        bot.reply_to(message, "⚠️ Bot already running.")
        return
    state["running"] = True
    state["learning"] = True
    state["collected"] = 0
    persist_state()
    bot.reply_to(message, "✅ Bot started. Learning mode ON — collecting real results for 10 rounds (~30 minutes).")
    send_owner("🔁 Bot started by owner.")
    threading.Thread(target=prediction_loop, daemon=True).start()

@bot.message_handler(commands=['stopbot'])
@owner_only
def cmd_stopbot(message):
    state["running"] = False
    persist_state()
    bot.reply_to(message, "🛑 Bot stopped.")
    send_owner("🛑 Bot stopped by owner.")

@bot.message_handler(commands=['stats'])
@owner_only
def cmd_stats(message):
    wins = db_exec("SELECT COUNT(*) FROM history WHERE win=1", fetch=True)[0][0]
    losses = db_exec("SELECT COUNT(*) FROM history WHERE win=0", fetch=True)[0][0]
    pending = db_exec("SELECT COUNT(*) FROM history WHERE win IS NULL", fetch=True)[0][0]
    bot.reply_to(message,
        f"📊 **Bot Stats:**\n"
        f"✔ Wins: {wins}\n"
        f"❌ Losses: {losses}\n"
        f"⏳ Pending: {pending}\n"
        f"📈 Level: {state['level']}\n"
        f"💰 Amount: {state['amount']}\n"
        f"📘 Learning: {state['learning']} ({state['collected']}/{LEARNING_RESULTS_N})"
    )

@bot.message_handler(commands=['history'])
@owner_only
def cmd_history(message):
    rows = db_exec("SELECT id,period,predicted,amount,level,actual,win,ts FROM history ORDER BY id DESC LIMIT 50", fetch=True)
    text = "Recent history:\n"
    for r in rows:
        text += f"{r}\n"
    bot.reply_to(message, text)

@bot.message_handler(commands=['mode'])
@owner_only
def cmd_mode(message):
    mode = detect_mode(list(previous_results))
    bot.reply_to(message, f"Current detected mode: {mode} (memory size: {len(previous_results)})")

# ---------------- start polling ----------------
if __name__ == "__main__":
    persist_state()
    logger.info("Starting bot V5. Owner id=%s", OWNER_ID)
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=60)
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt, exiting.")
    except Exception:
        logger.exception("Bot polling stopped unexpectedly.")

