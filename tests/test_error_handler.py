"""
Unit tests for error_handler module.
Covers SecretMaskingFilter, PendingConfirmationManager, expiration logic,
and exponential backoff retry mechanics.
"""

from datetime import datetime, timedelta, timezone
import logging
import os
import time
from unittest.mock import MagicMock, patch
import pytest

from error_handler import (
    ConfirmationExpiredError,
    GeminiRateLimitError,
    PendingConfirmation,
    PendingConfirmationManager,
    SecretMaskingFilter,
    with_retry,
)
from validation import Expense


@pytest.fixture
def sample_expense() -> Expense:
    return Expense(
        amount=250.0,
        currency="ETB",
        category="Transport",
        transcript="ትናንት ለትራንስፖርት 250 ብር ከፈልኩ።",
        date_expression="ትናንት",
        source_message_id="100:1",
    )


class TestSecretMaskingFilter:
    """Tests for redacting tokens and API keys in logs."""

    def test_masks_telegram_token(self, monkeypatch):
        test_token = "123456789:ABCdefGHIjklMNOpqrsTUVwxyz1234567"
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", test_token)

        flt = SecretMaskingFilter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg=f"Connecting to bot with token {test_token}",
            args=(),
            exc_info=None,
        )

        flt.filter(record)
        assert test_token not in record.msg
        assert "12***67" in record.msg or "[MASKED_TELEGRAM_TOKEN]" in record.msg


class TestPendingConfirmationManager:
    """Tests for pending expense proposal lifecycle and expiration."""

    def test_create_and_pop_pending(self, sample_expense: Expense):
        mgr = PendingConfirmationManager(default_ttl_seconds=300)
        action_id = mgr.create_pending(user_id=12345, expense=sample_expense)

        assert action_id is not None
        retrieved = mgr.pop_pending(action_id, user_id=12345)
        assert retrieved is not None
        assert retrieved.amount == 250.0

        # Popped items should no longer exist
        assert mgr.get_pending(action_id, user_id=12345) is None

    def test_unauthorized_user_cannot_access_pending(self, sample_expense: Expense):
        mgr = PendingConfirmationManager(default_ttl_seconds=300)
        action_id = mgr.create_pending(user_id=12345, expense=sample_expense)

        # Another user tries to access
        assert mgr.get_pending(action_id, user_id=99999) is None

    def test_expired_pending_raises_error(self, sample_expense: Expense):
        mgr = PendingConfirmationManager(default_ttl_seconds=300)
        action_id = mgr.create_pending(user_id=12345, expense=sample_expense)

        # Manually backdate the created_at timestamp to 6 minutes ago
        pending = mgr._store[action_id]
        pending.created_at = datetime.now(timezone.utc) - timedelta(minutes=6)

        with pytest.raises(ConfirmationExpiredError, match="expired"):
            mgr.get_pending(action_id, user_id=12345)

    def test_cancel_pending(self, sample_expense: Expense):
        mgr = PendingConfirmationManager(default_ttl_seconds=300)
        action_id = mgr.create_pending(user_id=12345, expense=sample_expense)

        assert mgr.cancel_pending(action_id, user_id=12345) is True
        assert mgr.get_pending(action_id, user_id=12345) is None


class TestRetryDecorator:
    """Tests for exponential backoff on transient errors."""

    def test_successful_execution_without_retry(self):
        mock_fn = MagicMock(return_value="success")
        decorated = with_retry(max_retries=3, base_delay=0.01)(mock_fn)

        result = decorated()
        assert result == "success"
        assert mock_fn.call_count == 1

    def test_retries_and_succeeds(self):
        mock_fn = MagicMock(side_effect=[ValueError("Glitch 1"), "recovered"])
        decorated = with_retry(max_retries=3, base_delay=0.01)(mock_fn)

        result = decorated()
        assert result == "recovered"
        assert mock_fn.call_count == 2

    def test_rate_limit_detection_raises_gemini_rate_limit_error(self):
        mock_fn = MagicMock(side_effect=Exception("429 ResourceExhausted: rate limit exceeded"))
        decorated = with_retry(max_retries=2, base_delay=0.01)(mock_fn)

        with pytest.raises(GeminiRateLimitError, match="Rate limit exceeded"):
            decorated()
        assert mock_fn.call_count == 2
