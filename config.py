import aiofiles
import yaml
import asyncio
from typing import Dict, Optional
from logger import logger, configure_file_logging

# 默认消息模板（用于配置加载的错误消息）
DEFAULT_MESSAGE_TEMPLATES = {
    "config_loaded": "配置加载完成: {log_file}",
    "config_failed": "加载 config.yaml 失败: {error}",
    "missing_config_key": "缺少必要的配置项: {key}",
    "invalid_bot_token": "bot_token 必须为非空字符串",
    "invalid_admin_id": "admin_id 必须为非空整数",
    "invalid_webhook_url": "webhook_url 必须为非空字符串",
    "invalid_webhook_port": "webhook_port 必须在 1-65535 之间，当前值: {port}",
    "invalid_secret_token": "secret_token 如果提供必须为字符串",
    "invalid_log_file": "log_file 必须为非空字符串",
    "missing_db_key": "缺少数据库配置项: {key}",
    "invalid_db_port": "数据库端口必须为 1-65535 之间的整数，当前值: {port}",
    "invalid_timeout": "{timeout_type} 必须为正整数",
    "invalid_pool_min": "pool_min 必须为正整数",
    "invalid_pool_max": "pool_max 必须为正整数",
    "invalid_pool_range": "pool_min 不能大于 pool_max",
    # 从 config.yaml 加载的消息模板
    "logging_configured": "日志配置完成",
    "db_initialized": "数据库初始化完成",
    "db_timeout": "数据库初始化超时",
    "db_failed": "数据库初始化失败: {error}",
    "app_initialized": "Telegram 应用初始化完成",
    "commands_set": "机器人命令设置完成",
    "webhook_started": "Webhook 启动成功，端口: {port}",
    "webhook_timeout": "Webhook 启动尝试 {attempt}/3 超时",
    "webhook_failed": "Webhook 启动尝试 {attempt}/3 失败: {error}",
    "webhook_failed_final": "Webhook 启动失败，重试次数耗尽",
    "shutdown_initiated": "开始关闭程序",
    "updater_stopped": "Updater 已停止",
    "app_shutdown": "应用已关闭",
    "db_closed": "数据库连接已关闭",
    "shutdown_timeout": "关闭程序超时",
    "shutdown_failed": "关闭程序失败: {error}",
    "program_terminated": "程序被用户终止",
    "fatal_error": "致命错误: {error}",
    "telegram_error_generic": "发生错误，请联系管理员",
    "telegram_error_forbidden": "无法发送消息，您可能已屏蔽机器人",
    "telegram_error_bad_request": "操作失败，请稍后重试",
    "telegram_user_blocked": "您已被管理员拉黑",
    "telegram_user_not_found": "用户不存在",
    "telegram_user_already_blocked": "用户已被拉黑",
    "telegram_user_not_blocked": "用户未被拉黑",
    "telegram_user_not_verified": "无法切换：用户未验证",
    "telegram_verification_timeout": "验证超时，剩余{remaining}次机会\n{question}\n请从以下选项选择正确答案（保留2位小数），需在{timeout}分钟内完成",
    "telegram_verification_failed": "验证失败三次，您已被拉黑，请联系管理员",
    "telegram_verification_success": "验证成功！第{attempts}次尝试通过",
    "telegram_verification_error": "答案错误，剩余{remaining}次机会\n{question}\n请从以下选项选择正确答案（保留2位小数），需在{timeout}分钟内完成",
    "telegram_verification_start": "请完成验证\n{question}\n请从以下选项选择正确答案（保留2位小数），需在{timeout}分钟内完成",
    "telegram_ban_success": "用户 {user_id} 已拉黑",
    "telegram_unban_success": "用户 {user_id} 已解除拉黑",
    "telegram_list_users_empty": "暂无最近验证用户",
    "telegram_blacklist_empty": "黑名单为空",
    "telegram_clean_success": "数据库已清空",
    "telegram_admin_only": "仅限管理员操作",
    "telegram_invalid_command": "无效命令格式",
}

class Config:
    BOT_TOKEN: str = ""
    ADMIN_ID: int = 0
    WEBHOOK_URL: str = ""
    WEBHOOK_PORT: int = 8443
    SECRET_TOKEN: Optional[str] = None
    DB_CONFIG: Dict = {}
    POOL_MIN: int = 1
    POOL_MAX: int = 10
    CHAT_TIMEOUT: int = 0
    VERIFICATION_TIMEOUT: int = 0
    LOG_FILE: str = "bot.log"
    MESSAGE_TEMPLATES: Dict = DEFAULT_MESSAGE_TEMPLATES

    _config: Dict = {}

    @classmethod
    async def load(cls):
        """异步加载 config.yaml 并验证配置"""
        try:
            async with aiofiles.open("config.yaml", mode="r", encoding="utf-8") as f:
                content = await f.read()
            config = await asyncio.get_running_loop().run_in_executor(None, yaml.safe_load, content)
            cls._config = config or {}

            # 分配配置值
            cls.BOT_TOKEN = cls._config.get("bot_token", cls.BOT_TOKEN)
            cls.ADMIN_ID = cls._config.get("admin_id", cls.ADMIN_ID)
            cls.WEBHOOK_URL = cls._config.get("webhook_url", cls.WEBHOOK_URL)
            cls.WEBHOOK_PORT = cls._config.get("webhook_port", cls.WEBHOOK_PORT)
            cls.SECRET_TOKEN = cls._config.get("secret_token", cls.SECRET_TOKEN)
            cls.DB_CONFIG = cls._config.get("database", {})
            cls.POOL_MIN = cls._config.get("pool_min", cls.POOL_MIN)
            cls.POOL_MAX = cls._config.get("pool_max", cls.POOL_MAX)
            cls.CHAT_TIMEOUT = cls._config.get("chat_timeout", cls.CHAT_TIMEOUT)
            cls.VERIFICATION_TIMEOUT = cls._config.get("verification_timeout", cls.VERIFICATION_TIMEOUT)
            cls.LOG_FILE = cls._config.get("log_file", cls.LOG_FILE)
            cls.MESSAGE_TEMPLATES = {**DEFAULT_MESSAGE_TEMPLATES, **cls._config.get("message_templates", {})}

            # 验证配置
            cls.validate()
            configure_file_logging(cls.LOG_FILE)
            logger.info(cls.MESSAGE_TEMPLATES["config_loaded"].format(log_file=cls.LOG_FILE))
        except Exception as e:
            logger.error(cls.MESSAGE_TEMPLATES["config_failed"].format(error=str(e)), exc_info=True)
            raise

    @classmethod
    def validate(cls):
        """验证配置项的完整性和有效性"""
        required = ["bot_token", "admin_id", "webhook_url", "database", "chat_timeout", "verification_timeout"]
        for key in required:
            if key not in cls._config:
                raise ValueError(cls.MESSAGE_TEMPLATES["missing_config_key"].format(key=key))

        if not isinstance(cls.BOT_TOKEN, str) or not cls.BOT_TOKEN.strip():
            raise ValueError(cls.MESSAGE_TEMPLATES["invalid_bot_token"])
        if not isinstance(cls.ADMIN_ID, int) or cls.ADMIN_ID <= 0:
            raise ValueError(cls.MESSAGE_TEMPLATES["invalid_admin_id"])
        if not isinstance(cls.WEBHOOK_URL, str) or not cls.WEBHOOK_URL.strip():
            raise ValueError(cls.MESSAGE_TEMPLATES["invalid_webhook_url"])
        if not isinstance(cls.WEBHOOK_PORT, int) or not (1 <= cls.WEBHOOK_PORT <= 65535):
            raise ValueError(cls.MESSAGE_TEMPLATES["invalid_webhook_port"].format(port=cls.WEBHOOK_PORT))
        if cls.SECRET_TOKEN is not None and not isinstance(cls.SECRET_TOKEN, str):
            raise ValueError(cls.MESSAGE_TEMPLATES["invalid_secret_token"])
        if not isinstance(cls.LOG_FILE, str) or not cls.LOG_FILE.strip():
            raise ValueError(cls.MESSAGE_TEMPLATES["invalid_log_file"])

        required_db = ["host", "port", "database", "user", "password"]
        for key in required_db:
            if key not in cls.DB_CONFIG:
                raise ValueError(cls.MESSAGE_TEMPLATES["missing_db_key"].format(key=key))

        port = cls.DB_CONFIG["port"]
        if isinstance(port, str):
            try:
                cls.DB_CONFIG["port"] = int(port)
            except ValueError:
                raise ValueError(cls.MESSAGE_TEMPLATES["invalid_db_port"].format(port=port))
        if not isinstance(cls.DB_CONFIG["port"], int) or not (1 <= cls.DB_CONFIG["port"] <= 65535):
            raise ValueError(cls.MESSAGE_TEMPLATES["invalid_db_port"].format(port=cls.DB_CONFIG["port"]))

        for timeout_type, value in [("chat_timeout", cls.CHAT_TIMEOUT), ("verification_timeout", cls.VERIFICATION_TIMEOUT)]:
            if not isinstance(value, int) or value <= 0:
                raise ValueError(cls.MESSAGE_TEMPLATES["invalid_timeout"].format(timeout_type=timeout_type))
        if not isinstance(cls.POOL_MIN, int) or cls.POOL_MIN <= 0:
            raise ValueError(cls.MESSAGE_TEMPLATES["invalid_pool_min"])
        if not isinstance(cls.POOL_MAX, int) or cls.POOL_MAX <= 0:
            raise ValueError(cls.MESSAGE_TEMPLATES["invalid_pool_max"])
        if cls.POOL_MIN > cls.POOL_MAX:
            raise ValueError(cls.MESSAGE_TEMPLATES["invalid_pool_range"])