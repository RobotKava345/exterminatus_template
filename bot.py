import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from config import BOT_TOKEN
from database.pool import init_pool, close_pool
from database.db import init_db

from mtproto import client as telethon_client

from handlers import ext, stats, tracking

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger(__name__)

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

# Порядок подключения важен: команды (ext, stats) должны быть выше
# tracking, чтобы отслеживание сообщений не перехватывало апдейт
# раньше нужного хендлера команды.
dp.include_router(ext.router)
dp.include_router(stats.router)
dp.include_router(tracking.router)


async def main():
    # 1. Пул соединений к Postgres — должен быть готов
    #    до любого обращения к database/*.py
    await init_pool()
    logger.info("Пул соединений к Postgres создан")

    # 2. Структура таблиц
    await init_db()

    # 3. Telethon-клиент — нужен постоянно подключённым, пока бот
    #    работает: forum_topics.py (используется в /exterminatus)
    #    и mtproto.sync_chat_members (используется в /sync)
    #    обращаются к нему в реальном времени.
    await telethon_client.start()
    logger.info("Telethon-клиент подключён")

    logger.info("Админ-бот запущен")

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=["message", "chat_member", "callback_query"])
    finally:
        await telethon_client.disconnect()
        await close_pool()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
