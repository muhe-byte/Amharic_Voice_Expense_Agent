"""
Validation and Data Schema Module for Amharic Voice Expense Agent.

Implements Pydantic schemas, relative Amharic date resolution
(using Africa/Addis_Ababa timezone), currency enforcement (ETB),
and cross-checking with deterministic Amharic number parsing.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import re
import uuid
from typing import Optional, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator

from amharic_numbers import extract_amount_from_text, parse_amharic_number


# Africa/Addis_Ababa is East Africa Time (EAT), which is UTC+3 with no daylight saving time.
def get_addis_timezone() -> timezone:
    """Return Africa/Addis_Ababa timezone with graceful fallback to UTC+3."""
    try:
        return ZoneInfo("Africa/Addis_Ababa")
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=3), name="Africa/Addis_Ababa")


ADDIS_TZ = get_addis_timezone()

# Simplified fallback dictionary for standard relative terms
RELATIVE_DATE_MAP: dict[str, int] = {
    "ዛሬ": 0,
    "ዛሬ ነው": 0,
    "ትናንት": -1,
    "ትላንት": -1,
    "ትናንትና": -1,
    "ትላንትና": -1,
    "ከትናንት ወዲያ": -2,
    "ከትላንት ወዲያ": -2,
    "ከትናንትና ወዲያ": -2,
    "ከትላንትና ወዲያ": -2,
    "today": 0,
    "yesterday": -1,
    "day before yesterday": -2,
}


def resolve_date_from_delta(
    days_ago_delta: int = 0,
    reference_time: Optional[datetime] = None,
) -> str:
    """
    Computes exact Gregorian calendar date (YYYY-MM-DD) from an integer days_ago_delta.
    Takes reference_time (or current UTC time), converts to Africa/Addis_Ababa timezone,
    applies days_ago_delta using timedelta, and validates the result.

    Args:
        days_ago_delta: Integer day offset (0 for today, -1 for yesterday, -7 for last week).
        reference_time: Optional reference datetime. Defaults to now in Africa/Addis_Ababa.

    Returns:
        Gregorian date string in 'YYYY-MM-DD' format.

    Raises:
        ValueError: If delta results in a future date (> 0) or unreasonable delta (< -3650 days).
    """
    if reference_time is None:
        ref_dt = datetime.now(timezone.utc).astimezone(ADDIS_TZ)
    else:
        if reference_time.tzinfo is None:
            # Assume UTC if naive, then convert to Addis Ababa
            ref_dt = reference_time.replace(tzinfo=timezone.utc).astimezone(ADDIS_TZ)
        else:
            ref_dt = reference_time.astimezone(ADDIS_TZ)

    ref_date = ref_dt.date()

    if days_ago_delta > 0:
        raise ValueError(
            f"Invalid expense date: future dates are not allowed (days_ago_delta={days_ago_delta} > 0)."
        )

    if days_ago_delta < -3650:
        raise ValueError(
            f"Unreasonable date delta: {days_ago_delta} days exceeds allowed history limit."
        )

    resolved_date = ref_date + timedelta(days=days_ago_delta)
    if resolved_date > ref_date:
        raise ValueError(f"Resolved date '{resolved_date}' cannot be in the future.")

    return resolved_date.isoformat()


def resolve_relative_date(
    date_expr: Optional[str] = None,
    reference_time: Optional[datetime] = None,
    days_ago_delta: Optional[int] = None,
) -> str:
    """
    Hybrid model-driven date resolver:
    1. If days_ago_delta is provided (from Gemini semantic extraction), Python computes
       exact Gregorian date in Africa/Addis_Ababa via integer timedelta directly.
    2. If explicit ISO format (YYYY-MM-DD) or DD/MM/YYYY is provided, validates that date.
    3. If legacy/unrecognized string, uses simplified fallback dictionary.

    Args:
        date_expr: Optional relative date string or ISO date string (YYYY-MM-DD).
        reference_time: Optional reference datetime for testing. Defaults to now in Addis Ababa.
        days_ago_delta: Optional integer day offset relative to today (0=today, -1=yesterday, etc.).

    Returns:
        Formatted date string 'YYYY-MM-DD'.

    Raises:
        ValueError: If the date expression cannot be parsed or resolved.
    """
    # 1. Primary path: model-driven integer days_ago_delta
    if days_ago_delta is not None:
        return resolve_date_from_delta(days_ago_delta=days_ago_delta, reference_time=reference_time)

    # 2. If no date expression and no delta, default to today (0)
    if not date_expr or not str(date_expr).strip():
        return resolve_date_from_delta(days_ago_delta=0, reference_time=reference_time)

    cleaned = str(date_expr).strip().lower()

    if reference_time is None:
        ref_dt = datetime.now(timezone.utc).astimezone(ADDIS_TZ)
    else:
        if reference_time.tzinfo is None:
            ref_dt = reference_time.replace(tzinfo=timezone.utc).astimezone(ADDIS_TZ)
        else:
            ref_dt = reference_time.astimezone(ADDIS_TZ)
    ref_date = ref_dt.date()

    # 3. Check for direct Gregorian ISO format: YYYY-MM-DD
    iso_match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", cleaned)
    if iso_match:
        year, month, day = map(int, iso_match.groups())
        try:
            parsed_date = date(year, month, day)
            if parsed_date > ref_date:
                raise ValueError(f"Future expense date '{date_expr}' is not allowed.")
            return parsed_date.isoformat()
        except ValueError as err:
            raise ValueError(f"Invalid Gregorian calendar date '{date_expr}': {err}") from err

    # 4. Check for common formats: DD/MM/YYYY or YYYY/MM/DD
    slash_match = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", cleaned)
    if slash_match:
        day, month, year = map(int, slash_match.groups())
        try:
            parsed_date = date(year, month, day)
            if parsed_date > ref_date:
                raise ValueError(f"Future expense date '{date_expr}' is not allowed.")
            return parsed_date.isoformat()
        except ValueError as err:
            raise ValueError(f"Invalid calendar date '{date_expr}': {err}") from err

    # 5. Check simplified fallback mapping for standard terms
    if cleaned in RELATIVE_DATE_MAP:
        return resolve_date_from_delta(days_ago_delta=RELATIVE_DATE_MAP[cleaned], reference_time=reference_time)

    # 6. Fallback dynamic regex (e.g., ከ3 ቀን በፊት or 3 days ago)
    days_ago_match = re.search(r"(?:ከ\s*)?(\d+)\s*(?:ቀን|days?)\s*(?:በፊት|ago)", cleaned)
    if days_ago_match:
        delta = -int(days_ago_match.group(1))
        return resolve_date_from_delta(days_ago_delta=delta, reference_time=reference_time)

    weeks_ago_match = re.search(r"(?:ከ\s*)?(\d+)\s*(?:ሳምንት|weeks?)\s*(?:በፊት|ago)", cleaned)
    if weeks_ago_match:
        delta = -int(weeks_ago_match.group(1)) * 7
        return resolve_date_from_delta(days_ago_delta=delta, reference_time=reference_time)

    raise ValueError(
        f"Unable to resolve date expression '{date_expr}'. "
        f"Supported: 'ዛሬ', 'ትናንት', 'ከትናንት ወዲያ', or 'YYYY-MM-DD'."
    )


class Expense(BaseModel):
    """
    Validated expense record matching Google Sheets schema and business rules.

    Fields:
        transaction_id: Unique UUID/identifier for the record.
        created_at: ISO timestamp of record creation (Africa/Addis_Ababa).
        transaction_date: Resolved Gregorian date (YYYY-MM-DD).
        amount: Positive numeric amount spent.
        currency: Strictly 'ETB' (Ethiopian Birr).
        category: Expense category (e.g. Transport, Food, Utilities).
        transcript: Verbatim Amharic transcript of the audio or text.
        source_message_id: Telegram chat:message idempotency key.
        description: Optional additional note or summary.
        date_expression: Raw relative or absolute date phrase extracted by Gemini.
    """

    transaction_id: str = Field(
        default_factory=lambda: f"TX-{uuid.uuid4().hex[:8].upper()}",
        description="Unique application transaction identifier",
    )
    created_at: str = Field(
        default_factory=lambda: datetime.now(ADDIS_TZ).isoformat(),
        description="Record creation timestamp in Addis Ababa timezone",
    )
    transaction_date: Optional[str] = Field(
        default=None,
        description="Resolved Gregorian date in YYYY-MM-DD format",
    )
    date_expression: Optional[str] = Field(
        default=None,
        description="Original date expression (e.g., 'ዛሬ', 'ትናንት', '2026-10-21')",
    )
    days_ago_delta: Optional[int] = Field(
        default=0,
        description="Integer day delta relative to today (0=today, -1=yesterday, -7=last week)",
    )
    amount: float = Field(
        ...,
        gt=0,
        description="Expense amount; must be strictly greater than 0",
    )
    currency: str = Field(
        default="ETB",
        description="Currency code; strictly ETB",
    )
    category: str = Field(
        ...,
        min_length=1,
        description="Normalized category (e.g., Food, Transport, Utilities)",
    )
    transcript: str = Field(
        ...,
        min_length=1,
        description="Verbatim Amharic audio transcript or user message",
    )
    source_message_id: Optional[str] = Field(
        default=None,
        description="Unique Telegram message identifier for idempotency (e.g., '12345:678')",
    )
    description: Optional[str] = Field(
        default=None,
        description="Optional human-readable description of expense",
    )

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, v: str) -> str:
        """Enforce strict ETB currency."""
        cleaned = v.strip().upper()
        if cleaned != "ETB":
            raise ValueError(f"Invalid currency '{v}'. Only 'ETB' (Ethiopian Birr) is supported.")
        return cleaned

    @field_validator("category")
    @classmethod
    def validate_category(cls, v: str) -> str:
        """Sanitize category string."""
        cleaned = v.strip().title()
        if not cleaned:
            raise ValueError("Category cannot be empty.")
        return cleaned

    @field_validator("transcript")
    @classmethod
    def validate_transcript(cls, v: str) -> str:
        """Ensure transcript is non-empty."""
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("Transcript cannot be empty.")
        return cleaned

    @model_validator(mode="before")
    @classmethod
    def resolve_date_and_numbers(cls, data: Union[dict, Expense]) -> Union[dict, Expense]:
        """
        Pre-validation hook:
        1. Resolves date_expression or transaction_date into valid YYYY-MM-DD.
        2. Normalizes amount if passed as an Amharic number word or string.
        """
        if isinstance(data, dict):
            # Parse amount if passed as string/Amharic words
            raw_amount = data.get("amount")
            if isinstance(raw_amount, str):
                parsed = parse_amharic_number(raw_amount)
                if parsed is not None:
                    data["amount"] = float(parsed)

            # Resolve date using model-driven delta or explicit date expression
            explicit_trans_date = data.get("transaction_date")
            days_delta = data.get("days_ago_delta")
            date_expr = data.get("date_expression")

            if explicit_trans_date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(explicit_trans_date)):
                data["transaction_date"] = str(explicit_trans_date)
            else:
                resolved = resolve_relative_date(
                    date_expr=date_expr or explicit_trans_date,
                    days_ago_delta=days_delta if (days_delta is not None and days_delta != 0) else None,
                )
                data["transaction_date"] = resolved

            if not data.get("date_expression") and date_expr:
                data["date_expression"] = date_expr

            # Auto-populate description if empty
            if not data.get("description"):
                cat = data.get("category", "Expense")
                data["description"] = f"{cat} expense"

        return data

    def cross_check_with_transcript(self, tolerance: float = 0.01) -> tuple[bool, Optional[str]]:
        """
        Cross-checks the model amount with deterministic extraction from transcript.

        Returns:
            (is_consistent, message)
        """
        extracted = extract_amount_from_text(self.transcript)
        if extracted is None:
            # Deterministic parser couldn't find a direct amount in transcript;
            # this is okay (e.g. colloquial phrasing), but worth noting.
            return True, "No deterministic amount found in transcript to cross-check."

        diff = abs(self.amount - extracted)
        if diff > tolerance:
            return False, (
                f"Amount mismatch: Gemini extracted {self.amount} ETB, "
                f"but transcript parsed as {extracted} ETB."
            )

        return True, "Deterministic transcript amount matches extracted amount."

    def to_sheet_row(self) -> list[Union[str, float, int]]:
        """
        Formats record into ordered columns for Google Sheets:
        [transaction_id, created_at, transaction_date, amount, currency, category, transcript, source_message_id]
        """
        return [
            self.transaction_id,
            self.created_at,
            self.transaction_date or "",
            self.amount,
            self.currency,
            self.category,
            self.transcript,
            self.source_message_id or "",
        ]
