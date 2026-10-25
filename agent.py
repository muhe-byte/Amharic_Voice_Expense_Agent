"""
Gemini Voice Expense Agent Module.

Interfaces with the Google Gemini API using the google-genai SDK.
Transcribes Amharic voice notes verbatim in Fidel, extracts structured expense fields
(amount, ETB currency, category, date expression), validates them against
business schemas, and cross-checks with deterministic Amharic number parsing.
Includes rate-limit retries and safe error handling.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import time
from typing import Any, Optional, Union

import dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from amharic_numbers import extract_amount_from_text, parse_amharic_number
from error_handler import GeminiRateLimitError, GeminiServiceError, with_retry
from validation import Expense, resolve_relative_date

# Configure logger
logger = logging.getLogger(__name__)

# Load environment variables
dotenv.load_dotenv()

DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")

SYSTEM_INSTRUCTION = """\
Amharic Voice Expense Assistant.
1. Transcribe voice verbatim in Fidel script. Do not translate, summarize, or hallucinate. If speech is unintelligible, set is_clear=false with an Amharic clarification_question.
2. Extract structured fields:
   - transcript: Verbatim Fidel text.
   - amount: Numeric float in ETB (null if missing or unclear; never guess).
   - currency: "ETB".
   - category: Transport, Food, Utilities, Airtime, Shopping, Rent, or Other.
   - date_expression: Raw phrase mentioned or "ዛሬ".
   - days_ago_delta: Integer day offset (0 for today/unspecified, -1 for yesterday, -2 for day before yesterday, -7 for last week, -N for N days ago). Must be <= 0.
   - intent: "add_expense", "query_spending", or "other".
   - is_clear: boolean flag.
   - clarification_question: Amharic clarification question if amount or details are missing.
"""


class ExtractedExpensePayload(BaseModel):
    """Raw structured extraction returned from Gemini."""
    transcript: str = Field(
        ...,
        description="Verbatim Fidel Amharic transcript. No English translation."
    )
    amount: Optional[float] = Field(
        default=None,
        description="Extracted numeric amount. Null if not specified or unclear."
    )
    currency: str = Field(
        default="ETB",
        description="Strictly ETB (Ethiopian Birr)."
    )
    category: str = Field(
        default="Other",
        description="Standardized category e.g. Transport, Food, Utilities, Airtime, Shopping, Rent, Other."
    )
    date_expression: Optional[str] = Field(
        default="ዛሬ",
        description="Raw Amharic or relative date expression e.g. ዛሬ, ትናንት, or YYYY-MM-DD."
    )
    days_ago_delta: int = Field(
        default=0,
        description="Integer day delta relative to today (0=today, -1=yesterday, -7=last week). Defaults to 0 if unspecified."
    )
    description: Optional[str] = Field(
        default=None,
        description="Short description of the expense."
    )
    intent: str = Field(
        default="add_expense",
        description="'add_expense', 'query_spending', 'spending_query', 'other', or 'clarification_needed'."
    )
    is_clear: bool = Field(
        default=True,
        description="True if audio was audible and coherent; False if distorted/unintelligible."
    )
    clarification_question: Optional[str] = Field(
        default=None,
        description="Amharic follow-up question if information is ambiguous or missing."
    )


class ExpenseAgent:
    """
    Coordinates Gemini audio/text processing and strict expense validation.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model_name: Optional[str] = None,
    ):
        """
        Initializes the Gemini agent client.

        Args:
            api_key: Gemini API key. Defaults to GEMINI_API_KEY environment variable.
            model_name: Gemini model name. Defaults to GEMINI_MODEL or gemini-2.5-flash.
        """
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        if not self.api_key:
            raise ValueError(
                "GEMINI_API_KEY is not configured. Please set GEMINI_API_KEY in your .env file."
            )

        self.model_name = model_name or DEFAULT_MODEL
        self.client = genai.Client(api_key=self.api_key)

    def _build_generation_config(self) -> types.GenerateContentConfig:
        """Constructs GenerateContentConfig enforcing JSON and ExtractedExpensePayload schema."""
        return types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=ExtractedExpensePayload,
            temperature=0.0,  # 0.0 for fastest deterministic token generation
        )

    @with_retry(max_retries=3, base_delay=1.0, backoff_factor=2.0)
    def _call_gemini_api(self, contents: list[Any]) -> str:
        """
        Calls Gemini API with latency profiling and automatic fallback across fast models.
        """
        models_to_try = [self.model_name] + [
            m for m in ["gemini-3.5-flash-lite", "gemini-3.5-flash", "gemini-3.8-flash"]
            if m != self.model_name
        ]

        last_error: Optional[Exception] = None
        for candidate in models_to_try:
            try:
                start_time = time.perf_counter()
                response = self.client.models.generate_content(
                    model=candidate,
                    contents=contents,
                    config=self._build_generation_config(),
                )
                elapsed = time.perf_counter() - start_time

                if not response or not response.text:
                    raise GeminiServiceError("Gemini returned an empty response.")

                logger.info("Gemini inference [%s] took %.2f seconds", candidate, elapsed)

                if candidate != self.model_name:
                    logger.info("Switched to fallback model %s (primary %s was unavailable)", candidate, self.model_name)
                    self.model_name = candidate

                return response.text
            except GeminiRateLimitError:
                raise
            except Exception as err:
                err_msg = str(err).lower()
                if any(term in err_msg for term in ["429", "resource_exhausted", "quota"]):
                    raise GeminiRateLimitError(f"Gemini rate limit exceeded: {err}") from err
                if any(term in err_msg for term in ["404", "not_found", "no longer available", "503", "unavailable", "high demand"]):
                    logger.warning("Gemini model %s error: %s. Trying next candidate model...", candidate, err)
                    last_error = err
                    continue
                logger.error("Gemini API call failed with model %s: %s", candidate, err)
                raise GeminiServiceError(f"Gemini API failure: {err}") from err

        raise GeminiServiceError(f"All candidate Gemini models failed. Last error: {last_error}")

    def process_audio(
        self,
        audio_input: Union[str, Path, bytes],
        mime_type: str = "audio/mp3",
        source_message_id: Optional[str] = None,
    ) -> tuple[Optional[Expense], ExtractedExpensePayload]:
        """
        Processes an audio file or raw bytes:
        1. Sends MP3 to Gemini for verbatim transcription and field extraction.
        2. Validates structured result against ExtractedExpensePayload schema.
        3. Deterministically cross-checks extracted amount with Amharic number parser.
        4. Returns validated Expense instance (or None if unclear/missing amount) along with raw payload.

        Args:
            audio_input: Path to audio file (str/Path) or raw audio bytes.
            mime_type: Audio MIME type, default 'audio/mp3'.
            source_message_id: Optional idempotency key (e.g. 'chat_id:message_id').

        Returns:
            tuple (validated_expense, raw_payload)
        """
        t0 = time.perf_counter()

        # Read bytes if path is provided
        if isinstance(audio_input, (str, Path)):
            audio_path = Path(audio_input)
            if not audio_path.exists():
                raise FileNotFoundError(f"Audio file not found: {audio_path}")
            audio_bytes = audio_path.read_bytes()
        elif isinstance(audio_input, bytes):
            audio_bytes = audio_input
        else:
            raise TypeError(f"Expected file path or bytes, got {type(audio_input).__name__}")

        audio_part = types.Part.from_bytes(data=audio_bytes, mime_type=mime_type)
        user_prompt = "ድምፁን ወደ አማርኛ ጽሑፍ ቀይረው የወጪ መረጃዎችን በJSON ያውጡ።"

        raw_json = self._call_gemini_api(contents=[audio_part, user_prompt])
        expense, payload = self._parse_and_validate_response(raw_json, source_message_id=source_message_id)

        logger.info("Audio expense processing took %.2f seconds total", time.perf_counter() - t0)
        return expense, payload

    def process_text(
        self,
        text: str,
        source_message_id: Optional[str] = None,
    ) -> tuple[Optional[Expense], ExtractedExpensePayload]:
        """
        Processes a raw Amharic text message (e.g., 'ትናንት ለትራንስፖርት 250 ብር ከፈልኩ').

        Args:
            text: Raw Amharic text string.
            source_message_id: Optional idempotency key.

        Returns:
            tuple (validated_expense, raw_payload)
        """
        if not text or not text.strip():
            raise ValueError("Input text cannot be empty.")

        t0 = time.perf_counter()
        raw_json = self._call_gemini_api(contents=[f"መልእክት: {text.strip()}"])
        expense, payload = self._parse_and_validate_response(raw_json, source_message_id=source_message_id)

        logger.info("Text expense processing took %.2f seconds total", time.perf_counter() - t0)
        return expense, payload

    def _parse_and_validate_response(
        self,
        raw_text_json: str,
        source_message_id: Optional[str] = None,
    ) -> tuple[Optional[Expense], ExtractedExpensePayload]:
        """
        Parses JSON response from Gemini, performs deterministic sanity checks,
        and constructs validated Expense object.
        """
        logger.debug("Raw Gemini response: %s", raw_text_json)

        # Parse JSON
        try:
            data = json.loads(raw_text_json)
            payload = ExtractedExpensePayload.model_validate(data)
        except Exception as err:
            logger.error("Failed to parse Gemini output as ExtractedExpensePayload: %s | Raw: %s", err, raw_text_json)
            raise ValueError(f"Malformed JSON from Gemini: {err}") from err

        # Normalize intent aliases
        if payload.intent == "spending_query":
            payload.intent = "query_spending"

        # If audio/text was unintelligible or not an expense
        if not payload.is_clear or payload.intent != "add_expense":
            return None, payload

        # Check for missing amount
        amount = payload.amount
        if amount is None or amount <= 0:
            # Try deterministic fallback parser on transcript
            det_amount = extract_amount_from_text(payload.transcript)
            if det_amount and det_amount > 0:
                amount = det_amount
            else:
                logger.info("No valid amount detected in audio/transcript: '%s'", payload.transcript)
                return None, payload

        # Cross-check extracted amount with deterministic transcript parser
        det_amount = extract_amount_from_text(payload.transcript)
        if det_amount is not None and abs(amount - det_amount) > 0.01:
            logger.warning(
                "Amount discrepancy: Gemini reported %s ETB, deterministic parser found %s ETB in transcript '%s'.",
                amount,
                det_amount,
                payload.transcript,
            )

        # Resolve date: Python computes exact date from Gemini's semantically extracted days_ago_delta
        # converted to Africa/Addis_Ababa timezone.
        try:
            resolved_date = resolve_relative_date(
                date_expr=payload.date_expression,
                days_ago_delta=payload.days_ago_delta,
            )
        except ValueError as date_err:
            logger.warning(
                "Could not resolve date (delta=%s, expr='%s'): %s. Defaulting to today.",
                payload.days_ago_delta,
                payload.date_expression,
                date_err,
            )
            resolved_date = resolve_relative_date("ዛሬ")

        expense = Expense(
            amount=amount,
            currency="ETB",
            category=payload.category or "Other",
            transcript=payload.transcript,
            date_expression=payload.date_expression,
            days_ago_delta=payload.days_ago_delta,
            transaction_date=resolved_date,
            description=payload.description or f"{payload.category} expense",
            source_message_id=source_message_id,
        )

        return expense, payload
