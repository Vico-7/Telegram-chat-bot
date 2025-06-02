from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes
from typing import List
import asyncio
from logger import logger
from telegram.ext import Application

def escape_markdown_v2(text: str) -> str:
    """转义 MarkdownV2 特殊字符，确保兼容数学表达式"""
    if not text:
        return ""
    special_chars = set(r'\*_[]()~`>#+-=|{}.!')
    return ''.join(f'\\{c}' if c in special_chars else c for c in text)

async def send_temp_message(
    application: Application, 
    chat_id: int, 
    text: str, 
    context: ContextTypes.DEFAULT_TYPE, 
    delay: float = 1.0
) -> None:
    """
    发送临时消息并异步调度删除操作。

    Args:
        application: Telegram 应用实例。
        chat_id: 目标聊天 ID。
        text: 要发送的消息内容。
        context: Telegram 上下文对象（未使用，保留以兼容调用）。
        delay: 消息显示的秒数，默认 1 秒。

    Raises:
        TelegramError: 如果 Telegram API 调用失败。
        Exception: 如果发生其他未知错误。
    """
    log_extra = {"chat_id": chat_id, "text": text[:50]}
    try:
        msg = await application.bot.send_message(
            chat_id=chat_id,
            text=text,
            disable_notification=True
        )
        logger.debug("Sent temporary message", extra={**log_extra, "message_id": msg.message_id})

        asyncio.create_task(
            application.bot.delete_message_later(chat_id, msg.message_id, delay)
        )

    except TelegramError as e:
        if "too many requests" in str(e).lower():
            logger.warning("Rate limit hit in send_temp_message", extra={**log_extra, "error": str(e)})
            await asyncio.sleep(1)
            await application.bot.send_message(chat_id=chat_id, text=text, disable_notification=True)
        else:
            logger.warning("Failed to send temporary message", extra={**log_extra, "error": str(e)})
    except Exception as e:
        logger.error("Unexpected error in send_temp_message", exc_info=True, extra={**log_extra, "error": str(e)})

def create_verification_keyboard(user_id: int, options: List[float]) -> InlineKeyboardMarkup:
    """
    创建验证问题的键盘。

    Args:
        user_id: 用户 ID。
        options: 包含四个浮点数的选项列表。

    Returns:
        InlineKeyboardMarkup: 包含验证选项的键盘。

    Raises:
        ValueError: 如果选项数量不为 4。
    """
    if len(options) != 4:
        logger.error(
            "Invalid number of verification options",
            extra={"user_id": user_id, "options_count": len(options)}
        )
        raise ValueError(f"Expected 4 verification options, got {len(options)}")

    prefixes = ['A', 'B', 'C', 'D']
    buttons = [
        InlineKeyboardButton(f"选项 {prefixes[i]}: {opt:.2f}", callback_data=f"verify_{user_id}_{opt:.2f}")
        for i, opt in enumerate(options)
    ]
    keyboard = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
    logger.debug(
        "Created verification keyboard",
        extra={"user_id": user_id, "options": [f"{opt:.2f}" for opt in options]}
    )
    return InlineKeyboardMarkup(keyboard)