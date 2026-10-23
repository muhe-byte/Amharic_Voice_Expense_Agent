"""
Google Sheets Service Module for Amharic Voice Expense Agent.

Handles authentication with Google Sheets API via gspread and oauth2client,
enforces the 8 standard schema columns, prevents duplicate transaction writes
using source_message_id, and records validated expense rows.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional, Union

import dotenv
import gspread
from oauth2client.service_account import ServiceAccountCredentials

from validation import Expense

# Configure logger
logger = logging.getLogger(__name__)

# Load environment variables
dotenv.load_dotenv()

# Expected columns in the Google Sheet
EXPECTED_COLUMNS: list[str] = [
    "transaction_id",
    "created_at",
    "transaction_date",
    "amount",
    "currency",
    "category",
    "transcript",
    "source_message_id",
]

DEFAULT_SCOPES: list[str] = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]


class SheetsConfigurationError(Exception):
    """Raised when environment variables or credential files are missing or misconfigured."""


class DuplicateExpenseError(Exception):
    """Raised when an expense with the same source_message_id is already recorded."""


class SheetsOperationError(Exception):
    """Raised when a Google Sheets read/write operation fails."""


class GoogleSheetsService:
    """
    Manages interactions with Google Sheets for recording and querying expenses.
    """

    def __init__(
        self,
        sheet_id: Optional[str] = None,
        credentials_path: Optional[str] = None,
        worksheet_title: Optional[str] = None,
    ):
        """
        Initializes Google Sheets service using credentials and sheet ID.

        Args:
            sheet_id: Google Sheet ID. If None, reads from GOOGLE_SHEET_ID env var.
            credentials_path: Path to service account JSON key file. If None, reads from GOOGLE_CREDENTIALS_PATH.
            worksheet_title: Optional specific worksheet name. If None, uses first worksheet (sheet1).
        """
        raw_sheet = (sheet_id or os.getenv("GOOGLE_SHEET_ID") or "").strip()
        if "/d/" in raw_sheet:
            match = re.search(r"/d/([a-zA-Z0-9-_]+)", raw_sheet)
            if match:
                raw_sheet = match.group(1)
        self.sheet_id = raw_sheet
        self.credentials_path = credentials_path or os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json")
        self.worksheet_title = worksheet_title

        self._client: Optional[gspread.Client] = None
        self._spreadsheet: Optional[gspread.Spreadsheet] = None
        self._worksheet: Optional[gspread.Worksheet] = None

    def connect(self) -> gspread.Worksheet:
        """
        Authenticates and connects to the configured Google Sheet and worksheet.
        Auto-initializes header row if the sheet is newly created or empty.

        Returns:
            The connected gspread Worksheet instance.
        """
        if self._worksheet is not None:
            return self._worksheet

        if not self.sheet_id:
            raise SheetsConfigurationError(
                "GOOGLE_SHEET_ID is not configured in .env or passed to GoogleSheetsService."
            )

        if not os.path.exists(self.credentials_path):
            raise SheetsConfigurationError(
                f"Google service account credentials file not found at '{self.credentials_path}'. "
                "Please verify GOOGLE_CREDENTIALS_PATH in your .env file."
            )

        try:
            creds = ServiceAccountCredentials.from_json_keyfile_name(
                self.credentials_path, DEFAULT_SCOPES
            )
            self._client = gspread.authorize(creds)
            self._spreadsheet = self._client.open_by_key(self.sheet_id)

            if self.worksheet_title:
                self._worksheet = self._spreadsheet.worksheet(self.worksheet_title)
            else:
                self._worksheet = self._spreadsheet.sheet1

            self._ensure_headers(self._worksheet)
            logger.info("Successfully connected to Google Sheet: %s", self._spreadsheet.title)
            return self._worksheet
        except Exception as err:
            logger.error("Failed to connect to Google Sheets: %s", err)
            raise SheetsOperationError(f"Google Sheets connection failed: {err}") from err

    def _ensure_headers(self, worksheet: gspread.Worksheet) -> None:
        """Ensures the first row contains all expected header columns."""
        try:
            existing_headers = worksheet.row_values(1)
            if not existing_headers:
                logger.info("Empty sheet detected. Initializing header columns: %s", EXPECTED_COLUMNS)
                worksheet.append_row(EXPECTED_COLUMNS, value_input_option="USER_ENTERED")
            else:
                # Log if headers mismatch
                if existing_headers[:len(EXPECTED_COLUMNS)] != EXPECTED_COLUMNS:
                    logger.warning(
                        "Sheet headers differ from standard columns. Existing: %s | Expected: %s",
                        existing_headers,
                        EXPECTED_COLUMNS,
                    )
        except Exception as err:
            logger.warning("Could not verify or initialize headers: %s", err)

    def is_duplicate_message(self, source_message_id: Optional[str]) -> bool:
        """
        Checks if an expense associated with source_message_id has already been recorded.

        Args:
            source_message_id: Telegram message idempotency key (e.g. '12345:678').

        Returns:
            True if source_message_id already exists in the sheet; False otherwise.
        """
        if not source_message_id or not str(source_message_id).strip():
            return False

        ws = self.connect()
        try:
            headers = ws.row_values(1)
            # Find column index for source_message_id (1-indexed for gspread)
            try:
                col_index = headers.index("source_message_id") + 1
            except ValueError:
                col_index = 8  # Standard fallback index

            existing_ids = ws.col_values(col_index)
            # Exclude header
            recorded_ids = [str(x).strip() for x in existing_ids[1:] if x]
            return str(source_message_id).strip() in recorded_ids
        except Exception as err:
            logger.error("Error during duplicate message check: %s", err)
            raise SheetsOperationError(f"Duplicate check failed: {err}") from err

    def append_expense_row(self, expense_data: Union[dict[str, Any], Expense]) -> dict[str, Any]:
        """
        Appends a validated expense record to the Google Sheet after verifying idempotency.

        Columns written:
            [transaction_id, created_at, transaction_date, amount, currency, category, transcript, source_message_id]

        Args:
            expense_data: Dictionary of expense fields or a validated Expense instance.

        Returns:
            Dictionary with operation status, transaction_id, and row contents.

        Raises:
            DuplicateExpenseError: If source_message_id was already recorded.
            ValueError / ValidationError: If expense data fails Pydantic validation.
            SheetsOperationError: If the Google Sheets API write fails.
        """
        # Validate data against Pydantic schema
        if isinstance(expense_data, Expense):
            expense = expense_data
        elif isinstance(expense_data, dict):
            expense = Expense.model_validate(expense_data)
        else:
            raise ValueError(f"Expected dict or Expense model, got {type(expense_data).__name__}")

        # Enforce duplicate message prevention
        if expense.source_message_id and self.is_duplicate_message(expense.source_message_id):
            raise DuplicateExpenseError(
                f"Expense with source_message_id '{expense.source_message_id}' already exists in sheet."
            )

        ws = self.connect()
        row_values = expense.to_sheet_row()

        try:
            ws.append_row(row_values, value_input_option="USER_ENTERED")
            logger.info("Successfully recorded expense %s (%s ETB)", expense.transaction_id, expense.amount)
            return {
                "success": True,
                "transaction_id": expense.transaction_id,
                "created_at": expense.created_at,
                "transaction_date": expense.transaction_date,
                "amount": expense.amount,
                "currency": expense.currency,
                "category": expense.category,
                "transcript": expense.transcript,
                "source_message_id": expense.source_message_id,
                "row": row_values,
            }
        except Exception as err:
            logger.error("Failed to append row to Google Sheet: %s", err)
            raise SheetsOperationError(f"Failed to append expense row: {err}") from err

    def get_all_expenses(self) -> list[dict[str, Any]]:
        """
        Retrieves all recorded expenses as a list of dictionaries for spending queries.

        Returns:
            List of row dicts keyed by header names.
        """
        ws = self.connect()
        try:
            return ws.get_all_records()
        except Exception as err:
            logger.error("Failed to retrieve expense records: %s", err)
            raise SheetsOperationError(f"Failed to read expenses: {err}") from err


# Module-level convenience functions
_default_service: Optional[GoogleSheetsService] = None


def get_sheets_service() -> GoogleSheetsService:
    """Returns singleton instance of GoogleSheetsService."""
    global _default_service
    if _default_service is None:
        _default_service = GoogleSheetsService()
    return _default_service


def append_expense_row(expense_data: Union[dict[str, Any], Expense]) -> dict[str, Any]:
    """Convenience wrapper for appending an expense row using default sheets service."""
    return get_sheets_service().append_expense_row(expense_data)
