"""
Amharic Number Parsing Module.

Simplified, clean parser focusing strictly on:
- Modern Amharic number words (e.g., 'ሁለት መቶ ሃምሳ' -> 250)
- Standard Arabic digits with optional commas and decimals (e.g., '250', '250.75', '1,500')
- Mixed words and digits (e.g., '2 ሺህ' -> 2000, '2.5 ሺህ' -> 2500, '5 መቶ' -> 500)
- Decimals and fractions ('ተኩል' -> half, 'ነጥብ' -> dot/decimal point)
- Sentence amount extraction (e.g., 'ትናንት ለትራንስፖርት 250 ብር ከፈልኩ።' -> 250.0)
"""

from __future__ import annotations

import re
from typing import Optional, Union

# Unit words (0-9)
UNITS: dict[str, int] = {
    "ዜሮ": 0,
    "አንድ": 1,
    "ሁለት": 2,
    "ሶስት": 3,
    "ሦስት": 3,
    "አራት": 4,
    "አምስት": 5,
    "ስድስት": 6,
    "ሰባት": 7,
    "ስምንት": 8,
    "ዘጠኝ": 9,
}

# Tens words (10-90)
TENS: dict[str, int] = {
    "አስር": 10,
    "አሥር": 10,
    "ዐስር": 10,
    "ዐሥር": 10,
    "ሃያ": 20,
    "ኻያ": 20,
    "ሰላሳ": 30,
    "ሠላሳ": 30,
    "አርባ": 40,
    "ሃምሳ": 50,
    "ሓምሳ": 50,
    "አምሳ": 50,
    "ስድሳ": 60,
    "ስልሳ": 60,
    "ሥልሳ": 60,
    "ሦልሳ": 60,
    "ሰባ": 70,
    "ሠባ": 70,
    "ሰማንያ": 80,
    "ሰማኔ": 80,
    "ሠማንያ": 80,
    "ዘጠና": 90,
}

# Standalone compound teens (11-19)
TEENS: dict[str, int] = {
    "አስራአንድ": 11, "አሥራአንድ": 11, "ዐስራአንድ": 11, "ዐሥራአንድ": 11,
    "አስራሁለት": 12, "አሥራሁለት": 12, "ዐስራሁለት": 12, "ዐሥራሁለት": 12,
    "አስራሶስት": 13, "አሥራሶስት": 13, "አስራሦስት": 13, "አሥራሦስት": 13,
    "አስራአራት": 14, "አሥራአራት": 14, "ዐስራአራት": 14, "ዐሥራአራት": 14,
    "አስራአምስት": 15, "አሥራአምስት": 15, "ዐስራአምስት": 15, "ዐሥራአምስት": 15,
    "አስራስድስት": 16, "አሥራስድስት": 16, "ዐስራስድስት": 16, "ዐሥራስድስት": 16,
    "አስራሰባት": 17, "አሥራሰባት": 17, "ዐስራሰባት": 17, "ዐሥራሰባት": 17,
    "አስራስምንት": 18, "አሥራስምንት": 18, "ዐስራስምንት": 18, "ዐሥራስምንት": 18,
    "አስራዘጠኝ": 19, "አሥራዘጠኝ": 19, "ዐስራዘጠኝ": 19, "ዐሥራዘጠኝ": 19,
}

# Prefix for composite teens like "አስራ አንድ"
TEEN_PREFIXES = {"አስራ", "አሥራ", "ዐስራ", "ዐሥራ"}

# Scale words
SCALES: dict[str, int] = {
    "መቶ": 100,
    "ምዕት": 100,
    "ሺህ": 1000,
    "ሺ": 1000,
    "እልፍ": 10000,
    "ሚሊዮን": 1_000_000,
    "ሚልዮን": 1_000_000,
    "ቢሊዮን": 1_000_000_000,
    "ቢልዮን": 1_000_000_000,
}

# Words indicating fractions or decimals
FRACTIONS: dict[str, float] = {
    "ተኩል": 0.5,
    "ሩብ": 0.25,
}

# Common currency and conversational noise words to strip
NOISE_WORDS = {
    "ብር", "ቅርሽ", "ሳንቲም", "etb", "birr", "ከፈልኩ", "አወጣሁ", "የብር", "ሒሳብ", "ለ"
}


def _clean_token(token: str) -> str:
    """Strip leading and trailing punctuation from a token."""
    return re.sub(r"^[^\w]+|[^\w]+$", "", token, flags=re.UNICODE)


def parse_amharic_number(text: Union[str, int, float]) -> Optional[Union[int, float]]:
    """
    Parses an Amharic number expression, Arabic digits, or mixed phrases.

    Examples:
        - "ሁለት መቶ ሃምሳ" -> 250
        - "አንድ ሺህ አምስት መቶ" -> 1500
        - "250" -> 250
        - "250.75" -> 250.75
        - "1,500" -> 1500
        - "2 ሺህ" -> 2000
        - "2.5 ሺህ" -> 2500
        - "5 መቶ" -> 500
        - "250 ብር" -> 250
        - "አስራ አምስት" -> 15
        - "ሁለት ሺህ ተኩል" -> 2500
        - "ሃምሳ ተኩል" -> 50.5
        - "ሃያ ነጥብ አምስት" -> 20.5
    """
    if isinstance(text, (int, float)):
        return text

    if not isinstance(text, str):
        return None

    raw_text = text.strip()
    if not raw_text:
        return None

    # 1. Pure Arabic numeric string (with optional comma grouping or decimal point)
    clean_numeric = raw_text.replace(",", "").strip()
    if re.fullmatch(r"^-?\d+(\.\d+)?$", clean_numeric):
        val = float(clean_numeric)
        return int(val) if val.is_integer() else val

    # 2. Handle decimal word "ነጥብ" (point)
    if "ነጥብ" in raw_text:
        parts = raw_text.split("ነጥብ", 1)
        int_part = parse_amharic_number(parts[0])
        frac_part_raw = parts[1].strip()
        if int_part is not None:
            # Check if fractional part is Arabic digits (e.g. "5", "75")
            frac_digits = re.findall(r"^\d+", frac_part_raw)
            if frac_digits:
                frac_val = float(f"0.{frac_digits[0]}")
                return float(int_part) + frac_val
            # Or words (e.g. "አምስት", "ሃምሳ")
            frac_words_val = parse_amharic_number(frac_part_raw)
            if frac_words_val is not None:
                frac_str = str(int(frac_words_val))
                frac_val = float(f"0.{frac_str}")
                return float(int_part) + frac_val

    # 3. Tokenize, normalize Ethiopic punctuation (፣፤፥፦፧፨።) into separators, and remove noise words
    normalized = re.sub(r"[፣፤፥፦፧፨።,;:?!]", " ", raw_text)
    raw_tokens = normalized.split()

    tokens: list[str] = []
    for t in raw_tokens:
        clean = _clean_token(t)
        if clean and clean.lower() not in NOISE_WORDS:
            tokens.append(clean)

    if not tokens:
        return None

    # Single-token fast path
    if len(tokens) == 1:
        tok = tokens[0]
        if re.fullmatch(r"^\d+(\.\d+)?$", tok):
            v = float(tok)
            return int(v) if v.is_integer() else v
        if tok in UNITS:
            return UNITS[tok]
        if tok in TENS:
            return TENS[tok]
        if tok in TEENS:
            return TEENS[tok]
        if tok in SCALES:
            return SCALES[tok]

    # 4. Multi-token compound evaluation
    total: float = 0.0
    current: float = 0.0
    last_scale: int = 1
    has_parsed_any = False

    i = 0
    while i < len(tokens):
        tok = tokens[i]

        # Check if token is Arabic digits (e.g., "2", "2.5", "500")
        if re.fullmatch(r"^\d+(\.\d+)?$", tok):
            num = float(tok)
            # If followed by a scale word (e.g. "2 ሺህ", "5 መቶ")
            if i + 1 < len(tokens) and tokens[i + 1] in SCALES:
                scale_val = SCALES[tokens[i + 1]]
                if scale_val >= 1000:
                    total += num * scale_val
                    last_scale = scale_val
                    current = 0.0
                else:
                    current += num * scale_val
                    last_scale = scale_val
                i += 2
                has_parsed_any = True
                continue
            else:
                current += num
                has_parsed_any = True
                i += 1
                continue

        # Check teen prefix ("አስራ አንድ" -> 11)
        if tok in TEEN_PREFIXES:
            if i + 1 < len(tokens) and tokens[i + 1] in UNITS:
                val = 10 + UNITS[tokens[i + 1]]
                current += val
                i += 2
                has_parsed_any = True
                continue
            else:
                # Standalone teen prefix treated as 10
                current += 10
                i += 1
                has_parsed_any = True
                continue

        # Check joined TEENS ("አስራአንድ")
        if tok in TEENS:
            current += TEENS[tok]
            has_parsed_any = True
            i += 1
            continue

        # Check UNITS (0-9)
        if tok in UNITS:
            current += UNITS[tok]
            has_parsed_any = True
            i += 1
            continue

        # Check TENS (10-90)
        if tok in TENS:
            current += TENS[tok]
            has_parsed_any = True
            i += 1
            continue

        # Check SCALES ("መቶ", "ሺህ", "ሚሊዮን")
        if tok in SCALES:
            scale_val = SCALES[tok]
            last_scale = scale_val
            # Standalone scale like "መቶ ሃምሳ" uses 1 as multiplier
            multiplier = current if current != 0.0 else 1.0

            if scale_val >= 1000:
                total += multiplier * scale_val
                current = 0.0
            else:
                current = multiplier * scale_val
            has_parsed_any = True
            i += 1
            continue

        # Check FRACTIONS ("ተኩል" -> 0.5, "ሩብ" -> 0.25)
        if tok in FRACTIONS:
            frac = FRACTIONS[tok]
            # Context-sensitive fraction:
            # "ሁለት ሺህ ተኩል" -> 2500 (half of 1000 is 500)
            # "ሁለት መቶ ተኩል" -> 250 (half of 100 is 50)
            # "ሃምሳ ተኩል" -> 50.5
            if last_scale >= 1000 and total > 0 and current == 0:
                total += last_scale * frac
            elif last_scale == 100 and current >= 100:
                current += 100 * frac
            else:
                current += frac
            has_parsed_any = True
            i += 1
            continue

        i += 1

    if not has_parsed_any:
        return None

    result = total + current
    return int(result) if result.is_integer() else result


def extract_amount_from_text(text: str) -> Optional[float]:
    """
    Extracts an expense amount from an Amharic natural language sentence or transcript.
    Matches Arabic digits, currency-adjacent expressions, or spelled-out Amharic number phrases.

    Examples:
        - 'ትናንት ለትራንስፖርት 250 ብር ከፈልኩ።' -> 250.0
        - 'ዛሬ ለምሳ ሁለት መቶ ሃምሳ ብር አወጣሁ' -> 250.0
        - 'ለሻይ 15.50 ብር' -> 15.5
    """
    if not text:
        return None

    # 1. Match numeric amount directly followed by currency (e.g., 250 ብር, 15.50 ETB)
    pattern = r"(\d+(?:\.\d+)?)\s*(?:ETB|ብር|ቅርሽ)"
    match = re.search(pattern, text, re.IGNORECASE)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass

    # 2. Match currency followed by amount (e.g., ብር 250)
    pattern_rev = r"(?:ETB|ብር|ቅርሽ)\s*(\d+(?:\.\d+)?)"
    match_rev = re.search(pattern_rev, text, re.IGNORECASE)
    if match_rev:
        try:
            return float(match_rev.group(1))
        except ValueError:
            pass

    # 3. Match Amharic words before currency (e.g., "ሁለት መቶ ሃምሳ ብር")
    words_before_birr = re.search(r"([\u1200-\u137F\s\d\.]+?)\s*(?:ብር|ቅርሽ|ETB)", text, re.IGNORECASE)
    if words_before_birr:
        candidate_phrase = words_before_birr.group(1).strip()
        parsed = parse_amharic_number(candidate_phrase)
        if parsed is not None and parsed > 0:
            return float(parsed)

    # 4. Fallback: match standalone Arabic digits
    digits_match = re.search(r"\b(\d+(?:\.\d+)?)\b", text)
    if digits_match:
        try:
            return float(digits_match.group(1))
        except ValueError:
            pass

    # 5. Fallback: entire text as an Amharic number phrase
    parsed_full = parse_amharic_number(text)
    if parsed_full is not None and parsed_full > 0:
        return float(parsed_full)

    return None
