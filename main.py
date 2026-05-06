import asyncio
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from pyrogram import Client, filters, idle
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- НАЛАШТУВАННЯ ---
load_dotenv()

try:
    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]
except KeyError as e:
    sys.exit(f"❌ Не задано змінну оточення {e}. Створи .env (див. .env.example).")

game_bot_username = os.environ.get("GAME_BOT_USERNAME", "ostromag_game_bot")

CONFIG_PATH = Path("config.json")
DEFAULT_BASELINE_TIME = "16:10"
DIFF_COOLDOWN_SECONDS = 5 * 60
SCHED_JOB_ID = "baseline_snapshot"

def load_config():
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {"baseline_time": DEFAULT_BASELINE_TIME}

def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))

config = load_config()
last_diff_call = 0.0
last_chlen_call = 0.0

ROLE_EMOJIS = ["👑", "⭐", "👤"]
STATUS_EMOJIS = ["🟢", "⚪", "🔴", "🟡"]

def _strip_member_prefix(name_part):
    s = name_part
    for e in ROLE_EMOJIS:
        if s.startswith(e):
            s = s[len(e):].lstrip()
            break
    for e in STATUS_EMOJIS:
        if s.startswith(e):
            s = s[len(e):].lstrip()
            break
    return s

app = Client("my_account", api_id=api_id, api_hash=api_hash)
scheduler = AsyncIOScheduler()

# --- ЛОГІКА БАЗИ ДАНИХ ---
def process_guild_stats(current_data, save_mode=False):
    conn = sqlite3.connect("global_guild_stats.db")
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS guild_ranks (date TEXT, guild_name TEXT, glory INTEGER)")
    
    today = datetime.now().strftime("%Y-%m-%d")
    report_lines = []

    baseline_date = None
    if save_mode:
        cursor.execute("DELETE FROM guild_ranks WHERE date = ?", (today,))
        for key, _display, glory in current_data:
            cursor.execute("INSERT INTO guild_ranks VALUES (?, ?, ?)", (today, key, int(glory)))
        conn.commit()
        baseline_date = today
        print(f"✅ Дані на {config['baseline_time']} збережено (всього гільдій: {len(current_data)})")
    else:
        cursor.execute("SELECT MAX(date) FROM guild_ranks")
        row = cursor.fetchone()
        baseline_date = row[0] if row and row[0] else None

        history = {}
        if baseline_date:
            cursor.execute("SELECT guild_name, glory FROM guild_ranks WHERE date = ?", (baseline_date,))
            history = {n.lower().strip(): g for n, g in cursor.fetchall()}

        sorted_data = sorted(
            ((key, display, int(glory)) for key, display, glory in current_data),
            key=lambda x: x[2],
            reverse=True,
        )

        for rank, (key, display, current_glory) in enumerate(sorted_data, start=1):
            marker = {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"{rank}.")
            old_glory = history.get(key.lower().strip())

            if old_glory is not None:
                diff = current_glory - old_glory
                report_lines.append(f"{marker} {display} ({current_glory}): {diff:+d}")
            else:
                report_lines.append(f"{marker} {display} ({current_glory}): вперше в ТОП")

    conn.close()
    return report_lines, baseline_date

def process_member_stats(current_data, save_mode=False):
    conn = sqlite3.connect("global_guild_stats.db")
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS member_ranks (date TEXT, member_name TEXT, glory INTEGER)")

    today = datetime.now().strftime("%Y-%m-%d")
    report_lines = []
    baseline_date = None

    if save_mode:
        cursor.execute("DELETE FROM member_ranks WHERE date = ?", (today,))
        for key, _display, glory in current_data:
            cursor.execute("INSERT INTO member_ranks VALUES (?, ?, ?)", (today, key, int(glory)))
        conn.commit()
        baseline_date = today
        print(f"✅ Дані членів на {config['baseline_time']} збережено (всього: {len(current_data)})")
    else:
        cursor.execute("SELECT MAX(date) FROM member_ranks")
        row = cursor.fetchone()
        baseline_date = row[0] if row and row[0] else None

        history = {}
        if baseline_date:
            cursor.execute("SELECT member_name, glory FROM member_ranks WHERE date = ?", (baseline_date,))
            history = {n.lower().strip(): g for n, g in cursor.fetchall()}

        sorted_data = sorted(
            ((key, display, int(glory)) for key, display, glory in current_data),
            key=lambda x: x[2],
            reverse=True,
        )

        for rank, (key, display, current_glory) in enumerate(sorted_data, start=1):
            marker = {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"{rank}.")
            old_glory = history.get(key.lower().strip())

            if old_glory is not None:
                diff = current_glory - old_glory
                report_lines.append(f"{marker} {display} ({current_glory}): {diff:+d}")
            else:
                report_lines.append(f"{marker} {display} ({current_glory}): вперше в ТОП")

    conn.close()
    return report_lines, baseline_date

# --- ФУНКЦІЯ ЗБОРУ РЕЙТИНГУ ---
async def collect_ranking_data(save_mode=False, target_chat_id=None):
    try:
        print(f"🤖 Починаю збір рейтингу...")
        await app.send_message(game_bot_username, "/start")
        await asyncio.sleep(2)
        await app.send_message(game_bot_username, "📂 Меню")
        await asyncio.sleep(2)
        await app.send_message(game_bot_username, "🏆 Рейтинг")
        await asyncio.sleep(3)
        
        async for message in app.get_chat_history(game_bot_username, limit=1):
            if message.reply_markup and message.reply_markup.inline_keyboard:
                for row in message.reply_markup.inline_keyboard:
                    for btn in row:
                        if "За Славою Гільдій" in btn.text:
                            await app.request_callback_answer(message.chat.id, message.id, btn.callback_data)
                            await asyncio.sleep(5)
                            break

        async for message in app.get_chat_history(game_bot_username, limit=1):
            text = message.text
            lines = text.split('\n')
            matches = []

            for line in lines:
                line = line.strip()
                # Шукаємо цифри в дужках у самому кінці рядка
                find_glory = re.search(r"\((\d+)\)$", line)
                
                if find_glory:
                    glory = find_glory.group(1)
                    name_part = line[:find_glory.start()].strip()

                    # Прибираємо лідируючу позицію: "1.", "10.", медалі 🥇🥈🥉
                    display_part = re.sub(r"^(?:\d+\.\s*|[\U0001F947\U0001F948\U0001F949]\s*)+", "", name_part).strip()

                    # Ключ для матчінгу між днями — чиста назва без емоджи гільдії
                    name_match = re.search(r"[A-Za-zА-Яа-яІіЇїЄєҐґ].*", display_part)
                    if name_match:
                        key = name_match.group(0).strip()
                        matches.append((key, display_part, glory))

            if matches:
                results, baseline_date = process_guild_stats(matches, save_mode=save_mode)
                if not save_mode and target_chat_id:
                    if baseline_date:
                        date_str = datetime.strptime(baseline_date, "%Y-%m-%d").strftime("%d.%m.%Y")
                        header = f"📊 **Приріст слави гільдій з {config['baseline_time']} {date_str}**\n\n"
                    else:
                        header = "📊 **Поточний рейтинг гільдій** (бейзлайн ще не зафіксовано)\n\n"
                    await app.send_message(target_chat_id, header + "\n".join(results))
                    print("✅ Звіт надіслано!")
            else:
                print("❌ Не вдалося розпізнати гільдії.")

    except Exception as e:
        print(f"❌ Помилка: {e}")

async def collect_members_data(save_mode=False, target_chat_id=None):
    try:
        print("🤖 Починаю збір членів гільдії...")
        await app.send_message(game_bot_username, "/start")
        await asyncio.sleep(2)
        await app.send_message(game_bot_username, "🏰 Гільдія")
        await asyncio.sleep(3)
        await app.send_message(game_bot_username, "👥 Члени")
        await asyncio.sleep(3)

        async for message in app.get_chat_history(game_bot_username, limit=1):
            text = message.text or ""
            blocks = re.split(r"—{3,}", text)
            matches = []

            for block in blocks:
                lines = [l.strip() for l in block.splitlines() if l.strip()]
                name_line = None
                glory = None
                for line in lines:
                    if name_line is None and "|" in line and "Рівень" in line:
                        name_line = line
                    glory_match = re.search(r"Слава:\s*(\d+)", line)
                    if glory_match:
                        glory = int(glory_match.group(1))
                if name_line is None or glory is None:
                    continue
                name_part = name_line.split("|", 1)[0].strip()
                display = _strip_member_prefix(name_part)
                if not display:
                    continue
                key = display
                matches.append((key, display, glory))

            if matches:
                results, baseline_date = process_member_stats(matches, save_mode=save_mode)
                if not save_mode and target_chat_id:
                    if baseline_date:
                        date_str = datetime.strptime(baseline_date, "%Y-%m-%d").strftime("%d.%m.%Y")
                        header = f"👥 **Приріст слави членів гільдії з {config['baseline_time']} {date_str}**\n\n"
                    else:
                        header = "👥 **Поточний рейтинг гільдійців** (бейзлайн ще не зафіксовано)\n\n"
                    await app.send_message(target_chat_id, header + "\n".join(results))
                    print("✅ Звіт по членах надіслано!")
            else:
                print("❌ Не вдалося розпізнати членів гільдії.")
    except Exception as e:
        print(f"❌ Помилка (members): {e}")

# --- ПЛАНУВАЛЬНИК ТА ОБРОБНИКИ ---
async def scheduled_task():
    await collect_ranking_data(save_mode=True)
    await asyncio.sleep(5)
    await collect_members_data(save_mode=True)

@app.on_message(filters.command("diff", prefixes="/"))
async def handle_diff(client, message):
    global last_diff_call
    now = time.monotonic()
    elapsed = now - last_diff_call
    if elapsed < DIFF_COOLDOWN_SECONDS:
        wait = int(DIFF_COOLDOWN_SECONDS - elapsed)
        m, s = divmod(wait, 60)
        await message.reply(f"⏳ Зачекай ще {m}хв {s}с перед наступним /diff")
        return
    last_diff_call = now
    await collect_ranking_data(save_mode=False, target_chat_id=message.chat.id)

@app.on_message(filters.command("chlen", prefixes="/"))
async def handle_chlen(client, message):
    global last_chlen_call
    now = time.monotonic()
    elapsed = now - last_chlen_call
    if elapsed < DIFF_COOLDOWN_SECONDS:
        wait = int(DIFF_COOLDOWN_SECONDS - elapsed)
        m, s = divmod(wait, 60)
        await message.reply(f"⏳ Зачекай ще {m}хв {s}с перед наступним /chlen")
        return
    last_chlen_call = now
    await collect_members_data(save_mode=False, target_chat_id=message.chat.id)

@app.on_message(filters.command("settime", prefixes="/") & filters.me)
async def handle_settime(client, message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply(
            f"⏰ Поточний час бейзлайну: {config['baseline_time']}\n"
            f"Формат: `/settime HH:MM` (напр. `/settime 16:10`)"
        )
        return
    try:
        hour_s, minute_s = parts[1].strip().split(":")
        hour, minute = int(hour_s), int(minute_s)
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError
    except ValueError:
        await message.reply("❌ Невірний формат. Приклад: `/settime 16:10`")
        return
    config["baseline_time"] = f"{hour:02d}:{minute:02d}"
    save_config(config)
    scheduler.reschedule_job(SCHED_JOB_ID, trigger="cron", hour=hour, minute=minute)
    await message.reply(f"✅ Бейзлайн тепер фіксується о {config['baseline_time']}")

async def main():
    try:
        await app.start()
        hour, minute = map(int, config["baseline_time"].split(":"))
        scheduler.add_job(scheduled_task, "cron", hour=hour, minute=minute, id=SCHED_JOB_ID)
        scheduler.start()
        print(f"🚀 BOT IS LIVE! Бейзлайн о {config['baseline_time']}, кулдаун /diff: {DIFF_COOLDOWN_SECONDS//60} хв")
        await idle()
    finally:
        await app.stop()

if __name__ == "__main__":
    app.run(main())