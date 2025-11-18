# bdg3min_final_bot.py
# Final BDG 3-min Telegram bot (API-based)
# Flow: Prediction (text) -> Result (GIF only) -> Next Prediction (text)
# Requirements: pip install pyTelegramBotAPI requests

import time
import threading
import logging
import sqlite3
import datetime
import requests
import telebot

# ---------------- CONFIG - EDIT THESE ----------------
BOT_TOKEN = "8595314247:AAG_WqyrlWX0VxRljU2zLnVwJo1DR-uZCgs"   # e.g. "123456:ABC..."
OWNER_ID = 5698239751                         # your numeric Telegram ID
BDG_HISTORY_API = "https://draw.ar-lottery01.com/WinGo/WinGo_3M/GetHistoryIssuePage.json"
MARTINGALE = [10, 30, 70, 210, 640, 1400]
PAGE_SIZE = 12               # how many past results to request
RESULT_STABLE_WAIT = 5       # seconds to wait after tick before reading API
DB_FILE = "bdg3min_final_bot.db"
REQUEST_TIMEOUT = 8
# GIF direct URLs (wins/losses)
WIN_GIF = "https://media3.giphy.com/media/v1.Y2lkPTc5MGI3NjExM2JheDIydTA3ZmdmZDc2aGRzeHdieDZpb2V3dmJ0cGxkZzkzOWl5aSZlcD12MV9pbnRlcm5hbF9naWZfYnlfaWQmY3Q9Zw/OR1aQzSbvf4DrgX22C/giphy.gif"
LOSS_GIF = "https://media.giphy.com/media/v1.Y2lkPTc5MGI3NjExd2t2MjFhamxrMnRnMTE2bGZubmduNW9zMXVpZGNyaGxybzR4dHVyciZlcD12MV9naWZzX3NlYXJjaCZjdD1n/a7BCmY3LaiFilsvR32/giphy.gif"

# ----------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s: %(message)s")
logger = logging.getLogger("bdg3min_final")

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

# ---------- state ----------
state = {
    "running": False,
    "channels": set(get_meta("channels", "") .split(",")) if get_meta("channels") else set(),
    "level": int(get_meta("level", "1") or 1),
    "amount": int(get_meta("amount", str(MARTINGALE[0])) or MARTINGALE[0]),
    "last_period": get_meta("last_period", None)
}
@bot.message_handler(commands=['testgif'])
def cmd_testgif(message):
    try:
        bot.send_animation(message.chat.id, "https://media.tenor.com/0jndurai1S0AAAAC/baby-yes.gif")
        bot.send_animation(message.chat.id, "https://media.tenor.com/o3GgW0lPx2oAAAAC/disappointed.gif")
        bot.send_message(message.chat.id, "GIF test completed.")
    except Exception as e:
        bot.send_message(message.chat.id, f"GIF FAILED: {e}")

def persist_state():
    set_meta("channels", ",".join([str(x) for x in state["channels"]]))
    set_meta("level", state["level"])
    set_meta("amount", state["amount"])
    set_meta("last_period", state["last_period"])

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

# ---------- core loop ----------
def prediction_loop():
    logger.info("prediction loop started (API-based)")

    # initial read to know last_period
    rows = fetch_history_from_api()
    if rows and len(rows) > 0:
        state["last_period"] = rows[0].get("issueNumber")
        persist_state()
        logger.info("initial last_period set to %s", state["last_period"])
        # prepare and send first prediction immediately based on last_period
        try:
            next_period = str(int(state["last_period"]) + 1)
        except Exception:
            next_period = state["last_period"] + "1"
        # prediction by parity of next_period
        try:
            pred_val = "SMALL" if (int(next_period) % 2 == 0) else "BIG"
        except:
            pred_val = "SMALL"
        # send prediction text to owner + channels
        pred_msg = f"🔮 Prediction\nPeriod: {next_period}\n➡️ {pred_val}\nAmount: {state['amount']} | Level: {state['level']}"
        send_to_owner_and_channels_send_text(pred_msg)
        # record pending prediction
        db_exec("INSERT INTO history(period,predicted,amount,level,actual,win) VALUES (?,?,?,?,?,?)",
                (next_period, pred_val, state["amount"], state["level"], None, None))
    else:
        logger.info("no initial rows found; will sync to tick")

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
                persist_state()
                logger.info("initialized last_period to %s", state["last_period"])
                continue

            # if same period no new result
            if actual_period == state["last_period"]:
                logger.info("same period as last (%s) — nothing new", actual_period)
                continue

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

                # Update martingale
                if win_flag == 1:
                    state["level"] = 1
                    state["amount"] = MARTINGALE[0]
                else:
                    if state["level"] < len(MARTINGALE):
                        state["level"] += 1
                        state["amount"] = MARTINGALE[state["level"] - 1]
                    else:
                        state["amount"] = MARTINGALE[-1]
                persist_state()

            # update last_period
            state["last_period"] = actual_period
            persist_state()

            # prepare next prediction (period + 1)
            try:
                next_period = str(int(actual_period) + 1)
            except:
                next_period = actual_period + "1"

            try:
                pred_val = "SMALL" if (int(next_period) % 2 == 0) else "BIG"
            except:
                pred_val = "SMALL"

            # Immediately send next prediction (text)
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
    bot.reply_to(message, "BDG 3min bot online. Owner commands: /startbot /stopbot /addchannel /removechannel /stats /history")

@bot.message_handler(commands=['startbot'])
@owner_only
def cmd_startbot(message):
    if state["running"]:
        bot.reply_to(message, "⚠️ Bot already running.")
        return
    state["running"] = True
    persist_state()
    bot.reply_to(message, "✅ Bot started. Will send prediction, then GIF result, then next prediction.")
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
    bot.reply_to(message, f"📊 Stats:\nWins: {wins}\nLosses: {losses}\nPending: {pending}\nLevel: {state['level']} Amount: {state['amount']}")

@bot.message_handler(commands=['history'])
@owner_only
def cmd_history(message):
    rows = db_exec("SELECT id,period,predicted,amount,level,actual,win,ts FROM history ORDER BY id DESC LIMIT 50", fetch=True)
    text = "Recent history:\n"
    for r in rows:
        text += f"{r}\n"
    bot.reply_to(message, text)

# ---------- start polling ----------
if __name__ == "__main__":
    if "REPLACE_WITH_YOUR_BOT_TOKEN" in BOT_TOKEN or BOT_TOKEN.strip()=="":
        print("ERROR: set BOT_TOKEN at top of file")
        raise SystemExit(1)
    if not isinstance(OWNER_ID, int) or OWNER_ID == 0:
        print("ERROR: set OWNER_ID at top of file")
        raise SystemExit(1)

    persist_state()
    logger.info("Starting bot. Owner id=%s", OWNER_ID)
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=60)
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt, exiting.")
    except Exception:
        logger.exception("Bot polling stopped unexpectedly.")
