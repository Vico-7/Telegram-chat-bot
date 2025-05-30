import asyncpg
from typing import Optional, List, Dict, Tuple, Any
from dataclasses import dataclass
import datetime
import pytz
from contextlib import asynccontextmanager
from config import Config
from logger import logger
import asyncio

BEIJING_TZ = pytz.timezone("Asia/Shanghai")

# 消息模板（保持不变）
MESSAGE_TEMPLATES = {
    "db_initialized": "数据库初始化完成",
    "db_already_initialized": "数据库连接池已初始化，关闭现有连接池",
    "db_connection_failed": "数据库连接失败: {error}",
    "db_init_failed": "数据库初始化失败: {error}",
    "db_closed": "数据库连接池已关闭",
    "db_schema_updated": "验证表 schema 更新完成",
    "db_schema_update_failed": "验证表 schema 更新失败: {error}",
    "db_query_failed": "数据库查询失败: {error}",
    "db_unexpected_error": "数据库意外错误: {error}",
    "db_not_initialized": "数据库连接池未初始化",
    "db_cleared": "数据库已清空",
    "invalid_user_data": "无效的用户 ID 或昵称",
    "invalid_block_data": "无效的用户 ID 或拉黑原因",
}

@dataclass
class UserInfo:
    user_id: int
    nickname: str
    username: Optional[str]
    registration_time: datetime.datetime
    is_blocked: bool = False
    block_reason: Optional[str] = None
    block_time: Optional[datetime.datetime] = None

    @staticmethod
    def format(user: 'UserInfo', blocked: bool = False) -> str:
        info = (
            f"ID: {user.user_id}\n"
            f"昵称: {user.nickname}\n"
            f"Username: {'@' + user.username if user.username else '无'}\n"
            f"主页: tg://user?id={user.user_id}\n"
            f"注册时间: {user.registration_time.astimezone(BEIJING_TZ).strftime('%Y-%m-%d %H:%M')} UTC+8"
        )
        if blocked and user.block_time:
            info += f"\n拉黑时间: {user.block_time.astimezone(BEIJING_TZ).strftime('%Y-%m-%d %H:%M')} UTC+8"
        return info

@dataclass
class Verification:
    user_id: int
    question: str
    answer: float
    options: List[float]
    verified: bool
    verification_time: datetime.datetime
    error_count: int = 0
    message_id: Optional[int] = None

    def update(self, **kwargs):
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
            else:
                logger.debug(f"忽略无效字段更新: {key}")

class ConnectionContext:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self.conn: Optional[asyncpg.Connection] = None

    async def __aenter__(self) -> asyncpg.Connection:
        self.conn = await self.pool.acquire()
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        if self.conn:
            await self.pool.release(self.conn)

class Database:
    def __init__(self):
        self.pool: Optional[asyncpg.Pool] = None

    async def initialize(self):
        if self.pool:
            logger.info(MESSAGE_TEMPLATES["db_already_initialized"])
            await self.close()
        try:
            self.pool = await asyncpg.create_pool(
                min_size=Config.POOL_MIN,
                max_size=Config.POOL_MAX,
                **Config.DB_CONFIG
            )
            await self._init_tables()
            logger.info(MESSAGE_TEMPLATES["db_initialized"])
        except asyncpg.exceptions.ConnectionFailureError as e:
            logger.error(MESSAGE_TEMPLATES["db_connection_failed"].format(error=str(e)), exc_info=True)
            raise
        except Exception as e:
            logger.error(MESSAGE_TEMPLATES["db_init_failed"].format(error=str(e)), exc_info=True)
            raise

    async def close(self):
        if self.pool:
            await self.pool.close()
            self.pool = None
            logger.info(MESSAGE_TEMPLATES["db_closed"])

    async def _init_tables(self):
        async with self._acquire_connection() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    nickname TEXT NOT NULL,
                    username TEXT,
                    registration_time TIMESTAMP NOT NULL,
                    is_blocked BOOLEAN DEFAULT FALSE,
                    block_reason TEXT,
                    block_time TIMESTAMP
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS verification (
                    user_id BIGINT PRIMARY KEY,
                    question TEXT NOT NULL,
                    answer FLOAT NOT NULL,
                    options FLOAT[] NOT NULL CHECK (array_length(options, 1) = 4),
                    verified BOOLEAN DEFAULT FALSE,
                    verification_time TIMESTAMP NOT NULL,
                    error_count INTEGER NOT NULL DEFAULT 0,
                    message_id BIGINT
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    user_id BIGINT PRIMARY KEY,
                    last_message_time TIMESTAMP NOT NULL
                )
            """)
            # 添加索引
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_users_is_blocked ON users (is_blocked);
                CREATE INDEX IF NOT EXISTS idx_verification_verified ON verification (verified);
                CREATE INDEX IF NOT EXISTS idx_conversations_last_message_time ON conversations (last_message_time);
            """)
            try:
                await conn.execute("""
                    ALTER TABLE verification
                    ADD COLUMN IF NOT EXISTS message_id BIGINT
                """)
                logger.debug(MESSAGE_TEMPLATES["db_schema_updated"])
            except Exception as e:
                logger.debug(MESSAGE_TEMPLATES["db_schema_update_failed"].format(error=str(e)))

    def _normalize_datetime(self, dt: datetime.datetime) -> datetime.datetime:
        return dt.astimezone(pytz.UTC).replace(tzinfo=None) if dt.tzinfo else dt

    def _validate_params(self, params: Any) -> Tuple:
        if not isinstance(params, tuple):
            params = (params,)
        return tuple(self._normalize_datetime(p) if isinstance(p, datetime.datetime) else p for p in params)

    def _sanitize_params(self, params: Tuple) -> str:
        return str([p if not isinstance(p, str) else "[redacted]" for p in params])

    def _acquire_connection(self) -> ConnectionContext:
        if not self.pool:
            raise RuntimeError(MESSAGE_TEMPLATES["db_not_initialized"])
        return ConnectionContext(self.pool)

    async def execute(self, query: str, params: Any = (), fetch: Optional[str] = None, retries: int = 3, delay: float = 0.5) -> Any:
        for attempt in range(retries):
            async with self._acquire_connection() as conn:
                try:
                    normalized_params = self._validate_params(params)
                    logger.debug(f"执行查询: {query}", extra={"params": self._sanitize_params(normalized_params)})
                    if fetch == "one":
                        return await conn.fetchrow(query, *normalized_params)
                    elif fetch == "all":
                        return await conn.fetch(query, *normalized_params)
                    elif fetch == "val":
                        return await conn.fetchval(query, *normalized_params)
                    await conn.execute(query, *normalized_params)
                    return
                except asyncpg.exceptions.DeadlockDetectedError as e:
                    logger.warning(f"死锁检测到，重试 {attempt + 1}/{retries}: {str(e)}")
                    if attempt < retries - 1:
                        await asyncio.sleep(delay * (2 ** attempt))  # 指数退避
                    else:
                        logger.error(MESSAGE_TEMPLATES["db_query_failed"].format(error=str(e)), exc_info=True)
                        raise
                except asyncpg.exceptions.PostgresError as e:
                    logger.error(MESSAGE_TEMPLATES["db_query_failed"].format(error=str(e)), exc_info=True)
                    raise
                except Exception as e:
                    logger.error(MESSAGE_TEMPLATES["db_unexpected_error"].format(error=str(e)), exc_info=True)
                    raise

    @asynccontextmanager
    async def transaction(self):
        async with self._acquire_connection() as conn:
            transaction = conn.transaction()
            await transaction.start()
            try:
                yield conn
                await transaction.commit()
            except Exception:
                await transaction.rollback()
                raise

    async def add_user(self, user: UserInfo):
        if not isinstance(user.user_id, int) or not user.nickname:
            raise ValueError(MESSAGE_TEMPLATES["invalid_user_data"])
        await self.execute(
            """
            INSERT INTO users (user_id, nickname, username, registration_time)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (user_id) DO UPDATE
            SET nickname = EXCLUDED.nickname, username = EXCLUDED.username
            """,
            (user.user_id, user.nickname, user.username, self._normalize_datetime(user.registration_time))
        )

    async def block_user(self, user_id: int, reason: str):
        if not isinstance(user_id, int) or not reason:
            raise ValueError(MESSAGE_TEMPLATES["invalid_block_data"])
        async with self.transaction():
            await self.execute(
                """
                UPDATE users
                SET is_blocked = TRUE, block_reason = $1, block_time = $2
                WHERE user_id = $3
                """,
                (reason[:255], self._normalize_datetime(datetime.datetime.now(BEIJING_TZ)), user_id)
            )
            await self.execute(
                """
                UPDATE verification
                SET verified = FALSE, error_count = 0, message_id = NULL
                WHERE user_id = $1
                """,
                (user_id,)
            )
            logger.debug(f"User {user_id} blocked, verification state reset")

    async def unblock_user(self, user_id: int):
        if not isinstance(user_id, int):
            raise ValueError(MESSAGE_TEMPLATES["invalid_block_data"])
        async with self.transaction():
            try:
                # 检查用户是否存在
                user_exists = await self.execute(
                    "SELECT 1 FROM users WHERE user_id = $1",
                    (user_id,),
                    fetch="val"
                )
                if not user_exists:
                    logger.error(f"Cannot unblock user {user_id}: User does not exist")
                    raise ValueError("用户不存在")
                # 清除现有验证记录
                await self.execute(
                    "DELETE FROM verification WHERE user_id = $1",
                    (user_id,)
                )
                # 插入新的默认验证记录
                verification = Verification(
                    user_id=user_id,
                    question="",
                    answer=0.0,
                    options=[],
                    verified=False,
                    verification_time=self._normalize_datetime(datetime.datetime.now(BEIJING_TZ)),
                    error_count=0,
                    message_id=None
                )
                await self.add_verification(verification)
                # 更新用户拉黑状态
                await self.execute(
                    """
                    UPDATE users
                    SET is_blocked   = FALSE,
                        block_reason = NULL,
                        block_time   = NULL
                    WHERE user_id = $1
                    """,
                    (user_id,)
                )
                # 清除对话记录（确保不影响管理员的当前对话目标）
                await self.execute(
                    "DELETE FROM conversations WHERE user_id = $1",
                    (user_id,)
                )
                logger.info(f"User {user_id} unblocked, verification and conversation state reset")
            except Exception as e:
                logger.error(f"Error unblocking user {user_id}: {str(e)}", exc_info=True)
                raise

    async def is_blocked(self, user_id: int) -> bool:
        result = await self.execute(
            "SELECT is_blocked FROM users WHERE user_id = $1",
            (user_id,),
            fetch="val"
        )
        return bool(result)

    async def get_user_info(self, user_id: int) -> Optional[UserInfo]:
        result = await self.execute(
            "SELECT * FROM users WHERE user_id = $1",
            (user_id,),
            fetch="one"
        )
        return UserInfo(**result) if result else None

    async def get_recent_users(self) -> List[UserInfo]:
        results = await self.execute(
            """
            SELECT u.* FROM users u
            JOIN verification v ON u.user_id = v.user_id
            WHERE v.verified = TRUE
            ORDER BY v.verification_time DESC LIMIT 3
            """,
            fetch="all"
        )
        return [UserInfo(**r) for r in results]

    async def get_blacklist(self) -> List[UserInfo]:
        results = await self.execute(
            """
            SELECT * FROM users
            WHERE is_blocked = TRUE
            ORDER BY block_time DESC LIMIT 5
            """,
            fetch="all"
        )
        return [UserInfo(**r) for r in results]

    async def get_stats(self) -> Dict:
        result = await self.execute(
            """
            SELECT 
                COUNT(DISTINCT u.user_id) as total_users,
                COUNT(DISTINCT u.user_id) FILTER (WHERE u.registration_time::date = CURRENT_DATE) as new_users,
                COUNT(DISTINCT u.user_id) FILTER (WHERE u.is_blocked = TRUE) as blocked_users,
                COUNT(DISTINCT v.user_id) FILTER (WHERE v.verified = TRUE) as verified_users
            FROM users u
            LEFT JOIN verification v ON u.user_id = v.user_id
            """,
            fetch="one"
        )
        return dict(result)

    async def add_verification(self, verification: Verification):
        await self.execute(
            """
            INSERT INTO verification (
                user_id, question, answer, options, verified,
                verification_time, error_count, message_id
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (user_id) DO UPDATE
            SET question = EXCLUDED.question,
                answer = EXCLUDED.answer,
                options = EXCLUDED.options,
                verified = EXCLUDED.verified,
                verification_time = EXCLUDED.verification_time,
                error_count = EXCLUDED.error_count,
                message_id = EXCLUDED.message_id
            """,
            (
                verification.user_id,
                verification.question,
                verification.answer,
                verification.options,
                verification.verified,
                self._normalize_datetime(verification.verification_time),
                verification.error_count,
                verification.message_id
            )
        )

    async def update_verification(self, verification: Verification):
        await self.execute(
            """
            UPDATE verification
            SET question = $2, answer = $3, options = $4, verified = $5,
                verification_time = $6, error_count = $7, message_id = $8
            WHERE user_id = $1
            """,
            (
                verification.user_id,
                verification.question,
                verification.answer,
                verification.options,
                verification.verified,
                self._normalize_datetime(verification.verification_time),
                verification.error_count,
                verification.message_id
            )
        )

    async def verify_user(self, user_id: int):
        async with self.transaction():
            try:
                result = await self.execute(
                    """
                    UPDATE verification
                    SET verified = TRUE, verification_time = $2, message_id = NULL
                    WHERE user_id = $1
                    RETURNING verified
                    """,
                    (user_id, self._normalize_datetime(datetime.datetime.now(BEIJING_TZ))),
                    fetch="val"
                )
                # 检查更新是否成功
                if result is None or result == 0:
                    logger.error(f"Failed to verify user {user_id}: No verification record found")
                    raise ValueError("用户验证记录不存在")
                logger.debug(f"User {user_id} marked as verified in database")
                # 验证更新结果
                is_verified = await self.is_verified(user_id)
                if not is_verified:
                    logger.error(f"Verification state not updated for user {user_id} after verify_user")
                    raise RuntimeError("验证状态更新失败")
            except Exception as e:
                logger.error(f"Error verifying user {user_id}: {str(e)}", exc_info=True)
                raise

    async def is_verified(self, user_id: int) -> bool:
        try:
            result = await self.execute(
                """
                SELECT verified FROM verification
                WHERE user_id = $1
                """,
                (user_id,),
                fetch="val"
            )
            verified = bool(result) if result is not None else False
            logger.debug(f"Checked verification status for user {user_id}: verified={verified}")
            return verified
        except Exception as e:
            logger.error(f"Error checking verification status for user {user_id}: {str(e)}", exc_info=True)
            return False

    async def get_verification(self, user_id: int) -> Optional[Verification]:
        result = await self.execute(
            """
            SELECT * FROM verification
            WHERE user_id = $1
            """,
            (user_id,),
            fetch="one"
        )
        return Verification(**result) if result else None

    async def update_conversation(self, user_id: int):
        await self.execute(
            """
            INSERT INTO conversations (user_id, last_message_time)
            VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE
            SET last_message_time = EXCLUDED.last_message_time
            """,
            (user_id, self._normalize_datetime(datetime.datetime.now(BEIJING_TZ)))
        )

    async def clean_database(self):
        async with self.transaction() as conn:
            await conn.execute("DELETE FROM users")
            await conn.execute("DELETE FROM verification")
            await conn.execute("DELETE FROM conversations")
            logger.info(MESSAGE_TEMPLATES["db_cleared"])

    # 在Database类中添加get_verified_users方法
    async def get_verified_users(self) -> List[UserInfo]:
        results = await self.execute(
            """
            SELECT u.* FROM users u
            JOIN verification v ON u.user_id = v.user_id
            WHERE v.verified = TRUE
            ORDER BY v.verification_time DESC
            """,
            fetch="all"
        )
        return [UserInfo(**r) for r in results]