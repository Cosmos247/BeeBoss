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
from pyrogram import utils as _pyrogram_utils
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Workaround Pyrogram 2.0.106: підтримка нових великих ID каналів/супергруп Telegram
def _patched_get_peer_type(peer_id):
    peer_id_str = str(peer_id)
    if not peer_id_str.startswith("-"):
        return "user"
    elif peer_id_str.startswith("-100"):
        return "channel"
    else:
        return "chat"

_pyrogram_utils.get_peer_type = _patched_get_peer_type

# --- НАЛАШТУВАННЯ ---
load_dotenv()

try:
    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]
except KeyError as e:
    sys.exit(f"❌ Не задано змінну оточення {e}. Створи .env (див. .env.example).")

game_bot_username = os.environ.get("GAME_BOT_USERNAME", "ostromag_game_bot")

CONFIG_PATH = Path("config.json")
DEFAULT_BASELINE_TIME = "12:00"
DEFAULT_REPORT_TIME = "11:55"
DIFF_COOLDOWN_SECONDS = 5 * 60
SCHED_JOB_ID = "baseline_snapshot"
AUTO_REPORT_JOB_ID = "auto_report"
BATTLE_RETRY_DELAY = 90

def load_config():
    cfg = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
        except json.JSONDecodeError:
            cfg = {}
    cfg.setdefault("baseline_time", DEFAULT_BASELINE_TIME)
    cfg.setdefault("report_time", DEFAULT_REPORT_TIME)
    cfg.setdefault("report_chat_id", None)
    return cfg

def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))

config = load_config()
last_diff_call = 0.0
last_kab_call = 0.0
last_fab_call = 0.0

ROLE_EMOJIS = ["👑", "⭐", "👤"]
STATUS_EMOJIS = ["🟢", "⚪", "🔴", "🟡"]

TOP_GUILD_PAIR = [
    ("Секта Буржуазної Утопії", "СБУ"),
    ("Керовані Авіа Бджоли", "КАБ"),
]

FAB_FULL_NAME = "Фугасні Авіа Бджоли"

def _find_guild_glory(full_name, items):
    target = full_name.lower()
    for key, glory in items:
        if target in key.lower():
            return int(glory)
    return None

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

app_kab = Client("my_account", api_id=api_id, api_hash=api_hash)
app_fab = Client("fab_account", api_id=api_id, api_hash=api_hash)
scheduler = AsyncIOScheduler()

# --- ЛОГІКА БАЗИ ДАНИХ ---
def migrate_db():
    conn = sqlite3.connect("global_guild_stats.db")
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS guild_ranks (date TEXT, guild_name TEXT, glory INTEGER)")
    cursor.execute("CREATE TABLE IF NOT EXISTS member_ranks (date TEXT, member_name TEXT, glory INTEGER)")
    cursor.execute("PRAGMA table_info(member_ranks)")
    cols = [r[1] for r in cursor.fetchall()]
    if "guild_tag" not in cols:
        cursor.execute("ALTER TABLE member_ranks ADD COLUMN guild_tag TEXT DEFAULT 'КАБ'")
        cursor.execute("UPDATE member_ranks SET guild_tag = 'КАБ' WHERE guild_tag IS NULL")
        conn.commit()
        print("✅ Міграція БД: додано колонку guild_tag (існуючі рядки → КАБ)")
    conn.close()

def process_guild_stats(current_data, fab_data, save_mode=False):
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
        # Якщо ФАБ за межами топ-10, зберігаємо її окремо для майбутнього diff
        fab_saved = False
        if fab_data and fab_data[0] > 10:
            _, fab_key, _, fab_glory = fab_data
            cursor.execute("INSERT INTO guild_ranks VALUES (?, ?, ?)", (today, fab_key, int(fab_glory)))
            fab_saved = True
        conn.commit()
        baseline_date = today
        total = len(current_data) + (1 if fab_saved else 0)
        print(f"✅ Дані на {config['baseline_time']} збережено (всього гільдій: {total})")
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

        # Якщо ФАБ у топ-10 — звичайний топ-10. Якщо за межами — топ-9 + сепаратор + ФАБ.
        fab_in_top10 = fab_data is not None and fab_data[0] <= 10
        display_limit = 10 if (fab_in_top10 or fab_data is None) else 9

        for rank, (key, display, current_glory) in enumerate(sorted_data, start=1):
            if rank > display_limit:
                break
            marker = {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"{rank}.")
            old_glory = history.get(key.lower().strip())

            if old_glory is not None:
                diff = current_glory - old_glory
                report_lines.append(f"{marker} {display} ({current_glory}): {diff:+d}")
            else:
                report_lines.append(f"{marker} {display} ({current_glory}): —")

        # ФАБ окремим рядком тільки якщо вона за межами топ-10
        separator = "―" * 20
        if fab_data and not fab_in_top10:
            fab_pos, fab_key, fab_display, fab_glory = fab_data
            fab_old = history.get(fab_key.lower().strip())
            if fab_old is not None:
                fab_diff_str = f"{fab_glory - fab_old:+d}"
            else:
                fab_diff_str = "—"
            report_lines.append(separator)
            report_lines.append(f"{fab_pos}. {fab_display} ({fab_glory}): {fab_diff_str}")
        elif fab_data is None:
            report_lines.append(separator)
            report_lines.append("(ФАБ не знайдено в рейтингу)")

        # Розрив між топ-парою (СБУ ↔ КАБ)
        current_items = [(key, glory) for key, _, glory in sorted_data]
        history_items = list(history.items())
        (g1_full, g1_short), (g2_full, g2_short) = TOP_GUILD_PAIR
        g1_now = _find_guild_glory(g1_full, current_items)
        g2_now = _find_guild_glory(g2_full, current_items)
        if g1_now is not None and g2_now is not None:
            gap_now = g1_now - g2_now
            g1_old = _find_guild_glory(g1_full, history_items)
            g2_old = _find_guild_glory(g2_full, history_items)
            report_lines.append("")
            if g1_old is not None and g2_old is not None:
                gap_old = g1_old - g2_old
                delta = gap_now - gap_old
                if delta < 0:
                    delta_str = f"🟢⬆ {abs(delta)}"
                elif delta > 0:
                    delta_str = f"🔴⬇ {delta}"
                else:
                    delta_str = "0"
                report_lines.append(f"{g1_short} ↔ {g2_short}: {gap_now} (було {gap_old}, {delta_str})")
            else:
                report_lines.append(f"{g1_short} ↔ {g2_short}: {gap_now}")

    conn.close()
    return report_lines, baseline_date

def process_member_stats(current_data, guild_tag, save_mode=False):
    conn = sqlite3.connect("global_guild_stats.db")
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS member_ranks (date TEXT, guild_tag TEXT, member_name TEXT, glory INTEGER)")

    today = datetime.now().strftime("%Y-%m-%d")
    report_lines = []
    baseline_date = None

    if save_mode:
        cursor.execute("DELETE FROM member_ranks WHERE date = ? AND guild_tag = ?", (today, guild_tag))
        for key, _display, glory in current_data:
            cursor.execute(
                "INSERT INTO member_ranks (date, guild_tag, member_name, glory) VALUES (?, ?, ?, ?)",
                (today, guild_tag, key, int(glory)),
            )
        conn.commit()
        baseline_date = today
        print(f"✅ Дані членів {guild_tag} на {config['baseline_time']} збережено (всього: {len(current_data)})")
    else:
        cursor.execute("SELECT MAX(date) FROM member_ranks WHERE guild_tag = ?", (guild_tag,))
        row = cursor.fetchone()
        baseline_date = row[0] if row and row[0] else None

        history = {}
        if baseline_date:
            cursor.execute(
                "SELECT member_name, glory FROM member_ranks WHERE date = ? AND guild_tag = ?",
                (baseline_date, guild_tag),
            )
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
                report_lines.append(f"{marker} {display} ({current_glory}): —")

    conn.close()
    return report_lines, baseline_date

# --- ФУНКЦІЯ ЗБОРУ РЕЙТИНГУ ---
async def _bail_if_in_battle(message, target_chat_id, reply_client, silent=False):
    if message and message.text and "Ви в бою" in message.text:
        if target_chat_id and not silent:
            await reply_client.send_message(target_chat_id, "⚔️ Зараз я в бою, спробуй пізніше.")
        print("⚔️ В бою — пропускаю запит" + (" (silent)" if silent else ""))
        return True
    return False

async def collect_ranking_data(save_mode=False, target_chat_id=None, silent_battle=False):
    try:
        print(f"🤖 Починаю збір рейтингу...")
        await app_kab.send_message(game_bot_username, "/start")
        await asyncio.sleep(2)
        await app_kab.send_message(game_bot_username, "📂 Меню")
        await asyncio.sleep(2)
        await app_kab.send_message(game_bot_username, "🏆 Рейтинг")
        await asyncio.sleep(3)

        async for message in app_kab.get_chat_history(game_bot_username, limit=1):
            if await _bail_if_in_battle(message, target_chat_id, app_kab, silent=silent_battle):
                return "battle"
            if message.reply_markup and message.reply_markup.inline_keyboard:
                for row in message.reply_markup.inline_keyboard:
                    for btn in row:
                        if "За Славою Гільдій" in btn.text:
                            await app_kab.request_callback_answer(message.chat.id, message.id, btn.callback_data)
                            await asyncio.sleep(5)
                            break

        page1_msg = None
        matches = []
        async for message in app_kab.get_chat_history(game_bot_username, limit=1):
            if await _bail_if_in_battle(message, target_chat_id, app_kab, silent=silent_battle):
                return "battle"
            page1_msg = message
            text = message.text or ""
            lines = text.split('\n')

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

        # Шукаємо ФАБ. Спочатку у топ-10 (page 1), якщо нема — переходимо на page 2.
        fab_data = None
        for i, (key, display, glory) in enumerate(matches, start=1):
            if FAB_FULL_NAME in key or FAB_FULL_NAME in display:
                fab_data = (i, key, display, int(glory))
                break

        if fab_data is None and page1_msg and page1_msg.reply_markup and page1_msg.reply_markup.inline_keyboard:
            next_btn_data = None
            for row in page1_msg.reply_markup.inline_keyboard:
                for btn in row:
                    btxt = btn.text or ""
                    if ("▶" in btxt or "➡" in btxt) and "◀" not in btxt and "⬅" not in btxt:
                        next_btn_data = btn.callback_data
                        break
                if next_btn_data:
                    break
            if next_btn_data:
                await app_kab.request_callback_answer(page1_msg.chat.id, page1_msg.id, next_btn_data)
                await asyncio.sleep(3)
                async for page2_msg in app_kab.get_chat_history(game_bot_username, limit=1):
                    if await _bail_if_in_battle(page2_msg, target_chat_id, app_kab, silent=silent_battle):
                        return "battle"
                    text2 = page2_msg.text or ""
                    for line in text2.split('\n'):
                        line = line.strip()
                        if FAB_FULL_NAME in line:
                            pos_m = re.match(r"^(\d+)\.\s*", line)
                            glory_m = re.search(r"\((\d+)\)\s*$", line)
                            if pos_m and glory_m:
                                pos = int(pos_m.group(1))
                                fab_glory = int(glory_m.group(1))
                                display_part = line[pos_m.end():glory_m.start()].strip()
                                name_match = re.search(r"[A-Za-zА-Яа-яІіЇїЄєҐґ].*", display_part)
                                key = name_match.group(0).strip() if name_match else FAB_FULL_NAME
                                fab_data = (pos, key, display_part, fab_glory)
                            break

        if matches:
            results, baseline_date = process_guild_stats(matches, fab_data, save_mode=save_mode)
            if not save_mode and target_chat_id:
                if baseline_date:
                    date_str = datetime.strptime(baseline_date, "%Y-%m-%d").strftime("%d.%m.%Y")
                    header = f"📊 **Приріст слави гільдій з {config['baseline_time']} {date_str}**\n\n"
                else:
                    header = "📊 **Поточний рейтинг гільдій** (бейзлайн ще не зафіксовано)\n\n"
                await app_kab.send_message(target_chat_id, header + "\n".join(results))
                print("✅ Звіт надіслано!")
            return "ok"
        else:
            print("❌ Не вдалося розпізнати гільдії.")
            return "error"

    except Exception as e:
        print(f"❌ Помилка: {e}")
        return "error"

async def collect_members_data(client, guild_tag, save_mode=False, target_chat_id=None, reply_client=None, silent_battle=False):
    if reply_client is None:
        reply_client = client
    try:
        print(f"🤖 Починаю збір членів гільдії {guild_tag}...")
        await client.send_message(game_bot_username, "/start")
        await asyncio.sleep(2)
        await client.send_message(game_bot_username, "🏰 Гільдія")
        await asyncio.sleep(3)
        await client.send_message(game_bot_username, "👥 Члени")
        await asyncio.sleep(3)

        async for message in client.get_chat_history(game_bot_username, limit=1):
            if await _bail_if_in_battle(message, target_chat_id, reply_client, silent=silent_battle):
                return "battle"
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
                results, baseline_date = process_member_stats(matches, guild_tag, save_mode=save_mode)
                if not save_mode and target_chat_id:
                    if baseline_date:
                        date_str = datetime.strptime(baseline_date, "%Y-%m-%d").strftime("%d.%m.%Y")
                        header = f"👥 **{guild_tag}: приріст слави членів з {config['baseline_time']} {date_str}**\n\n"
                    else:
                        header = f"👥 **{guild_tag}: поточний рейтинг гільдійців** (бейзлайн ще не зафіксовано)\n\n"
                    await reply_client.send_message(target_chat_id, header + "\n".join(results))
                    print(f"✅ Звіт по членах {guild_tag} надіслано!")
                return "ok"
            else:
                print(f"❌ Не вдалося розпізнати членів гільдії {guild_tag}.")
                return "error"
        return "error"
    except Exception as e:
        print(f"❌ Помилка (members {guild_tag}): {e}")
        return "error"

# --- ПЛАНУВАЛЬНИК ТА ОБРОБНИКИ ---
async def scheduled_task():
    await collect_ranking_data(save_mode=True)
    await asyncio.sleep(5)
    await collect_members_data(app_kab, "КАБ", save_mode=True)
    await asyncio.sleep(5)
    await collect_members_data(app_fab, "ФАБ", save_mode=True)

async def auto_report_task():
    chat_id = config.get("report_chat_id")
    if not chat_id:
        return
    print(f"📤 Авто-звіт у чат {chat_id}")

    async def run_one(name, silent):
        if name == "diff":
            return await collect_ranking_data(save_mode=False, target_chat_id=chat_id, silent_battle=silent)
        if name == "kab":
            return await collect_members_data(app_kab, "КАБ", save_mode=False, target_chat_id=chat_id, silent_battle=silent)
        if name == "fab":
            return await collect_members_data(app_fab, "ФАБ", save_mode=False, target_chat_id=chat_id, reply_client=app_kab, silent_battle=silent)

    pending = ["diff", "kab", "fab"]
    failed = []
    for name in pending:
        result = await run_one(name, silent=True)
        await asyncio.sleep(5)
        if result == "battle":
            failed.append(name)

    if not failed:
        return

    print(f"⏳ {failed} в бою — повтор через {BATTLE_RETRY_DELAY}с")
    await asyncio.sleep(BATTLE_RETRY_DELAY)
    for name in failed:
        await run_one(name, silent=False)
        await asyncio.sleep(5)

@app_kab.on_message(filters.command("diff", prefixes="/"))
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

@app_kab.on_message(filters.command("kab", prefixes="/"))
async def handle_kab(client, message):
    global last_kab_call
    now = time.monotonic()
    elapsed = now - last_kab_call
    if elapsed < DIFF_COOLDOWN_SECONDS:
        wait = int(DIFF_COOLDOWN_SECONDS - elapsed)
        m, s = divmod(wait, 60)
        await message.reply(f"⏳ Зачекай ще {m}хв {s}с перед наступним /kab")
        return
    last_kab_call = now
    await collect_members_data(app_kab, "КАБ", save_mode=False, target_chat_id=message.chat.id)

@app_kab.on_message(filters.command("fab", prefixes="/"))
async def handle_fab(client, message):
    global last_fab_call
    now = time.monotonic()
    elapsed = now - last_fab_call
    if elapsed < DIFF_COOLDOWN_SECONDS:
        wait = int(DIFF_COOLDOWN_SECONDS - elapsed)
        m, s = divmod(wait, 60)
        await message.reply(f"⏳ Зачекай ще {m}хв {s}с перед наступним /fab")
        return
    last_fab_call = now
    await collect_members_data(app_fab, "ФАБ", save_mode=False, target_chat_id=message.chat.id, reply_client=app_kab)

@app_kab.on_message(filters.command("settime", prefixes="/") & filters.me)
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

@app_kab.on_message(filters.command("setreportchat", prefixes="/") & filters.me)
async def handle_setreportchat(client, message):
    config["report_chat_id"] = message.chat.id
    save_config(config)
    if not scheduler.get_job(AUTO_REPORT_JOB_ID):
        h, m = map(int, config["report_time"].split(":"))
        scheduler.add_job(auto_report_task, "cron", hour=h, minute=m, id=AUTO_REPORT_JOB_ID)
    await message.reply(f"✅ Авто-звіт активовано для цього чату о {config['report_time']}")

@app_kab.on_message(filters.command("clearreportchat", prefixes="/") & filters.me)
async def handle_clearreportchat(client, message):
    config["report_chat_id"] = None
    save_config(config)
    if scheduler.get_job(AUTO_REPORT_JOB_ID):
        scheduler.remove_job(AUTO_REPORT_JOB_ID)
    await message.reply("✅ Авто-звіт вимкнено")

@app_kab.on_message(filters.command("setreporttime", prefixes="/") & filters.me)
async def handle_setreporttime(client, message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply(
            f"⏰ Поточний час авто-звіту: {config['report_time']}\n"
            f"Формат: `/setreporttime HH:MM` (напр. `/setreporttime 11:55`)"
        )
        return
    try:
        hh, mm = parts[1].strip().split(":")
        h, m = int(hh), int(mm)
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError
    except ValueError:
        await message.reply("❌ Невірний формат. Приклад: `/setreporttime 11:55`")
        return
    config["report_time"] = f"{h:02d}:{m:02d}"
    save_config(config)
    if scheduler.get_job(AUTO_REPORT_JOB_ID):
        scheduler.reschedule_job(AUTO_REPORT_JOB_ID, trigger="cron", hour=h, minute=m)
    await message.reply(f"✅ Час авто-звіту тепер {config['report_time']}")

@app_kab.on_message(filters.command("autoreport", prefixes="/") & filters.me)
async def handle_autoreport_status(client, message):
    chat_id = config.get("report_chat_id")
    rtime = config.get("report_time", DEFAULT_REPORT_TIME)
    enabled = chat_id is not None and scheduler.get_job(AUTO_REPORT_JOB_ID) is not None
    state = "✅ УВІМКНЕНО" if enabled else "❌ ВИМКНЕНО"
    chat_str = f"`{chat_id}`" if chat_id else "не задано"
    await message.reply(
        f"📋 **Стан авто-звіту**\n"
        f"Стан: {state}\n"
        f"Час: {rtime}\n"
        f"Чат: {chat_str}\n\n"
        f"Команди:\n"
        f"`/setreportchat` — встановити поточний чат\n"
        f"`/clearreportchat` — вимкнути\n"
        f"`/setreporttime HH:MM` — змінити час"
    )

async def main():
    migrate_db()
    try:
        await app_kab.start()
        await app_fab.start()
        hour, minute = map(int, config["baseline_time"].split(":"))
        scheduler.add_job(scheduled_task, "cron", hour=hour, minute=minute, id=SCHED_JOB_ID)
        if config.get("report_chat_id"):
            rh, rm = map(int, config["report_time"].split(":"))
            scheduler.add_job(auto_report_task, "cron", hour=rh, minute=rm, id=AUTO_REPORT_JOB_ID)
            print(f"📤 Авто-звіт активний: {config['report_time']} → чат {config['report_chat_id']}")
        scheduler.start()
        print(f"🚀 BOT IS LIVE! 2 аккаунти (КАБ+ФАБ), бейзлайн о {config['baseline_time']}, кулдаун: {DIFF_COOLDOWN_SECONDS//60} хв")
        await idle()
    finally:
        try:
            await app_kab.stop()
        except Exception as e:
            print(f"⚠️ Помилка зупинки КАБ: {e}")
        try:
            await app_fab.stop()
        except Exception as e:
            print(f"⚠️ Помилка зупинки ФАБ: {e}")

if __name__ == "__main__":
    app_kab.run(main())