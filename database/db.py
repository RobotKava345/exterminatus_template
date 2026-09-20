import logging

from database.pool import get_pool

logger = logging.getLogger(__name__)


# ============================================================
# DATABASE INITIALIZATION
# ============================================================

async def init_db():
    """
    Создаёт недостающие таблицы. Не удаляет существующие данные.

    ВАЖНО: перед вызовом должен быть инициализирован пул
    (database.pool.init_pool()).
    """

    pool = get_pool()

    async with pool.acquire() as db:
        # ====================================================
        # SEEN USERS
        # ====================================================
        await db.execute("""
            CREATE TABLE IF NOT EXISTS seen_users (
                chat_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                first_seen TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, user_id)
            )
        """)

        await db.execute("CREATE INDEX IF NOT EXISTS idx_seen_users_chat ON seen_users(chat_id)")

    logger.info("Структура базы данных инициализирована (Postgres)")


# ============================================================
# SEEN USERS
# ============================================================

async def add_user(chat_id: int, user_id: int):
    pool = get_pool()
    try:
        await pool.execute(
            """
            INSERT INTO seen_users (chat_id, user_id)
            VALUES ($1, $2)
            ON CONFLICT (chat_id, user_id) DO NOTHING
            """,
            chat_id, user_id,
        )
    except Exception as e:
        logger.error(f"Ошибка записи пользователя {user_id} в чат {chat_id}: {e}")


async def get_seen_users(chat_id: int) -> list[int]:
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT user_id FROM seen_users WHERE chat_id = $1",
        chat_id,
    )
    return [row["user_id"] for row in rows]


async def count_seen_users(chat_id: int) -> int:
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT COUNT(*) AS cnt FROM seen_users WHERE chat_id = $1",
        chat_id,
    )
    return row["cnt"] if row else 0
