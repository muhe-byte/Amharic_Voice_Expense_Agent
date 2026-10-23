"""
Unit tests for sheets_service module.
Mocks gspread and oauth2client to verify connection, duplicate prevention,
header checking, and expense appending without external network calls.
"""

from unittest.mock import MagicMock, patch
import pytest

from sheets_service import (
    DuplicateExpenseError,
    GoogleSheetsService,
    SheetsConfigurationError,
)
from validation import Expense


@pytest.fixture
def mock_worksheet():
    ws = MagicMock()
    ws.row_values.return_value = [
        "transaction_id",
        "created_at",
        "transaction_date",
        "amount",
        "currency",
        "category",
        "transcript",
        "source_message_id",
    ]
    ws.col_values.return_value = [
        "source_message_id",
        "100:1",
        "100:2",
    ]
    return ws


@pytest.fixture
def service_with_mock(mock_worksheet):
    service = GoogleSheetsService(
        sheet_id="test_sheet_id_123",
        credentials_path="dummy_credentials.json",
    )
    # Inject mock worksheet directly to bypass auth in unit tests
    service._worksheet = mock_worksheet
    return service


class TestGoogleSheetsService:
    """Tests for GoogleSheetsService operations."""

    def test_missing_sheet_id_raises_error(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_SHEET_ID", raising=False)
        service = GoogleSheetsService(sheet_id="")
        with pytest.raises(SheetsConfigurationError, match="GOOGLE_SHEET_ID is not configured"):
            service.connect()

    def test_missing_credentials_file_raises_error(self, tmp_path):
        non_existent_file = str(tmp_path / "non_existent.json")
        service = GoogleSheetsService(
            sheet_id="test_sheet_id",
            credentials_path=non_existent_file,
        )
        with pytest.raises(SheetsConfigurationError, match="credentials file not found"):
            service.connect()

    def test_is_duplicate_message_true(self, service_with_mock):
        # '100:1' exists in mock col_values
        assert service_with_mock.is_duplicate_message("100:1") is True

    def test_is_duplicate_message_false(self, service_with_mock):
        # '100:999' does not exist in mock col_values
        assert service_with_mock.is_duplicate_message("100:999") is False

    def test_is_duplicate_message_empty(self, service_with_mock):
        assert service_with_mock.is_duplicate_message(None) is False
        assert service_with_mock.is_duplicate_message("") is False

    def test_append_expense_row_success(self, service_with_mock, mock_worksheet):
        expense_data = {
            "amount": 250.0,
            "currency": "ETB",
            "category": "Transport",
            "transcript": "ትናንት ለትራንስፖርት 250 ብር ከፈልኩ።",
            "date_expression": "ትናንት",
            "source_message_id": "100:999",  # Unique
        }
        result = service_with_mock.append_expense_row(expense_data)

        assert result["success"] is True
        assert result["amount"] == 250.0
        assert result["category"] == "Transport"
        mock_worksheet.append_row.assert_called_once()

    def test_append_duplicate_expense_raises_error(self, service_with_mock):
        expense_data = {
            "amount": 250.0,
            "currency": "ETB",
            "category": "Transport",
            "transcript": "ትናንት ለትራንስፖርት 250 ብር ከፈልኩ።",
            "source_message_id": "100:1",  # Already exists!
        }
        with pytest.raises(DuplicateExpenseError, match="already exists"):
            service_with_mock.append_expense_row(expense_data)

    def test_get_all_expenses(self, service_with_mock, mock_worksheet):
        mock_worksheet.get_all_records.return_value = [
            {"transaction_id": "TX-1", "amount": 250, "category": "Transport"},
            {"transaction_id": "TX-2", "amount": 100, "category": "Food"},
        ]
        expenses = service_with_mock.get_all_expenses()
        assert len(expenses) == 2
        assert expenses[0]["amount"] == 250
