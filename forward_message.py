from typing import Dict, Optional, Tuple, Callable
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from logger import logger
from database import Database, UserInfo
from config import Config
from telegram.error import Forbidden

class ForwardMessageHandler:
    def __init__(self, db: Database, application):
        self.db = db
        self.application = application
        self.current_chats: Dict[int, int] = {}
        self.chat_timers: Dict[int, asyncio.TimerHandle] = {}
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
            # 处理非按钮触发的 CallbackQuery（例如验证按钮）
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
                    if not await self.db.is_verified(target_user_id):
                        logger.warning(
                            f"User {target_user_id} is not verified for admin {admin_id}, clearing chat state")
                        await self.clear_chat_state(admin_id)
                        return None, None, "用户未验证"
                    logger.debug(f"Validated chat target {target_user_id} for admin {admin_id}")
                    return target_user_id, user_info, None
        except asyncio.TimeoutError:
            logger.warning(f"Timeout while validating chat for admin {admin_id}")
            await self.clear_chat_state(admin_id)  # 超时情况下清除状态
            return None, None, "操作超时，请稍后再试"
        except Exception as e:
            logger.error(f"Failed to validate chat for admin {admin_id}: {str(e)}", exc_info=True)
            await self.clear_chat_state(admin_id)  # 异常情况下清除状态
            return None, None, "操作失败，请稍后再试"

    async def forward_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        message = update.message
        if not message:
            logger.warning(f"No message found in update for user {user.id}")
            return

        # 确定消息类型
        message_type = "text" if message.text else "sticker" if message.sticker else "other"

        # 处理管理员输入用户 ID 的情况
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
            # 管理员发送消息
            target_user_id, user_info, error_msg = await self.get_current_chat_with_validation(user.id)
            if not target_user_id or not user_info:
                await message.reply_text(error_msg or "请先选择目标用户（/chat 或按钮）", parse_mode=None)
                return

            try:
                # 直接转发消息
                await self.application.bot.forward_message(
                    chat_id=target_user_id,
                    from_chat_id=message.chat_id,
                    message_id=message.message_id
                )
                await self.application.bot_data['bot'].send_temp_message(
                    message.chat_id,
                    f"消息已转发给 {user_info.nickname}"
                )
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

        # 普通用户发送消息
        async with self.db.transaction():
            if await self.db.is_blocked(user.id):
                logger.debug(f"User {user.id} is blocked, rejecting message")
                await message.reply_text("您已被拉黑，无法使用机器人", parse_mode=None)
                return
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
            # 直接转发消息给管理员
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
        # 允许非管理员 update（用于自动切换），但必须明确指定 admin_id
        if admin_id != Config.ADMIN_ID:
            if update.effective_user.id != Config.ADMIN_ID:
                logger.debug(f"Non-admin user {admin_id} attempted to switch chat to user {user_id}")
                # 不发送错误消息，因为可能是自动切换
                return
            admin_id = Config.ADMIN_ID  # 强制使用管理员 ID

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
                    if not await self.db.is_verified(user_id):
                        await reply_method("无法切换：用户未验证", parse_mode=None)
                        logger.warning(f"User {user_id} is not verified for admin {admin_id}")
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
        await self.reset_timer(admin_id, Config.CHAT_TIMEOUT, self.reset_chat)
        logger.debug(f"Admin {admin_id} set target to user {user_id}, current_chats: {self.current_chats}")

        try:
            escaped_nickname = self.escape_markdown_v2(
                user_info.nickname or "未知用户") if self.escape_markdown_v2 else user_info.nickname
            await reply_method(
                text=f"已将对话目标切换为“[{escaped_nickname}](tg://user?id={user_id})”",
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

    async def reset_chat(self, admin_id: int):
        if admin_id in self.current_chats:
            target_user_id = self.current_chats[admin_id]
            try:
                user_info = await self.db.get_user_info(target_user_id)
                nickname = user_info.nickname if user_info else "未知用户"
                escaped_nickname = self.escape_markdown_v2(nickname) if self.escape_markdown_v2 else nickname
                await self.application.bot.send_message(
                    chat_id=admin_id,
                    text=f"与“[{escaped_nickname}](tg://user?id={target_user_id})”的对话已超时重置，当前无对话目标",
                    parse_mode="MarkdownV2"
                )
                logger.info(f"Chat reset for admin {admin_id} due to timeout, target: {target_user_id}")
            except Exception as e:
                logger.error(f"Failed to send timeout reset message to admin {admin_id} for target {target_user_id}: {str(e)}", exc_info=True)
            self.current_chats.pop(admin_id, None)
        if admin_id in self.chat_timers:
            self.chat_timers.pop(admin_id, None)
            logger.debug(f"Removed chat timer for admin {admin_id} after reset")

    async def reset_timer(self, admin_id: int, timeout: int, callback):
        try:
            if admin_id in self.chat_timers:
                self.chat_timers[admin_id].cancel()
                logger.debug(f"Cancelled existing timer for admin {admin_id}")
            loop = asyncio.get_event_loop()
            self.chat_timers[admin_id] = loop.call_later(
                timeout, lambda: asyncio.create_task(callback(admin_id))
            )
            logger.debug(f"Timer reset for admin {admin_id}, timeout: {timeout}s")
        except Exception as e:
            logger.error(f"Failed to reset timer for admin {admin_id}: {str(e)}", exc_info=True)