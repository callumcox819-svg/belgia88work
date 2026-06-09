# Belgium Bot (Narkologia)

Отдельный Telegram-бот для рассылки по Бельгии. Основа — `finland-bot` / `happy88-main`, адаптирована под команду **Narkologia** и GOO Network.

## Регион

- Страна: **Бельгия (BE)**
- Площадка: **2dehands.be**
- Команда GOO: **Narkologia**
- Сервисы API: `2dehands_be`, `bpost_be`
- HTML-шаблоны: `data/HTMLbe/`

## Переменные окружения

| Переменная | Описание |
|---|---|
| `BOT_TOKEN` | Telegram Bot API token |
| `ADMIN_IDS` | ID админов через запятую |
| `DATABASE_URL` | Postgres (Railway) или пусто → SQLite локально |
| `NARKOLOGIA_TEAM_API_KEY` | Токен команды (X-Team-Key) из бота Narkologia → Профиль → API |
| `GOO_API_BASE` | По умолчанию `https://api-old.goo.network` |

Личный API key каждый пользователь задаёт в боте: ⚙️ → 🔑 → «Личный API key» (поле «Ваш токен» в Narkologia).

## Локальный запуск

```bash
cd C:\Users\user\Projects\belgium-bot
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
# заполнить BOT_TOKEN и при необходимости ADMIN_IDS
python bot.py
```

## Деплой

См. `railway.toml` и `RAILWAY_IMAP_WORKER.txt` (как у finland-bot).
