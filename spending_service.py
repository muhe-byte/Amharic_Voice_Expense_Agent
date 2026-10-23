"""
Spending Query and Deterministic Calculation Service.

Processes read-only spending questions without modifying or adding transactions.
Queries Google Sheets data, filters by category and/or date period in Python,
and computes deterministic totals.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import logging
import re
from typing import Any, Optional

from validation import ADDIS_TZ

logger = logging.getLogger(__name__)

# Keywords indicating a spending question rather than adding an expense
SPENDING_QUERY_KEYWORDS = [
    "ምን ያህል",
    "ስንት",
    "ጠቅላላ",
    "ድምር",
    "አጠቃላይ",
    "ወጪዬ",
    "ወጪዬን",
    "ወጪዬ ስንት",
    "ምን ያህል አወጣሁ",
    "ስንት ከፈልኩ",
    "ስንት አወጣሁ",
    "ሪፖርት",
    "ማጠቃለያ",
    "how much",
    "total spent",
    "total spending",
    "spending query",
    "expense summary",
    "spending summary",
]

# Standard categories to detect in questions
KNOWN_CATEGORIES = {
    "ትራንስፖርት": "Transport",
    "ታክሲ": "Transport",
    "ጋዝ": "Transport",
    "transport": "Transport",
    "ምግብ": "Food",
    "ምሳ": "Food",
    "ቁርስ": "Food",
    "እራት": "Food",
    "ሻይ": "Food",
    "ቡና": "Food",
    "food": "Food",
    "መብራት": "Utilities",
    "ውሃ": "Utilities",
    "ኢንተርኔት": "Utilities",
    "ስልክ": "Utilities",
    "utility": "Utilities",
    "utilities": "Utilities",
    "ገበያ": "Shopping",
    "ልብስ": "Shopping",
    "shopping": "Shopping",
    "መዝናኛ": "Entertainment",
    "entertainment": "Entertainment",
    "ጤና": "Health",
    "መድሃኒት": "Health",
    "ሕክምና": "Health",
    "health": "Health",
    "ትምህርት": "Education",
    "education": "Education",
}


def is_spending_query(text: str) -> bool:
    """
    Determines if user input is an inquiry about recorded expenses.
    """
    if not text:
        return False
    lower = text.lower()
    return any(keyword in lower for keyword in SPENDING_QUERY_KEYWORDS)


def extract_query_category(text: str) -> Optional[str]:
    """
    Extracts expense category filter mentioned in the query text.
    """
    lower = text.lower()
    for keyword, category in KNOWN_CATEGORIES.items():
        if keyword in lower:
            return category
    return None


def calculate_spending(
    records: list[dict[str, Any]],
    category: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict[str, Any]:
    """
    Calculates deterministic spending totals in Python from raw sheet records.

    Args:
        records: List of expense record dictionaries from Google Sheets.
        category: Optional category filter (e.g. 'Transport', 'Food').
        start_date: Optional ISO date string 'YYYY-MM-DD'.
        end_date: Optional ISO date string 'YYYY-MM-DD'.

    Returns:
        Structured spending summary.
    """
    total_amount = 0.0
    matching_records_count = 0
    category_breakdown: dict[str, float] = {}

    for row in records:
        try:
            raw_amount = row.get("amount")
            if raw_amount is None or str(raw_amount).strip() == "":
                continue

            amount = float(str(raw_amount).replace(",", "").strip())
            row_category = str(row.get("category", "Other")).strip().title()
            row_date = str(row.get("transaction_date", "")).strip()

            # Filter by category
            if category and row_category.lower() != category.lower():
                continue

            # Filter by start_date
            if start_date and row_date and row_date < start_date:
                continue

            # Filter by end_date
            if end_date and row_date and row_date > end_date:
                continue

            total_amount += amount
            matching_records_count += 1
            category_breakdown[row_category] = (
                category_breakdown.get(row_category, 0.0) + amount
            )
        except (ValueError, TypeError) as err:
            logger.warning("Skipping invalid row in calculation: %s (%s)", row, err)
            continue

    return {
        "total_amount": round(total_amount, 2),
        "currency": "ETB",
        "count": matching_records_count,
        "category_filter": category or "All",
        "category_breakdown": {k: round(v, 2) for k, v in category_breakdown.items()},
    }


def format_spending_response(summary: dict[str, Any]) -> str:
    """
    Formats calculated spending summary into an Amharic message for Telegram.
    """
    total = summary["total_amount"]
    curr = summary["currency"]
    count = summary["count"]
    cat = summary["category_filter"]

    if count == 0:
        if cat != "All":
            return f"ℹ️ ለ'{cat}' ምድብ ምንም የተመዘገበ ወጪ አልተገኘም። (No expenses found for '{cat}'.)"
        return "ℹ️ ምንም የተመዘገበ ወጪ አልተገኘም። (No expenses recorded yet.)"

    lines = [
        "📊 የወጪ ማጠቃለያ (Spending Summary):",
        "━━━━━━━━━━━━━━━━━━━━",
        f"💰 ጠቅላላ ወጪ (Total): {total:,.2f} {curr}",
        f"🔢 የግብይቶች ብዛት (Count): {count}",
    ]

    if cat != "All":
        lines.append(f"🏷️ የተመረጠ ምድብ (Category): {cat}")
    elif summary.get("category_breakdown"):
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append("📋 በምድብ ሲከፋፈል (By Category):")
        for c, amt in summary["category_breakdown"].items():
            lines.append(f"  • {c}: {amt:,.2f} {curr}")

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("ℹ️ ይህ መረጃ በቀጥታ ከGoogle Sheet ተሰልቶ የቀረበ ነው።")
    return "\n".join(lines)
