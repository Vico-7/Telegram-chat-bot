import structlog
import logging
import os
import re
from typing import Any, Dict
from pathlib import Path
from structlog.processors import JSONRenderer, TimeStamper, EventRenamer
from structlog.stdlib import add_log_level, BoundLogger

# 设置第三方库日志级别，减少无关日志
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

# 全局配置
BOT_VERSION = "1.0.0"
DEFAULT_LOG_FILE = "bot.log"
LOG_LEVEL = logging.DEBUG if os.getenv("DEBUG_MODE") == "true" else logging.INFO  # 支持调试模式

# 添加全局日志过滤器，仅过滤 Telegram API 请求的敏感部分
class TelegramAPIFilter(logging.Filter):
    """Filter out sensitive Telegram API request logs while preserving other logs."""
    def filter(self, record):
        msg = record.getMessage().lower()
        # 仅过滤包含敏感 token 的 Telegram API 请求
        if "api.telegram.org" in msg and "bot" in msg:
            return False
        return True

# 应用全局过滤器
logging.getLogger().addFilter(TelegramAPIFilter())

# 敏感信息过滤器
class SensitiveDataFilter:
    """Structlog processor to filter sensitive data."""
    SENSITIVE_KEYS = {
        "bot_token",
        "webhook_url",
        "secret_token",
        "password",
        "database_url",
        "db_config",
        "host",
        "port",
        "dbname",
        "user",
    }

    def __call__(self, logger: Any, method_name: str, event_dict: Dict[str, Any]) -> Dict[str, Any]:
        # 仅过滤敏感字段，不丢弃整个日志
        for key, value in event_dict.copy().items():
            if key.lower() in self.SENSITIVE_KEYS and isinstance(value, (str, dict)):
                event_dict[key] = "****"
            elif isinstance(value, str):
                # 仅替换敏感参数值（如 token=xxx）
                event_dict[key] = re.sub(
                    r"(?i)(token|password|secret|url)=[^&]+",
                    r"\1=****",
                    value
                )
            elif isinstance(value, dict):
                event_dict[key] = self.__call__(logger, method_name, value)
        return event_dict

# 安全异常处理器
class SafeExceptionPrinter(structlog.processors.ExceptionPrettyPrinter):
    """Custom processor to log stack traces safely."""
    def __call__(self, logger: Any, method_name: str, event_dict: Dict[str, Any]) -> Dict[str, Any]:
        if "exc_info" in event_dict:
            exc_info = event_dict["exc_info"]
            if isinstance(exc_info, bool):
                event_dict["exception"] = "No exception info available"
            elif isinstance(exc_info, tuple) and len(exc_info) == 3:
                exc_type, exc_value, traceback = exc_info
                event_dict["exception"] = (
                    f"{exc_type.__name__}: {str(exc_value)}\n"
                    f"Traceback: {''.join(__import__('traceback').format_tb(traceback))}"
                )
            else:
                event_dict["exception"] = "Invalid exception info"
            del event_dict["exc_info"]
        return event_dict

# 日志初始化
logging.basicConfig(
    format="%(message)s",
    handlers=[logging.StreamHandler()],
    level=LOG_LEVEL,
)

structlog.configure(
    processors=[
        TimeStamper(fmt="iso"),
        add_log_level,
        EventRenamer("message"),
        SensitiveDataFilter(),
        SafeExceptionPrinter(),
        structlog.processors.CallsiteParameterAdder(
            {
                structlog.processors.CallsiteParameter.FUNC_NAME,
                structlog.processors.CallsiteParameter.LINENO,
                structlog.processors.CallsiteParameter.PATHNAME,
            }
        ),
        JSONRenderer(),
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=BoundLogger,
    cache_logger_on_first_use=True,
)

structlog.contextvars.bind_contextvars(
    bot_version=BOT_VERSION,
)

logger = structlog.get_logger()

def configure_file_logging(log_file: str = DEFAULT_LOG_FILE) -> BoundLogger:
    """Configure logger with specified log file."""
    logging.getLogger().handlers.clear()

    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(log_path.parent, 0o700)  # 限制日志目录权限
    except OSError as e:
        logger.warning(f"Failed to set log directory permissions: {str(e)}")

    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=10_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(LOG_LEVEL)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(LOG_LEVEL)

    logging.basicConfig(
        format="%(message)s",
        handlers=[file_handler, console_handler],
        level=LOG_LEVEL,
    )

    structlog.configure(
        processors=[
            TimeStamper(fmt="iso"),
            add_log_level,
            EventRenamer("message"),
            SensitiveDataFilter(),
            SafeExceptionPrinter(),
            structlog.processors.CallsiteParameterAdder(
                {
                    structlog.processors.CallsiteParameter.FUNC_NAME,
                    structlog.processors.CallsiteParameter.LINENO,
                    structlog.processors.CallsiteParameter.PATHNAME,
                }
            ),
            JSONRenderer(),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=BoundLogger,
        cache_logger_on_first_use=True,
    )

    logger.debug(f"Logger configured with file: {log_file}, level: {logging.getLevelName(LOG_LEVEL)}")
    return structlog.get_logger()