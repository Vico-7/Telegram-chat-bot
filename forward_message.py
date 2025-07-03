from typing import Dict, Optional, Tuple, Callable
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from logger import logger
from database import Database, UserInfo
from config import Config
from telegram.error import Forbidden
import time

class ForwardMessageHandler:
    def __init__(self, db: Database, application):
        self.db = db
        self.application = application
        self.current_chats: Dict[int, int] = {}  # 管理员ID -> 目标用户ID
        self.chat_timers: Dict[int, asyncio.TimerHandle] = {}  # 管理员ID -> 定时器句柄
        self.last_message_time: Dict[int, float] = {}  # 管理员ID -> 最后消息发送时间戳
        self.escape_markdown_v2: Optional[Callable[[str], str]] = None
        logger.debug("ForwardMessageHandler initialized")

    def set_escape_markdown_v2(self, escape_func: Callable[[str], str]):
        self.escape_markdown_v2 = escape_func
        logger.debug("escape_markdown_v2 set for ForwardMessageHandler")

    @staticmethod
    async def _get_reply_method(update: Update, context: ContextTypes.DEFAULT_TYPE, is_button: bool,
                               default_chat_id: int):
        if is_button and update.callback_query and update.callback_query.message:
            logger.debug(f"Using callback_query.message.reply_text for chat_id={update.callback_query.message.chat_id}")
            return update.callback_query.message.reply_text
        elif update.message:
            logger.debug(f"Using message.reply_text for chat_id={update.message.chat_id}")
            return update.message.reply_text
        elif update.callback_query and not is_button:
            logger.debug(f"Using callback_query.message.reply_text for chat_id={update.callback_query.message.chat_id}")
            return update.callback_query.message.reply_text
        logger.debug(f"Falling back to send_message for default_chat_id={default_chat_id}")
        return lambda text, **kwargs: context.bot.send_message(chat_id=default_chat_id, text=text, **kwargs)

    async def get_current_chat_with_validation(self, admin_id: int) -> Tuple[
        Optional[int], Optional[UserInfo], Optional[str]]:
        try:
            async with asyncio.timeout(5):
                target_user_id = self.current_chats.get(admin_id)
                if not target_user_id:
                    logger.debug(f"No current chat target for admin {admin_id}")
                    return None, None, "请先选择目标用户（使用 /chat 或按钮）"

                async with self.db.transaction():
                    user_info = await self.db.get_user_info(target_user_id)
                    if not user_info:
                        logger.warning(f"User {target_user_id} not found for admin {admin_id}, clearing chat state")
                        await self.clear_chat_state(admin_id)
                        return None, None, "用户不存在"
                    if user_info.is_blocked:
                        logger.warning(f"User {target_user_id} is blocked for admin {admin_id}, clearing chat state")
                        await self.clear_chat_state(admin_id)
                        return None, None, "用户已拉黑"
                    logger.debug(f"Validated chat target {target_user_id} for admin {admin_id}")
                    return target_user_id, user_info, None
        except asyncio.TimeoutError:
            logger.warning(f"Timeout while validating chat for admin {admin_id}")
            await self.clear_chat_state(admin_id)
            return None, None, "操作超时，请稍后再试"
        except Exception as e:
            logger.error(f"Failed to validate chat for admin {admin_id}: {str(e)}", exc_info=True)
            await self.clear_chat_state(admin_id)
            return None, None, "操作失败，请稍后再试"

    async def forward_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        message = update.message
        if not message:
            logger.warning(f"No message found in update for user {user.id}")
            return

        message_type = "text" if message.text else "sticker" if message.sticker else "other"

        if user.id == Config.ADMIN_ID and user.id in self.application.bot_data.get('bot', {}).waiting_user_id:
            logger.debug(f"Admin {user.id} is waiting for user ID input, command: {self.application.bot_data['bot'].waiting_user_id[user.id]}")
            if not message.text:
                logger.debug(f"Non-text message (type: {message_type}) received while waiting for user ID from admin {user.id}")
                await message.reply_text("请输入有效的用户 ID（纯数字）", parse_mode=None)
                return
            if await self.application.bot_data['bot']._handle_interactive_user_id(update, context):
                logger.debug(f"Processed user ID input for admin {user.id}")
            else:
                await message.reply_text("请输入有效的用户 ID", parse_mode=None)
            return
        elif user.id == Config.ADMIN_ID:
            logger.debug(f"No waiting_user_id for admin {user.id}, proceeding with forward_message")

        if user.id == Config.ADMIN_ID:
            target_user_id, user_info, error_msg = await self.get_current_chat_with_validation(user.id)
            if not target_user_id or not user_info:
                await message.reply_text(error_msg or "请先选择目标用户（/chat 或按钮）", parse_mode=None)
                return

            try:
                await self.application.bot.forward_message(
                    chat_id=target_user_id,
                    from_chat_id=message.chat_id,
                    message_id=message.message_id
                )
                await self.application.bot_data['bot'].send_temp_message(
                    message.chat_id,
                    f"消息已转发给 {user_info.nickname}"
                )
                # 更新最后消息时间
                self.last_message_time[user.id] = time.time()
                logger.debug(f"Updated last message time for admin {user.id} to {self.last_message_time[user.id]}")
                # 重置定时器
                await self.reset_timer(user.id, Config.CHAT_TIMEOUT, self.check_and_reset_chat)
                logger.info(f"Message forwarded from admin {user.id} to user {target_user_id}, type: {message_type}")
            except Forbidden:
                logger.warning(f"Cannot forward message to user {target_user_id}: User has blocked the bot")
                await message.reply_text("无法转发消息：目标用户已禁用机器人", parse_mode=None)
                await self.db.block_user(target_user_id, "用户禁用机器人")
                await self.clear_chat_state(user.id)
            except Exception as e:
                logger.error(f"Failed to forward message to {target_user_id}: {str(e)}", exc_info=True)
                await message.reply_text("无法转发消息，请稍后再试", parse_mode=None)
            return

        async with self.db.transaction():
            if await self.db.is_blocked(user.id):
                logger.debug(f"User {user.id} is blocked, rejecting message")
                await message.reply_text("您已被拉黑，无法使用机器人", parse_mode=None)
                return
            verification_enabled = await self.db.get_verification_enabled()
            if verification_enabled:
                is_verified = await self.db.is_verified(user.id)
                logger.debug(f"Verification check for user {user.id}: is_verified={is_verified}")
                if not is_verified:
                    logger.debug(f"User {user.id} is not verified, deleting message and prompting for verification")
                    try:
                        await message.delete()
                        logger.debug(f"Deleted message from unverified user {user.id}")
                    except Exception as e:
                        logger.warning(f"Failed to delete message from unverified user {user.id}: {str(e)}")
                    await self.application.bot_data['bot'].reply_error(update, "请先完成人机验证，使用 /start 开始")
                    return

            await self.db.update_conversation(user.id)
            logger.debug(f"Updated conversation for user {user.id}")

            try:
                admin_id = Config.ADMIN_ID
                await self.application.bot.forward_message(
                    chat_id=admin_id,
                    from_chat_id=message.chat_id,
                    message_id=message.message_id
                )
                await self.application.bot_data['bot'].send_temp_message(message.chat_id, "消息已转发")
                logger.info(f"Message forwarded from user {user.id} to admin {admin_id}, type: {message_type}")
            except Forbidden:
                logger.warning(f"Cannot forward message to admin {admin_id}: Admin has blocked the bot")
                await message.reply_text("无法转发消息：管理员不可用", parse_mode=None)
            except Exception as e:
                logger.error(f"Failed to forward message to admin {admin_id}: {str(e)}", exc_info=True)
                await message.reply_text("无法转发消息，请稍后再试", parse_mode=None)

    async def switch_chat(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int,
                         is_button: bool = False):
        admin_id = update.effective_user.id
        if admin_id != Config.ADMIN_ID:
            logger.debug(f"Non-admin user {admin_id} attempted to switch chat to user {user_id}")
            return

        reply_method = await self._get_reply_method(update, context, is_button, admin_id)

        try:
            async with asyncio.timeout(10):
                async with self.db.transaction():
                    user_info = await self.db.get_user_info(user_id)
                    if not user_info:
                        await reply_method("用户不存在", parse_mode=None)
                        logger.warning(f"User {user_id} not found for admin {admin_id}")
                        return
                    if user_info.is_blocked:
                        await reply_method("无法切换：用户已拉黑", parse_mode=None)
                        logger.warning(f"User {user_id} is blocked for admin {admin_id}")
                        return
        except asyncio.TimeoutError:
            await reply_method("操作超时，请稍后再试", parse_mode=None)
            logger.warning(f"Timeout while validating user {user_id} in switch_chat for admin {admin_id}")
            return
        except Exception as e:
            await reply_method("操作失败，请稍后再试", parse_mode=None)
            logger.error(f"Failed to check user {user_id} in switch_chat for admin {admin_id}: {str(e)}", exc_info=True)
            return

        bot = self.application.bot_data.get('bot')
        if bot and admin_id in bot.pending_request:
            del bot.pending_request[admin_id]
        if bot and admin_id in bot.waiting_user_id:
            del bot.waiting_user_id[admin_id]
        logger.debug(f"Cleared pending_request and waiting_user_id for admin {admin_id} in switch_chat")

        self.current_chats[admin_id] = user_id
        # 初始化最后消息时间（切换时尚未发送消息）
        self.last_message_time[admin_id] = 0
        await self.reset_timer(admin_id, Config.CHAT_TIMEOUT, self.check_and_reset_chat)
        logger.debug(f"Admin {admin_id} set target to user {user_id}, current_chats: {self.current_chats}")

        try:
            # 计算超时分钟数
            timeout_minutes = Config.CHAT_TIMEOUT // 60
            # 使用 MarkdownV2 格式，支持点击跳转用户主页
            escaped_nickname = self.escape_markdown_v2(user_info.nickname or "未知用户")
            await reply_method(
                text=f"对话目标已切换为 [{escaped_nickname}](tg://user?id={user_id})，将在管理员 *{timeout_minutes}* 分钟未发送消息后自动重置",
                parse_mode="MarkdownV2"
            )
            logger.info(f"Admin {admin_id} switched to user {user_id}")
        except Exception as e:
            logger.error(f"Failed to send switch confirmation for admin {admin_id} to user {user_id}: {str(e)}",
                         exc_info=True)

    async def clear_chat_state(self, admin_id: int):
        if admin_id in self.current_chats:
            logger.debug(f"Clearing chat state for admin {admin_id}, target: {self.current_chats[admin_id]}")
            self.current_chats.pop(admin_id, None)
        if admin_id in self.chat_timers:
            self.chat_timers[admin_id].cancel()
            del self.chat_timers[admin_id]
            logger.debug(f"Cancelled chat timer for admin {admin_id}")
        if admin_id in self.last_message_time:
            del self.last_message_time[admin_id]
            logger.debug(f"Removed last message time for admin {admin_id}")

    async def check_and_reset_chat(self, admin_id: int):
        if admin_id not in self.current_chats:
            logger.debug(f"No chat target for admin {admin_id}, skipping reset")
            return

        current_time = time.time()
        last_time = self.last_message_time.get(admin_id, 0)
        elapsed = current_time - last_time

        if last_time == 0 or elapsed >= Config.CHAT_TIMEOUT:
            target_user_id = self.current_chats[admin_id]
            logger.info(f"Chat reset for admin {admin_id} due to inactivity, target: {target_user_id}, elapsed: {elapsed}s")
            await self.clear_chat_state(admin_id)
            try:
                await self.application.bot.send_message(
                    chat_id=admin_id,
                    text="由于长时间未发送消息，对话目标已自动重置",
                    parse_mode=None
                )
            except Exception as e:
                logger.error(f"Failed to notify admin {admin_id} of chat reset: {str(e)}")
        else:
            # 如果未达到冷却时间，重新设置定时器，检查剩余时间
            remaining_time = Config.CHAT_TIMEOUT - elapsed
            await self.reset_timer(admin_id, int(remaining_time), self.check_and_reset_chat)
            logger.debug(f"Rescheduled timer for admin {admin_id}, remaining time: {remaining_time}s")

    async def reset_timer(self, admin_id: int, timeout: int, callback):
        try:
            if admin_id in self.chat_timers:
                self.chat_timers[admin_id].cancel()
                logger.debug(f"Cancelled existing timer for admin {admin_id}")
            loop = asyncio.get_event_loop()
            self.chat_timers[admin_id] = loop.call_later(
                timeout, lambda: asyncio.create_task(callback(admin_id))
            )
            logger.debug(f"Timer set for admin {admin_id}, timeout: {timeout}s")
        except Exception as e:
            logger.error(f"Failed to reset timer for admin {admin_id}: {str(e)}", exc_info=True)