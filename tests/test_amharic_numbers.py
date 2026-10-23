"""
Unit tests for amharic_numbers module.
Covers modern Amharic number words, Arabic digits, separators,
decimals, fractions (ተኩል, ነጥብ), and sentence extraction.
"""

import pytest
from amharic_numbers import (
    extract_amount_from_text,
    parse_amharic_number,
)


class TestBasicAmharicNumbers:
    """Tests for single unit and ten numbers."""

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("ዜሮ", 0),
            ("አንድ", 1),
            ("ሁለት", 2),
            ("ሶስት", 3),
            ("ሦስት", 3),
            ("አራት", 4),
            ("አምስት", 5),
            ("ስድስት", 6),
            ("ሰባት", 7),
            ("ስምንት", 8),
            ("ዘጠኝ", 9),
        ],
    )
    def test_units(self, text: str, expected: int):
        assert parse_amharic_number(text) == expected

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("አስር", 10),
            ("አሥር", 10),
            ("ዐስር", 10),
            ("ሃያ", 20),
            ("ኻያ", 20),
            ("ሰላሳ", 30),
            ("ሠላሳ", 30),
            ("አርባ", 40),
            ("ሃምሳ", 50),
            ("ሓምሳ", 50),
            ("አምሳ", 50),
            ("ስልሳ", 60),
            ("ስድሳ", 60),
            ("ሰባ", 70),
            ("ሰማንያ", 80),
            ("ሰማኔ", 80),
            ("ዘጠና", 90),
        ],
    )
    def test_tens(self, text: str, expected: int):
        assert parse_amharic_number(text) == expected


class TestTeensAndCompounds:
    """Tests for teen numbers (11-19) and two-token combinations."""

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("አስራ አንድ", 11),
            ("አስራ ሁለት", 12),
            ("አስራ ሶስት", 13),
            ("አስራ አራት", 14),
            ("አስራ አምስት", 15),
            ("አስራ ስድስት", 16),
            ("አስራ ሰባት", 17),
            ("አስራ ስምንት", 18),
            ("አስራ ዘጠኝ", 19),
            # Joined forms
            ("አስራአንድ", 11),
            ("አስራሁለት", 12),
            ("አስራአምስት", 15),
            # Spelling variants with ሥ
            ("አሥራ አንድ", 11),
            ("አሥራሁለት", 12),
            ("አሥራ አምስት", 15),
        ],
    )
    def test_teens(self, text: str, expected: int):
        assert parse_amharic_number(text) == expected

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("ሃያ አንድ", 21),
            ("ሃያ አምስት", 25),
            ("ሰላሳ ሁለት", 32),
            ("አርባ አምስት", 45),
            ("ሃምሳ ስድስት", 56),
            ("ሰባ ሰባት", 77),
            ("ሰማንያ ስምንት", 88),
            ("ዘጠና ዘጠኝ", 99),
        ],
    )
    def test_tens_with_units(self, text: str, expected: int):
        assert parse_amharic_number(text) == expected


class TestHundredsAndThousands:
    """Tests for scales: hundreds, thousands, millions."""

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("መቶ", 100),
            ("አንድ መቶ", 100),
            ("ሁለት መቶ", 200),
            ("ሁለት መቶ ሃምሳ", 250),
            ("ሁለት መቶ ሃምሳ አምስት", 255),
            ("አምስት መቶ", 500),
            ("ዘጠኝ መቶ ዘጠና ዘጠኝ", 999),
            ("ሺህ", 1000),
            ("አንድ ሺህ", 1000),
            ("አንድ ሺህ አምስት መቶ", 1500),
            ("ሁለት ሺህ", 2000),
            ("ሁለት ሺህ አምስት መቶ ሃምሳ", 2550),
            ("ሶስት ሺህ አራት መቶ ሰማንያ", 3480),
            ("አስር ሺህ", 10000),
            ("አስራ አምስት ሺህ", 15000),
            ("ሃምሳ ሺህ", 50000),
            ("አንድ ሚሊዮን", 1000000),
        ],
    )
    def test_large_scales(self, text: str, expected: int):
        assert parse_amharic_number(text) == expected


class TestMixedFormatsAndDigits:
    """Tests for Arabic digits, mixed words and digits, and currency noise."""

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("250", 250),
            ("250.0", 250),
            ("250.75", 250.75),
            ("1,500", 1500),
            ("2 ሺህ", 2000),
            ("2.5 ሺህ", 2500),
            ("10 ሺ", 10000),
            ("5 መቶ", 500),
            ("5 መቶ ሃምሳ", 550),
            ("2 ሺህ 5 መቶ", 2500),
            # With currency words
            ("250 ብር", 250),
            ("250.50 ብር", 250.5),
            ("ሁለት መቶ ሃምሳ ብር", 250),
            ("አንድ ሺህ ብር", 1000),
            ("500 ETB", 500),
        ],
    )
    def test_mixed_formats(self, text: str, expected: float):
        assert parse_amharic_number(text) == expected


class TestFractionsAndDecimals:
    """Tests for 'ተኩል' (half) and 'ነጥብ' (point)."""

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("አንድ ተኩል", 1.5),
            ("ሁለት ተኩል", 2.5),
            ("ሃምሳ ተኩል", 50.5),
            ("ሁለት ሺህ ተኩል", 2500),
            ("ሁለት መቶ ተኩል", 250),
            ("ሃያ ነጥብ አምስት", 20.5),
            ("መቶ ነጥብ ሃምሳ", 100.5),
            ("250 ነጥብ 75", 250.75),
        ],
    )
    def test_fractions(self, text: str, expected: float):
        assert parse_amharic_number(text) == expected


class TestSentenceExtraction:
    """Tests for extract_amount_from_text in natural language sentences."""

    def test_numeric_with_birr(self):
        text = "ትናንት ለትራንስፖርት 250 ብር ከፈልኩ።"
        assert extract_amount_from_text(text) == 250.0

    def test_words_with_birr(self):
        text = "ዛሬ ለምሳ ሁለት መቶ ሃምሳ ብር አወጣሁ"
        assert extract_amount_from_text(text) == 250.0

    def test_decimal_amount(self):
        text = "ለሻይ 15.50 ብር ከፈልኩ"
        assert extract_amount_from_text(text) == 15.5

    def test_prefix_currency(self):
        text = "ጠቅላላ ወጪ ብር 350 ነበር"
        assert extract_amount_from_text(text) == 350.0

    def test_no_amount_found(self):
        text = "ዛሬ ታክሲ ተሳፈርኩ ግን ዋጋውን አልነገሩኝም"
        assert extract_amount_from_text(text) is None


class TestEdgeCases:
    """Tests for empty strings, non-numeric strings, and punctuation."""

    def test_empty_input(self):
        assert parse_amharic_number("") is None
        assert parse_amharic_number("   ") is None
        assert extract_amount_from_text("") is None

    def test_invalid_text(self):
        assert parse_amharic_number("ሰላም") is None
        assert parse_amharic_number("አመሰግናለሁ") is None

    def test_numeric_types(self):
        assert parse_amharic_number(100) == 100
        assert parse_amharic_number(125.5) == 125.5
