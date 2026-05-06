# BeeBoss

Telegram userbot, який збирає рейтинги гри **Острог Маг**: загальний рейтинг гільдій і рейтинг учасників своєї гільдії. О заданому часі знімає бейзлайн, а на команду — порівнює і присилає звіт.

## Команди

- `/diff` — приріст слави гільдій з останнього бейзлайну
- `/chlen` — приріст слави гільдійців твоєї гільдії
- `/settime HH:MM` — змінити час щоденного бейзлайну (тільки твої повідомлення)

Кулдаун `/diff` і `/chlen` — 5 хвилин на кожну команду.

## Установка

Потрібен **Python 3.12** (Pyrogram несумісний з 3.14).

```bash
git clone <url> beeboss && cd beeboss
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# відкрий .env і встав свої api_id/api_hash з https://my.telegram.org
.venv/bin/python main.py
```

При першому запуску Pyrogram запитає номер телефону + код з Telegram. Після цього з'явиться файл `my_account.session` — наступні запуски без авторизації.

## Файли

- `main.py` — увесь код
- `config.json` — час бейзлайну (створюється автоматично)
- `global_guild_stats.db` — SQLite з історією бейзлайнів
- `my_account.session` — сесія Telegram (**ніколи не коміть!**)
- `.env` — `api_id`, `api_hash` (**ніколи не коміть!**)

## Безпека

`.env` і `*.session` у `.gitignore`. Якщо випадково запушив — **негайно** перегенеруй `api_id/api_hash` на https://my.telegram.org.
