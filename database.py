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
            info += f"\n拉黑原因: {user.block_reason or '无'}"
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

class Database:
    def __init__(self):
        self.pool: Optional[asyncpg.Pool] = None

    async def initialize(self):
        """初始化数据库连接池并创建表结构。"""
        if self.pool:
            logger.info(MESSAGE_TEMPLATES["db_already_initialized"])
            await self.close()
        try:
            # 优化：设置连接超时和命令超时
            self.pool = await asyncpg.create_pool(
                min_size=Config.POOL_MIN,
                max_size=Config.POOL_MAX,
                command_timeout=10,  # 每个命令最大执行时间10秒
                server_settings={"tcp_keepalives_idle": "300"},  # 优化TCP连接保持
                **Config.DB_CONFIG
            )
            await self._init_tables()
            logger.info(MESSAGE_TEMPLATES["db_initialized"])
        except asyncpg.exceptions.ConnectionFailureError as e:
            logger.error(MESSAGE_TEMPLATES["db_connection_failed"].format(error=str(e)))
            raise
        except Exception as e:
            logger.error(MESSAGE_TEMPLATES["db_init_failed"].format(error=str(e)))
            raise

    async def close(self):
        """关闭数据库连接池。"""
        if self.pool:
            await self.pool.close()
            self.pool = None
            logger.info(MESSAGE_TEMPLATES["db_closed"])

    async def _init_tables(self):
        """初始化数据库表结构和索引。"""
        async with self._acquire_connection() as conn:
            async with conn.transaction():
                # 创建 users 表
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
                # 创建 verification 表
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
                # 创建 conversations 表
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS conversations (
                        user_id BIGINT PRIMARY KEY,
                        last_message_time TIMESTAMP NOT NULL
                    )
                """)
                # 创建 settings 表
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS settings (
                        setting_key TEXT PRIMARY KEY,
                        setting_value BOOLEAN,
                        verification_difficulty INTEGER
                    )
                """)
                # 添加缺失的列
                await conn.execute("""
                    ALTER TABLE settings
                    ADD COLUMN IF NOT EXISTS verification_difficulty INTEGER
                """)
                # 创建索引
                await conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_users_is_blocked ON users (is_blocked);
                    CREATE INDEX IF NOT EXISTS idx_verification_verified ON verification (verified);
                    CREATE INDEX IF NOT EXISTS idx_conversations_last_message_time ON conversations (last_message_time);
                """)
                # 更新 users 表 schema
                await conn.execute("""
                    ALTER TABLE users
                    ADD COLUMN IF NOT EXISTS block_reason TEXT,
                    ADD COLUMN IF NOT EXISTS block_time TIMESTAMP
                """)
                # 更新 verification 表 schema
                await conn.execute("""
                    ALTER TABLE verification
                    ADD COLUMN IF NOT EXISTS message_id BIGINT
                """)
                # 检查 settings 表是否已有记录
                existing_settings = await conn.fetchval("""
                    SELECT COUNT(*) FROM settings WHERE setting_key = $1
                """, "verification_enabled")
                if existing_settings == 0:
                    # 仅在 settings 表为空时插入默认值
                    await conn.execute("""
                        INSERT INTO settings (setting_key, setting_value, verification_difficulty)
                        VALUES ('verification_enabled', TRUE, 2)
                    """)
                logger.debug(MESSAGE_TEMPLATES["db_schema_updated"])

    async def get_verification_enabled(self) -> bool:
        """获取人机验证开关状态，默认开启。"""
        try:
            result = await self.execute(
                "SELECT setting_value FROM settings WHERE setting_key = $1",
                "verification_enabled",
                fetch="val"
            )
            return bool(result) if result is not None else True
        except Exception as e:
            logger.error(f"获取验证开关状态失败: {str(e)}")
            return True

    async def set_verification_enabled(self, enabled: bool):
        """设置人机验证开关状态。"""
        try:
            await self.execute(
                """
                INSERT INTO settings (setting_key, setting_value)
                VALUES ($1, $2)
                ON CONFLICT (setting_key) DO UPDATE
                SET setting_value = EXCLUDED.setting_value
                """,
                ("verification_enabled", enabled)
            )
            logger.debug(f"验证开关设置为 {enabled}")
        except Exception as e:
            logger.error(f"设置验证开关失败: {str(e)}")
            raise

    async def get_verification_difficulty(self) -> int:
        """获取人机验证难度等级，默认简单（1）。"""
        try:
            result = await self.execute(
                "SELECT verification_difficulty FROM settings WHERE setting_key = $1",
                "verification_enabled",
                fetch="val"
            )
            return result if result is not None else 1  # 默认返回简单难度
        except Exception as e:
            logger.error(f"获取验证难度等级失败: {str(e)}")
            return 1  # 出错时返回默认简单难度

    async def set_verification_difficulty(self, difficulty: int):
        """设置人机验证难度等级。"""
        if difficulty not in [1, 2, 3]:
            raise ValueError("无效的难度等级，必须为1（简单）、2（中等）或3（困难）")
        try:
            await self.execute(
                """
                UPDATE settings
                SET verification_difficulty = $1
                WHERE setting_key = $2
                """,
                (difficulty, "verification_enabled")
            )
            logger.debug(f"验证难度等级设置为 {difficulty}")
        except Exception as e:
            logger.error(f"设置验证难度等级失败: {str(e)}")
            raise

    @staticmethod
    def _normalize_datetime(dt: datetime.datetime) -> datetime.datetime:
        """规范化时间格式为UTC无时区信息。"""
        return dt.astimezone(pytz.UTC).replace(tzinfo=None) if dt.tzinfo else dt

    def _validate_params(self, params: Any) -> Tuple:
        """验证和规范化查询参数。"""
        if not isinstance(params, tuple):
            params = (params,)
        return tuple(self._normalize_datetime(p) if isinstance(p, datetime.datetime) else p for p in params)

    def _acquire_connection(self) -> asyncpg.Connection:
        """获取数据库连接。"""
        if not self.pool:
            raise RuntimeError(MESSAGE_TEMPLATES["db_not_initialized"])
        return self.pool.acquire()

    async def execute(self, query: str, params: Any = (), fetch: Optional[str] = None, retries: int = 3, delay: float = 0.5) -> Any:
        """执行数据库查询，支持重试机制。"""
        params = self._validate_params(params)
        for attempt in range(retries):
            async with self._acquire_connection() as conn:
                try:
                    if fetch == "one":
                        return await conn.fetchrow(query, *params)
                    elif fetch == "all":
                        return await conn.fetch(query, *params)
                    elif fetch == "val":
                        return await conn.fetchval(query, *params)
                    await conn.execute(query, *params)
                    return
                except asyncpg.exceptions.DeadlockDetectedError as e:
                    if attempt < retries - 1:
                        await asyncio.sleep(delay * (2 ** attempt))
                        continue
                    logger.error(MESSAGE_TEMPLATES["db_query_failed"].format(error=str(e)))
                    raise
                except asyncpg.exceptions.PostgresError as e:
                    logger.error(MESSAGE_TEMPLATES["db_query_failed"].format(error=str(e)))
                    raise
                except Exception as e:
                    logger.error(MESSAGE_TEMPLATES["db_unexpected_error"].format(error=str(e)))
                    raise

    @asynccontextmanager
    async def transaction(self):
        """事务上下文管理器。"""
        async with self._acquire_connection() as conn:
            async with conn.transaction():
                yield conn

    async def add_user(self, user: UserInfo):
        """添加或更新用户信息。"""
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
        """拉黑用户并重置验证状态。"""
        if not isinstance(user_id, int) or not reason:
            raise ValueError(MESSAGE_TEMPLATES["invalid_block_data"])
        async with self.transaction():
            now = self._normalize_datetime(datetime.datetime.now(BEIJING_TZ))
            await self.execute(
                """
                UPDATE users
                SET is_blocked = TRUE, block_reason = $1, block_time = $2
                WHERE user_id = $3
                """,
                (reason[:255], now, user_id)
            )
            await self.execute(
                """
                UPDATE verification
                SET verified = FALSE, error_count = 0, message_id = NULL
                WHERE user_id = $1
                """,
                user_id
            )
            logger.debug(f"用户 {user_id} 已拉黑，验证状态已重置")

    async def unblock_user(self, user_id: int):
        """解除用户拉黑状态并重置验证和对话记录。"""
        if not isinstance(user_id, int):
            raise ValueError(MESSAGE_TEMPLATES["invalid_block_data"])
        async with self.transaction():
            user_exists = await self.execute(
                "SELECT 1 FROM users WHERE user_id = $1",
                user_id,
                fetch="val"
            )
            if not user_exists:
                logger.error(f"无法解除拉黑，用户 {user_id} 不存在")
                raise ValueError("用户不存在")
            await self.execute(
                "DELETE FROM verification WHERE user_id = $1",
                user_id
            )
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
            await self.execute(
                """
                UPDATE users
                SET is_blocked = FALSE, block_reason = NULL, block_time = NULL
                WHERE user_id = $1
                """,
                user_id
            )
            await self.execute(
                "DELETE FROM conversations WHERE user_id = $1",
                user_id
            )
            logger.info(f"用户 {user_id} 已解除拉黑，验证和对话状态已重置")

    async def is_blocked(self, user_id: int) -> bool:
        """检查用户是否被拉黑。"""
        result = await self.execute(
            "SELECT is_blocked FROM users WHERE user_id = $1",
            user_id,
            fetch="val"
        )
        return bool(result)

    async def get_user_info(self, user_id: int) -> Optional[UserInfo]:
        """获取用户信息。"""
        result = await self.execute(
            "SELECT * FROM users WHERE user_id = $1",
            user_id,
            fetch="one"
        )
        return UserInfo(**result) if result else None

    async def get_recent_users(self) -> List[UserInfo]:
        """获取最近验证的用户（最多3个）。"""
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
        """获取黑名单用户（最多5个）。"""
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
        """获取用户统计信息。"""
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
        """添加或更新验证记录。"""
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
        """更新验证记录。"""
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
        """标记用户为已验证。"""
        async with self.transaction():
            now = self._normalize_datetime(datetime.datetime.now(BEIJING_TZ))
            result = await self.execute(
                """
                UPDATE verification
                SET verified = TRUE, verification_time = $2, message_id = NULL
                WHERE user_id = $1
                RETURNING verified
                """,
                (user_id, now),
                fetch="val"
            )
            if result is None:
                logger.error(f"验证用户 {user_id} 失败：无验证记录")
                raise ValueError("用户验证记录不存在")
            logger.debug(f"用户 {user_id} 已标记为已验证")

    async def is_verified(self, user_id: int) -> bool:
        """检查用户是否已验证。"""
        result = await self.execute(
            "SELECT verified FROM verification WHERE user_id = $1",
            user_id,
            fetch="val"
        )
        return bool(result) if result is not None else False

    async def get_verification(self, user_id: int) -> Optional[Verification]:
        """获取用户验证记录。"""
        result = await self.execute(
            "SELECT * FROM verification WHERE user_id = $1",
            user_id,
            fetch="one"
        )
        return Verification(**result) if result else None

    async def update_conversation(self, user_id: int):
        """更新用户对话时间。"""
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
        """清除数据库所有数据。"""
        async with self.transaction():
            await self.execute("DELETE FROM users")
            await self.execute("DELETE FROM verification")
            await self.execute("DELETE FROM conversations")
            logger.info(MESSAGE_TEMPLATES["db_cleared"])

    async def get_verified_users(self) -> List[UserInfo]:
        """获取所有已验证用户。"""
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
