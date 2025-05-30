import structlog
import logging
import os
import re
from typing import Any, Dict
from pathlib import Path
from structlog.processors import JSONRenderer, TimeStamper, EventRenamer
from structlog.stdlib import add_log_level, BoundLogger

# 设置第三方库日志级别
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

# 全局配置
BOT_VERSION = "1.0.0"
DEFAULT_LOG_FILE = "bot.log"
LOG_LEVEL = logging.DEBUG if os.getenv("DEBUG_MODE") == "true" else logging.INFO


# Telegram API 请求过滤器
class TelegramAPIFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage().lower()
        if "api.telegram.org" in msg and "bot" in msg:
            return False
        return True


logging.getLogger().addFilter(TelegramAPIFilter())


# 敏感信息过滤器
class SensitiveDataFilter:
    SENSITIVE_KEYS = {
        "bot_token", "webhook_url", "secret_token", "password",
        "database_url", "db_config", "host", "port", "dbname", "user",
    }

    def __call__(self, logger: Any, method_name: str, event_dict: Dict[str, Any]) -> Dict[str, Any]:
        for key, value in event_dict.copy().items():
            if key.lower() in self.SENSITIVE_KEYS and isinstance(value, (str, dict)):
                event_dict[key] = "****"
            elif isinstance(value, str):
                event_dict[key] = re.sub(r"(?i)(token|password|secret|url)=[^&]+", r"\1=****", value)
            elif isinstance(value, dict):
                event_dict[key] = self.__call__(logger, method_name, value)
        return event_dict


# 安全异常处理器
class SafeExceptionPrinter(structlog.processors.ExceptionPrettyPrinter):
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


# Structlog 配置
def get_structlog_config() -> dict:
    return {
        "processors": [
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
        "context_class": dict,
        "logger_factory": structlog.stdlib.LoggerFactory(),
        "wrapper_class": BoundLogger,
        "cache_logger_on_first_use": True,
    }


# 日志配置
def configure_logging(log_file: str = None) -> BoundLogger:
    logging.getLogger().handlers.clear()

    handlers = [logging.StreamHandler()]
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(log_path.parent, 0o700)
        except OSError as e:
            logger.warning(f"Failed to set log directory permissions: {str(e)}")

        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=10_000_000,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(LOG_LEVEL)
        handlers.append(file_handler)

    logging.basicConfig(
        format="%(message)s",
        handlers=handlers,
        level=LOG_LEVEL,
    )

    structlog.configure(**get_structlog_config())
    structlog.contextvars.bind_contextvars(bot_version=BOT_VERSION)

    if log_file:
        logger.debug(f"Logger configured with file: {log_file}, level: {logging.getLevelName(LOG_LEVEL)}")
    return structlog.get_logger()


# 初始化日志
logger = configure_logging()