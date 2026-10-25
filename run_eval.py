"""
Evaluation Runner for Amharic Voice Expense Agent.

Evaluates speech recognition (WER), entity extraction (amount, category, date),
intent classification, and tool selection against a labelled dataset of Amharic
audio recordings (.aac, .amr, .m4a, .oga, .ogg).

Guarantees:
- Zero writes to Google Sheets during evaluation (mocked/disabled).
- Automatic conversion of varied mobile audio formats into 128k mono MP3.
- Quantitative metrics computation matching the project workflow guide.
- Outputs detailed evaluation results to eval/results.csv and summary metrics.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import io
import logging
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Optional, Union

import dotenv

import amharic_numbers
from amharic_numbers import (
    FRACTIONS,
    SCALES,
    TEEN_PREFIXES,
    TEENS,
    TENS,
    UNITS,
    extract_amount_from_text,
    parse_amharic_number,
)
import audio
from agent import ExpenseAgent, ExtractedExpensePayload
import spending_service
from validation import ADDIS_TZ, Expense, resolve_relative_date

# Load environment configuration
dotenv.load_dotenv()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | [%(name)s] %(message)s",
)
logger = logging.getLogger("run_eval")

# Ensure UTF-8 output on Windows consoles
if sys.stdout.encoding != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 7 Fixed Categories defined in the system
FIXED_CATEGORIES = {
    "Transport",
    "Food",
    "Utilities",
    "Airtime",
    "Shopping",
    "Rent",
    "Other",
}

# Supported audio extensions for voice notes
AUDIO_EXTENSIONS = [".amr", ".aac", ".m4a", ".oga", ".ogg", ".wav", ".mp3", ".flac", ".mp4", ".3gp"]


def clean_transcript(text: Optional[str]) -> str:
    """
    Strips Ethiopic and standard punctuation and collapses extra whitespace.
    """
    if not text:
        return ""
    # Remove Ethiopic punctuation (U+1361 to U+1368) and standard punctuation marks
    cleaned = re.sub(r"[\u1360-\u1368\.,!\?:;\"\'\(\)\[\]\{\}\-—_]+", " ", str(text))
    return " ".join(cleaned.strip().split())


def normalize_numbers_in_transcript(text: Optional[str]) -> str:
    """
    Converts spelled-out Amharic number words in a transcript into Arabic digits.
    Allows fair number-normalized WER calculation (e.g. 'ሃያ' vs '20').
    """
    cleaned = clean_transcript(text)
    if not cleaned:
        return ""

    tokens = cleaned.split()
    new_tokens: list[str] = []
    num_buffer: list[str] = []

    all_num_words = (
        set(UNITS.keys())
        | set(TENS.keys())
        | set(TEENS.keys())
        | set(TEEN_PREFIXES)
        | set(SCALES.keys())
        | set(FRACTIONS.keys())
    )

    def flush_buffer() -> None:
        nonlocal num_buffer
        if not num_buffer:
            return
        buf_str = " ".join(num_buffer)
        parsed = parse_amharic_number(buf_str)
        if parsed is not None:
            formatted = str(int(parsed) if float(parsed).is_integer() else parsed)
            new_tokens.append(formatted)
        else:
            new_tokens.extend(num_buffer)
        num_buffer = []

    for tok in tokens:
        cleaned_tok = tok.replace(",", "").replace(".", "")
        if cleaned_tok.isdigit() or tok in all_num_words:
            num_buffer.append(tok)
        else:
            flush_buffer()
            new_tokens.append(tok)

    flush_buffer()
    return " ".join(new_tokens)


def compute_wer(reference: str, hypothesis: str) -> float:
    """
    Computes Word Error Rate (WER) between reference and hypothesis transcripts.
    Uses 'jiwer' if installed, otherwise calculates exact Levenshtein word edit distance.
    WER = (Substitutions + Deletions + Insertions) / len(reference_words)
    """
    try:
        import jiwer
        return float(jiwer.wer(reference, hypothesis))
    except Exception:
        ref_words = reference.strip().split()
        hyp_words = hypothesis.strip().split()
        if not ref_words:
            return 0.0 if not hyp_words else float(len(hyp_words))

        # Wagner-Fischer algorithm for word tokens
        d = [[0] * (len(hyp_words) + 1) for _ in range(len(ref_words) + 1)]
        for i in range(len(ref_words) + 1):
            d[i][0] = i
        for j in range(len(hyp_words) + 1):
            d[0][j] = j

        for i in range(1, len(ref_words) + 1):
            for j in range(1, len(hyp_words) + 1):
                if ref_words[i - 1] == hyp_words[j - 1]:
                    d[i][j] = d[i - 1][j - 1]
                else:
                    d[i][j] = min(
                        d[i - 1][j - 1] + 1,  # Substitution
                        d[i][j - 1] + 1,      # Insertion
                        d[i - 1][j] + 1,      # Deletion
                    )

        return float(d[len(ref_words)][len(hyp_words)] / len(ref_words))


def detect_number_style(text: str) -> str:
    """
    Detects whether Gemini transcribed numbers as words, digits, or mixed.
    """
    if not text:
        return "none"

    has_digits = bool(re.search(r"\d", text))

    all_num_words = (
        set(UNITS.keys())
        | set(TENS.keys())
        | set(TEENS.keys())
        | set(SCALES.keys())
    )
    tokens = set(clean_transcript(text).split())
    has_words = bool(tokens & all_num_words)

    if has_digits and has_words:
        return "mixed"
    if has_digits:
        return "digits"
    if has_words:
        return "words"
    return "none"


def locate_audio_file(sample_id: str, audio_file_name: str, voice_notes_dir: Path) -> Optional[Path]:
    """
    Locates the audio file for a given sample, handling differences in extensions
    such as .aac, .amr, .m4a, or .ogg.
    """
    if not voice_notes_dir.exists():
        return None

    # 1. Direct path check
    if audio_file_name:
        direct = voice_notes_dir / audio_file_name
        if direct.exists() and direct.is_file():
            return direct

    # 2. Try stems from audio_file_name and sample_id
    stems = []
    if audio_file_name:
        stems.append(Path(audio_file_name).stem)
    if sample_id:
        stems.append(sample_id)

    for stem in stems:
        for ext in AUDIO_EXTENSIONS:
            candidate = voice_notes_dir / f"{stem}{ext}"
            if candidate.exists() and candidate.is_file():
                return candidate
            candidate_upper = voice_notes_dir / f"{stem}{ext.upper()}"
            if candidate_upper.exists() and candidate_upper.is_file():
                return candidate_upper

    return None


def map_expected_tool(expected_behavior: str) -> str:
    """Maps expected behavior to expected tool name."""
    b = (expected_behavior or "").strip().lower()
    if b == "save-after-confirm":
        return "add_expense"
    elif b == "answer-query":
        return "get_expenses"
    elif b in ("ask-amount", "ask-date"):
        return "ask_for_clarification"
    elif b == "refuse-no-tool":
        return "none"
    return "none"


def determine_prediction_behavior_and_tool(
    intent: str,
    amount: Optional[float],
    days_ago_delta: int,
    date_expr: Optional[str],
    is_clear: bool,
    transcript: str,
) -> tuple[str, str, str]:
    """
    Determines predicted behavior, tool, and date_ref from Gemini extraction & validation.
    Returns: (predicted_behavior, predicted_tool, predicted_date_ref)
    """
    # Map intent
    norm_intent = intent.lower() if intent else "other"
    if norm_intent in ("spending_query", "query_spending"):
        norm_intent = "query_spending"
    elif norm_intent in ("clarification_needed", "other"):
        norm_intent = "other"

    # Query routing
    if norm_intent == "query_spending" or spending_service.is_spending_query(transcript):
        date_ref = "today" if days_ago_delta == 0 else ("yesterday" if days_ago_delta == -1 else "other")
        return "answer-query", "get_expenses", date_ref

    # Non-expense routing
    if norm_intent == "other" or not is_clear:
        return "refuse-no-tool", "none", "none"

    # Expense routing
    if norm_intent == "add_expense":
        # Check missing amount
        if amount is None or amount <= 0:
            date_ref = "today" if days_ago_delta == 0 else ("yesterday" if days_ago_delta == -1 else ("day_before" if days_ago_delta == -2 else "other"))
            return "ask-amount", "ask_for_clarification", date_ref

        # Check unsupported date (e.g., 'ባለፈው ሳምንት' or delta < -3)
        if date_expr and "ሳምንት" in date_expr:
            return "ask-date", "ask_for_clarification", "other"
        if days_ago_delta < -2:
            return "ask-date", "ask_for_clarification", "other"

        # Valid expense with resolvable date
        if days_ago_delta == 0:
            date_ref = "today"
        elif days_ago_delta == -1:
            date_ref = "yesterday"
        elif days_ago_delta == -2:
            date_ref = "day_before"
        else:
            date_ref = "other"

        return "save-after-confirm", "add_expense", date_ref

    return "refuse-no-tool", "none", "none"


def run_evaluation(
    dataset_path: Union[str, Path] = "eval/dataset.csv",
    audio_dir: Union[str, Path] = "eval/voice_notes",
    output_path: Union[str, Path] = "eval/results.csv",
    model_name: str = "gemini-3.5-flash-lite",
    delay: float = 1.0,
    limit: Optional[int] = None,
    mock_agent: Optional[Any] = None,
) -> dict[str, Any]:
    """
    Main evaluation workflow:
    1. Loads dataset.csv.
    2. Aligns audio files.
    3. Converts to 128k mono MP3 in a temp dir.
    4. Calls Gemini via ExpenseAgent.
    5. Validates extraction and calculates metrics.
    6. Saves results.csv and returns summary dict.
    """
    d_path = Path(dataset_path)
    if not d_path.exists():
        # Try case-insensitive fallback (e.g. Eval/dataset.csv)
        fallback = Path("Eval/dataset.csv") if str(d_path).startswith("eval") else Path("eval/dataset.csv")
        if fallback.exists():
            d_path = fallback
        else:
            raise FileNotFoundError(f"Dataset CSV not found at {d_path}")

    a_dir = Path(audio_dir)
    if not a_dir.exists():
        fallback_a = Path("Eval/voice_notes") if str(a_dir).startswith("eval") else Path("eval/voice_notes")
        if fallback_a.exists():
            a_dir = fallback_a

    out_p = Path(output_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)

    # Initialize Agent
    if mock_agent is not None:
        agent = mock_agent
    else:
        agent = ExpenseAgent(model_name=model_name)

    # Load dataset
    with open(d_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        all_rows = list(reader)

    if limit is not None and limit > 0:
        rows_to_process = all_rows[:limit]
    else:
        rows_to_process = all_rows

    logger.info(
        "Starting evaluation runner: %d rows to evaluate | Model: %s | Audio dir: %s",
        len(rows_to_process),
        getattr(agent, "model_name", model_name),
        a_dir,
    )

    results: list[dict[str, Any]] = []

    # Processing Loop
    for idx, row in enumerate(rows_to_process, start=1):
        sample_id = row.get("id", f"sample_{idx}").strip()
        audio_name = row.get("audio_file", "").strip()
        ref_transcript = row.get("reference_transcript", "").strip()
        exp_intent = row.get("intent", "").strip().lower()
        if exp_intent == "spending_query":
            exp_intent = "query_spending"

        exp_amount_str = row.get("expected_amount", "").strip()
        exp_amount = float(exp_amount_str) if exp_amount_str else None
        exp_cat = row.get("expected_category", "").strip()
        exp_date_ref = row.get("expected_date_ref", "").strip()
        exp_date = row.get("expected_date", "").strip()
        exp_behavior = row.get("expected_behavior", "").strip()
        exp_tool = map_expected_tool(exp_behavior)

        # Locate audio file
        audio_file_path = locate_audio_file(sample_id, audio_name, a_dir)

        if not audio_file_path:
            logger.warning(
                "[%d/%d] Sample %s: audio file not found (expected %s). Skipping prediction.",
                idx,
                len(rows_to_process),
                sample_id,
                audio_name,
            )
            results.append({
                "id": sample_id,
                "audio_file": audio_name,
                "speaker_id": row.get("speaker_id", ""),
                "noise_level": row.get("noise_level", ""),
                "reference_transcript": ref_transcript,
                "intent": exp_intent,
                "expected_amount": exp_amount_str,
                "expected_category": exp_cat,
                "expected_date_ref": exp_date_ref,
                "expected_date": exp_date,
                "expected_behavior": exp_behavior,
                "gemini_transcript": "",
                "gemini_number_style": "none",
                "pred_intent": "",
                "pred_amount": "",
                "pred_category": "",
                "pred_date_ref": "",
                "pred_date": "",
                "pred_tool": "",
                "pred_behavior": "",
                "wer_strict": "",
                "wer_normalized": "",
                "amount_match": "False",
                "category_match": "False",
                "intent_match": "False",
                "date_match": "False",
                "tool_match": "False",
                "status": "MISSING_AUDIO",
            })
            continue

        temp_mp3: Optional[Path] = None
        try:
            # 1. Convert to 128k mono MP3 in temp dir
            temp_mp3 = audio.convert_to_temp_mp3(
                input_path=audio_file_path,
                bitrate="128k",
                sample_rate=16000,
            )

            # 2. Call Gemini
            t0 = time.perf_counter()
            expense, payload = agent.process_audio(temp_mp3)
            elapsed = time.perf_counter() - t0

            gemini_transcript = payload.transcript or ""
            pred_amount = payload.amount
            if pred_amount is None and payload.transcript:
                # Deterministic fallback check
                det_amt = extract_amount_from_text(payload.transcript)
                if det_amt:
                    pred_amount = det_amt

            pred_category = payload.category or "Other"
            pred_intent = payload.intent.lower() if payload.intent else "other"
            if pred_intent == "spending_query":
                pred_intent = "query_spending"

            pred_behavior, pred_tool, pred_date_ref = determine_prediction_behavior_and_tool(
                intent=pred_intent,
                amount=pred_amount,
                days_ago_delta=payload.days_ago_delta,
                date_expr=payload.date_expression,
                is_clear=payload.is_clear,
                transcript=gemini_transcript,
            )

            # Resolve predicted date
            try:
                pred_date = resolve_relative_date(
                    date_expr=payload.date_expression,
                    days_ago_delta=payload.days_ago_delta,
                )
            except Exception:
                pred_date = resolve_relative_date("ዛሬ")

            # Detect number style
            num_style = detect_number_style(gemini_transcript)

            # Compute WER
            strict_ref = clean_transcript(ref_transcript)
            strict_hyp = clean_transcript(gemini_transcript)
            wer_strict = compute_wer(strict_ref, strict_hyp)

            norm_ref = normalize_numbers_in_transcript(ref_transcript)
            norm_hyp = normalize_numbers_in_transcript(gemini_transcript)
            wer_norm = compute_wer(norm_ref, norm_hyp)

            # Evaluate Metrics Matches
            # Amount Match (for add_expense items with expected amount)
            amount_match = False
            if exp_amount is not None:
                amount_match = (pred_amount is not None and abs(pred_amount - exp_amount) < 0.01)

            # Category Match
            category_match = False
            if exp_cat:
                category_match = (pred_category.strip().lower() == exp_cat.strip().lower())

            # Intent Match
            intent_match = (pred_intent == exp_intent)

            # Date Match
            date_match = False
            if exp_date_ref:
                norm_exp_dr = exp_date_ref.lower()
                norm_pred_dr = pred_date_ref.lower()
                if norm_exp_dr == "none":
                    date_match = norm_pred_dr in ("today", "none")
                elif norm_exp_dr == "other":
                    date_match = (norm_pred_dr == "other" or pred_behavior == "ask-date")
                else:
                    date_match = (norm_exp_dr == norm_pred_dr)

            # Tool Match
            tool_match = (pred_tool == exp_tool)

            logger.info(
                "[%d/%d] Sample %s | WER: %.2f (Norm: %.2f) | Intent: %s (exp: %s) | Amount: %s (exp: %s) | (%.2fs)",
                idx,
                len(rows_to_process),
                sample_id,
                wer_strict,
                wer_norm,
                pred_intent,
                exp_intent,
                pred_amount,
                exp_amount,
                elapsed,
            )

            results.append({
                "id": sample_id,
                "audio_file": audio_file_path.name,
                "speaker_id": row.get("speaker_id", ""),
                "noise_level": row.get("noise_level", ""),
                "reference_transcript": ref_transcript,
                "intent": exp_intent,
                "expected_amount": exp_amount_str,
                "expected_category": exp_cat,
                "expected_date_ref": exp_date_ref,
                "expected_date": exp_date,
                "expected_behavior": exp_behavior,
                "gemini_transcript": gemini_transcript,
                "gemini_number_style": num_style,
                "pred_intent": pred_intent,
                "pred_amount": f"{pred_amount:.2f}" if pred_amount is not None else "",
                "pred_category": pred_category,
                "pred_date_ref": pred_date_ref,
                "pred_date": pred_date,
                "pred_tool": pred_tool,
                "pred_behavior": pred_behavior,
                "wer_strict": f"{wer_strict:.4f}",
                "wer_normalized": f"{wer_norm:.4f}",
                "amount_match": str(amount_match),
                "category_match": str(category_match),
                "intent_match": str(intent_match),
                "date_match": str(date_match),
                "tool_match": str(tool_match),
                "status": "SUCCESS",
            })

        except Exception as err:
            logger.error("[%d/%d] Sample %s failed: %s", idx, len(rows_to_process), sample_id, err)
            results.append({
                "id": sample_id,
                "audio_file": audio_file_path.name if audio_file_path else audio_name,
                "speaker_id": row.get("speaker_id", ""),
                "noise_level": row.get("noise_level", ""),
                "reference_transcript": ref_transcript,
                "intent": exp_intent,
                "expected_amount": exp_amount_str,
                "expected_category": exp_cat,
                "expected_date_ref": exp_date_ref,
                "expected_date": exp_date,
                "expected_behavior": exp_behavior,
                "gemini_transcript": "",
                "gemini_number_style": "none",
                "pred_intent": "error",
                "pred_amount": "",
                "pred_category": "",
                "pred_date_ref": "",
                "pred_date": "",
                "pred_tool": "none",
                "pred_behavior": "error",
                "wer_strict": "1.0000",
                "wer_normalized": "1.0000",
                "amount_match": "False",
                "category_match": "False",
                "intent_match": "False",
                "date_match": "False",
                "tool_match": "False",
                "status": f"ERROR: {err}",
            })
        finally:
            if temp_mp3 is not None:
                audio.cleanup_files(temp_mp3)

        # Rate-limiting delay between Gemini calls
        if delay > 0 and idx < len(rows_to_process):
            time.sleep(delay)

    # Write results.csv
    fieldnames = [
        "id",
        "audio_file",
        "speaker_id",
        "noise_level",
        "reference_transcript",
        "intent",
        "expected_amount",
        "expected_category",
        "expected_date_ref",
        "expected_date",
        "expected_behavior",
        "gemini_transcript",
        "gemini_number_style",
        "pred_intent",
        "pred_amount",
        "pred_category",
        "pred_date_ref",
        "pred_date",
        "pred_tool",
        "pred_behavior",
        "wer_strict",
        "wer_normalized",
        "amount_match",
        "category_match",
        "intent_match",
        "date_match",
        "tool_match",
        "status",
    ]

    with open(out_p, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    logger.info("Detailed evaluation results successfully written to: %s", out_p)

    # Compute Aggregate Metrics
    summary = compute_summary_metrics(results)
    print_summary_report(summary, output_csv_path=out_p)

    return summary


def compute_summary_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Computes quantitative metrics according to Section 7 of the project workflow guide.
    """
    evaluated = [r for r in results if r.get("status") == "SUCCESS"]
    total_evaluated = len(evaluated)
    total_dataset = len(results)

    if total_evaluated == 0:
        return {
            "total_samples": total_dataset,
            "evaluated_samples": 0,
            "skipped_samples": total_dataset,
            "wer_strict_avg": 0.0,
            "wer_norm_avg": 0.0,
            "intent_accuracy": 0.0,
            "tool_accuracy": 0.0,
            "category_accuracy": 0.0,
            "date_accuracy": 0.0,
            "amount_accuracy_overall": 0.0,
            "amount_accuracy_group_a": 0.0,
            "amount_accuracy_group_b": 0.0,
            "amount_accuracy_group_c": 0.0,
        }

    # 1. Transcript WER
    wers_strict = [float(r["wer_strict"]) for r in evaluated if r.get("wer_strict")]
    wers_norm = [float(r["wer_normalized"]) for r in evaluated if r.get("wer_normalized")]

    avg_wer_strict = sum(wers_strict) / len(wers_strict) if wers_strict else 0.0
    avg_wer_norm = sum(wers_norm) / len(wers_norm) if wers_norm else 0.0

    # 2. Intent Accuracy (across all evaluated items)
    intent_matches = sum(1 for r in evaluated if r["intent_match"] == "True")
    intent_acc = intent_matches / total_evaluated

    # 3. Tool Selection Accuracy
    tool_matches = sum(1 for r in evaluated if r["tool_match"] == "True")
    tool_acc = tool_matches / total_evaluated

    # 4. Category Accuracy (for items with expected category)
    cat_items = [r for r in evaluated if r.get("expected_category")]
    cat_matches = sum(1 for r in cat_items if r["category_match"] == "True")
    cat_acc = cat_matches / len(cat_items) if cat_items else 0.0

    # 5. Date Accuracy
    date_items = [r for r in evaluated if r.get("expected_date_ref")]
    date_matches = sum(1 for r in date_items if r["date_match"] == "True")
    date_acc = date_matches / len(date_items) if date_items else 0.0

    # 6. Amount Accuracy (Overall and by Groups A, B, C)
    amt_items = [r for r in evaluated if r.get("expected_amount") and r.get("intent") == "add_expense"]
    amt_matches = sum(1 for r in amt_items if r["amount_match"] == "True")
    amt_acc_overall = amt_matches / len(amt_items) if amt_items else 0.0

    # Group A: Small amounts (A01 - A08)
    group_a = [r for r in amt_items if r["id"].startswith("A")]
    group_a_matches = sum(1 for r in group_a if r["amount_match"] == "True")
    group_a_acc = group_a_matches / len(group_a) if group_a else 0.0

    # Group B: Hundreds (B01 - B08)
    group_b = [r for r in amt_items if r["id"].startswith("B")]
    group_b_matches = sum(1 for r in group_b if r["amount_match"] == "True")
    group_b_acc = group_b_matches / len(group_b) if group_b else 0.0

    # Group C: Thousands (C01 - C06)
    group_c = [r for r in amt_items if r["id"].startswith("C")]
    group_c_matches = sum(1 for r in group_c if r["amount_match"] == "True")
    group_c_acc = group_c_matches / len(group_c) if group_c else 0.0

    return {
        "total_samples": total_dataset,
        "evaluated_samples": total_evaluated,
        "skipped_samples": total_dataset - total_evaluated,
        "wer_strict_avg": avg_wer_strict,
        "wer_norm_avg": avg_wer_norm,
        "intent_accuracy": intent_acc,
        "intent_counts": f"{intent_matches}/{total_evaluated}",
        "tool_accuracy": tool_acc,
        "tool_counts": f"{tool_matches}/{total_evaluated}",
        "category_accuracy": cat_acc,
        "category_counts": f"{cat_matches}/{len(cat_items)}",
        "date_accuracy": date_acc,
        "date_counts": f"{date_matches}/{len(date_items)}",
        "amount_accuracy_overall": amt_acc_overall,
        "amount_counts_overall": f"{amt_matches}/{len(amt_items)}",
        "amount_accuracy_group_a": group_a_acc,
        "amount_counts_group_a": f"{group_a_matches}/{len(group_a)}",
        "amount_accuracy_group_b": group_b_acc,
        "amount_counts_group_b": f"{group_b_matches}/{len(group_b)}",
        "amount_accuracy_group_c": group_c_acc,
        "amount_counts_group_c": f"{group_c_matches}/{len(group_c)}",
    }


def print_summary_report(summary: dict[str, Any], output_csv_path: Optional[Path] = None) -> None:
    """
    Prints a formatted summary metrics report matching Section 7 of the project workflow guide.
    """
    total = summary["total_samples"]
    evaluated = summary["evaluated_samples"]
    skipped = summary["skipped_samples"]

    report = f"""
================================================================================
            AMHARIC VOICE EXPENSE AGENT - EVALUATION BENCHMARK REPORT
================================================================================
Dataset Size:           {total} target samples ({evaluated} evaluated, {skipped} missing/skipped)
Timezone:               Africa/Addis_Ababa (UTC+3)
Currency:               ETB (Ethiopian Birr)
Google Sheets Writes:   COMPLETELY DISABLED / MOCKED (Live data protected)
--------------------------------------------------------------------------------
METRIC                         RESULT          COUNTS / DETAILS
--------------------------------------------------------------------------------
Transcript WER (Strict)        {summary['wer_strict_avg']*100:6.2f}%         Punctuation/whitespace cleaned
Transcript WER (Normalized)    {summary['wer_norm_avg']*100:6.2f}%         Numbers converted to digits
Amount Accuracy (Overall)      {summary['amount_accuracy_overall']*100:6.2f}%         {summary.get('amount_counts_overall', '')}
  - Group A: Small (<100)      {summary['amount_accuracy_group_a']*100:6.2f}%         {summary.get('amount_counts_group_a', '')}
  - Group B: Hundreds          {summary['amount_accuracy_group_b']*100:6.2f}%         {summary.get('amount_counts_group_b', '')}
  - Group C: Thousands         {summary['amount_accuracy_group_c']*100:6.2f}%         {summary.get('amount_counts_group_c', '')}
Date Accuracy                  {summary['date_accuracy']*100:6.2f}%         {summary.get('date_counts', '')}
Category Accuracy              {summary['category_accuracy']*100:6.2f}%         {summary.get('category_counts', '')} (7 fixed classes)
Intent Classification Accuracy {summary['intent_accuracy']*100:6.2f}%         {summary.get('intent_counts', '')} (add vs query vs other)
Tool Selection Accuracy        {summary['tool_accuracy']*100:6.2f}%         {summary.get('tool_counts', '')} (add / get / ask / none)
================================================================================
Detailed predictions saved to: {output_csv_path or 'eval/results.csv'}
================================================================================
"""
    print(report)


def main() -> None:
    """CLI Entrypoint for running dataset evaluation."""
    parser = argparse.ArgumentParser(
        description="Amharic Voice Expense Agent - Evaluation Runner (Step 14)"
    )
    parser.add_argument(
        "--dataset",
        type=str,
        default="Eval/dataset.csv",
        help="Path to evaluation dataset CSV (default: Eval/dataset.csv)",
    )
    parser.add_argument(
        "--audio-dir",
        type=str,
        default="Eval/voice_notes",
        help="Directory containing voice recordings (default: Eval/voice_notes)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="Eval/results.csv",
        help="Path to save evaluation results CSV (default: Eval/results.csv)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        help="Gemini model name (default: gemini-3.5-flash-lite)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Delay in seconds between Gemini API calls to respect rate limits (default: 1.0)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit evaluation to the first N samples (optional)",
    )

    args = parser.parse_args()

    run_evaluation(
        dataset_path=args.dataset,
        audio_dir=args.audio_dir,
        output_path=args.output,
        model_name=args.model,
        delay=args.delay,
        limit=args.limit,
    )


if __name__ == "__main__":
    main()
