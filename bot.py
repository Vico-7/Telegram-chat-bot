import asyncio
import uuid
from typing import Optional
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters
from config import Config
from database import Database
from telegram_bot import TelegramBot
from forward_message import ForwardMessageHandler
from logger import logger, configure_file_logging
from functools import wraps

try:
    from utils import escape_markdown_v2
except ImportError:
    logger.error("Failed to import escape_markdown_v2 from utils, using default implementation")
    def escape_markdown_v2(text: str) -> str:
        """默认的 MarkdownV2 转义实现"""
        if not text:
            return ""
        special_chars = r'\*_[]()~`>#+-=|{}.!'
        result = ""
        for char in text:
            if char in special_chars:
                result += f'\\{char}'
            else:
                result += char
        return result

def handle_async_errors(func):
    """装饰器，用于捕获异步函数中的错误并记录日志"""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except asyncio.TimeoutError:
            logger.error(Config.MESSAGE_TEMPLATES["shutdown_timeout"])
            raise
        except Exception as e:
            logger.error(Config.MESSAGE_TEMPLATES["fatal_error"].format(error=str(e)), exc_info=True)
            raise
    return wrapper

class BotApplication:
    async def initialize(self):
        configure_file_logging(Config.LOG_FILE)
        logger.info(Config.MESSAGE_TEMPLATES["logging_configured"])

        await Config.load()
        logger.info(Config.MESSAGE_TEMPLATES["config_loaded"])

        if not isinstance(Config.ADMIN_ID, int) or Config.ADMIN_ID <= 0:
            logger.error("Config.ADMIN_ID must be a valid positive integer")
            raise ValueError("Config.ADMIN_ID must be a valid positive integer")
        if not Config.BOT_TOKEN or not isinstance(Config.BOT_TOKEN, str):
            logger.error("Config.BOT_TOKEN must be a non-empty string")
            raise ValueError("Config.BOT_TOKEN must be a non-empty string")
        if not Config.WEBHOOK_URL or not isinstance(Config.WEBHOOK_URL, str):
            logger.error("Config.WEBHOOK_URL must be a non-empty string")
            raise ValueError("Config.WEBHOOK_URL must be a non-empty string")

        self.db = Database()
        await asyncio.wait_for(self.db.initialize(), timeout=10.0)
        logger.info(Config.MESSAGE_TEMPLATES["db_initialized"])

        self.application = Application.builder().token(Config.BOT_TOKEN).build()
        self.bot = TelegramBot(self.db, self.application)
        self.forward_handler = ForwardMessageHandler(self.db, self.application)
        self.bot.set_forward_handler(self.forward_handler)
        logger.debug("ForwardMessageHandler initialized and set for TelegramBot")

        logger.info(Config.MESSAGE_TEMPLATES["app_initialized"])

        handlers = [
            CommandHandler("start", self.bot.start),
            CommandHandler("ban", self.bot.ban),
            CommandHandler("unban", self.bot.unban),
            CommandHandler("list", self.bot.list_users),
            CommandHandler("blacklist", self.bot.blacklist),
            CommandHandler("status", self.bot.status),
            CommandHandler("clean", self.bot.clean),
            CommandHandler("chat", self.bot.chat),
            CommandHandler("count", self.bot.count),
            MessageHandler(filters.ALL & ~filters.COMMAND, self.bot.handle_message),
            MessageHandler(
                filters.ALL & ~filters.COMMAND & filters.User(user_id=Config.ADMIN_ID) & filters.REPLY,
                self.bot.handle_message
            ),
            MessageHandler(
                filters.Regex(r'^\d+$') & filters.User(user_id=Config.ADMIN_ID),
                self.bot._handle_interactive_user_id
            ),
            CallbackQueryHandler(self.bot.button)
        ]
        for handler in handlers:
            self.application.add_handler(handler)
            logger.debug(f"Handler registered: {handler.__class__.__name__}")

        self.application.add_error_handler(self.bot.error_handler)
        logger.debug("Global error handler registered")

    async def start_webhook(self):
        """启动 Webhook，包含重试机制"""
        webhook_port = Config.WEBHOOK_PORT
        secret_token = Config.SECRET_TOKEN or str(uuid.uuid4())

        async def try_start_webhook():
            await self.application.initialize()
            await self.application.start()
            await self.application.updater.start_webhook(
                listen="0.0.0.0",
                port=webhook_port,
                url_path="",
                webhook_url=Config.WEBHOOK_URL,
                secret_token=secret_token
            )

        for attempt in range(3):
            try:
                await asyncio.wait_for(try_start_webhook(), timeout=30.0)
                logger.info(Config.MESSAGE_TEMPLATES["webhook_started"].format(port=webhook_port))
                return
            except asyncio.TimeoutError:
                logger.error(Config.MESSAGE_TEMPLATES["webhook_timeout"].format(attempt=attempt + 1))
                if attempt == 2:
                    logger.error(Config.MESSAGE_TEMPLATES["webhook_failed_final"])
                    raise
                await asyncio.sleep(2 ** attempt)  # 指数退避：2s, 4s, 8s
            except Exception as e:
                logger.error(Config.MESSAGE_TEMPLATES["webhook_failed"].format(attempt=attempt + 1, error=str(e)))
                if attempt == 2:
                    logger.error(Config.MESSAGE_TEMPLATES["webhook_failed_final"])
                    raise
                await asyncio.sleep(2 ** attempt)

    @handle_async_errors
    async def shutdown(self):
        """优雅关闭应用，清理资源"""
        logger.info(Config.MESSAGE_TEMPLATES["shutdown_initiated"])
        try:
            # 清理 ForwardMessageHandler 状态
            if self.forward_handler and hasattr(self.forward_handler, 'clear_chat_state'):
                try:
                    await asyncio.wait_for(self.forward_handler.clear_chat_state(Config.ADMIN_ID), timeout=5.0)
                    logger.info("ForwardMessageHandler state cleared")
                except Exception as e:
                    logger.error(f"Failed to clear ForwardMessageHandler state: {str(e)}", exc_info=True)
            else:
                logger.warning(f"ForwardMessageHandler is None or lacks clear_chat_state method: forward_handler={self.forward_handler}")

            # 清理 bot 状态
            if self.bot:
                try:
                    await asyncio.wait_for(self.bot.shutdown(), timeout=10.0)
                    logger.info("Bot shutdown completed")
                except Exception as e:
                    logger.error(f"Failed to shutdown bot: {str(e)}", exc_info=True)

            # 停止 Telegram 应用
            if self.application:
                if self.application.updater and self.application.updater.running:
                    try:
                        await asyncio.wait_for(self.application.updater.stop(), timeout=10.0)
                        logger.info(Config.MESSAGE_TEMPLATES["updater_stopped"])
                    except Exception as e:
                        logger.error(f"Failed to stop updater: {str(e)}", exc_info=True)
                if self.application.running:
                    try:
                        await asyncio.wait_for(self.application.stop(), timeout=5.0)
                        logger.info("Application stopped")
                    except Exception as e:
                        logger.error(f"Failed to stop application: {str(e)}", exc_info=True)
                    try:
                        await asyncio.wait_for(self.application.shutdown(), timeout=5.0)
                        logger.info(Config.MESSAGE_TEMPLATES["app_shutdown"])
                    except Exception as e:
                        logger.error(f"Failed to shutdown application: {str(e)}", exc_info=True)

            # 关闭数据库连接
            if self.db:
                try:
                    await asyncio.wait_for(self.db.close(), timeout=10.0)
                    logger.info(Config.MESSAGE_TEMPLATES["db_closed"])
                except Exception as e:
                    logger.error(f"Failed to close database: {str(e)}", exc_info=True)
        except Exception as e:
            logger.error(Config.MESSAGE_TEMPLATES["shutdown_failed"].format(error=str(e)), exc_info=True)
            raise

    async def run(self):
        """运行应用，设置命令并启动 Webhook"""
        await self.initialize()

        # 设置机器人命令
        for attempt in range(3):
            try:
                await asyncio.wait_for(self.bot.set_bot_commands(), timeout=10.0)
                logger.info(Config.MESSAGE_TEMPLATES["commands_set"])
                break
            except asyncio.TimeoutError:
                logger.error(f"set_bot_commands timeout, attempt {attempt + 1}/3")
                if attempt == 2:
                    logger.error("Failed to set bot commands after 3 attempts")
                    raise
                await asyncio.sleep(2)
            except Exception as e:
                logger.error(f"set_bot_commands failed, attempt {attempt + 1}/3: {str(e)}")
                if attempt == 2:
                    logger.error("Failed to set bot commands after 3 attempts")
                    raise
                await asyncio.sleep(2)

        # 启动 Webhook
        await self.start_webhook()

        # 保持运行
        await asyncio.Event().wait()

async def main():
    """主入口函数"""
    app = BotApplication()
    try:
        await app.run()
    except KeyboardInterrupt:
        logger.info(Config.MESSAGE_TEMPLATES["program_terminated"])
    except Exception as e:
        logger.error(f"Application failed: {str(e)}", exc_info=True)
    finally:
        await app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())