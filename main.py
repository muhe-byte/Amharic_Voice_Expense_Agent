"""
Main Telegram Bot Entrypoint for Amharic Voice Expense Agent.

Wired with:
- python-telegram-bot async application
- Allowed user ID authorization checking
- Voice note download and FFmpeg MP3 conversion
- Gemini agent verbatim Fidel transcription and structured extraction
- Pydantic schema validation and Africa/Addis_Ababa date resolution
- Safe confirmation gate with 5-minute expiration
- Google Sheets write tool with duplicate-message prevention
- Deterministic read-only spending query handling
- Structured logging with secret token masking
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Optional

# Ensure UTF-8 output on Windows consoles so emojis and Fidel characters don't cause charmap errors
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import audio
from agent import ExpenseAgent
from error_handler import (
    AudioConversionError,
    ConfirmationExpiredError,
    GeminiRateLimitError,
    GeminiServiceError,
    PendingConfirmationManager,
    logger,
)
from sheets_service import (
    DuplicateExpenseError,
    GoogleSheetsService,
    SheetsOperationError,
    get_sheets_service,
)
import spending_service
from validation import Expense

# Load environment variables
dotenv.load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_USER_IDS_STR = os.getenv("ALLOWED_USER_IDS", "").strip()

# Initialize services
pending_manager = PendingConfirmationManager(default_ttl_seconds=300)  # 5 minutes
pending_edits: dict[int, str] = {}  # user_id -> action_id
gemini_agent: Optional[ExpenseAgent] = None
sheets_service: Optional[GoogleSheetsService] = None


def get_allowed_user_ids() -> set[int]:
    """Parses ALLOWED_USER_IDS environment variable into a set of integer IDs."""
    if not ALLOWED_USER_IDS_STR:
        return set()
    ids = set()
    for raw_id in ALLOWED_USER_IDS_STR.split(","):
        clean_id = raw_id.strip()
        if clean_id.isdigit():
            ids.add(int(clean_id))
    return ids


def is_authorized(user_id: int) -> bool:
    """Verifies whether the Telegram user is authorized to use the bot."""
    allowed = get_allowed_user_ids()
    if not allowed:
        # If not set, allow all for MVP / live testing, but log notice
        return True
    return user_id in allowed


async def check_auth_or_reject(update: Update) -> bool:
    """Helper to reject unauthorized users gracefully."""
    user = update.effective_user
    if not user:
        return False
    if not is_authorized(user.id):
        logger.warning("Unauthorized access attempt from user_id=%s (%s)", user.id, user.username)
        if update.effective_message:
            await update.effective_message.reply_text(
                "⛔ ይቅርታ፣ ይህንን ቦት ለመጠቀም ፈቃድ የለዎትም። (Unauthorized user)."
            )
        return False
    return True


def format_confirmation_message(expense: Expense) -> str:
    """Formats the pending proposal card for user approval."""
    date_note = f"`{expense.transaction_date}`"
    if expense.date_expression and expense.date_expression != expense.transaction_date:
        date_note += f" ({expense.date_expression})"
    elif not expense.date_expression or expense.date_expression == "ዛሬ":
        date_note += " (ዛሬ)"

    lines = [
        "📝 *የቀረበ የወጪ መረጃ (Expense Proposal)*",
        "━━━━━━━━━━━━━━━━━━━━",
        f"💰 *መጠን (Amount):* `{expense.amount:,.2f} {expense.currency}`",
        f"🏷️ *ምድብ (Category):* `{expense.category}`",
        f"📅 *ቀን (Date):* {date_note}",
        f"🗒️ *ማስታወሻ (Description):* {expense.description or 'Expense'}",
        f"🎙️ *የድምፅ ጽሑፍ (Transcript):* _{expense.transcript}_",
        "━━━━━━━━━━━━━━━━━━━━",
        "ይህ ወጪ ወደ Google Sheet ይመዝገብ? (Save this expense?)",
        "⏱️ *ማስታወሻ:* ይህ ጥያቄ ከ5 ደቂቃ በኋላ ጊዜው ያልፋል።",
    ]
    return "\n".join(lines)


def build_confirmation_keyboard(action_id: str) -> InlineKeyboardMarkup:
    """Creates the Yes / Edit / Cancel inline keyboard."""
    keyboard = [
        [
            InlineKeyboardButton("✅ አዎ (አስቀምጥ) / Yes", callback_data=f"confirm:{action_id}"),
            InlineKeyboardButton("✏️ አርም / Edit", callback_data=f"edit:{action_id}"),
        ],
        [
            InlineKeyboardButton("❌ ሰርዝ / Cancel", callback_data=f"cancel:{action_id}"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


# Command Handlers
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /start command."""
    if not await check_auth_or_reject(update):
        return

    welcome_text = (
        "👋 *እንኳን ወደ አማርኛ የድምፅ ወጪ መመዝገቢያ ወኪል በደህና መጡ!*"
        "\n\n"
        "ይህ ቦት የዕለት ተዕለት ወጪዎችዎን በድምፅ ወይም በጽሑፍ ተቀብሎ በGoogle Sheet ላይ ይመዘግባል፣ እንዲሁም የወጪ ጥያቄዎችን ይመልሳል።"
        "\n\n"
        "📌 *እንዴት እንደሚጠቀሙበት:*\n"
        "1. *የድምፅ መልእክት ይላኩ:* ለምሳሌ 'ትናንት ለታክሲ 250 ብር ከፈልኩ'።\n"
        "2. *በጽሑፍ ይላኩ:* ለምሳሌ 'ዛሬ ለምሳ 150 ብር'።\n"
        "3. *የወጪ ጥያቄ ይጠይቁ:* ለምሳሌ 'ምን ያህል ወጪ አደረግኩ?' ወይም 'የዚህ ሳምንት ወጪ ስንት ነው?'።\n\n"
        "እባክዎ አሁን የድምፅ ወይም የጽሑፍ ወጪዎን ይላኩ!"
    )
    await update.message.reply_text(welcome_text, parse_mode=ParseMode.MARKDOWN)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /help command."""
    if not await check_auth_or_reject(update):
        return

    help_text = (
        "💡 *የአጠቃቀም መመሪያ (Help & Instructions)*\n\n"
        "• *የወጪ ምሳሌዎች (ድምፅ ወይም ጽሑፍ):*\n"
        "  - 'ዛሬ ለምሳ ሁለት መቶ ሃምሳ ብር አወጣሁ'\n"
        "  - 'ትናንት ለትራንስፖርት 300 ብር ከፈልኩ'\n"
        "  - 'ከትናንት ወዲያ ለሱፐርማርኬት 1500 ብር'\n\n"
        "• *የወጪ ጥያቄዎች:*\n"
        "  - 'ጠቅላላ ወጪዬ ስንት ነው?'\n"
        "  - 'ለትራንስፖርት ምን ያህል አወጣሁ?'\n"
        "  - 'የምግብ ወጪ ስንት ነው?'\n\n"
        "• *ማረጋገጫ:* እያንዳንዱ ወጪ ከመመዝገቡ በፊት የቀረበውን መረጃ አይተው [✅ አዎ] ካላሉ በስተቀር አይመዘገብም።"
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shortcut command to get total spending summary."""
    if not await check_auth_or_reject(update):
        return

    await handle_spending_inquiry(update, query_text="ጠቅላላ ወጪዬ ስንት ነው?")


# Voice Message Handler
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Downloads voice message, converts to MP3 via FFmpeg, passes to Gemini,
    validates fields, and displays confirmation gate.
    """
    if not await check_auth_or_reject(update):
        return

    message = update.effective_message
    user = update.effective_user
    if not message or not message.voice:
        return

    chat_id = message.chat_id
    message_id = message.message_id
    source_message_id = f"{chat_id}:{message_id}"

    status_msg = await message.reply_text("🎧 የድምፅ መልእክትዎን በማዳመጥ ላይ ነኝ... (Processing voice note...)")

    temp_oga = None
    temp_mp3 = None

    try:
        # Download voice file (.oga)
        voice_file = await context.bot.get_file(message.voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".oga", delete=False) as oga_f:
            temp_oga = Path(oga_f.name)

        await voice_file.download_to_drive(custom_path=str(temp_oga))

        # Convert to MP3 using FFmpeg
        temp_mp3 = audio.convert_to_mp3(temp_oga)

        # Process with Gemini agent
        global gemini_agent
        if gemini_agent is None:
            gemini_agent = ExpenseAgent()

        expense, payload = gemini_agent.process_audio(
            audio_input=temp_mp3,
            source_message_id=source_message_id,
        )

        # If audio was unintelligible or not an expense
        if not payload.is_clear or payload.intent != "add_expense" or expense is None:
            clarification = payload.clarification_question or "ስንት ብር እንደከፈሉ በግልጽ ይንገሩኝ?"
            response_text = (
                f"🎙️ *የሰማሁት (Heard):* _{payload.transcript}_\n\n"
                f"❓ *ጥያቄ:* {clarification}\n\n"
                f"ℹ️ እባክዎ የወጪውን መጠን ጠቅሰው በድምፅ ወይም በጽሑፍ እንደገና ይላኩ።"
            )
            await status_msg.edit_text(response_text, parse_mode=ParseMode.MARKDOWN)
            return

        # Store in PendingConfirmationManager with 5-minute expiration
        action_id = pending_manager.create_pending(user_id=user.id, expense=expense)

        # Send confirmation card with Inline Keyboard
        card_text = format_confirmation_message(expense)
        keyboard = build_confirmation_keyboard(action_id)
        await status_msg.edit_text(card_text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

    except AudioConversionError as err:
        logger.error("Audio conversion failed for message %s: %s", source_message_id, err)
        await status_msg.edit_text(
            "❌ ይቅርታ፣ የድምፅ መልእክቱን ወደ MP3 መቀየር አልተቻለም። እባክዎ ድምፅዎን እንደገና ይላኩ።"
        )
    except GeminiRateLimitError as err:
        logger.warning("Gemini rate limit encountered: %s", err)
        await status_msg.edit_text(
            "⏳ ስርዓቱ በአሁኑ ወቅት በስራ ተጠምዷል። እባክዎ ከ1-2 ደቂቃ በኋላ እንደገና ይሞክሩ።"
        )
    except GeminiServiceError as err:
        logger.error("Gemini service error: %s", err)
        await status_msg.edit_text(
            "❌ በGemini AI አገልግሎት ላይ ጊዜያዊ ችግር አጋጥሟል። ምንም ወጪ አልተመዘገበም። እባክዎ ጥቂት ቆይተው ይሞክሩ።"
        )
    except Exception as err:
        logger.error("Unexpected error in handle_voice: %s", err, exc_info=True)
        await status_msg.edit_text(
            "❌ ያልታሰበ ስህተት አጋጥሟል። ምንም መረጃ አልተቀመጠም። እባክዎ ደግመው ይሞክሩ።"
        )
    finally:
        audio.cleanup_files(temp_oga, temp_mp3)


# Text Message Handler
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles text messages:
    1. If user is in 'edit' mode, updates the pending expense.
    2. If text is a spending question, routes to deterministic calculator (read-only).
    3. If text is an expense note, validates and shows confirmation gate.
    """
    if not await check_auth_or_reject(update):
        return

    message = update.effective_message
    user = update.effective_user
    if not message or not message.text:
        return

    user_text = message.text.strip()
    chat_id = message.chat_id
    message_id = message.message_id
    source_message_id = f"{chat_id}:{message_id}"

    # 1. Check if user is editing an active pending proposal
    if user.id in pending_edits:
        action_id = pending_edits.pop(user.id)
        try:
            pending = pending_manager.get_pending(action_id, user.id)
            if pending:
                # Re-process text to extract new amount or details
                global gemini_agent
                if gemini_agent is None:
                    gemini_agent = ExpenseAgent()

                new_expense, payload = gemini_agent.process_text(
                    text=user_text,
                    source_message_id=source_message_id,
                )
                if new_expense:
                    pending.expense = new_expense
                    card_text = format_confirmation_message(new_expense)
                    keyboard = build_confirmation_keyboard(action_id)
                    await message.reply_text(
                        f"✏️ *መረጃው ተስተካክሏል!*\n\n{card_text}",
                        reply_markup=keyboard,
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    return
        except ConfirmationExpiredError:
            await message.reply_text(
                "⏱️ የሚስተካከለው ወጪ ጊዜው አልፎበታል (ከ5 ደቂቃ በላይ ቆይቷል)። እባክዎ ወጪውን እንደ አዲስ ይላኩ።"
            )
            return

    # 2. Check if user is asking a spending question (read-only)
    if spending_service.is_spending_query(user_text):
        await handle_spending_inquiry(update, query_text=user_text)
        return

    # 3. Regular text expense note
    status_msg = await message.reply_text("⏳ የወጪ መረጃውን በማስተናገድ ላይ ነኝ...")

    try:
        if gemini_agent is None:
            gemini_agent = ExpenseAgent()

        expense, payload = gemini_agent.process_text(
            text=user_text,
            source_message_id=source_message_id,
        )

        if not payload.is_clear or payload.intent != "add_expense" or expense is None:
            clarification = payload.clarification_question or "ስንት ብር እንደከፈሉ በግልጽ ይንገሩኝ?"
            response_text = (
                f"❓ *ጥያቄ:* {clarification}\n\n"
                f"ℹ️ እባክዎ የወጪውን መጠን ጠቅሰው እንደገና ይላኩ (ለምሳሌ፡ 'ለትራንስፖርት 250 ብር')።"
            )
            await status_msg.edit_text(response_text, parse_mode=ParseMode.MARKDOWN)
            return

        action_id = pending_manager.create_pending(user_id=user.id, expense=expense)
        card_text = format_confirmation_message(expense)
        keyboard = build_confirmation_keyboard(action_id)
        await status_msg.edit_text(card_text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

    except GeminiRateLimitError:
        await status_msg.edit_text(
            "⏳ ስርዓቱ በአሁኑ ወቅት በስራ ተጠምዷል። እባክዎ ከ1-2 ደቂቃ በኋላ እንደገና ይሞክሩ።"
        )
    except Exception as err:
        logger.error("Error processing text expense: %s", err, exc_info=True)
        await status_msg.edit_text(
            "❌ ወጪውን ማስተናገድ አልተቻለም። ምንም አልተመዘገበም። እባክዎ እንደገና ይሞክሩ።"
        )


async def handle_spending_inquiry(update: Update, query_text: str) -> None:
    """
    Executes a read-only query against Google Sheets and computes deterministic results in Python.
    """
    message = update.effective_message
    if not message:
        return

    status_msg = await message.reply_text("🔍 መረጃዎችን ከGoogle Sheet በማስላት ላይ ነኝ...")

    try:
        sheets = get_sheets_service()
        records = sheets.get_all_expenses()

        category_filter = spending_service.extract_query_category(query_text)
        summary = spending_service.calculate_spending(
            records=records,
            category=category_filter,
        )

        response_text = spending_service.format_spending_response(summary)
        await status_msg.edit_text(response_text)

    except SheetsOperationError as err:
        logger.error("Failed to read expenses for query: %s", err)
        await status_msg.edit_text(
            "❌ ይቅርታ፣ ከGoogle Sheet መረጃዎችን ማንበብ አልተቻለም። እባክዎ ጥቂት ቆይተው እንደገና ይሞክሩ።"
        )
    except Exception as err:
        logger.error("Unexpected error in spending inquiry: %s", err, exc_info=True)
        await status_msg.edit_text("❌ የወጪ ጥያቄውን ማስላት አልተቻለም።")


# Callback Query Handler (Inline Keyboard: Yes / Edit / Cancel)
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Processes inline button clicks:
    - confirm:<action_id>: Appends row to Google Sheets (checks duplicate prevention).
    - edit:<action_id>: Registers user in edit mode and requests corrected text.
    - cancel:<action_id>: Cancels pending proposal.
    """
    query = update.callback_query
    if not query or not query.data:
        return

    await query.answer()

    user = query.from_user
    if not is_authorized(user.id):
        await query.edit_message_text("⛔ ይቅርታ፣ ይህንን ቦት ለመጠቀም ፈቃድ የለዎትም።")
        return

    data = query.data
    if ":" not in data:
        return

    action_type, action_id = data.split(":", 1)

    # 1. Cancel Action
    if action_type == "cancel":
        cancelled = pending_manager.cancel_pending(action_id, user.id)
        if cancelled:
            await query.edit_message_text(
                "❌ *የወጪ ምዝገባው ተሰርዟል።* ምንም መረጃ አልተቀመጠም። (Expense cancelled.)",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await query.edit_message_text(
                "ℹ️ ይህ የማረጋገጫ ጥያቄ አስቀድሞ ተሰርዟል ወይም ጊዜው አልፎበታል።"
            )
        return

    # 2. Edit Action
    if action_type == "edit":
        try:
            pending = pending_manager.get_pending(action_id, user.id)
            if not pending:
                await query.edit_message_text("ℹ️ የሚስተካከለው ወጪ አልተገኘም ወይም ጊዜው አልፎበታል።")
                return

            pending_edits[user.id] = action_id
            await query.edit_message_text(
                f"✏️ *ማረም:* እባክዎ ትክክለኛውን የወጪ መጠን ወይም ምድብ በጽሑፍ ይላኩ (ለምሳሌ፡ '300 ብር' ወይም 'ምሳ 180 ብር')፦\n\n"
                f"ያሁኑ መረጃ: `{pending.expense.amount} {pending.expense.currency}` ({pending.expense.category})",
                parse_mode=ParseMode.MARKDOWN,
            )
        except ConfirmationExpiredError:
            await query.edit_message_text(
                "⏱️ ይህ የማረጋገጫ ጥያቄ ከ5 ደቂቃ በላይ ስለቆየ ጊዜው አልፎበታል። እባክዎ ወጪውን እንደገና ይላኩ።"
            )
        return

    # 3. Confirm Action (Save to Google Sheets)
    if action_type == "confirm":
        try:
            expense = pending_manager.pop_pending(action_id, user.id)
            if not expense:
                await query.edit_message_text(
                    "ℹ️ ይህ ወጪ አስቀድሞ ተመዝግቧል ወይም ጊዜው አልፎበታል።"
                )
                return

            # Append to Google Sheet (enforces duplicate prevention)
            sheets = get_sheets_service()
            res = sheets.append_expense_row(expense)

            # Report success ONLY after Google Sheets confirms the write
            success_msg = (
                "✅ *ወጪው በተሳካ ሁኔታ በGoogle Sheet ተመዝግቧል!*\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"🆔 *Transaction ID:* `{res['transaction_id']}`\n"
                f"💰 *መጠን:* `{res['amount']:,.2f} {res['currency']}`\n"
                f"🏷️ *ምድብ:* `{res['category']}`\n"
                f"📅 *ቀን:* `{res['transaction_date']}`\n"
                f"🗒️ *ማስታወሻ:* {res['transcript']}\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "🎉 ተጨማሪ ወጪ ካለዎት በድምፅ ወይም በጽሑፍ መላክ ይችላሉ!"
            )
            await query.edit_message_text(success_msg, parse_mode=ParseMode.MARKDOWN)

        except DuplicateExpenseError as err:
            logger.warning("Prevented duplicate expense write: %s", err)
            await query.edit_message_text(
                "⚠️ *ይህ ወጪ አስቀድሞ በGoogle Sheet ተመዝግቧል!* (Duplicate transaction prevented).\n"
                "ተደጋጋሚ ምዝገባን ለመከላከል ምንም አዲስ ረድፍ አልተጨመረም።",
                parse_mode=ParseMode.MARKDOWN,
            )
        except ConfirmationExpiredError:
            await query.edit_message_text(
                "⏱️ ይህ የማረጋገጫ ጥያቄ ከ5 ደቂቃ በላይ ስለቆየ ጊዜው አልፎበታል። ምንም አልተመዘገበም። እባክዎ ወጪውን እንደገና ይላኩ።"
            )
        except SheetsOperationError as err:
            logger.error("Failed to append row to Google Sheets: %s", err)
            await query.edit_message_text(
                "❌ *ስህተት:* መረጃውን ወደ Google Sheet ማስቀመጥ አልተቻለም። ምንም አልተመዘገበም። እባክዎ ጥቂት ቆይተው እንደገና ይሞክሩ።",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as err:
            logger.error("Unexpected error confirming expense: %s", err, exc_info=True)
            await query.edit_message_text(
                "❌ ያልታሰበ ስህተት አጋጥሟል። ምንም መረጃ አልተመዘገበም።"
            )


# Global Error Handler
async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catches uncaught exceptions across all telegram handlers and notifies the user."""
    logger.error("Exception while handling an update: %s", context.error, exc_info=context.error)

    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "ይቅርታ፣ ያልታሰበ ስህተት አጋጥሟል። እባክዎ ከጥቂት ደቂቃዎች በኋላ እንደገና ይሞክሩ።"
            )
        except Exception:
            pass


def main() -> None:
    """Builds and runs the Telegram bot application."""
    if not TELEGRAM_BOT_TOKEN:
        logger.critical("TELEGRAM_BOT_TOKEN is missing in .env! Cannot start bot.")
        print("\n[ERROR] TELEGRAM_BOT_TOKEN is missing in your .env file.")
        print("Please configure TELEGRAM_BOT_TOKEN before starting the bot.\n")
        return

    logger.info("Initializing Amharic Voice Expense Agent Bot...")

    from telegram.request import HTTPXRequest
    proxy_url = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or os.getenv("TELEGRAM_PROXY")
    request_kwargs: dict[str, Any] = {"connect_timeout": 30.0, "read_timeout": 30.0}
    if proxy_url:
        logger.info("Configuring Telegram bot proxy: %s", proxy_url)
        request_kwargs["proxy"] = proxy_url

    req = HTTPXRequest(**request_kwargs)
    app: Application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).request(req).build()

    # Register Command Handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("report", report_command))

    # Register Voice Message Handler
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    # Register Text Message Handler (filters out commands)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Register Inline Callback Query Handler
    app.add_handler(CallbackQueryHandler(handle_callback_query))

    # Register Global Error Handler
    app.add_error_handler(global_error_handler)

    logger.info("Bot application constructed successfully. Starting polling...")
    print("=" * 60)
    print("🚀 Amharic Voice Expense Agent Bot is now RUNNING!")
    print("Press Ctrl+C to stop.")
    print("=" * 60)

    try:
        app.run_polling(drop_pending_updates=True)
    except Exception as err:
        err_msg = str(err)
        if "Timed out" in err_msg or "ConnectTimeout" in err_msg:
            logger.critical("Network timeout while connecting to Telegram API: %s", err)
            print("\n" + "=" * 60)
            print("❌ [NETWORK ERROR] Connection to api.telegram.org timed out.")
            print("Cause: Telegram servers are unreachable from your current network")
            print("       (often blocked or restricted by local ISPs).")
            print("Fix: Turn on a VPN, or set HTTPS_PROXY in your environment/.env,")
            print("     then run the command again.")
            print("=" * 60 + "\n")
        else:
            raise


if __name__ == "__main__":
    main()
