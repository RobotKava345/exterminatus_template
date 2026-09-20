from aiogram import Router, types, Bot
from aiogram.filters import Command

from database.db import count_seen_users
from mtproto import sync_chat_members

router = Router()


@router.message(Command("stats"))
async def cmd_stats(message: types.Message, bot: Bot):
    # Статистика доступна любому участнику чата.

    tracked_count = await count_seen_users(message.chat.id)

    # Реальное общее число участников чата — берём напрямую из Telegram.
    try:
        real_count = await bot.get_chat_member_count(message.chat.id)
    except Exception:
        real_count = None

    lines = []

    if real_count is not None:
        lines.append(
            f"Всего участников чата (по данным Telegram): "
            f"<b>{real_count}</b>"
        )

    lines.append(
        f"Отслежено в базе: <b>{tracked_count}</b>"
    )

    if real_count is not None and real_count > tracked_count:
        lines.append(
            "\n<i>Есть расхождение — выполни /sync, чтобы подтянуть "
            "полный список участников через Telethon.</i>"
        )

    await message.reply("\n".join(lines))


@router.message(Command("sync"))
async def cmd_sync(message: types.Message, bot: Bot):
    # Синхронизация доступна любому участнику чата.

    status_message = await message.reply(
        "Синхронизация участников через Telethon..."
    )

    try:
        count = await sync_chat_members(message.chat.id)
    except Exception as e:
        return await status_message.edit_text(
            f"Ошибка синхронизации: {e}"
        )

    await status_message.edit_text(
        f"Синхронизация завершена. Обработано участников: <b>{count}</b>"
    )
