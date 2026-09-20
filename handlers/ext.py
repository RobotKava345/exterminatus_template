import asyncio
import logging

from aiogram import Router, types, Bot
from aiogram.filters import Command, CommandObject
from aiogram.exceptions import (
    TelegramRetryAfter,
    TelegramForbiddenError,
    TelegramBadRequest,
)

from utils import get_admin_ids, AdaptiveThrottle
from database.db import get_seen_users
from forum_topics import delete_all_topics_except, create_replacement_topics


logger = logging.getLogger(__name__)
router = Router()


@router.message(Command("fire_exterminatus"))
async def cmd_ext(
    message: types.Message,
    command: CommandObject,
    bot: Bot,
):
    # ============================================================
    # ПРОВЕРКА ПОДТВЕРЖДЕНИЯ
    # ============================================================

    if not command.args or command.args.strip().lower() != "confirm":
        return await message.reply(
            "<b>ORDO INQUISITIONIS</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>EXTERMINATUS</b>\n\n"
            "Класс протокола: <b>EXTREMIS</b>\n"
            "Статус: <b>ОЖИДАНИЕ ПОДТВЕРЖДЕНИЯ</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "<b>ОБНАРУЖЕНА ЕРЕСЬ</b>\n\n"
            "Сектор признан заражённым.\n"
            "Инквизиция санкционирует полное\n"
            "очищение сектора.\n\n"
            "Все обнаруженные в реестре\n"
            "субъекты, кроме авторизованного\n"
            "персонала, будут подвергнуты\n"
            "окончательному изгнанию.\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>ДИРЕКТИВА</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Для активации протокола введите:\n"
            "<code>/fire_exterminatus confirm</code>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<i>«Ересь требует очищения.\n"
            "Очищение требует огня.»</i>"
        )

    # ============================================================
    # ОСНОВНЫЕ ДАННЫЕ
    # ============================================================

    chat_id = message.chat.id

    # ID форумной ветки, в которой была написана команда.
    #
    # Для обычного сообщения в General значение обычно будет None
    # или соответствующее Telegram-значение.
    #
    # Если команда написана внутри конкретной темы форума,
    # здесь будет её message_thread_id.
    keep_topic_id = message.message_thread_id

    logger.info(
        "Запущен EXTERMINATUS: chat_id=%s, keep_topic_id=%s",
        chat_id,
        keep_topic_id,
    )

    # ============================================================
    # СТАТУС ОПЕРАЦИИ
    # ============================================================

    status_msg = await message.reply(
        "<b>ORDO INQUISITIONIS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<b>EXTERMINATUS</b>\n\n"
        "Авторизация: <b>ПОДТВЕРЖДЕНА</b>\n"
        "Приоритет: <b>АБСОЛЮТНЫЙ</b>\n"
        "Статус: <b>ВЫПОЛНЕНИЕ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Орбитальная группа возмездия\n"
        "выходит на позицию.\n\n"
        "Сканирование реестра...\n"
        "Идентификация субъектов...\n"
        "Синхронизация форумных веток...\n\n"
        "<b>ОГОНЬ РАЗРЕШЁН.</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<i>«Во имя Императора.»</i>"
    )

    # ============================================================
    # АДМИНИСТРАТОРЫ
    # ============================================================

    admin_ids = await get_admin_ids(
        bot,
        chat_id,
    )

    # Сам бот также должен быть исключён из бана.
    try:
        me = await bot.get_me()
        admin_ids.add(me.id)
    except Exception:
        logger.exception("Не удалось получить ID бота")

    # Тот, кто запустил команду, всегда исключается — даже если
    # Telegram формально не считает его админом чата (например,
    # права выданы только через внутреннюю RBAC/БД, а не через
    # реальный статус администратора).
    admin_ids.add(message.from_user.id)

    # ============================================================
    # ПОЛУЧЕНИЕ ЗАРЕГИСТРИРОВАННЫХ ПОЛЬЗОВАТЕЛЕЙ
    # ============================================================

    users = await get_seen_users(chat_id)

    total = len(users)
    banned = 0
    skipped = 0
    errors = 0

    # ============================================================
    # ФАЗА 1: БАН ПОЛЬЗОВАТЕЛЕЙ
    # ============================================================

    throttle = AdaptiveThrottle(base_delay=1.0, max_delay=8.0)
    STATUS_UPDATE_EVERY = 25

    async def _update_progress(processed: int):
        """
        Промежуточный апдейт статус-сообщения — best-effort,
        без ретраев: если не получилось, просто пропускаем,
        это не критично для самой операции.
        """
        try:
            await status_msg.edit_text(
                "<b>ORDO INQUISITIONIS</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "<b>EXTERMINATUS</b>\n\n"
                "Статус: <b>ВЫПОЛНЕНИЕ</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Обработано: <b>{processed}/{total}</b>\n"
                f"Уничтожено: <b>{banned}</b>\n"
                f"Пропущено (админы): <b>{skipped}</b>\n"
                f"Ошибок: <b>{errors}</b>\n\n"
                "<i>«Огонь не гаснет, пока не сгорит вся ересь.»</i>"
            )
        except Exception:
            logger.debug("Не удалось обновить промежуточный статус", exc_info=True)

    for i, user_id in enumerate(users, start=1):

        # Администраторов и самого бота не трогаем.
        if user_id in admin_ids:
            skipped += 1
            continue

        success = False
        attempts = 0

        while attempts < 2 and not success:

            try:
                await bot.ban_chat_member(
                    chat_id=chat_id,
                    user_id=user_id,
                )

                banned += 1
                success = True

                logger.info(
                    "Пользователь %s исключён из чата %s",
                    user_id,
                    chat_id,
                )

            except TelegramRetryAfter as e:
                wait = e.retry_after

                logger.warning(
                    "FloodWait %s секунд на user_id=%s",
                    wait,
                    user_id,
                )

                throttle.on_flood_wait(wait)
                await asyncio.sleep(wait)
                attempts += 1

            except (
                TelegramForbiddenError,
                TelegramBadRequest,
            ) as e:

                logger.warning(
                    "Не удалось исключить user_id=%s: %s",
                    user_id,
                    e,
                )

                errors += 1
                break

            except Exception as e:

                logger.exception(
                    "Ошибка бана пользователя %s: %s",
                    user_id,
                    e,
                )

                errors += 1
                break

        # Если обе попытки закончились FloodWait.
        if not success and attempts >= 2:
            errors += 1

        if success:
            throttle.on_success()

        # Адаптивная пауза между операциями: базово небольшая,
        # растёт только при реальном FloodWait и потом плавно
        # возвращается к базовой.
        if success:
            await throttle.wait()

        if i % STATUS_UPDATE_EVERY == 0:
            await _update_progress(i)

    # Пауза между фазами (баны -> удаление веток), чтобы это не
    # выглядело одной непрерывной серией разрушительных действий.
    await asyncio.sleep(3)

    # ============================================================
    # ФАЗА 2: УДАЛЕНИЕ ФОРУМНЫХ ВЕТОК
    # ============================================================

    topics_total = 0
    topics_deleted = 0
    topics_created = 0
    topics_errors = 0

    try:
        (
            topics_total,
            topics_deleted,
            topics_errors,
        ) = await delete_all_topics_except(
            bot=bot,
            chat_id=chat_id,
            keep_topic_id=keep_topic_id,
        )

        logger.info(
            "Ветки удалены: всего=%s, удалено=%s, ошибок=%s",
            topics_total,
            topics_deleted,
            topics_errors,
        )

    except Exception as e:
        logger.exception(
            "Ошибка удаления форумных веток: %s",
            e,
        )

        topics_errors += 1

    # Пауза между фазами (удаление -> создание заменяющих веток).
    await asyncio.sleep(3)

    # ============================================================
    # ФАЗА 3: СОЗДАНИЕ ЗАМЕНЯЮЩИХ ВЕТОК
    # ============================================================

    if topics_deleted > 0:
        try:
            topics_created, create_errors = await create_replacement_topics(
                bot=bot,
                chat_id=chat_id,
                count=topics_deleted,
            )

            topics_errors += create_errors

            logger.info(
                "Заменяющие ветки созданы: создано=%s",
                topics_created,
            )

        except Exception as e:
            logger.exception(
                "Ошибка создания заменяющих веток: %s",
                e,
            )

            topics_errors += 1

    # ============================================================
    # ФИНАЛЬНЫЙ ОТЧЁТ
    # ============================================================

    if keep_topic_id is None:
        kept_topic_text = "General / текущая тема не определена"
    elif keep_topic_id == 1:
        kept_topic_text = "General"
    else:
        kept_topic_text = f"ID {keep_topic_id}"

    text = (
        "<b>ORDO INQUISITIONIS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<b>EXTERMINATUS — FINAL REPORT</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"

        "<b>СТАТУС ОПЕРАЦИИ</b>\n"
        "ЗАВЕРШЕНО\n\n"

        "<b>ФОРУМНЫЕ ВЕТКИ</b>\n\n"
        f"Найдено: <b>{topics_total}</b>\n"
        f"Удалено: <b>{topics_deleted}</b>\n"
        f"Создано веток «АВЕ ИМП»: <b>{topics_created}</b>\n"
        f"Ошибок: <b>{topics_errors}</b>\n"
        f"Сохранена ветка: <b>{kept_topic_text}</b>\n\n"

        "<b>ОПЕРАТИВНЫЕ ДАННЫЕ</b>\n\n"
        f"Зарегистрировано: <b>{total}</b>\n"
        f"Администраторов: <b>{skipped}</b>\n"
        f"Уничтожено: <b>{banned}</b>\n"
        f"Ошибок: <b>{errors}</b>\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n\n"

        "<b>СЕКТОР ОЧИЩЕН</b>\n\n"
        "Враждебные субъекты устранены.\n"
        "Форумные ветки обработаны.\n"
        "Протокол Exterminatus завершён.\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n"
        "<i>«Из пепла рождается порядок.\n"
        "Из порядка — Империум.»</i>\n\n"

        "<b>СЛАВА ИМПЕРИУМУ ЧЕЛОВЕЧЕСТВА</b>"
    )

    # ============================================================
    # ОБНОВЛЕНИЕ СТАТУСА
    # ============================================================

    async def _send_final_report():
        """
        Пытается отправить финальный отчёт с учётом FloodWait.
        Сначала пробует отредактировать статус-сообщение,
        при неудаче — отправляет новое.
        """

        attempts = 0
        max_attempts = 5

        while attempts <= max_attempts:
            try:
                await status_msg.edit_text(text)
                return

            except TelegramRetryAfter as e:
                attempts += 1

                logger.warning(
                    "FloodWait при edit_text: ожидание %s сек "
                    "(%s/%s)",
                    e.retry_after,
                    attempts,
                    max_attempts,
                )

                await asyncio.sleep(e.retry_after + 1)

            except Exception as e:
                logger.warning(
                    "Не удалось изменить статус-сообщение: %s",
                    e,
                )
                break

        # Если редактирование не удалось — пробуем отправить
        # новое сообщение, тоже с учётом FloodWait.
        attempts = 0

        while attempts <= max_attempts:
            try:
                await message.answer(text)
                return

            except TelegramRetryAfter as e:
                attempts += 1

                logger.warning(
                    "FloodWait при answer: ожидание %s сек "
                    "(%s/%s)",
                    e.retry_after,
                    attempts,
                    max_attempts,
                )

                await asyncio.sleep(e.retry_after + 1)

            except Exception:
                logger.exception(
                    "Не удалось отправить финальный отчёт"
                )
                return

        logger.error(
            "Не удалось отправить финальный отчёт "
            "после нескольких попыток FloodWait"
        )

    await _send_final_report()
