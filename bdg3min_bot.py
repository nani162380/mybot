# bdg3min_final_bot_v5.py
# BDG 3-min Telegram bot — V5 Market-Mode + 10-result learning phase
# Merged & upgraded from your uploaded bdg3min_final_bot.py
# Requirements: pip install pyTelegramBotAPI requests

import time
import threading
import logging
import sqlite3
import datetime
import requests
import telebot
import statistics
import collections

# ---------------- CONFIG - EDIT THESE ----------------
BOT_TOKEN = "8595314247:AAG_WqyrlWX0VxRljU2zLnVwJo1DR-uZCgs"   # your bot token
OWNER_ID = 5698239751                         # your numeric Telegram ID
BDG_HISTORY_API = "https://draw.ar-lottery01.com/WinGo/WinGo_3M/GetHistoryIssuePage.json"
PAGE_SIZE = 12               # how many past results to request
RESULT_STABLE_WAIT = 5       # seconds to wait after tick before reading API
DB_FILE = "bdg3min_final_bot_v5.db"
REQUEST_TIMEOUT = 8
# GIF direct URLs (wins/losses)
WIN_GIF = "https://media3.giphy.com/media/v1.Y2lkPTc5MGI3NjExM2JheDIydTA3ZmdmZDc2aGRzeHdieDZpb2V3dmJ0cGxkZzkzOWl5aSZlcD12MV9pbnRlcm5hbF9naWZfYnlfaWQmY3Q9Zw/OR1aQzSbvf4DrgX22C/giphy.gif"
LOSS_GIF = "https://media.giphy.com/media/v1.Y2lkPTc5MGI3NjExd2t2MjFhamxrMnRnMTE2bGZubmduNW9zMXVpZGNyaGxybzR4dHVyciZlcD12MV9naWZzX3NlYXJjaCZjdD1n/a7BCmY3LaiFilsvR32/giphy.gif"

# ---------------- Level/Amount Rules ----------------
# Levels 1..5 specific amounts; level >=6 amount fixed at 1470
LEVEL_AMOUNTS = {1:10, 2:30, 3:70, 4:210, 5:490}
FIXED_AMOUNT_AFTER_6 = 1470

# How many real results to collect before AI activates
LEARNING_RESULTS_N = 10
MAX_MEMORY = 80

# ----------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s: %(message)s")
logger = logging.getLogger("bdg3min_final_v5")

bot = telebot.TeleBot(BOT_TOKEN)

# ---------- DB helpers ----------
def db_exec(query: str, params: tuple = (), fetch=False):
    con = sqlite3.connect(DB_FILE, timeout=20)
    cur = con.cursor()
    try:
        cur.execute(query, params)
        if fetch:
            rows = cur.fetchall()
            return rows
        else:
            con.commit()
            return None
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

# ---------- utility ----------
def amount_for_level(lvl: int) -> int:
    if lvl <= 0:
        return LEVEL_AMOUNTS[1]
    if lvl <= 5:
        return LEVEL_AMOUNTS.get(lvl, LEVEL_AMOUNTS[5])
    return FIXED_AMOUNT_AFTER_6

# ---------- state ----------
state = {
    "running": False,
    "channels": set(get_meta("channels", "") .split(",")) if get_meta("channels") else set(),
    "level": int(get_meta("level", "1") or 1),
    "amount": int(get_meta("amount", str(LEVEL_AMOUNTS[1])) or LEVEL_AMOUNTS[1]),
    "last_period": get_meta("last_period", None),
    "learning": get_meta("learning", "1") == "1",   # learning mode enabled by default
    "collected": int(get_meta("collected", "0") or 0),
}

previous_results = collections.deque(maxlen=MAX_MEMORY)  # store recent actuals as "BIG"/"SMALL"

@bot.message_handler(commands=['testgif'])
def cmd_testgif(message):
    try:
        bot.send_animation(message.chat.id, WIN_GIF)
        bot.send_animation(message.chat.id, LOSS_GIF)
        bot.send_message(message.chat.id, "GIF test completed.")
    except Exception as e:
        bot.send_message(message.chat.id, f"GIF FAILED: {e}")

def persist_state():
    set_meta("channels", ",".join([str(x) for x in state["channels"]]))
    set_meta("level", state["level"])
    set_meta("amount", state["amount"])
    set_meta("last_period", state["last_period"])
    set_meta("learning", "1" if state.get("learning") else "0")
    set_meta("collected", state.get("collected", 0))

# ---------- API fetch ----------
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

# ---------- messaging ----------
def send_to_owner_and_channels_send_text(text: str):
    try:
        bot.send_message(OWNER_ID, text)
    except Exception:
        logger.debug("send text to owner failed")
    for ch in list(state["channels"]):
        try:
            bot.send_message(ch, text)
        except Exception:
            logger.debug("send text to channel failed: %s", ch)

def send_to_owner_and_channels_send_gif(gif_url: str):
    # send GIF only (no caption) as requested
    try:
        bot.send_animation(OWNER_ID, gif_url)
    except Exception:
        logger.debug("send gif to owner failed")
    for ch in list(state["channels"]):
        try:
            bot.send_animation(ch, gif_url)
        except Exception:
            logger.debug("send gif to channel failed: %s", ch)

# ---------- time helper ----------
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

# ---------- MARKET MODE LOGIC (V5) ----------

def detect_mode(recent):
    """
    recent: list of last N results where each item is 'BIG' or 'SMALL'
    Returns: mode string among ['TRAP','TREND','RANGING','BREAKOUT','UNKNOWN']
    """
    if not recent:
        return "UNKNOWN"
    n = len(recent)
    # counts
    cnt_big = recent.count("BIG")
    cnt_small = recent.count("SMALL")

    # Trap detection: specific patterns that indicate fakeouts
    pattern_str = ",".join(recent[-8:])
    traps = [
        "BIG,BIG,BIG,SMALL,BIG,SMALL",
        "SMALL,SMALL,SMALL,BIG,SMALL,BIG",
    ]
    for t in traps:
        if t in pattern_str:
            return "TRAP"

    # Strong trend: 5 or more same in last 8
    last8 = recent[-8:]
    if len(last8) >= 5:
        if last8.count("BIG") >= 5:
            return "TREND_BIG"
        if last8.count("SMALL") >= 5:
            return "TREND_SMALL"

    # Ranging / zig-zag detection: alternating pattern in last 6
    last6 = recent[-6:]
    if len(last6) >= 4:
        # check alternating
        alt = True
        for i in range(1, len(last6)):
            if last6[i] == last6[i-1]:
                alt = False
                break
        if alt:
            return "RANGING"

    # Breakout: sudden change after trend: last 4 has 3 of one then different
    if len(last8) >= 5:
        if last8[-5:-1].count(last8[-5]) >= 3 and last8[-1] != last8[-2]:
            return "BREAKOUT"

    return "UNKNOWN"


def decide_prediction(recent):
    """
    Uses Market Mode priority: TRAP > TREND > RANGING > BREAKOUT > UNKNOWN
    Returns 'BIG' or 'SMALL'
    """
    mode = detect_mode(recent)
    logger.debug("Market mode detected: %s", mode)

    if mode == "TRAP":
        # conservative: predict opposite of immediate-looking trend or last
        if not recent:
            return random_choice()
        # reverse of last to avoid trap
        return "SMALL" if recent[-1] == "BIG" else "BIG"

    if mode == "TREND_BIG":
        return "BIG"
    if mode == "TREND_SMALL":
        return "SMALL"

    if mode == "RANGING":
        # anti-trend: reverse last
        return "SMALL" if recent[-1] == "BIG" else "BIG"

    if mode == "BREAKOUT":
        # continuation of last
        return recent[-1]

    # UNKNOWN -> use weighted recent scoring
    # weight recent more: last 6 with linear weights
    weights = [1,1,2,2,3,4]
    seq = recent[-6:]
    if not seq:
        return random_choice()
    # pad weights and seq if necessary
    w = weights[-len(seq):]
    score = 0
    for i, val in enumerate(seq):
        weight = w[i]
        if val == "BIG":
            score += weight
        else:
            score -= weight
    return "BIG" if score > 0 else "SMALL"


def random_choice():
    import random
    return random.choice(["BIG","SMALL"])

# ---------- core loop ----------

def prediction_loop():
    logger.info("prediction loop started (API-based V5)")

    # initial read to know last_period and to prefill recent results buffer if available
    rows = fetch_history_from_api()
    if rows and len(rows) > 0:
        state["last_period"] = rows[0].get("issueNumber")
        # fill previous_results with recent history (most recent first)
        for r in rows[::-1]:
            num = r.get("number")
            try:
                bs = "BIG" if int(num) > 4 else "SMALL"
            except:
                bs = "SMALL"
            previous_results.append(bs)
        persist_state()
        logger.info("initial last_period set to %s, primed %d results", state["last_period"], len(previous_results))
    else:
        logger.info("no initial rows found; will sync to tick")

    # if learning enabled and not yet collected enough, inform owner
    if state.get("learning"):
        send_to_owner_and_channels_send_text(f"⚠️ Learning mode ON: I will collect {LEARNING_RESULTS_N} real results first (~{LEARNING_RESULTS_N*3} minutes) before activating AI predictions.")

    while state["running"]:
        try:
            now = datetime.datetime.now()
            target = next_3min_tick(now)
            wait_seconds = (target - now).total_seconds()
            logger.info("Next tick at %s (in %.1f s)", target.strftime("%H:%M:%S"), wait_seconds)
            # sleep in 1s steps to be interruptible
            remaining = wait_seconds
            while remaining > 0 and state["running"]:
                time.sleep(min(1.0, remaining))
                remaining -= 1.0
            if not state["running"]:
                break

            # tick reached -> wait a bit then fetch
            logger.info("Tick reached; waiting %ds for API stabilization...", RESULT_STABLE_WAIT)
            time.sleep(RESULT_STABLE_WAIT)

            # fetch history
            rows = fetch_history_from_api()
            if not rows or len(rows) == 0:
                logger.warning("No rows after tick; quick retries")
                found = False
                for _ in range(6):
                    time.sleep(1)
                    rows = fetch_history_from_api()
                    if rows and len(rows) > 0:
                        found = True
                        break
                if not found:
                    logger.warning("still no rows — skipping this tick")
                    continue

            latest = rows[0]
            actual_period = latest.get("issueNumber")
            actual_num = latest.get("number")
            # determine BIG/SMALL: number > 4 => BIG else SMALL (based on your rule)
            try:
                actual_bs = "BIG" if int(actual_num) > 4 else "SMALL"
            except:
                actual_bs = "SMALL"

            logger.info("Read latest: period=%s num=%s bs=%s", actual_period, actual_num, actual_bs)

            # if first-time initialization
            if not state["last_period"]:
                state["last_period"] = actual_period
                previous_results.append(actual_bs)
                state["collected"] = min(LEARNING_RESULTS_N, state.get("collected", 0) + 1)
                persist_state()
                logger.info("initialized last_period to %s", state["last_period"])
                continue

            # if same period no new result
            if actual_period == state["last_period"]:
                logger.info("same period as last (%s) — nothing new", actual_period)
                continue

            # add actual to memory
            previous_results.append(actual_bs)
            state["collected"] = min(LEARNING_RESULTS_N, state.get("collected", 0) + 1) if state.get("learning") else state.get("collected", 0)

            # Resolve pending prediction (if exists)
            pending_rows = db_exec("SELECT id, period, predicted, amount, level FROM history WHERE win IS NULL ORDER BY id DESC LIMIT 1", fetch=True)
            pending = pending_rows[0] if pending_rows else None
            if pending:
                pid, pred_period, pred_val, amt, lvl = pending
                win_flag = 1 if pred_val.upper() == actual_bs else 0
                db_exec("UPDATE history SET actual=?, win=? WHERE id=?", (actual_bs, win_flag, pid))

                # Send GIF only (no text) for result
                gif = WIN_GIF if win_flag == 1 else LOSS_GIF
                send_to_owner_and_channels_send_gif(gif)

                # Update martingale / levels according to your rule:
                if win_flag == 1:
                    state["level"] = 1
                    state["amount"] = amount_for_level(1)
                else:
                    # increment level by 1 (no cap) but amount fixed at FIXED_AMOUNT_AFTER_6 for level>=6
                    state["level"] = state.get("level",1) + 1
                    state["amount"] = amount_for_level(state["level"])
                persist_state()

            # update last_period
            state["last_period"] = actual_period
            persist_state()

            # If learning mode and not yet collected enough results, do not send next prediction
            if state.get("learning") and state.get("collected",0) < LEARNING_RESULTS_N:
                logger.info("Learning phase: collected %d/%d results. Waiting...", state.get("collected",0), LEARNING_RESULTS_N)
                continue

            # If learning mode finished now, announce and set learning=False
            if state.get("learning") and state.get("collected",0) >= LEARNING_RESULTS_N:
                state["learning"] = False
                persist_state()
                send_to_owner_and_channels_send_text(f"✅ Learning complete ({LEARNING_RESULTS_N} results). AI predictions activated.")

            # prepare next prediction (period + 1)
            try:
                next_period = str(int(actual_period) + 1)
            except:
                next_period = actual_period + "1"

            # Decide prediction using market-mode logic based on previous_results
            pred_val = decide_prediction(list(previous_results))

            # send prediction text to owner + channels
            pred_msg = f"🔮 Prediction\nPeriod: {next_period}\n➡️ {pred_val}\nAmount: {state['amount']} | Level: {state['level']}"
            send_to_owner_and_channels_send_text(pred_msg)

            # insert new pending
            db_exec("INSERT INTO history(period,predicted,amount,level,actual,win) VALUES (?,?,?,?,?,?)",
                    (next_period, pred_val, state["amount"], state["level"], None, None))

        except Exception as e:
            logger.exception("prediction_loop exception: %s", e)
            time.sleep(3)

    logger.info("prediction loop stopped")

# ---------- owner-only decorator ----------
def owner_only(func):
    def wrapper(message):
        try:
            uid = message.from_user.id
        except:
            bot.reply_to(message, "Cannot verify user.")
            return
        if uid != OWNER_ID:
            bot.reply_to(message, "❌ You are not authorized.")
            return
        return func(message)
    return wrapper

# ---------- Telegram commands ----------
@bot.message_handler(commands=['start'])
def cmd_start(message):
    bot.reply_to(message, "BDG 3min bot online. Owner commands: /startbot /stopbot /addchannel /removechannel /stats /history /mode")

@bot.message_handler(commands=['startbot'])
@owner_only
def cmd_startbot(message):
    if state["running"]:
        bot.reply_to(message, "⚠️ Bot already running.")
        return
    state["running"] = True
    # reset learning counters so we collect fresh results
    state["learning"] = True
    state["collected"] = 0
    persist_state()
    bot.reply_to(message, "✅ Bot started. Learning mode ON — collecting real results for 10 rounds (~30 minutes). I will notify when AI activates.")
    threading.Thread(target=prediction_loop, daemon=True).start()

@bot.message_handler(commands=['stopbot'])
@owner_only
def cmd_stopbot(message):
    state["running"] = False
    persist_state()
    bot.reply_to(message, "🛑 Bot stopped.")

@bot.message_handler(commands=['addchannel'])
@owner_only
def cmd_addchannel(message):
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /addchannel <chat_id or @channelusername>")
        return
    ch = parts[1]
    state["channels"].add(ch)
    persist_state()
    bot.reply_to(message, f"➕ Added {ch}")

@bot.message_handler(commands=['removechannel'])
@owner_only
def cmd_removechannel(message):
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /removechannel <chat_id>")
        return
    ch = parts[1]
    if ch in state["channels"]:
        state["channels"].remove(ch)
        persist_state()
        bot.reply_to(message, f"➖ Removed {ch}")
    else:
        bot.reply_to(message, "Channel not found.")

@bot.message_handler(commands=['stats'])
@owner_only
def cmd_stats(message):
    wins = db_exec("SELECT COUNT(*) FROM history WHERE win=1", fetch=True)[0][0]
    losses = db_exec("SELECT COUNT(*) FROM history WHERE win=0", fetch=True)[0][0]
    pending = db_exec("SELECT COUNT(*) FROM history WHERE win IS NULL", fetch=True)[0][0]
    bot.reply_to(message, f"📊 Stats:\nWins: {wins}\nLosses: {losses}\nPending: {pending}\nLevel: {state['level']} Amount: {state['amount']} Learning: {state.get('learning')} Collected: {state.get('collected')}")

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
    # quick dump of the detected mode using current memory
    mode = detect_mode(list(previous_results))
    bot.reply_to(message, f"Current detected mode: {mode} (memory size: {len(previous_results)})")

# ---------- start polling ----------
if __name__ == "__main__":
    if "REPLACE_WITH_YOUR_BOT_TOKEN" in BOT_TOKEN or BOT_TOKEN.strip()=="":
        print("ERROR: set BOT_TOKEN at top of file")
        raise SystemExit(1)
    if not isinstance(OWNER_ID, int) or OWNER_ID == 0:
        print("ERROR: set OWNER_ID at top of file")
        raise SystemExit(1)

    persist_state()
    logger.info("Starting bot V5. Owner id=%s", OWNER_ID)
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=60)
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt, exiting.")
    except Exception:
        logger.exception("Bot polling stopped unexpectedly.")
