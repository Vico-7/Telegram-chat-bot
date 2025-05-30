from typing import Dict, Optional, Callable, List
import asyncio
import datetime
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, ContextTypes
from telegram.error import TelegramError, Forbidden, BadRequest
from functools import wraps
from logger import logger
from database import Database, UserInfo, Verification
from config import Config
from utils import create_verification_keyboard, escape_markdown_v2
from verification import MathVerification
from forward_message import ForwardMessageHandler
import enum
import asyncpg

BEIJING_TZ = pytz.timezone("Asia/Shanghai")


class CommandType(enum.Enum):
    BAN = "ban"
    UNBAN = "unban"
    CHAT = "chat"
    LIST = "list"
    BLACKLIST = "blacklist"
    STATUS = "status"
    CLEAN = "clean"
    COUNT = "count"


def handle_errors(func):
    @wraps(func)
    async def wrapper(self, update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        try:
            return await func(self, update, context, *args, **kwargs)
        except TelegramError as e:
            error_msg = Config.MESSAGE_TEMPLATES.get(
                f"telegram_{type(e).__name__.lower()}",
                Config.MESSAGE_TEMPLATES["telegram_error_generic"]
            )
            logger.error(f"Error in {func.__name__} for update {update.update_id if update else 'unknown'}: {str(e)}",
                         exc_info=True)
            await self.reply_error(update, error_msg)

    return wrapper


class VerificationHandler:
    """处理人机验证按钮的逻辑"""

    def __init__(self, bot: 'TelegramBot', db: Database):
        self.bot = bot
        self.db = db

    async def handle_verification_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int,
                                         answer: float):
        query = update.callback_query
        try:
            async with self.db.transaction():
                verification = await self.db.get_verification(user_id)
                if not verification:
                    logger.debug(f"No verification record for user {user_id}")
                    await query.message.edit_text("验证记录不存在，请重新使用 /start", parse_mode=None)
                    return

                if verification.verified:
                    logger.debug(f"User {user_id} already verified")
                    await query.message.edit_text(
                        "🎉 *您已通过验证！* 🎉\n\n可以开始与管理员对话了！😊",
                        parse_mode="MarkdownV2",
                        reply_markup=None
                    )
                    return

                if verification.message_id != query.message.message_id:
                    logger.debug(
                        f"Verification message ID mismatch for user {user_id}: expected {verification.message_id}, got {query.message.message_id}")
                    await query.message.edit_text("验证消息已过期，请重新使用 /start", parse_mode=None)
                    return

                # 清理定时器
                if user_id in self.bot.verification_timers:
                    self.bot.verification_timers[user_id].cancel()
                    del self.bot.verification_timers[user_id]
                    logger.debug(f"Cancelled verification timer for user {user_id}")

                if abs(verification.answer - answer) < 1e-6:
                    # 验证成功
                    verification.verified = True
                    verification.verification_time = self.db._normalize_datetime(datetime.datetime.now(BEIJING_TZ))
                    verification.message_id = None
                    await self.db.update_verification(verification)
                    await query.message.edit_text(
                        "🎉 *验证通过！欢迎使用！* 🎉\n\n您可以开始与管理员对话了！😊",
                        parse_mode="MarkdownV2",
                        reply_markup=None
                    )
                    logger.info(f"User {user_id} passed verification")

                    # 检查管理员是否无对话目标，若无则自动切换
                    admin_id = Config.ADMIN_ID
                    target_user_id, user_info, error_msg = await self.bot.forward_handler.get_current_chat_with_validation(
                        admin_id)
                    if not target_user_id:
                        try:
                            # 直接调用 switch_chat 的核心逻辑，避免伪 Update
                            user_info = await self.db.get_user_info(user_id)
                            if user_info and not user_info.is_blocked and await self.db.is_verified(user_id):
                                self.bot.forward_handler.current_chats[admin_id] = user_id
                                await self.bot.forward_handler.reset_timer(admin_id, Config.CHAT_TIMEOUT,
                                                                           self.bot.forward_handler.reset_chat)
                                escaped_nickname = escape_markdown_v2(user_info.nickname or "未知用户")
                                await self.bot.application.bot.send_message(
                                    chat_id=admin_id,
                                    text=f"已将对话目标切换为“[{escaped_nickname}](tg://user?id={user_id})”",
                                    parse_mode="MarkdownV2"
                                )
                                logger.info(f"Automatically switched admin {admin_id} chat to user {user_id}")
                            else:
                                logger.warning(f"Auto-switch failed: invalid user {user_id} (blocked or not verified)")
                        except Exception as e:
                            logger.error(f"Failed to auto-switch admin {admin_id} chat to user {user_id}: {str(e)}",
                                         exc_info=True)
                    else:
                        logger.debug(f"Admin {admin_id} already has chat target {target_user_id}, skipping auto-switch")

                    # 通知管理员
                    user_info = await self.db.get_user_info(user_id)
                    notification_message = (
                        f"用户通过验证:\n{UserInfo.format(user_info, blocked=False)}\n"
                        f"验证尝试次数: {verification.error_count + 1}\n"
                        f"验证题目: {verification.question}\n"
                        f"正确答案: {verification.answer}"
                    )
                    buttons = [
                        [
                            InlineKeyboardButton("🚫 拉黑", callback_data=f"confirm_ban_{user_id}"),
                            InlineKeyboardButton("💬 切换对话", callback_data=f"cb_switch_{user_id}")
                        ]
                    ]
                    await self.bot.send_admin_notification(
                        context=context,
                        message=notification_message,
                        user_id=user_id,
                        buttons=buttons
                    )
                else:
                    # 验证失败
                    await self.bot.ban_handler.handle_verification_failure(user_id, verification, reason="wrong_answer")
        except Forbidden:
            logger.warning(f"User {user_id} blocked bot during verification")
            await self.db.block_user(user_id, "用户禁用机器人")
        except asyncpg.exceptions.PostgresError as e:
            logger.error(f"Database error during verification for user {user_id}: {str(e)}", exc_info=True)
            await query.message.edit_text("数据库错误，请稍后重试", parse_mode=None)
        except BadRequest as e:
            logger.error(f"Failed to edit verification message for user {user_id}: {str(e)}")
            await self.bot.reply_error(update, "无法编辑验证消息，请重新使用 /start")
        except Exception as e:
            logger.error(f"Error handling verification button for user {user_id}: {str(e)}", exc_info=True)
            await query.message.edit_text(Config.MESSAGE_TEMPLATES["telegram_error_generic"], parse_mode=None)


class BanHandler:
    """处理所有拉黑相关的逻辑"""

    def __init__(self, bot: 'TelegramBot', db: Database, forward_handler: 'ForwardMessageHandler'):
        self.bot = bot
        self.db = db
        self.forward_handler = forward_handler

    async def _send_verification_message(self, user_id: int, verification: Verification, question_message: str,
                                         options: List[float]) -> bool:
        try:
            if verification.message_id:
                try:
                    await self.bot.application.bot.edit_message_text(
                        chat_id=user_id,
                        message_id=verification.message_id,
                        text=question_message,
                        reply_markup=create_verification_keyboard(user_id, options),
                        parse_mode=None
                    )
                    logger.debug(f"Edited verification message {verification.message_id} for user {user_id}")
                except (BadRequest, Forbidden) as e:
                    logger.debug(
                        f"Failed to edit verification message {verification.message_id} for user {user_id}: {str(e)}")
                    verification.message_id = None
            if not verification.message_id:
                msg = await self.bot.application.bot.send_message(
                    chat_id=user_id,
                    text=question_message,
                    reply_markup=create_verification_keyboard(user_id, options),
                    parse_mode=None
                )
                verification.message_id = msg.message_id
            await self.db.update_verification(verification)
            # 设置定时器
            loop = asyncio.get_running_loop()
            self.bot.verification_timers[user_id] = loop.call_later(
                Config.VERIFICATION_TIMEOUT,
                lambda: asyncio.create_task(self.bot.timeout_verification(user_id))
            )
            logger.debug(f"Sent/updated verification message to user {user_id}, message_id: {verification.message_id}")
            return True
        except Forbidden:
            await self.db.block_user(user_id, "用户禁用机器人")
            logger.warning(f"User {user_id} blocked bot, user banned")
            return False
        except asyncpg.exceptions.PostgresError as e:
            logger.error(f"Database error updating verification for user {user_id}: {str(e)}", exc_info=True)
            return False
        except Exception as e:
            logger.error(f"Failed to send/edit verification message to user {user_id}: {str(e)}", exc_info=True)
            return False

    async def ban_user(self, user_id: int, update: Update, context: ContextTypes.DEFAULT_TYPE,
                       is_button: bool = False, needs_confirmation: bool = True, reason: str = "管理员操作",
                       admin_id: Optional[int] = None) -> bool:
        try:
            async with asyncio.timeout(10):
                user_info = await self.db.get_user_info(user_id)
                if not user_info:
                    if update:
                        await self.bot.reply_error(update, Config.MESSAGE_TEMPLATES["telegram_user_not_found"])
                    else:
                        logger.warning(f"User {user_id} not found, no valid update to reply")
                    return False
                if user_info.is_blocked:
                    if update:
                        await self.bot.reply_error(update, Config.MESSAGE_TEMPLATES["telegram_user_already_blocked"])
                    else:
                        logger.warning(f"User {user_id} already blocked, no valid update to reply")
                    return False

                effective_admin_id = admin_id or Config.ADMIN_ID

                if not is_button or not needs_confirmation:
                    try:
                        async with self.db.transaction():
                            await self.db.block_user(user_id, reason)
                    except asyncpg.exceptions.PostgresError as e:
                        logger.error(f"Database error banning user {user_id}: {str(e)}", exc_info=True)
                        if update:
                            await self.bot.reply_error(update, "数据库操作失败，请稍后重试")
                        return False

                    if user_id in self.bot.verification_timers:
                        self.bot.verification_timers[user_id].cancel()
                        del self.bot.verification_timers[user_id]
                        logger.debug(f"Cancelled verification timer for user {user_id} during ban")

                    await self.forward_handler.clear_chat_state(effective_admin_id)

                    # 仅在 update 有效时发送成功消息
                    if update and update.effective_user:
                        buttons = [[InlineKeyboardButton("✅ 解除拉黑", callback_data=f"cb_unban_{user_id}")]]
                        reply_markup = InlineKeyboardMarkup(buttons)
                        reply_method = await self.bot._get_reply_method(update, is_button)
                        if callable(reply_method) and reply_method.__name__ != "<lambda>":  # 确保 reply_method 有效
                            msg = await reply_method(
                                f"已成功拉黑用户 {user_id}",
                                parse_mode=None,
                                reply_markup=reply_markup
                            )
                            context.user_data['ban_success_message_id'] = msg.message_id
                        else:
                            logger.debug(f"Skipping ban success message for user {user_id}, invalid reply method")
                    else:
                        logger.debug(f"Skipping ban success message for user {user_id}, no valid update")
                    logger.info(f"Admin {effective_admin_id} banned user {user_id} with reason: {reason}")
                    return True

                if not update:  # 如果没有有效的 update，直接返回 False
                    logger.warning(f"Cannot send ban confirmation for user {user_id}, no valid update")
                    return False

                escaped_nickname = escape_markdown_v2(user_info.nickname or "未知用户")
                buttons = [
                    [
                        InlineKeyboardButton("确认拉黑 🚫", callback_data=f"confirm_ban_{user_id}"),
                        InlineKeyboardButton("取消 ❌", callback_data=f"cancel_ban_{user_id}")
                    ]
                ]
                reply_markup = InlineKeyboardMarkup(buttons)
                warning_message = (
                    f"⚠️ *即将拉黑用户* ⚠️\n\n"
                    f"您将拉黑 [{escaped_nickname}](tg://user?id={user_id})。\n"
                    f"拉黑后，用户将无法使用机器人，且对话记录将被清空。\n"
                    f"请确认是否继续？"
                )
                msg = await self.bot.reply_success(update, warning_message, parse_mode="MarkdownV2",
                                                   reply_markup=reply_markup)
                if msg:
                    context.user_data['ban_message_id'] = msg.message_id
                return False
        except Exception as e:
            logger.error(f"Error banning user {user_id}: {str(e)}", exc_info=True)
            if update:
                await self.bot.reply_error(update, Config.MESSAGE_TEMPLATES["telegram_error_generic"])
            else:
                logger.warning(f"Cannot send error reply for user {user_id}, no valid update")
            return False
        finally:
            effective_admin_id = admin_id or Config.ADMIN_ID
            self.bot.pending_request.pop(effective_admin_id, None)
            self.bot.waiting_user_id.pop(effective_admin_id, None)
            logger.debug(f"Cleared state for admin {effective_admin_id}")

    async def handle_verification_failure(self, user_id: int, verification: Verification, reason: str = "wrong_answer"):
        # 清理现有定时器
        if user_id in self.bot.verification_timers:
            self.bot.verification_timers[user_id].cancel()
            del self.bot.verification_timers[user_id]
            logger.debug(f"Cancelled verification timer for user {user_id}")

        verification.error_count += 1
        remaining = 3 - verification.error_count

        if remaining > 0:
            # 生成新题目
            question, answer, options = MathVerification.generate_question()
            verification.update(
                question=question,
                answer=answer,
                options=options,
                verification_time=datetime.datetime.now(BEIJING_TZ).astimezone(pytz.UTC).replace(tzinfo=None)
            )
            error_prompt = (
                "❌ 答案错误，请重试！\n\n" if reason == "wrong_answer" else
                "⏰ 题目已超时，新题目已生成！\n\n"
            )
            question_message = (
                "🎉 欢迎使用我的机器人！ 🎉\n\n"
                "为了确保您是真人用户，请完成以下人机验证 🔐\n\n"
                "📝 验证规则：\n"
                "1️⃣ 回答数学题目，点击下方选项提交答案。\n"
                "2️⃣ 每题有 3分钟 作答时间，超时将刷新题目 ⏳\n"
                "3️⃣ 共 3次 尝试机会，答错或超时扣除一次。机会用尽自动拉黑\n\n"
                f"{error_prompt}"
                "❓ 验证题目 ❓\n"
                f"📌 {verification.question}\n\n"
                f"⏰ 请在 {Config.VERIFICATION_TIMEOUT // 60}分钟 内作答！\n"
                f"🔄 剩余尝试次数：{remaining}/3"
            )
            try:
                await self.bot.application.bot.edit_message_text(
                    chat_id=user_id,
                    message_id=verification.message_id,
                    text=question_message,
                    reply_markup=create_verification_keyboard(user_id, options),
                    parse_mode=None
                )
                await self.db.update_verification(verification)
                # 设置新定时器
                loop = asyncio.get_running_loop()
                self.bot.verification_timers[user_id] = loop.call_later(
                    Config.VERIFICATION_TIMEOUT,
                    lambda: asyncio.create_task(self.bot.timeout_verification(user_id))
                )
                logger.debug(f"Updated verification message for user {user_id}, message_id: {verification.message_id}")
            except (Forbidden, BadRequest) as e:
                logger.warning(f"Failed to edit verification message for user {user_id}: {str(e)}")
                await self.db.block_user(user_id, "用户禁用机器人或消息不可编辑")
            except Exception as e:
                logger.error(f"Failed to update verification message for user {user_id}: {str(e)}", exc_info=True)
        else:
            # 编辑消息为失败提示
            try:
                await self.bot.application.bot.edit_message_text(
                    chat_id=user_id,
                    message_id=verification.message_id,
                    text="人机验证失败，您已被拉黑",
                    parse_mode=None,
                    reply_markup=None
                )
                logger.debug(f"Edited verification message to failure for user {user_id}")
            except (Forbidden, BadRequest) as e:
                logger.warning(f"Failed to edit verification failure message for user {user_id}: {str(e)}")
            except Exception as e:
                logger.debug(f"Failed to edit verification failure message for user {user_id}: {str(e)}")

            # 拉黑用户
            await self.ban_user(
                user_id=user_id,
                update=Update(update_id=0, message=None, callback_query=None),
                context=self.bot.application,
                is_button=False,
                needs_confirmation=False,
                reason="验证失败三次",
                admin_id=Config.ADMIN_ID  # 明确指定 admin_id
            )

            # 发送管理员通知
            user_info = await self.db.get_user_info(user_id)
            buttons = [[InlineKeyboardButton("✅ 解除拉黑", callback_data=f"cb_unban_{user_id}")]]
            await self.bot.send_admin_notification(
                context=self.bot.application,
                message=f"用户验证失败，已拉黑:\n{UserInfo.format(user_info, blocked=True)}",
                user_id=user_id,
                buttons=buttons
            )


class TelegramBot:
    def __init__(self, db: Database, application: Application):
        self.db = db
        self.application = application
        self.verification_timers: Dict[int, asyncio.TimerHandle] = {}
        self.forward_handler: Optional[ForwardMessageHandler] = None
        self.waiting_user_id: Dict[int, CommandType] = {}
        self.pending_request: Dict[int, Optional[CommandType]] = {}
        self.ban_handler = None
        self.verification_handler = VerificationHandler(self, db)
        self.application.bot_data['bot'] = self  # 确保 bot 实例存储
        logger.info("TelegramBot initialized and stored in bot_data")

    def set_forward_handler(self, forward_handler: ForwardMessageHandler):
        """设置 ForwardMessageHandler 并初始化相关依赖"""
        self.forward_handler = forward_handler
        self.forward_handler.set_escape_markdown_v2(escape_markdown_v2)
        self.ban_handler = BanHandler(self, self.db, self.forward_handler)
        logger.debug("ForwardMessageHandler set for TelegramBot")

    async def _get_reply_method(self, update: Update, is_button: bool):
        if is_button and update and update.callback_query and update.callback_query.message:
            return update.callback_query.message.reply_text
        elif update and update.message:
            return update.message.reply_text
        if update and update.effective_user:
            return lambda *args, **kwargs: self.application.bot.send_message(
                chat_id=update.effective_user.id, *args, **kwargs
            )
        logger.warning(f"Cannot determine reply method: Invalid update object")
        return lambda *args, **kwargs: None  # 返回空操作，避免后续调用抛出错误

    async def _restrict_and_validate(self, update: Update, user_id: Optional[int] = None) -> bool:
        if not update.effective_user:
            logger.error("No effective user found in update")
            await self.reply_error(update, Config.MESSAGE_TEMPLATES["telegram_error_generic"])
            return False
        admin_id = update.effective_user.id
        if admin_id != Config.ADMIN_ID:
            await self.reply_error(update, Config.MESSAGE_TEMPLATES["telegram_admin_only"])
            return False
        if user_id is not None:
            user_info = await self.db.get_user_info(user_id)
            if not user_info:
                await self.reply_error(update, Config.MESSAGE_TEMPLATES["telegram_user_not_found"])
                return False
        return True

    async def _check_mutex(self, update: Update, command: CommandType) -> bool:
        admin_id = update.effective_user.id
        if admin_id in self.pending_request and self.pending_request[admin_id] in [CommandType.CHAT, CommandType.BAN,
                                                                                   CommandType.UNBAN]:
            await self.reply_error(update, "请先完成或取消当前操作（/chat, /ban, 或 /unban）")
            logger.debug(f"Mutex check failed for admin {admin_id}: pending {self.pending_request[admin_id]}")
            return False
        return True

    async def _delete_request_user_id_message(self, admin_id: int, context: ContextTypes.DEFAULT_TYPE):
        if 'request_user_id_message_id' in context.user_data:
            try:
                await self.application.bot.delete_message(
                    chat_id=admin_id,
                    message_id=context.user_data['request_user_id_message_id']
                )
                logger.debug(
                    f"Deleted request_user_id message {context.user_data['request_user_id_message_id']} for admin {admin_id}")
            except Exception as e:
                logger.debug(f"Failed to delete request_user_id message for admin {admin_id}: {str(e)}")
            finally:
                del context.user_data['request_user_id_message_id']

    async def _execute_command(self, command: CommandType, update: Update, context: ContextTypes.DEFAULT_TYPE,
                               user_id: Optional[int] = None, is_button: bool = False):
        if command == CommandType.BAN:
            await self.ban_handler.ban_user(
                user_id=user_id,
                update=update,
                context=context,
                is_button=is_button,
                needs_confirmation=is_button
            )
        elif command == CommandType.UNBAN:
            await self.unban_user(user_id, update, context, is_button)
        elif command == CommandType.CHAT:
            await self.forward_handler.switch_chat(update, context, user_id, is_button)
        elif command == CommandType.LIST:
            await self.list_users(update, context, is_button)
        elif command == CommandType.BLACKLIST:
            await self.blacklist(update, context, is_button)
        elif command == CommandType.STATUS:
            await self.status(update, context, is_button)
        elif command == CommandType.CLEAN:
            await self.clean(update, context, is_button)
        elif command == CommandType.COUNT:
            await self.count(update, context, is_button)

    async def check_user_status(self, user_id: int, update: Update) -> bool:
        if await self.db.is_blocked(user_id):
            await self.reply_error(update, Config.MESSAGE_TEMPLATES["telegram_user_blocked"])
            return False
        if not await self.db.is_verified(user_id):
            await self.reply_error(update, "请先完成人机验证，使用 /start 开始")
            return False
        return True

    async def _request_user_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE, command: CommandType):
        admin_id = update.effective_user.id
        self.pending_request[admin_id] = command
        self.waiting_user_id[admin_id] = command
        buttons = [[InlineKeyboardButton("取消 ❌", callback_data="cancel_user_id")]]
        reply_markup = InlineKeyboardMarkup(buttons)
        message = (
            "📩 *请输入用户 ID*\n\n"
            "请回复一个纯数字的用户 ID 以继续操作。\n"
            "或点击下方按钮取消。"
        )
        reply_method = await self._get_reply_method(update, is_button=update.callback_query is not None)
        msg = await reply_method(message, reply_markup=reply_markup, parse_mode="MarkdownV2")
        context.user_data['request_user_id_message_id'] = msg.message_id
        if update.message:
            context.user_data['command_message_id'] = update.message.message_id

    async def _handle_interactive_user_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id not in self.waiting_user_id:
            logger.debug(f"No waiting_user_id for user {user_id}")
            return False
        command = self.waiting_user_id[user_id]
        logger.debug(
            f"Processing interactive user ID for user {user_id}, command: {command}, input: {update.message.text}")

        if not update.message or not update.message.text:
            logger.debug(f"No valid text input for user {user_id}, command: {command}")
            await self.reply_error(update, "请输入有效的用户 ID")
            return True

        if update.message:
            try:
                await update.message.delete()
                logger.debug(f"Deleted command message for admin {user_id}")
            except Exception as e:
                logger.debug(f"Failed to delete command message for admin {user_id}: {str(e)}")

        try:
            target_user_id = int(update.message.text.strip())
            if not await self._restrict_and_validate(update, target_user_id):
                logger.debug(f"Validation failed for target_user_id {target_user_id}")
                return True
            await self._execute_command(command, update, context, target_user_id)
            logger.info(f"Successfully executed command {command} for user ID {target_user_id} by admin {user_id}")
            await self._delete_request_user_id_message(user_id, context)
            if user_id in self.waiting_user_id:
                del self.waiting_user_id[user_id]
            if user_id in self.pending_request:
                del self.pending_request[user_id]
            logger.debug(f"Cleared waiting_user_id and pending_request for user {user_id}")
            return True
        except ValueError:
            logger.debug(f"Invalid user ID input for user {user_id}: {update.message.text}")
            await self.reply_error(update, (
                "❌ *无效的用户 ID*\n\n"
                "请回复一个纯数字的用户 ID，例如：`123456789`"
            ), parse_mode="MarkdownV2")
            return True
        except asyncio.TimeoutError:
            logger.warning(
                f"Timeout processing command {command} for user {user_id}, target: {locals().get('target_user_id', 'unknown')}")
            if user_id in self.waiting_user_id:
                del self.waiting_user_id[user_id]
            if user_id in self.pending_request:
                del self.pending_request[user_id]
            await self._delete_request_user_id_message(user_id, context)
            logger.debug(f"Cleared waiting_user_id and pending_request for user {user_id} due to timeout")
            return True
        except Exception as e:
            logger.error(f"Error processing interactive user ID for user {user_id}, command: {command}: {str(e)}",
                         exc_info=True)
            if user_id in self.waiting_user_id:
                del self.waiting_user_id[user_id]
            if user_id in self.pending_request:
                del self.pending_request[user_id]
            await self._delete_request_user_id_message(user_id, context)
            logger.debug(f"Cleared waiting_user_id and pending_request for user {user_id} due to error")
            return True

    async def shutdown(self):
        await self.db.close()
        logger.info("Bot shutdown completed")

    async def reply_error(self, update: Update, text: str, parse_mode: Optional[str] = None):
        if not update:
            logger.debug(f"Cannot send error message: Update is None")
            return
        try:
            if update.callback_query and update.callback_query.message:
                await update.callback_query.message.reply_text(text, parse_mode=parse_mode)
            elif update.message:
                await update.message.reply_text(text, parse_mode=parse_mode)
            elif update.effective_user:
                await self.application.bot.send_message(
                    chat_id=update.effective_user.id,
                    text=text,
                    parse_mode=parse_mode
                )
            else:
                logger.debug(f"Cannot send error message: No valid user in update {update.update_id}")
        except Exception as e:
            user_id = update.effective_user.id if update.effective_user else "unknown"
            logger.error(f"Failed to send error message to user {user_id}: {str(e)}", exc_info=True)

    async def reply_success(self, update: Update, text: str, parse_mode: Optional[str] = None,
                            reply_markup: Optional[InlineKeyboardMarkup] = None):
        try:
            reply_method = await self._get_reply_method(update, is_button=update.callback_query is not None)
            msg = await reply_method(text, parse_mode=parse_mode, reply_markup=reply_markup)
            return msg
        except Exception as e:
            logger.error(f"Failed to send success message to user {update.effective_user.id}: {str(e)}", exc_info=True)
            return None

    async def send_temp_message(self, chat_id: int, text: str, timeout: int = 1):
        try:
            msg = await self.application.bot.send_message(chat_id=chat_id, text=text, parse_mode=None)
            asyncio.create_task(self.delete_message_later(chat_id, msg.message_id, timeout))
        except Exception as e:
            logger.debug(f"Failed to send temp message to {chat_id}: {str(e)}")

    async def delete_message_later(self, chat_id: int, message_id: int, timeout: int):
        await asyncio.sleep(timeout)
        try:
            await self.application.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception as e:
            logger.debug(f"Failed to delete message {message_id} in {chat_id}: {str(e)}")

    async def send_admin_notification(self, context: ContextTypes.DEFAULT_TYPE, message: str,
                                      user_id: Optional[int] = None,
                                      buttons: Optional[List[List[InlineKeyboardButton]]] = None):
        admin_id = Config.ADMIN_ID
        if user_id == admin_id:
            logger.debug(f"Skipping notification to admin {admin_id} as it matches user_id")
            return

        try:
            reply_markup = InlineKeyboardMarkup(buttons) if buttons else None
            await context.bot.send_message(
                chat_id=admin_id,
                text=message,
                reply_markup=reply_markup,
                parse_mode=None,
                disable_notification=True
            )
            logger.debug(f"Sent notification to admin {admin_id} for user {user_id}: {message[:50]}...")
        except Exception as e:
            logger.error(f"Failed to notify admin {admin_id} for user {user_id}: {str(e)}", exc_info=True)

    async def timeout_verification(self, user_id: int):
        async with self.db.transaction():
            verification = await self.db.get_verification(user_id)
            if not verification:
                logger.debug(f"No verification record for user {user_id}, skipping timeout")
                return
            if await self.db.is_verified(user_id):
                logger.debug(f"User {user_id} is already verified, skipping timeout")
                return
            logger.debug(f"Verification timeout for user {user_id}, question: {verification.question}")
            try:
                await self.bot.application.bot.edit_message_text(
                    chat_id=user_id,
                    message_id=verification.message_id,
                    text="⏰ 验证超时，请重新尝试！",
                    parse_mode=None,
                    reply_markup=None
                )
                logger.debug(f"Edited verification message to timeout for user {user_id}")
            except (Forbidden, BadRequest) as e:
                logger.warning(f"Failed to edit timeout message for user {user_id}: {str(e)}")
            except Exception as e:
                logger.debug(f"Failed to edit timeout message for user {user_id}: {str(e)}")
            await self.ban_handler.handle_verification_failure(user_id, verification, reason="timeout")

    async def start_verification(self, user_id: int, verification: Verification):
        remaining = 3 - verification.error_count
        if remaining <= 0:
            logger.debug(f"Max verification attempts reached for user {user_id}")
            await self.ban_handler.handle_verification_failure(user_id, verification, reason="max_attempts")
            return

        question, answer, options = MathVerification.generate_question()
        verification.update(
            question=question,
            answer=answer,
            options=options,
            verification_time=datetime.datetime.now(BEIJING_TZ).astimezone(pytz.UTC).replace(tzinfo=None)
        )

        try:
            question_message = (
                "🎉 欢迎使用我的机器人！ 🎉\n\n"
                "为了确保您是真人用户，请完成以下人机验证 🔐\n\n"
                "📝 验证规则：\n"
                "1️⃣ 回答数学题目，点击下方选项提交答案。\n"
                "2️⃣ 每题有 3分钟 作答时间，超时将刷新题目 ⏳\n"
                "3️⃣ 共 3次 尝试机会，答错或超时扣除一次。机会用尽自动拉黑\n\n"
                "❓ 验证题目 ❓\n"
                f"📌 {verification.question}\n\n"
                f"⏰ 请在 {Config.VERIFICATION_TIMEOUT // 60}分钟 内作答！\n"
                f"🔄 剩余尝试次数：{remaining}/3"
            )
            msg = await self.application.bot.send_message(
                chat_id=user_id,
                text=question_message,
                reply_markup=create_verification_keyboard(user_id, options),
                parse_mode=None  # 移除 Markdown 解析
            )
            verification.message_id = msg.message_id
            await self.db.update_verification(verification)
            loop = asyncio.get_running_loop()
            self.verification_timers[user_id] = loop.call_later(
                Config.VERIFICATION_TIMEOUT,
                lambda: asyncio.create_task(self.timeout_verification(user_id))
            )
            logger.debug(
                f"Started verification for user {user_id}, question: {verification.question}, message_id: {msg.message_id}")
        except Forbidden:
            logger.warning(f"User {user_id} has blocked the bot")
            await self.db.block_user(user_id, "用户禁用机器人")
        except Exception as e:
            logger.error(f"Failed to start verification for user {user_id}: {str(e)}", exc_info=True)
            raise

    async def unban_user(self, user_id: int, update: Update, context: ContextTypes.DEFAULT_TYPE,
                         is_button: bool = False):
        try:
            async with asyncio.timeout(15):
                user_info = await self.db.get_user_info(user_id)
                if not user_info:
                    await self.reply_error(update, Config.MESSAGE_TEMPLATES["telegram_user_not_found"])
                    return
                if not user_info.is_blocked:
                    await self.reply_error(update, Config.MESSAGE_TEMPLATES["telegram_user_not_blocked"])
                    return
                try:
                    async with self.db.transaction():
                        # 清除验证记录
                        await self.db.execute(
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
                            verification_time=self.db._normalize_datetime(datetime.datetime.now(BEIJING_TZ)),
                            error_count=0,
                            message_id=None
                        )
                        await self.db.add_verification(verification)
                        # 解除拉黑
                        await self.db.execute(
                            """
                            UPDATE users
                            SET is_blocked   = FALSE,
                                block_reason = NULL,
                                block_time   = NULL
                            WHERE user_id = $1
                            """,
                            (user_id,)
                        )
                        logger.debug(f"Unblock and verification reset completed for user {user_id}")
                except asyncpg.exceptions.DeadlockDetectedError:
                    logger.warning(f"Deadlock detected in unban_user for user {user_id}, retrying")
                    await asyncio.sleep(0.5)
                    async with self.db.transaction():
                        await self.db.execute(
                            "DELETE FROM verification WHERE user_id = $1",
                            (user_id,)
                        )
                        verification = Verification(
                            user_id=user_id,
                            question="",
                            answer=0.0,
                            options=[],
                            verified=False,
                            verification_time=self.db._normalize_datetime(datetime.datetime.now(BEIJING_TZ)),
                            error_count=0,
                            message_id=None
                        )
                        await self.db.add_verification(verification)
                        await self.db.execute(
                            """
                            UPDATE users
                            SET is_blocked   = FALSE,
                                block_reason = NULL,
                                block_time   = NULL
                            WHERE user_id = $1
                            """,
                            (user_id,)
                        )
                # 清除验证定时器
                if user_id in self.verification_timers:
                    self.verification_timers[user_id].cancel()
                    del self.verification_timers[user_id]
                    logger.debug(f"Cancelled verification timer for user {user_id} during unban")
                await self.forward_handler.clear_chat_state(update.effective_user.id)
                reply_method = await self._get_reply_method(update, is_button)
                msg = await reply_method(Config.MESSAGE_TEMPLATES["telegram_unban_success"].format(user_id=user_id),
                                         parse_mode=None)
                context.user_data['unban_message_id'] = msg.message_id
                # 添加延迟删除逻辑
                asyncio.create_task(self.delete_message_later(update.effective_user.id, msg.message_id, 1))
                logger.debug(
                    f"Admin {update.effective_user.id} unblocked user {user_id}, message {msg.message_id} scheduled for deletion")
                if is_button and update.callback_query and update.callback_query.message:
                    try:
                        await update.callback_query.message.delete()
                    except Exception as e:
                        logger.debug(
                            f"Failed to delete callback query message for admin {update.effective_user.id}: {str(e)}")
        except asyncio.TimeoutError:
            logger.warning(f"Timeout in unban_user for user {user_id}")
            await self.reply_error(update, "操作超时，请稍后重试")
        except asyncpg.exceptions.PostgresError as e:
            logger.error(f"Database error in unban_user for user {user_id}: {str(e)}", exc_info=True)
            await self.reply_error(update, "数据库操作失败，请稍后重试")
        except Exception as e:
            logger.error(f"Error in unban_user for user {user_id}: {str(e)}", exc_info=True)
            await self.reply_error(update, "解除拉黑失败，请稍后重试")
        finally:
            admin_id = update.effective_user.id if update.effective_user else None
            if admin_id and admin_id in self.pending_request:
                del self.pending_request[admin_id]
            if admin_id and admin_id in self.waiting_user_id:
                del self.waiting_user_id[admin_id]
            logger.debug(
                f"Cleared pending_request and waiting_user_id for admin {admin_id if admin_id else 'unknown'} in unban_user")

    @handle_errors
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        message = update.message
        if not message:
            logger.debug(f"No message in update for user {user_id}")
            return

        message_type = "text" if message.text else "sticker" if message.sticker else "other"
        logger.debug(f"Handling message for user {user_id}, type: {message_type}")

        if update.message and await self._handle_interactive_user_id(update, context):
            logger.debug(f"Processed interactive user ID input for user {user_id}")
            return

        if user_id != Config.ADMIN_ID:
            async with self.db.transaction():
                if await self.db.is_blocked(user_id):
                    logger.debug(f"User {user_id} is blocked, deleting message and sending blocked message")
                    try:
                        await message.delete()
                        logger.debug(f"Deleted message from blocked user {user_id}")
                    except Exception as e:
                        logger.warning(f"Failed to delete message from blocked user {user_id}: {str(e)}")
                    await self.reply_error(update, Config.MESSAGE_TEMPLATES["telegram_user_blocked"])
                    return

                if not await self.db.is_verified(user_id):
                    logger.debug(f"User {user_id} is not verified, deleting message and prompting for verification")
                    try:
                        await message.delete()
                        logger.debug(f"Deleted message from unverified user {user_id}")
                    except Exception as e:
                        logger.warning(f"Failed to delete message from unverified user {user_id}: {str(e)}")
                    await self.reply_error(update, "请先完成人机验证，使用 /start 开始")
                    return

        await self.forward_handler.forward_message(update, context)

    @handle_errors
    async def button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if not query or not query.data:
            logger.error(f"Invalid callback query or missing data for update {update.update_id}")
            await self.reply_error(update, Config.MESSAGE_TEMPLATES["telegram_error_generic"])
            return

        try:
            await query.answer()
        except BadRequest as e:
            if "Query is too old" in str(e):
                logger.warning(f"Expired callback query for update {update.update_id}: {str(e)}")
                await self.reply_error(update, "按钮已过期，请重新操作")
                return
            raise

        user_id = query.from_user.id
        logger.debug(f"Processing button {query.data} for user {user_id}")

        # 处理验证按钮
        if query.data.startswith("verify_"):
            try:
                _, target_user_id, answer = query.data.split("_", 2)
                target_user_id = int(target_user_id)
                answer = float(answer)
                if user_id != target_user_id:
                    logger.warning(f"User {user_id} attempted to answer verification for user {target_user_id}")
                    await self.reply_error(update, "您无权操作此验证")
                    return
                await self.verification_handler.handle_verification_button(update, context, target_user_id, answer)
                return
            except ValueError as e:
                logger.error(f"Invalid verification callback data {query.data}: {str(e)}")
                await self.reply_error(update, "无效验证操作，请重新使用 /start")
                return

        # 管理员按钮处理
        if not query.from_user or query.from_user.id != Config.ADMIN_ID:
            logger.debug(f"Non-admin user {user_id} attempted to use admin button: {query.data}")
            await self.reply_error(update, Config.MESSAGE_TEMPLATES["telegram_admin_only"])
            return

        admin_id = query.from_user.id
        valid_single_actions = ["confirm_clean", "cancel_clean", "reset_chat", "cancel_user_id",
                                "request_ban", "request_unban", "request_chat", "list",
                                "blacklist", "status", "clean", "count"]
        if "_" not in query.data and query.data not in valid_single_actions:
            logger.warning(f"Invalid callback data format: {query.data} for user {admin_id}")
            await self.reply_error(update, "无效按钮操作，请重试")
            return

        if query.data in ["request_ban", "request_unban", "request_chat"]:
            if admin_id in self.pending_request and self.pending_request[admin_id] in [CommandType.CHAT,
                                                                                       CommandType.BAN,
                                                                                       CommandType.UNBAN]:
                await self.reply_error(update, "请先完成或取消当前操作（/chat, /ban, 或 /unban）")
                logger.debug(f"Mutex check failed for button {query.data} by admin {admin_id}")
                return
            logger.debug(f"Mutex check passed for button {query.data} by admin {admin_id}")

        try:
            if query.data == "confirm_clean":
                async with self.db.transaction():
                    await self.db.clean_database()
                await self.reply_success(update, Config.MESSAGE_TEMPLATES["telegram_clean_success"], parse_mode=None)
                await query.message.delete()
            elif query.data == "cancel_clean":
                await query.message.delete()
                logger.info(f"Admin {admin_id} cancelled clean operation")
            elif query.data == "reset_chat":
                await self.forward_handler.clear_chat_state(admin_id)
                await self.reply_success(update, "已重置对话目标", parse_mode=None)
                logger.info(f"Admin {admin_id} reset chat target")
            elif query.data == "cancel_user_id":
                if admin_id in self.waiting_user_id:
                    del self.waiting_user_id[admin_id]
                if admin_id in self.pending_request:
                    del self.pending_request[admin_id]
                await self._delete_request_user_id_message(admin_id, context)
                try:
                    await query.message.delete()
                except Exception as e:
                    logger.debug(f"Failed to delete query message for admin {admin_id}: {str(e)}")
                logger.info(f"Admin {admin_id} cancelled user ID input")
            elif query.data in ["request_ban", "request_unban", "request_chat"]:
                command_map = {
                    "request_ban": CommandType.BAN,
                    "request_unban": CommandType.UNBAN,
                    "request_chat": CommandType.CHAT
                }
                await self._request_user_id(update, context, command_map[query.data])
                logger.info(f"Requested user ID for command {command_map[query.data]} by admin {admin_id}")
            else:
                command_map = {
                    "list": CommandType.LIST,
                    "blacklist": CommandType.BLACKLIST,
                    "status": CommandType.STATUS,
                    "clean": CommandType.CLEAN,
                    "count": CommandType.COUNT
                }
                if query.data.startswith("confirm_ban_"):
                    action = "ban"
                    user_id = query.data[len("confirm_ban_"):]
                elif query.data.startswith("cancel_ban_"):
                    action = "cancel_ban"
                    user_id = query.data[len("cancel_ban_"):]
                elif query.data.startswith("cb_unban_"):
                    action = "unban"
                    user_id = query.data[len("cb_unban_"):]
                elif query.data.startswith("cb_switch_"):
                    action = "switch"
                    user_id = query.data[len("cb_switch_"):]
                else:
                    action, user_id = query.data.split("_", 1) if "_" in query.data else (query.data, None)

                if action in command_map:
                    await self._execute_command(command_map[action], update, context, is_button=True)
                elif action == "ban":
                    await self.ban_handler.ban_user(
                        user_id=int(user_id),
                        update=update,
                        context=context,
                        is_button=True,
                        needs_confirmation=False
                    )
                    try:
                        await query.message.delete()
                        logger.debug(f"Deleted user info message for user {user_id} after ban")
                    except Exception as e:
                        logger.debug(f"Failed to delete user info message for user {user_id}: {str(e)}")
                elif action == "cancel_ban":
                    try:
                        await query.message.delete()
                    except Exception as e:
                        logger.debug(f"Failed to delete ban confirm message for admin {admin_id}: {str(e)}")
                    await self._delete_request_user_id_message(admin_id, context)
                    if 'ban_message_id' in context.user_data:
                        try:
                            await self.application.bot.delete_message(
                                chat_id=admin_id,
                                message_id=context.user_data['ban_message_id']
                            )
                        except Exception as e:
                            logger.debug(f"Failed to delete ban message for admin {admin_id}: {str(e)}")
                        finally:
                            del context.user_data['ban_message_id']
                    if admin_id in self.pending_request:
                        del self.pending_request[admin_id]
                    if admin_id in self.waiting_user_id:
                        del self.waiting_user_id[admin_id]
                    logger.info(f"Admin {admin_id} cancelled ban for user {user_id}")
                elif action == "unban":
                    await self.unban_user(int(user_id), update, context, is_button=True)
                    try:
                        await query.message.delete()
                        logger.debug(f"Deleted user info or ban success message for user {user_id} after unban")
                    except Exception as e:
                        logger.debug(f"Failed to delete user info or ban success message for user {user_id}: {str(e)}")
                    await self._delete_request_user_id_message(admin_id, context)
                    if admin_id in self.pending_request:
                        del self.pending_request[admin_id]
                    if admin_id in self.waiting_user_id:
                        del self.waiting_user_id[admin_id]
                elif action == "switch":
                    await self._execute_command(CommandType.CHAT, update, context, int(user_id), is_button=True)
                    await self._delete_request_user_id_message(admin_id, context)
                    if admin_id in self.pending_request:
                        del self.pending_request[admin_id]
                    if admin_id in self.waiting_user_id:
                        del self.waiting_user_id[admin_id]
                else:
                    logger.warning(f"Unknown button action: {action} for user {admin_id}, callback_data: {query.data}")
                    await self.reply_error(update, "无效按钮操作，请重试")
        except ValueError as e:
            logger.error(f"Failed to parse callback data {query.data} for user {admin_id}: {str(e)}")
            await self.reply_error(update, "无效按钮操作，请重试")
        except Exception as e:
            logger.error(f"Unexpected error in button handler for user {admin_id}: {str(e)}", exc_info=True)
            await self.reply_error(update, Config.MESSAGE_TEMPLATES["telegram_error_generic"])

    @handle_errors
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # 删除指令消息
        if update.message:
            try:
                await update.message.delete()
                logger.debug(f"Deleted /start command message for user {update.effective_user.id}")
            except Exception as e:
                logger.debug(f"Failed to delete /start command message for user {update.effective_user.id}: {str(e)}")

        user = update.effective_user
        logger.debug(f"Processing /start for user {user.id}")

        if await self.db.is_blocked(user.id):
            await self.reply_error(update, Config.MESSAGE_TEMPLATES["telegram_user_blocked"])
            return

        if user.id == Config.ADMIN_ID:
            buttons = [
                [
                    InlineKeyboardButton("🚫 拉黑", callback_data="request_ban"),
                    InlineKeyboardButton("✅ 解禁", callback_data="request_unban")
                ],
                [
                    InlineKeyboardButton("💬 对话", callback_data="request_chat"),
                    InlineKeyboardButton("📋 用户", callback_data="list")
                ],
                [
                    InlineKeyboardButton("🛑 黑名单", callback_data="blacklist"),
                    InlineKeyboardButton("📡 状态", callback_data="status")
                ],
                [
                    InlineKeyboardButton("🗑️ 清除", callback_data="clean"),
                    InlineKeyboardButton("📈 统计", callback_data="count")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(buttons)
            message = (
                "👋 *管理员，您好！* 👋\n\n"
                "使用下方按钮管理用户和对话："
            )
            await self.reply_success(update, message, reply_markup=reply_markup, parse_mode="MarkdownV2")
            return

        async with self.db.transaction():
            user_info = await self.db.get_user_info(user.id)
            if not user_info:
                user_info = UserInfo(
                    user_id=user.id,
                    nickname=user.full_name,
                    username=user.username,
                    registration_time=datetime.datetime.now(BEIJING_TZ).astimezone(pytz.UTC).replace(tzinfo=None)
                )
                await self.db.add_user(user_info)

            await self.db.update_conversation(user.id)
            if await self.db.is_verified(user.id):
                await self.reply_success(update,
                                         "🎉 *验证通过！欢迎使用！* 🎉\n\n您可以开始与管理员对话了！😊",
                                         parse_mode="MarkdownV2"
                                         )
                return

            verification = await self.db.get_verification(user.id)
            if verification and verification.message_id:
                remaining = 3 - verification.error_count
                if remaining > 0:
                    try:
                        # 检查消息是否仍有效
                        await self.application.bot.get_chat(user.id)  # 确保用户未拉黑机器人
                        await self.reply_success(update,
                                                 f"您已有正在进行的验证，请完成当前题目！\n剩余尝试次数：*{remaining}/3*",
                                                 parse_mode="MarkdownV2"
                                                 )
                        logger.debug(
                            f"Prompted user {user.id} to continue existing verification, message_id={verification.message_id}")
                        return
                    except Forbidden:
                        logger.warning(f"User {user.id} has blocked the bot, blocking user")
                        await self.db.block_user(user.id, "用户禁用机器人")
                        return
                    except BadRequest:
                        logger.debug(
                            f"Verification message {verification.message_id} for user {user.id} is invalid, resetting")
                        verification.message_id = None
                        await self.db.update_verification(verification)
                    except Exception as e:
                        logger.error(f"Failed to check verification message for user {user.id}: {str(e)}")
                        verification.message_id = None
                        await self.db.update_verification(verification)

            if not verification:
                verification = Verification(
                    user_id=user.id,
                    question="",
                    answer=0.0,
                    options=[],
                    verified=False,
                    verification_time=datetime.datetime.now(BEIJING_TZ).astimezone(pytz.UTC).replace(tzinfo=None),
                    error_count=0
                )
                await self.db.add_verification(verification)

            try:
                await self.start_verification(user.id, verification)
            except Exception as e:
                logger.error(f"Failed to start verification for user {user.id}: {str(e)}", exc_info=True)
                await self.reply_error(update, Config.MESSAGE_TEMPLATES["telegram_error_generic"])

    @handle_errors
    async def ban(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # 删除指令消息
        if update.message:
            try:
                await update.message.delete()
                logger.debug(f"Deleted /ban command message for admin {update.effective_user.id}")
            except Exception as e:
                logger.debug(f"Failed to delete /ban command message for admin {update.effective_user.id}: {str(e)}")

        if not await self._restrict_and_validate(update):
            return
        if not await self._check_mutex(update, CommandType.BAN):
            return
        logger.debug(f"Processing /ban command for user {update.effective_user.id}, args: {context.args}")
        try:
            user_id = int(context.args[0])
            await self.ban_handler.ban_user(
                user_id=user_id,
                update=update,
                context=context,
                needs_confirmation=False
            )
        except (ValueError, IndexError):
            logger.debug(f"No valid user ID provided for /ban by user {update.effective_user.id}")
            await self._request_user_id(update, context, CommandType.BAN)

    @handle_errors
    async def unban(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # 删除指令消息
        if update.message:
            try:
                await update.message.delete()
                logger.debug(f"Deleted /unban command message for admin {update.effective_user.id}")
            except Exception as e:
                logger.debug(f"Failed to delete /unban command message for admin {update.effective_user.id}: {str(e)}")

        if not await self._restrict_and_validate(update):
            return
        if not await self._check_mutex(update, CommandType.UNBAN):
            return
        logger.debug(f"Processing /unban command for user {update.effective_user.id}, args: {context.args}")
        try:
            user_id = int(context.args[0])
            await self._execute_command(CommandType.UNBAN, update, context, user_id)
        except (ValueError, IndexError):
            logger.debug(f"No valid user ID provided for /unban by user {update.effective_user.id}")
            await self._request_user_id(update, context, CommandType.UNBAN)

    async def _send_user_list(self, update: Update, users: List[UserInfo], is_blacklist: bool, is_button: bool):
        if not users:
            message = Config.MESSAGE_TEMPLATES["telegram_blacklist_empty"] if is_blacklist else \
            Config.MESSAGE_TEMPLATES["telegram_list_users_empty"]
            await self.reply_error(update, message, parse_mode=None)
            return

        reply_method = await self._get_reply_method(update, is_button)
        for user in users:
            message = UserInfo.format(user, blocked=is_blacklist)
            buttons = [
                [InlineKeyboardButton("✅ 解除拉黑", callback_data=f"cb_unban_{user.user_id}")] if is_blacklist else
                [
                    InlineKeyboardButton("🚫 拉黑", callback_data=f"confirm_ban_{user.user_id}"),
                    InlineKeyboardButton("💬 切换对话", callback_data=f"cb_switch_{user.user_id}")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(buttons)
            await reply_method(message, reply_markup=reply_markup, parse_mode=None)

    @handle_errors
    async def chat(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # 删除指令消息
        if update.message:
            try:
                await update.message.delete()
                logger.debug(f"Deleted /chat command message for admin {update.effective_user.id}")
            except Exception as e:
                logger.debug(f"Failed to delete /chat command message for admin {update.effective_user.id}: {str(e)}")

        if not await self._restrict_and_validate(update):
            return
        if not await self._check_mutex(update, CommandType.CHAT):
            return
        logger.debug(f"Processing /chat command for user {update.effective_user.id}, args: {context.args}")
        try:
            user_id = int(context.args[0])
            await self._execute_command(CommandType.CHAT, update, context, user_id)
        except (ValueError, IndexError):
            logger.debug(f"No valid user ID provided for /chat by user {update.effective_user.id}")
            await self._request_user_id(update, context, CommandType.CHAT)

    @handle_errors
    async def list_users(self, update: Update, context: ContextTypes.DEFAULT_TYPE, is_button: bool = False):
        # 删除指令消息（仅当不是按键触发）
        if update.message and not is_button:
            try:
                await update.message.delete()
                logger.debug(f"Deleted /list command message for admin {update.effective_user.id}")
            except Exception as e:
                logger.debug(f"Failed to delete /list command message for admin {update.effective_user.id}: {str(e)}")

        if not await self._restrict_and_validate(update):
            return
        try:
            users = await self.db.get_verified_users()
            users = users[:3]  # 限制为最近三位用户
            await self._send_user_list(update, users, is_blacklist=False, is_button=is_button)
            logger.info(f"List users command completed for user {update.effective_user.id}")
        except AttributeError as e:
            logger.error(f"Failed to call _send_user_list in list_users for user {update.effective_user.id}: {str(e)}",
                         exc_info=True)
            await self.reply_error(update, Config.MESSAGE_TEMPLATES["telegram_error_generic"])
        except Exception as e:
            logger.error(f"Error in list_users for user {update.effective_user.id}: {str(e)}", exc_info=True)
            await self.reply_error(update, Config.MESSAGE_TEMPLATES["telegram_error_generic"])

    @handle_errors
    async def blacklist(self, update: Update, context: ContextTypes.DEFAULT_TYPE, is_button: bool = False):
        # 删除指令消息（仅当不是按键触发）
        if update.message and not is_button:
            try:
                await update.message.delete()
                logger.debug(f"Deleted /blacklist command message for admin {update.effective_user.id}")
            except Exception as e:
                logger.debug(
                    f"Failed to delete /blacklist command message for admin {update.effective_user.id}: {str(e)}")

        if not await self._restrict_and_validate(update):
            return
        try:
            blocked_users = await self.db.get_blacklist()
            await self._send_user_list(update, blocked_users, is_blacklist=True, is_button=is_button)
            logger.info(f"Blacklist command completed for user {update.effective_user.id}")
        except AttributeError as e:
            logger.error(f"Failed to call _send_user_list in blacklist for user {update.effective_user.id}: {str(e)}",
                         exc_info=True)
            await self.reply_error(update, Config.MESSAGE_TEMPLATES["telegram_error_generic"])
        except Exception as e:
            logger.error(f"Error in blacklist for user {update.effective_user.id}: {str(e)}", exc_info=True)
            await self.reply_error(update, Config.MESSAGE_TEMPLATES["telegram_error_generic"])

    @handle_errors
    async def count(self, update: Update, context: ContextTypes.DEFAULT_TYPE, is_button: bool = False):
        # 删除指令消息（仅当不是按键触发）
        if update.message and not is_button:
            try:
                await update.message.delete()
                logger.debug(f"Deleted /count command message for admin {update.effective_user.id}")
            except Exception as e:
                logger.debug(f"Failed to delete /count command message for admin {update.effective_user.id}: {str(e)}")

        if not await self._restrict_and_validate(update):
            return
        try:
            async with asyncio.timeout(10):
                stats = await self.db.get_stats()
                message = (
                    "📊 *统计信息*\n\n"
                    f"👥 总用户数: {stats['total_users']}\n"
                    f"🆕 今日新用户: {stats['new_users']}\n"
                    f"🚫 已拉黑用户: {stats['blocked_users']}\n"
                    f"✅ 已验证用户: {stats['verified_users']}"
                )
                reply_method = await self._get_reply_method(update, is_button)
                await reply_method(message, parse_mode="MarkdownV2")
                logger.info(f"Count command completed for user {update.effective_user.id}")
        except asyncio.TimeoutError:
            logger.warning(f"Timeout in count command for user {update.effective_user.id}")
            await self.reply_error(update, "操作超时，请稍后重试")
        except Exception as e:
            logger.error(f"Error in count command for user {update.effective_user.id}: {str(e)}", exc_info=True)
            await self.reply_error(update, Config.MESSAGE_TEMPLATES["telegram_error_generic"])

    @handle_errors
    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE, is_button: bool = False):
        # 删除指令消息（仅当不是按键触发）
        if update.message and not is_button:
            try:
                await update.message.delete()
                logger.debug(f"Deleted /status command message for admin {update.effective_user.id}")
            except Exception as e:
                logger.debug(f"Failed to delete /status command message for admin {update.effective_user.id}: {str(e)}")

        if not await self._restrict_and_validate(update):
            return
        user_id = update.effective_user.id
        logger.debug(f"Starting status command for user {user_id}")
        try:
            async with asyncio.timeout(10):
                logger.debug(f"Fetching webhook info for user {user_id}")
                webhook_info = await self.application.bot.get_webhook_info()
                logger.debug(f"Webhook info retrieved: {webhook_info}")

                bot_status = "在线😊" if webhook_info.url is not None else "离线😣"
                certificate_status = "✅ 安全" if not webhook_info.has_custom_certificate else "❌ 不安全"
                pending_updates = webhook_info.pending_update_count if webhook_info.pending_update_count is not None else "未知"
                ip_address = escape_markdown_v2(webhook_info.ip_address or "未知")

                current_chat_info = "无"
                reply_markup = None
                try:
                    logger.debug(f"Fetching current chat info for user {user_id}")
                    target_user_id, user_info, error_msg = await self.forward_handler.get_current_chat_with_validation(
                        user_id)
                    if user_info and not error_msg:
                        escaped_nickname = escape_markdown_v2(user_info.nickname or "未知用户")
                        current_chat_info = f"[{escaped_nickname}](tg://user?id={user_info.user_id})"
                        buttons = [[InlineKeyboardButton("重置对话目标", callback_data="reset_chat")]]
                        reply_markup = InlineKeyboardMarkup(buttons)
                        logger.debug(f"Current chat info set: {current_chat_info} with reset button")
                    else:
                        logger.debug(f"No valid chat target for user {user_id}, error: {error_msg}")
                except Exception as e:
                    logger.error(f"Failed to get current chat info for user {user_id}: {str(e)}", exc_info=True)
                    current_chat_info = "错误"

                message = (
                    "📡 *机器人状态*\n\n"
                    f"🤖 机器人状态: {bot_status}\n"
                    f"🔒 证书安全: {certificate_status}\n"
                    f"📬 待处理更新: {pending_updates}\n"
                    f"📍 服务器 IP: {ip_address}\n"
                    f"💬 当前对话目标: {current_chat_info}"
                )
                reply_method = await self._get_reply_method(update, is_button)
                await reply_method(message, parse_mode="MarkdownV2", reply_markup=reply_markup)
                logger.info(f"Status command completed for user {user_id}")
        except asyncio.TimeoutError:
            logger.warning(f"Timeout in status command for user {user_id}")
            await self.reply_error(update, "操作超时，请稍后重试")
        except Exception as e:
            logger.error(f"Error in status command for user {user_id}: {str(e)}", exc_info=True)
            await self.reply_error(update, Config.MESSAGE_TEMPLATES["telegram_error_generic"])

    @handle_errors
    async def clean(self, update: Update, context: ContextTypes.DEFAULT_TYPE, is_button: bool = False):
        # 删除指令消息（仅当不是按键触发）
        if update.message and not is_button:
            try:
                await update.message.delete()
                logger.debug(f"Deleted /clean command message for admin {update.effective_user.id}")
            except Exception as e:
                logger.debug(f"Failed to delete /clean command message for admin {update.effective_user.id}: {str(e)}")

        if not await self._restrict_and_validate(update):
            return
        buttons = [
            [
                InlineKeyboardButton("确认清除 ✅", callback_data="confirm_clean"),
                InlineKeyboardButton("取消 ❌", callback_data="cancel_clean")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(buttons)
        warning_message = (
            "⚠️ *警告：此操作不可逆！*\n\n"
            "执行清除将删除所有用户数据，包括验证记录和黑名单。\n"
            "请谨慎操作，确认是否继续？"
        )
        reply_method = await self._get_reply_method(update, is_button)
        await reply_method(warning_message, reply_markup=reply_markup, parse_mode="MarkdownV2")

    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        error_msg = str(context.error) if context.error else "Unknown error"
        logger.error(f"Update {update} caused error: {error_msg}", exc_info=True)
        if update and (update.message or (update.callback_query and update.callback_query.message)):
            try:
                await self.reply_error(update, Config.MESSAGE_TEMPLATES["telegram_error_generic"])
            except Exception as e:
                logger.error(f"Failed to send error message in error_handler: {str(e)}", exc_info=True)

    async def set_bot_commands(self):
        admin_commands = [
            BotCommand("start", "使用机器人"),
            BotCommand("ban", "拉黑用户：<id>"),
            BotCommand("unban", "解除拉黑：<id>"),
            BotCommand("chat", "切换对话目标：<id>"),
            BotCommand("list", "列出最近验证用户"),
            BotCommand("blacklist", "列出黑名单"),
            BotCommand("status", "查看机器人状态和当前对话目标"),
            BotCommand("clean", "清除数据库"),
            BotCommand("count", "查看用户统计信息")
        ]
        user_commands = [
            BotCommand("start", "使用机器人")
        ]

        await self.application.bot.set_my_commands(
            user_commands,
            scope={"type": "all_private_chats"}
        )

        await self.application.bot.set_my_commands(
            admin_commands,
            scope={"type": "chat", "chat_id": Config.ADMIN_ID}
        )

        logger.info("Bot commands set successfully: admin_commands=%s, user_commands=%s",
                    [cmd.command for cmd in admin_commands],
                    [cmd.command for cmd in user_commands])