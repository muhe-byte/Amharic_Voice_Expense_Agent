"""
Unit tests for spending_service module.
Covers deterministic calculation, category filtering, date filtering,
and response formatting.
"""

import pytest
import spending_service


@pytest.fixture
def sample_records():
    return [
        {"amount": "250.0", "category": "Transport", "transaction_date": "2026-10-20"},
        {"amount": 150.0, "category": "Food", "transaction_date": "2026-10-21"},
        {"amount": 500, "category": "Transport", "transaction_date": "2026-10-22"},
        {"amount": "100.50", "category": "Utilities", "transaction_date": "2026-10-22"},
    ]


class TestSpendingCalculations:
    """Tests for deterministic calculations from sheet rows."""

    def test_total_spending_all(self, sample_records):
        res = spending_service.calculate_spending(sample_records)
        assert res["total_amount"] == 1000.50
        assert res["count"] == 4
        assert res["category_filter"] == "All"
        assert res["category_breakdown"]["Transport"] == 750.0
        assert res["category_breakdown"]["Food"] == 150.0

    def test_filter_by_category(self, sample_records):
        res = spending_service.calculate_spending(sample_records, category="Transport")
        assert res["total_amount"] == 750.0
        assert res["count"] == 2
        assert res["category_filter"] == "Transport"

    def test_filter_by_date(self, sample_records):
        res = spending_service.calculate_spending(
            sample_records,
            start_date="2026-10-22",
            end_date="2026-10-22",
        )
        assert res["total_amount"] == 600.50
        assert res["count"] == 2

    def test_empty_records(self):
        res = spending_service.calculate_spending([])
        assert res["total_amount"] == 0.0
        assert res["count"] == 0


class TestSpendingQueryDetection:
    """Tests for identifying questions vs expense notes."""

    @pytest.mark.parametrize(
        "query, expected",
        [
            ("ምን ያህል ወጪ አደረግኩ?", True),
            ("ጠቅላላ ወጪዬ ስንት ነው?", True),
            ("How much did I spend this week?", True),
            ("ለትራንስፖርት ስንት ከፈልኩ?", True),
            ("ትናንት ለትራንስፖርት 250 ብር ከፈልኩ።", False),
            ("ዛሬ ለምሳ 150 ብር", False),
        ],
    )
    def test_is_spending_query(self, query: str, expected: bool):
        assert spending_service.is_spending_query(query) == expected

    def test_extract_query_category(self):
        assert spending_service.extract_query_category("ለትራንስፖርት ስንት አወጣሁ?") == "Transport"
        assert spending_service.extract_query_category("ለምሳ ምን ያህል ወጪ ሆነ?") == "Food"
        assert spending_service.extract_query_category("ጠቅላላ ወጪዬ ስንት ነው?") is None


class TestFormatSpendingResponse:
    """Tests for formatting Amharic spending summary message."""

    def test_format_with_records(self, sample_records):
        summary = spending_service.calculate_spending(sample_records)
        text = spending_service.format_spending_response(summary)
        assert "1,000.50 ETB" in text
        assert "የግብይቶች ብዛት (Count): 4" in text

    def test_format_empty_records(self):
        summary = spending_service.calculate_spending([])
        text = spending_service.format_spending_response(summary)
        assert "ምንም የተመዘገበ ወጪ አልተገኘም" in text
