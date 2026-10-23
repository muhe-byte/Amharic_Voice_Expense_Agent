"""
Unit tests for validation module.
Covers relative date resolution (Africa/Addis_Ababa), Expense Pydantic schema,
currency enforcement, amount validation, and transcript cross-checking.
"""

from datetime import datetime, timedelta, timezone
import pytest
from pydantic import ValidationError

from validation import (
    ADDIS_TZ,
    Expense,
    resolve_date_from_delta,
    resolve_relative_date,
)


@pytest.fixture
def fixed_ref_time() -> datetime:
    """Fixed reference time in Africa/Addis_Ababa (UTC+3) for deterministic tests."""
    return datetime(2026, 10, 22, 14, 30, 0, tzinfo=ADDIS_TZ)


class TestRelativeDateResolution:
    """Tests for relative and absolute date parsing."""

    def test_today_amharic(self, fixed_ref_time: datetime):
        assert resolve_relative_date("ዛሬ", reference_time=fixed_ref_time) == "2026-10-22"
        assert resolve_relative_date("ዛሬ ነው", reference_time=fixed_ref_time) == "2026-10-22"

    def test_yesterday_amharic(self, fixed_ref_time: datetime):
        assert resolve_relative_date("ትናንት", reference_time=fixed_ref_time) == "2026-10-21"
        assert resolve_relative_date("ትላንት", reference_time=fixed_ref_time) == "2026-10-21"
        assert resolve_relative_date("ትናንትና", reference_time=fixed_ref_time) == "2026-10-21"

    def test_day_before_yesterday_amharic(self, fixed_ref_time: datetime):
        assert resolve_relative_date("ከትናንት ወዲያ", reference_time=fixed_ref_time) == "2026-10-20"
        assert resolve_relative_date("ከትላንት ወዲያ", reference_time=fixed_ref_time) == "2026-10-20"
        assert resolve_relative_date("ከትናንትና ወዲያ", reference_time=fixed_ref_time) == "2026-10-20"

    def test_english_relative_terms(self, fixed_ref_time: datetime):
        assert resolve_relative_date("today", reference_time=fixed_ref_time) == "2026-10-22"
        assert resolve_relative_date("yesterday", reference_time=fixed_ref_time) == "2026-10-21"
        assert resolve_relative_date("day before yesterday", reference_time=fixed_ref_time) == "2026-10-20"

    def test_iso_date_preservation(self, fixed_ref_time: datetime):
        assert resolve_relative_date("2026-10-15", reference_time=fixed_ref_time) == "2026-10-15"
        assert resolve_relative_date("2025-01-01", reference_time=fixed_ref_time) == "2025-01-01"

    def test_none_or_empty_defaults_to_today(self, fixed_ref_time: datetime):
        assert resolve_relative_date(None, reference_time=fixed_ref_time) == "2026-10-22"
        assert resolve_relative_date("", reference_time=fixed_ref_time) == "2026-10-22"

    def test_invalid_date_raises(self, fixed_ref_time: datetime):
        with pytest.raises(ValueError, match="Unable to resolve date"):
            resolve_relative_date("invalid_date_xyz", reference_time=fixed_ref_time)

        with pytest.raises(ValueError, match="Invalid Gregorian calendar date"):
            resolve_relative_date("2026-02-30", reference_time=fixed_ref_time)

    def test_resolve_date_from_delta(self, fixed_ref_time: datetime):
        assert resolve_date_from_delta(0, reference_time=fixed_ref_time) == "2026-10-22"
        assert resolve_date_from_delta(-1, reference_time=fixed_ref_time) == "2026-10-21"
        assert resolve_date_from_delta(-2, reference_time=fixed_ref_time) == "2026-10-20"
        assert resolve_date_from_delta(-7, reference_time=fixed_ref_time) == "2026-10-15"
        assert resolve_date_from_delta(-30, reference_time=fixed_ref_time) == "2026-09-22"

    def test_resolve_date_from_delta_future_rejected(self, fixed_ref_time: datetime):
        with pytest.raises(ValueError, match="future dates are not allowed"):
            resolve_date_from_delta(1, reference_time=fixed_ref_time)
        with pytest.raises(ValueError, match="future dates are not allowed"):
            resolve_date_from_delta(7, reference_time=fixed_ref_time)

    def test_resolve_date_from_delta_unreasonable_past_rejected(self, fixed_ref_time: datetime):
        with pytest.raises(ValueError, match="Unreasonable date delta"):
            resolve_date_from_delta(-4000, reference_time=fixed_ref_time)

    def test_hybrid_model_driven_date_resolution(self, fixed_ref_time: datetime):
        # Gemini provides days_ago_delta directly alongside diverse Amharic phrasing
        assert resolve_relative_date(
            date_expr="ባለፈው ሳምንት", days_ago_delta=-7, reference_time=fixed_ref_time
        ) == "2026-10-15"
        # Variation in spelling ("በለፈው ሳምንት" vs "ባለፈው ሳምንት") works without hardcoded dictionary failure
        assert resolve_relative_date(
            date_expr="በለፈው ሳምንት", days_ago_delta=-7, reference_time=fixed_ref_time
        ) == "2026-10-15"
        assert resolve_relative_date(
            date_expr="ሳምንት በፊት", days_ago_delta=-7, reference_time=fixed_ref_time
        ) == "2026-10-15"
        assert resolve_relative_date(
            date_expr="ከአምስት ቀን በፊት", days_ago_delta=-5, reference_time=fixed_ref_time
        ) == "2026-10-17"
        assert resolve_relative_date(
            days_ago_delta=0, reference_time=fixed_ref_time
        ) == "2026-10-22"


class TestExpenseSchema:
    """Tests for the Pydantic Expense model."""

    def test_valid_expense_creation(self):
        expense = Expense(
            amount=250.0,
            currency="ETB",
            category="Transport",
            transcript="ትናንት ለትራንስፖርት 250 ብር ከፈልኩ።",
            date_expression="ትናንት",
            source_message_id="12345:678",
        )
        assert expense.amount == 250.0
        assert expense.currency == "ETB"
        assert expense.category == "Transport"
        assert expense.transaction_id.startswith("TX-")
        assert expense.source_message_id == "12345:678"
        assert expense.transaction_date is not None

    def test_currency_case_insensitive_normalization(self):
        expense = Expense(
            amount=50.0,
            currency="etb",
            category="Food",
            transcript="ለምሳ 50 ብር",
        )
        assert expense.currency == "ETB"

    def test_invalid_currency_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            Expense(
                amount=100.0,
                currency="USD",
                category="Food",
                transcript="100 USD",
            )
        assert "Only 'ETB' (Ethiopian Birr) is supported" in str(exc_info.value)

    def test_negative_or_zero_amount_rejected(self):
        with pytest.raises(ValidationError):
            Expense(
                amount=0.0,
                category="Transport",
                transcript="ትራንስፖርት",
            )

        with pytest.raises(ValidationError):
            Expense(
                amount=-50.0,
                category="Transport",
                transcript="ትራንስፖርት",
            )

    def test_empty_category_rejected(self):
        with pytest.raises(ValidationError):
            Expense(
                amount=100.0,
                category="",
                transcript="ወጪ",
            )

    def test_empty_transcript_rejected(self):
        with pytest.raises(ValidationError):
            Expense(
                amount=100.0,
                category="Food",
                transcript="   ",
            )

    def test_amount_from_amharic_words_string(self):
        expense = Expense(
            amount="ሁለት መቶ ሃምሳ",  # Passed as Amharic words string
            category="Food",
            transcript="ለምሳ ሁለት መቶ ሃምሳ ብር",
        )
        assert expense.amount == 250.0

    def test_to_sheet_row_format(self):
        expense = Expense(
            transaction_id="TX-TEST1234",
            created_at="2026-10-22T10:00:00+03:00",
            transaction_date="2026-10-21",
            amount=250.0,
            currency="ETB",
            category="Transport",
            transcript="ትናንት ለትራንስፖርት 250 ብር ከፈልኩ።",
            source_message_id="999:888",
        )
        row = expense.to_sheet_row()
        assert len(row) == 8
        assert row == [
            "TX-TEST1234",
            "2026-10-22T10:00:00+03:00",
            "2026-10-21",
            250.0,
            "ETB",
            "Transport",
            "ትናንት ለትራንስፖርት 250 ብር ከፈልኩ።",
            "999:888",
        ]


class TestTranscriptCrossCheck:
    """Tests for Expense.cross_check_with_transcript()."""

    def test_matching_amount(self):
        expense = Expense(
            amount=250.0,
            category="Transport",
            transcript="ትናንት ለትራንስፖርት 250 ብር ከፈልኩ።",
        )
        is_consistent, msg = expense.cross_check_with_transcript()
        assert is_consistent is True

    def test_mismatching_amount(self):
        expense = Expense(
            amount=300.0,  # Differs from 250 in transcript
            category="Transport",
            transcript="ትናንት ለትራንስፖርት 250 ብር ከፈልኩ።",
        )
        is_consistent, msg = expense.cross_check_with_transcript()
        assert is_consistent is False
        assert "Amount mismatch" in msg

    def test_transcript_without_numeric_amount(self):
        expense = Expense(
            amount=50.0,
            category="Transport",
            transcript="ታክሲ ተሳፈርኩ",
        )
        is_consistent, msg = expense.cross_check_with_transcript()
        assert is_consistent is True  # Non-deterministic phrasing allowed
