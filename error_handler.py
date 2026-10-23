"""
Error Handling, Safety Wrappers, Structured Logging, and Pending Expiration Manager.

Ensures:
- Secrets (tokens, API keys, sheet IDs) are masked in all logs.
- Safe wrappers with exponential backoff for Gemini API rate limits.
- Upstream service error isolation (FFmpeg, Gemini, Google Sheets).
- Pending confirmation state management with explicit 5-minute expiration.
- Friendly Amharic user failure notices without exposing internal traces.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta
from functools import wraps
from typing import Any, Callable, Optional, TypeVar

from validation import Expense

# Configure Structured Logging
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | [%(name)s] [%(filename)s:%(lineno)d] | %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("ExpenseAgent")

# Sensitive tokens to mask in logs
SENSITIVE_ENV_KEYS = [
    "TELEGRAM_BOT_TOKEN",
    "GEMINI_API_KEY",
    "GOOGLE_SHEET_ID",
]


class SecretMaskingFilter(logging.Filter):
    """Filter that masks sensitive tokens and keys in log output."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not isinstance(record.msg, str):
            record.msg = str(record.msg)

        for env_key in SENSITIVE_ENV_KEYS:
            secret = os.getenv(env_key)
            if secret and len(secret) > 4:
                # Mask secret leaving only first 2 and last 2 characters
                masked = f"{secret[:2]}***{secret[-2:]}"
                record.msg = record.msg.replace(secret, masked)

        # Mask generic bot tokens (123456:ABC-DEF...)
        record.msg = re.sub(
            r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b",
            "[MASKED_TELEGRAM_TOKEN]",
            record.msg,
        )
        return True


# Apply masking filter to root logger
for handler in logging.root.handlers:
    handler.addFilter(SecretMaskingFilter())


# Custom Exception Hierarchy
class ExpenseAgentException(Exception):
    """Base exception for all expense agent errors."""
    def __init__(self, message: str, error_code: str = "INTERNAL_ERROR", user_friendly_am: Optional[str] = None):
        super().__init__(message)
        self.error_code = error_code
        self.user_friendly_am = user_friendly_am or (
            "ይቅርታ፣ ያልታሰበ ችግር አጋጥሟል። እባክዎ ከጥቂት ደቂቃዎች በኋላ እንደገና ይሞክሩ።"
        )


class AudioConversionError(ExpenseAgentException):
    """Raised when audio download or FFmpeg conversion fails."""
    def __init__(self, message: str):
        super().__init__(
            message=message,
            error_code="AUDIO_CONVERSION_ERROR",
            user_friendly_am="ይቅርታ፣ የድምፅ መልእክትዎን ማስተናገድ አልተቻለም። እባክዎ ድምፅዎን በTelegram Voice በግልጽ ደግመው ይላኩ።",
        )


class GeminiServiceError(ExpenseAgentException):
    """Raised when Gemini API fails or returns invalid responses."""
    def __init__(self, message: str):
        super().__init__(
            message=message,
            error_code="GEMINI_API_ERROR",
            user_friendly_am="ይቅርታ፣ በGemini AI አገልግሎት ላይ ጊዜያዊ ችግር አጋጥሟል። እባክዎ ከጥቂት ሰከንዶች በኋላ እንደገና ይሞክሩ።",
        )


class GeminiRateLimitError(GeminiServiceError):
    """Raised when Gemini API rate limit is exceeded."""
    def __init__(self, message: str):
        super().__init__(message=message)
        self.error_code = "RATE_LIMIT_EXCEEDED"
        self.user_friendly_am = "ስርዓቱ በአሁኑ ወቅት በስራ ተጠምዷል። እባክዎ ከጥቂት ደቂቃዎች በኋላ እንደገና ይሞክሩ።"


class SheetsServiceError(ExpenseAgentException):
    """Raised when Google Sheets write or read operation fails."""
    def __init__(self, message: str):
        super().__init__(
            message=message,
            error_code="SHEETS_API_ERROR",
            user_friendly_am="ይቅርታ፣ መረጃውን ወደ Google Sheet ማስቀመጥ አልተቻለም። ምንም መረጃ አልተመዘገበም። እባክዎ እንደገና ይሞክሩ።",
        )


class ConfirmationExpiredError(ExpenseAgentException):
    """Raised when a pending confirmation has expired (> 5 minutes)."""
    def __init__(self, message: str = "Pending action expired"):
        super().__init__(
            message=message,
            error_code="CONFIRMATION_EXPIRED",
            user_friendly_am="ይህ የማረጋገጫ ጥያቄ ጊዜው አልፎበታል (ከ5 ደቂቃ በላይ ቆይቷል)። ምንም ወጪ አልተመዘገበም። እባክዎ ወጪውን እንደገና ይላኩ።",
        )


T = TypeVar("T")


def with_retry(
    max_retries: int = 3,
    base_delay: float = 1.0,
    backoff_factor: float = 2.0,
    retryable_exceptions: tuple[type[Exception], ...] = (Exception,),
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """
    Decorator for exponential backoff retries on rate limits and transient network glitches.
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        func_name = getattr(func, "__name__", str(func))

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            delay = base_delay
            last_exception = None

            for attempt in range(1, max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except retryable_exceptions as exc:
                    last_exception = exc
                    err_msg = str(exc).lower()

                    # Check for rate-limiting keywords
                    is_rate_limit = any(term in err_msg for term in ["429", "resource_exhausted", "quota", "rate limit"])
                    if attempt == max_retries:
                        logger.error(
                            "Function %s failed after %d attempts. Last error: %s",
                            func_name,
                            attempt,
                            exc,
                        )
                        if is_rate_limit:
                            raise GeminiRateLimitError(f"Rate limit exceeded after {max_retries} retries: {exc}") from exc
                        raise

                    sleep_time = delay * (backoff_factor ** (attempt - 1))
                    logger.warning(
                        "Transient failure in %s (attempt %d/%d): %s. Retrying in %.2fs...",
                        func_name,
                        attempt,
                        max_retries,
                        exc,
                        sleep_time,
                    )
                    time.sleep(sleep_time)

            if last_exception:
                raise last_exception
            raise RuntimeError(f"Unexpected retry exit for {func_name}")

        return wrapper
    return decorator


class PendingConfirmation:
    """Represents a pending expense awaiting user approval."""

    def __init__(
        self,
        action_id: str,
        user_id: int,
        expense: Expense,
        created_at: Optional[datetime] = None,
        max_age_seconds: int = 300,  # 5 minutes
    ):
        self.action_id = action_id
        self.user_id = user_id
        self.expense = expense
        self.created_at = created_at or datetime.now(timezone.utc)
        self.max_age_seconds = max_age_seconds

    @property
    def is_expired(self) -> bool:
        """Checks if 5 minutes have elapsed since creation."""
        now = datetime.now(timezone.utc)
        age = (now - self.created_at).total_seconds()
        return age > self.max_age_seconds


class PendingConfirmationManager:
    """
    In-memory store managing unconfirmed expense proposals with auto-expiration.
    """

    def __init__(self, default_ttl_seconds: int = 300):
        self.default_ttl = default_ttl_seconds
        self._store: dict[str, PendingConfirmation] = {}

    def create_pending(
        self,
        user_id: int,
        expense: Expense,
    ) -> str:
        """
        Stores an unconfirmed expense and returns a unique action_id.
        """
        self.cleanup_expired()
        action_id = uuid.uuid4().hex[:12]
        self._store[action_id] = PendingConfirmation(
            action_id=action_id,
            user_id=user_id,
            expense=expense,
            max_age_seconds=self.default_ttl,
        )
        logger.info(
            "Created pending confirmation [action_id=%s] for user_id=%s (%s ETB)",
            action_id,
            user_id,
            expense.amount,
        )
        return action_id

    def get_pending(self, action_id: str, user_id: int) -> Optional[PendingConfirmation]:
        """
        Retrieves pending action if it exists, belongs to user, and is unexpired.
        Raises ConfirmationExpiredError if expired.
        """
        pending = self._store.get(action_id)
        if not pending:
            return None

        if pending.user_id != user_id:
            logger.warning("User %s attempted to access action_id %s belonging to %s", user_id, action_id, pending.user_id)
            return None

        if pending.is_expired:
            self._store.pop(action_id, None)
            raise ConfirmationExpiredError(f"Action {action_id} has expired (> {self.default_ttl}s).")

        self.cleanup_expired()
        return pending

    def pop_pending(self, action_id: str, user_id: int) -> Optional[Expense]:
        """
        Retrieves and removes pending expense upon confirmation.
        """
        pending = self.get_pending(action_id, user_id)
        if pending:
            self._store.pop(action_id, None)
            return pending.expense
        return None

    def cancel_pending(self, action_id: str, user_id: int) -> bool:
        """
        Cancels a pending expense proposal.
        """
        pending = self._store.get(action_id)
        if pending and pending.user_id == user_id:
            self._store.pop(action_id, None)
            logger.info("Cancelled pending confirmation [action_id=%s] for user_id=%s", action_id, user_id)
            return True
        return False

    def cleanup_expired(self) -> int:
        """Removes expired pending proposals to prevent memory leak."""
        expired_keys = [k for k, v in self._store.items() if v.is_expired]
        for k in expired_keys:
            self._store.pop(k, None)
        return len(expired_keys)
