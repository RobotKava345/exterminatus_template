import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN")

# Публичный HTTPS-адрес мини-аппа (ngrok на этапе разработки,
# домен хостинга — в проде). Нужен для web_app-кнопки в ЛС.
MINIAPP_BASE_URL = os.environ.get("MINIAPP_BASE_URL", "")

# Строка подключения к Postgres (Render External Database URL).
# Формат: postgresql://user:password@host/dbname
DATABASE_URL = os.environ.get("DATABASE_URL")

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не найден. Создай файл .env рядом с bot.py "
        "и укажи в нём BOT_TOKEN=твой_токен"
    )

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL не найден. Добавь в .env строку вида:\n"
        "DATABASE_URL=postgresql://user:password@host/dbname"
    )
