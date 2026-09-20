import logging
import os

from dotenv import load_dotenv
from telethon import TelegramClient

from database.db import add_user, get_seen_users


load_dotenv()

API_ID = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")

logger = logging.getLogger(__name__)

client = TelegramClient(
    "telegram_session",
    API_ID,
    API_HASH,
)


# Чаты, которые /sync умеет обходить пачкой (sync_all_chats).
# Ключ — chat_id (число), значение — просто человекочитаемое имя
# для логов. Отредактируй под свои чаты.
SYNC_CHATS = {
    # -1001234567890: "Мой чат",
}


async def sync_chat_members(chat_id: int) -> int:
    """
    Синхронизирует участников указанного Telegram-чата
    с таблицей seen_users.

    Возвращает количество обработанных пользователей.

    После синхронизации отдельно проверяет через БД,
    какие из полученных user_id действительно присутствуют
    в seen_users, и выводит результат в консоль/лог.
    """

    chat = await client.get_entity(chat_id)

    title = getattr(chat, "title", str(chat_id))

    logger.info(
        "Начинается синхронизация чата '%s' (%s)",
        title,
        chat_id,
    )

    count = 0
    synced_user_ids = []

    async for user in client.iter_participants(chat):
        if user.bot:
            continue

        user_id = user.id
        synced_user_ids.append(user_id)

        await add_user(
            chat_id=chat_id,
            user_id=user_id,
        )

        count += 1

    # ============================================================
    # ОТДЕЛЬНАЯ ПРОВЕРКА БАЗЫ ДАННЫХ
    # ============================================================

    logger.info(
        "Проверка наличия синхронизированных пользователей в БД..."
    )

    db_user_ids = set(await get_seen_users(chat_id))
    synced_user_ids_set = set(synced_user_ids)

    found_user_ids = synced_user_ids_set & db_user_ids
    missing_user_ids = synced_user_ids_set - db_user_ids

    logger.info(
        "ПРОВЕРКА БД для '%s' (%s):",
        title,
        chat_id,
    )

    logger.info(
        "  Получено через Telethon: %d",
        len(synced_user_ids_set),
    )

    logger.info(
        "  Найдено в БД: %d",
        len(found_user_ids),
    )

    logger.info(
        "  НЕ найдено в БД: %d",
        len(missing_user_ids),
    )

    if missing_user_ids:
        logger.warning(
            "User ID отсутствуют в БД: %s",
            sorted(missing_user_ids),
        )
    else:
        logger.info(
            "✅ Все синхронизированные пользователи найдены в БД."
        )

    logger.info(
        "Синхронизация '%s' завершена. Обработано: %d",
        title,
        count,
    )

    return count


async def sync_all_chats():
    """
    Синхронизирует все чаты из SYNC_CHATS.
    """

    for chat_id, title in SYNC_CHATS.items():
        try:
            await sync_chat_members(chat_id)

        except Exception:
            logger.exception(
                "Ошибка синхронизации чата '%s' (%s)",
                title,
                chat_id,
            )
