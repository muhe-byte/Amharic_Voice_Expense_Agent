"""
Unit tests for agent module.
Verifies parsing of Gemini responses, handling of unclear audio,
missing amounts, and Pydantic validation integration using mocks.
"""

import json
from unittest.mock import MagicMock, patch
import pytest

from agent import ExpenseAgent, ExtractedExpensePayload
from validation import Expense


@pytest.fixture
def mock_agent():
    with patch("agent.genai.Client"):
        agent = ExpenseAgent(api_key="test_dummy_key_12345")
        return agent


class TestExpenseAgentResponseParsing:
    """Tests for response parsing and validation in ExpenseAgent."""

    def test_parse_valid_response(self, mock_agent):
        gemini_json = json.dumps({
            "transcript": "ትናንት ለትራንስፖርት 250 ብር ከፈልኩ።",
            "amount": 250.0,
            "currency": "ETB",
            "category": "Transport",
            "date_expression": "ትናንት",
            "description": "የታክሲ ክፍያ",
            "intent": "add_expense",
            "is_clear": True,
            "clarification_question": None,
        })

        expense, payload = mock_agent._parse_and_validate_response(
            gemini_json, source_message_id="999:123"
        )

        assert expense is not None
        assert isinstance(expense, Expense)
        assert expense.amount == 250.0
        assert expense.currency == "ETB"
        assert expense.category == "Transport"
        assert expense.transcript == "ትናንት ለትራንስፖርት 250 ብር ከፈልኩ።"
        assert expense.source_message_id == "999:123"
        assert payload.is_clear is True

    def test_parse_unclear_audio(self, mock_agent):
        gemini_json = json.dumps({
            "transcript": "ድምፅ ግልጽ አይደለም",
            "amount": None,
            "currency": "ETB",
            "category": "Other",
            "date_expression": "ዛሬ",
            "description": None,
            "intent": "clarification_needed",
            "is_clear": False,
            "clarification_question": "እባክዎ ድምጽዎን ደግመው ይላኩ።",
        })

        expense, payload = mock_agent._parse_and_validate_response(gemini_json)

        assert expense is None
        assert payload.is_clear is False
        assert payload.clarification_question == "እባክዎ ድምጽዎን ደግመው ይላኩ።"

    def test_missing_amount_triggers_deterministic_fallback(self, mock_agent):
        # Gemini forgot to extract amount, but it is present in the transcript
        gemini_json = json.dumps({
            "transcript": "ዛሬ ለምሳ 150 ብር ከፈልኩ",
            "amount": None,
            "currency": "ETB",
            "category": "Food",
            "date_expression": "ዛሬ",
            "description": "የምሳ ወጪ",
            "intent": "add_expense",
            "is_clear": True,
            "clarification_question": None,
        })

        expense, payload = mock_agent._parse_and_validate_response(gemini_json)

        assert expense is not None
        assert expense.amount == 150.0  # Successfully extracted by deterministic fallback!
        assert expense.category == "Food"

    def test_missing_amount_and_no_transcript_amount_returns_none(self, mock_agent):
        gemini_json = json.dumps({
            "transcript": "ዛሬ ታክሲ ተሳፈርኩ",
            "amount": None,
            "currency": "ETB",
            "category": "Transport",
            "date_expression": "ዛሬ",
            "description": None,
            "intent": "add_expense",
            "is_clear": True,
            "clarification_question": "ስንት ብር ከፈሉ?",
        })

        expense, payload = mock_agent._parse_and_validate_response(gemini_json)
        assert expense is None
        assert payload.amount is None

    def test_malformed_json_raises_value_error(self, mock_agent):
        with pytest.raises(ValueError, match="Malformed JSON from Gemini"):
            mock_agent._parse_and_validate_response("invalid json string {}")

    def test_parse_relative_date_with_days_ago_delta(self, mock_agent):
        # Demonstrates flexible handling of phrasing variations like "በለፈው ሳምንት"
        # without hardcoded dictionary lookup failures
        gemini_json = json.dumps({
            "transcript": "በለፈው ሳምንት ለገበያ 1200 ብር ከፈልኩ",
            "amount": 1200.0,
            "currency": "ETB",
            "category": "Shopping",
            "date_expression": "በለፈው ሳምንት",
            "days_ago_delta": -7,
            "description": "የገበያ ወጪ",
            "intent": "add_expense",
            "is_clear": True,
            "clarification_question": None,
        })

        expense, payload = mock_agent._parse_and_validate_response(gemini_json)
        assert expense is not None
        assert expense.amount == 1200.0
        assert expense.category == "Shopping"
        assert expense.date_expression == "በለፈው ሳምንት"
        assert expense.days_ago_delta == -7
        assert payload.days_ago_delta == -7
