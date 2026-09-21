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

# Размер первой партии банов. Разбиение на партии (а не один
# монолитный прогон по всему списку) меняет "отпечаток" поведения
# бота для антиспам-эвристик Telegram: между партиями баны
# перемежаются другим типом активности (работа с ветками), а не
# идут одной сплошной серией одинаковых вызовов.
BAN_BATCH_SIZE = 50

# Порог подряд идущих неудачных банов (не FloodWait — тот
# по-прежнему обрабатывается отдельным ретраем внутри
# _ban_single_user). Если подряд накопилось столько ошибок —
# считаем, что Telegram начал системно резать действия бота
# в этом чате, и дальше долбить запросами только хуже.
MAX_CONSECUTIVE_FAILURES = 5


class BanCircuitBreaker:
    """
    Отслеживает подряд идущие неудачные попытки бана.

    Успешный бан сбрасывает счётчик подряд идущих неудач.
    Как только счётчик достигает `limit` — брейкер "срабатывает"
    (`tripped = True`), и вызывающий код должен прекратить
    дальнейшие попытки банить кого-либо.
    """

    def __init__(self, limit: int = MAX_CONSECUTIVE_FAILURES):
        self.limit = limit
        self.consecutive_failures = 0
        self.tripped = False

    def record_success(self):
        self.consecutive_failures = 0

    def record_failure(self) -> bool:
        """Возвращает True, если ИМЕННО ЭТА неудача взвела брейкер."""
        self.consecutive_failures += 1

        if self.consecutive_failures >= self.limit:
            self.tripped = True

        return self.tripped


async def _ban_single_user(
    bot: Bot,
    chat_id: int,
    user_id: int,
    throttle: AdaptiveThrottle,
) -> str:
    """
    Пытается забанить одного пользователя.

    Возвращает "banned" или "error".

    FloodWait (429) ретраится до 2 попыток — это единственный
    тип ошибки, для которого повтор имеет смысл, так как
    Telegram сам сообщает, сколько ждать. Все остальные ошибки
    (включая generic Exception) считаются финальными для этого
    пользователя и сразу уходят в "error" — без слепых ретраев.
    """
    attempts = 0

    while attempts < 2:
        try:
            await bot.ban_chat_member(
                chat_id=chat_id,
                user_id=user_id,
            )

            logger.info(
                "Пользователь %s исключён из чата %s",
                user_id,
                chat_id,
            )

            return "banned"

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

            return "error"

        except Exception as e:

            logger.exception(
                "Ошибка бана пользователя %s: %s",
                user_id,
                e,
            )

            return "error"

    # Обе попытки закончились FloodWait подряд — тоже финальная
    # неудача для этого пользователя.
    logger.warning(
        "user_id=%s: обе попытки бана закончились FloodWait подряд",
        user_id,
    )
    return "error"


async def _ban_batch(
    bot: Bot,
    chat_id: int,
    users_batch: list,
    admin_ids: set,
    throttle: AdaptiveThrottle,
    breaker: BanCircuitBreaker,
    counters: dict,
    status_msg: types.Message,
    total: int,
    status_update_every: int,
) -> bool:
    """
    Банит одну партию пользователей с учётом circuit breaker'а.

    counters — общий (сквозной между партиями) словарь со
    счётчиками processed/banned/skipped/errors, обновляется
    на месте.

    Возвращает True, если брейкер сработал и партия была
    прервана досрочно (в этом случае вызывающий код не должен
    запускать следующие фазы — ни оставшиеся баны, ни работу
    с ветками).
    """

    async def _update_progress():
        try:
            await status_msg.edit_text(
                "<b>ORDO INQUISITIONIS</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "<b>EXTERMINATUS</b>\n\n"
                "Статус: <b>ВЫПОЛНЕНИЕ</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Обработано: <b>{counters['processed']}/{total}</b>\n"
                f"Уничтожено: <b>{counters['banned']}</b>\n"
                f"Пропущено (админы): <b>{counters['skipped']}</b>\n"
                f"Ошибок: <b>{counters['errors']}</b>\n\n"
                "<i>«Огонь не гаснет, пока не сгорит вся ересь.»</i>"
            )
        except Exception:
            logger.debug(
                "Не удалось обновить промежуточный статус",
                exc_info=True,
            )

    for user_id in users_batch:
        counters["processed"] += 1

        if user_id in admin_ids:
            counters["skipped"] += 1

        else:
            result = await _ban_single_user(bot, chat_id, user_id, throttle)

            if result == "banned":
                counters["banned"] += 1
                throttle.on_success()
                breaker.record_success()
                await throttle.wait()

            else:
                counters["errors"] += 1
                tripped = breaker.record_failure()

                if tripped:
                    logger.error(
                        "Circuit breaker сработал: %s подряд идущих "
                        "неудачных банов подряд. Похоже, Telegram "
                        "ограничил действия бота в этом чате "
                        "(PeerFlood-подобное поведение). Партия и вся "
                        "операция прерваны.",
                        breaker.limit,
                    )
                    await _update_progress()
                    return True

        if counters["processed"] % status_update_every == 0:
            await _update_progress()

    return False


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

    # Тот, кто запустил команду, всегда исключается.
    admin_ids.add(message.from_user.id)

    # ============================================================
    # ПОЛУЧЕНИЕ ЗАРЕГИСТРИРОВАННЫХ ПОЛЬЗОВАТЕЛЕЙ
    # ============================================================

    users = await get_seen_users(chat_id)
    total = len(users)

    first_batch = users[:BAN_BATCH_SIZE]
    remaining_batch = users[BAN_BATCH_SIZE:]

    counters = {
        "processed": 0,
        "banned": 0,
        "skipped": 0,
        "errors": 0,
    }

    throttle = AdaptiveThrottle(base_delay=1.0, max_delay=8.0)
    breaker = BanCircuitBreaker()
    STATUS_UPDATE_EVERY = 25

    aborted = False

    # ============================================================
    # ФАЗА 1: ПЕРВАЯ ПАРТИЯ (до BAN_BATCH_SIZE человек)
    # ============================================================

    aborted = await _ban_batch(
        bot=bot,
        chat_id=chat_id,
        users_batch=first_batch,
        admin_ids=admin_ids,
        throttle=throttle,
        breaker=breaker,
        counters=counters,
        status_msg=status_msg,
        total=total,
        status_update_every=STATUS_UPDATE_EVERY,
    )

    topics_total = 0
    topics_deleted = 0
    topics_created = 0
    topics_errors = 0

    if not aborted:
        # Пауза между фазами (баны -> удаление веток), чтобы это
        # не выглядело одной непрерывной серией разрушительных
        # действий — и заодно даёт естественный "остыть" перед
        # следующей пачкой банов.
        await asyncio.sleep(3)

        # ========================================================
        # ФАЗА 2: УДАЛЕНИЕ ФОРУМНЫХ ВЕТОК
        # ========================================================

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

        await asyncio.sleep(3)

        # ========================================================
        # ФАЗА 3: ОСТАВШИЕСЯ ПОЛЬЗОВАТЕЛИ
        # ========================================================

        if remaining_batch:
            aborted = await _ban_batch(
                bot=bot,
                chat_id=chat_id,
                users_batch=remaining_batch,
                admin_ids=admin_ids,
                throttle=throttle,
                breaker=breaker,
                counters=counters,
                status_msg=status_msg,
                total=total,
                status_update_every=STATUS_UPDATE_EVERY,
            )

        if not aborted:
            await asyncio.sleep(3)

            # ====================================================
            # ФАЗА 4: СОЗДАНИЕ ЗАМЕНЯЮЩИХ ВЕТОК
            # ====================================================

            if topics_deleted > 0:
                try:
                    (
                        topics_created,
                        create_errors,
                    ) = await create_replacement_topics(
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
    else:
        logger.warning(
            "EXTERMINATUS прерван на первой партии банов — "
            "circuit breaker сработал до фазы с ветками."
        )

    # ============================================================
    # ФИНАЛЬНЫЙ ОТЧЁТ
    # ============================================================

    if keep_topic_id is None:
        kept_topic_text = "General / текущая тема не определена"
    elif keep_topic_id == 1:
        kept_topic_text = "General"
    else:
        kept_topic_text = f"ID {keep_topic_id}"

    if aborted:
        status_block = (
            "<b>СТАТУС ОПЕРАЦИИ</b>\n"
            "ПРЕРВАНО ДОСРОЧНО\n\n"
            f"Telegram начал системно отклонять баны после "
            f"{breaker.limit} неудач подряд — похоже, чат/бот "
            "временно ограничены на стороне Telegram. Ветки НЕ "
            "трогались. Рекомендуется подождать и повторить позже.\n\n"
        )
    else:
        status_block = (
            "<b>СТАТУС ОПЕРАЦИИ</b>\n"
            "ЗАВЕРШЕНО\n\n"
        )

    text = (
        "<b>ORDO INQUISITIONIS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<b>EXTERMINATUS — FINAL REPORT</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"

        + status_block +

        "<b>ФОРУМНЫЕ ВЕТКИ</b>\n\n"
        f"Найдено: <b>{topics_total}</b>\n"
        f"Удалено: <b>{topics_deleted}</b>\n"
        f"Создано веток «АВЕ ИМП»: <b>{topics_created}</b>\n"
        f"Ошибок: <b>{topics_errors}</b>\n"
        f"Сохранена ветка: <b>{kept_topic_text}</b>\n\n"

        "<b>ОПЕРАТИВНЫЕ ДАННЫЕ</b>\n\n"
        f"Зарегистрировано: <b>{total}</b>\n"
        f"Обработано: <b>{counters['processed']}</b>\n"
        f"Администраторов: <b>{counters['skipped']}</b>\n"
        f"Уничтожено: <b>{counters['banned']}</b>\n"
        f"Ошибок: <b>{counters['errors']}</b>\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n\n"

        + (
            "<b>СЕКТОР ОЧИЩЕН</b>\n\n"
            "Враждебные субъекты устранены.\n"
            "Форумные ветки обработаны.\n"
            "Протокол Exterminatus завершён.\n\n"
            if not aborted else
            "<b>ОТСТУПЛЕНИЕ</b>\n\n"
            "Орбитальная группа отозвана до\n"
            "прояснения обстановки.\n\n"
        )

        + "━━━━━━━━━━━━━━━━━━━━\n"
        "<i>«Из пепла рождается порядок.\n"
        "Из порядка — Империум.»</i>\n\n"

        "<b>СЛАВА ИМПЕРИУМУ ЧЕЛОВЕЧЕСТВА</b>"
    )

    # ============================================================
    # ОТПРАВКА ФИНАЛЬНОГО ОТЧЁТА
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