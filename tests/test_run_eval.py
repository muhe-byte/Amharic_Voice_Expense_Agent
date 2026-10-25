"""
Unit tests for run_eval.py evaluation runner.

Tests:
- Audio file alignment and format mapping (.aac, .amr, .m4a, .ogg)
- Verbatim transcript cleaning and punctuation removal
- Amharic number-word normalization in transcripts
- Word Error Rate (WER) computation (exact Levenshtein and jiwer fallback)
- Intent, category, date, amount, and tool mapping logic
- Metric summarization and CSV reporting
- Verification that Sheets writes are never triggered
"""

from pathlib import Path
import tempfile
from unittest.mock import MagicMock
import pytest

from agent import ExtractedExpensePayload
from run_eval import (
    clean_transcript,
    compute_summary_metrics,
    compute_wer,
    detect_number_style,
    determine_prediction_behavior_and_tool,
    locate_audio_file,
    map_expected_tool,
    normalize_numbers_in_transcript,
    run_evaluation,
)
from validation import Expense


class TestTranscriptProcessingAndWER:
    """Tests for text normalization and WER computation."""

    def test_clean_transcript_removes_ethiopic_and_latin_punctuation(self):
        raw = "ዛሬ ለታክሲ፣ ሃያ ብር ከፈልኩ።"
        cleaned = clean_transcript(raw)
        assert cleaned == "ዛሬ ለታክሲ ሃያ ብር ከፈልኩ"

        raw_query = "ዛሬ ለትራንስፖርት ምን ያህል አወጣሁ?"
        assert clean_transcript(raw_query) == "ዛሬ ለትራንስፖርት ምን ያህል አወጣሁ"

    def test_normalize_numbers_converts_amharic_words_to_digits(self):
        assert normalize_numbers_in_transcript("ዛሬ ለታክሲ ሃያ ብር ከፈልኩ") == "ዛሬ ለታክሲ 20 ብር ከፈልኩ"
        assert normalize_numbers_in_transcript("ትናንት ለታክሲ አሥራ አምስት ብር ከፈልኩ") == "ትናንት ለታክሲ 15 ብር ከፈልኩ"
        assert normalize_numbers_in_transcript("ትናንት ለትራንስፖርት ሁለት መቶ ሃምሳ ብር ከፈልኩ") == "ትናንት ለትራንስፖርት 250 ብር ከፈልኩ"
        assert normalize_numbers_in_transcript("ትናንት ለኤሌክትሪክ አንድ ሺህ ሁለት መቶ ብር ከፈልኩ") == "ትናንት ለኤሌክትሪክ 1200 ብር ከፈልኩ"

    def test_compute_wer_exact_and_substitutions(self):
        ref = "ዛሬ ለታክሲ ሃያ ብር ከፈልኩ"
        hyp_exact = "ዛሬ ለታክሲ ሃያ ብር ከፈልኩ"
        assert compute_wer(ref, hyp_exact) == 0.0

        # One word substituted (5 words total: 1/5 = 0.20)
        hyp_sub = "ዛሬ ለታክሲ ሠላሳ ብር ከፈልኩ"
        assert abs(compute_wer(ref, hyp_sub) - 0.20) < 1e-5

        # One word deleted (4 words in hyp vs 5 in ref: 1/5 = 0.20)
        hyp_del = "ዛሬ ለታክሲ ብር ከፈልኩ"
        assert abs(compute_wer(ref, hyp_del) - 0.20) < 1e-5

    def test_detect_number_style(self):
        assert detect_number_style("ዛሬ ለታክሲ ሃያ ብር") == "words"
        assert detect_number_style("ዛሬ ለታክሲ 20 ብር") == "digits"
        assert detect_number_style("ዛሬ 2 ሺህ ብር") == "mixed"
        assert detect_number_style("ሰላም እንዴት ነህ") == "none"


class TestAudioAlignmentAndMapping:
    """Tests for locating audio files with varying extensions."""

    def test_locate_audio_file_various_extensions(self, tmp_path):
        # Create dummy audio files with various mobile formats
        (tmp_path / "A01.amr").write_bytes(b"dummy_amr")
        (tmp_path / "C01.aac").write_bytes(b"dummy_aac")
        (tmp_path / "E05.m4a").write_bytes(b"dummy_m4a")

        # 1. Direct name lookup
        found_amr = locate_audio_file("A01", "A01.amr", tmp_path)
        assert found_amr is not None
        assert found_amr.name == "A01.amr"

        # 2. Lookup when dataset specifies .ogg but file is .aac
        found_aac = locate_audio_file("C01", "C01.ogg", tmp_path)
        assert found_aac is not None
        assert found_aac.name == "C01.aac"

        # 3. Lookup when audio_file is just stem
        found_m4a = locate_audio_file("E05", "E05", tmp_path)
        assert found_m4a is not None
        assert found_m4a.name == "E05.m4a"

        # 4. Missing file returns None
        assert locate_audio_file("H01", "H01.m4a", tmp_path) is None


class TestToolAndBehaviorMapping:
    """Tests for behavioral and tool selection mapping."""

    def test_map_expected_tool(self):
        assert map_expected_tool("save-after-confirm") == "add_expense"
        assert map_expected_tool("answer-query") == "get_expenses"
        assert map_expected_tool("ask-amount") == "ask_for_clarification"
        assert map_expected_tool("ask-date") == "ask_for_clarification"
        assert map_expected_tool("refuse-no-tool") == "none"

    def test_determine_prediction_behavior_and_tool(self):
        # Add expense valid
        b, t, dr = determine_prediction_behavior_and_tool(
            intent="add_expense",
            amount=250.0,
            days_ago_delta=-1,
            date_expr="ትናንት",
            is_clear=True,
            transcript="ትናንት ለትራንስፖርት 250 ብር ከፈልኩ",
        )
        assert b == "save-after-confirm"
        assert t == "add_expense"
        assert dr == "yesterday"

        # Query spending
        b, t, dr = determine_prediction_behavior_and_tool(
            intent="query_spending",
            amount=None,
            days_ago_delta=0,
            date_expr="ዛሬ",
            is_clear=True,
            transcript="ዛሬ ለትራንስፖርት ምን ያህል አወጣሁ",
        )
        assert b == "answer-query"
        assert t == "get_expenses"

        # Missing amount
        b, t, dr = determine_prediction_behavior_and_tool(
            intent="add_expense",
            amount=None,
            days_ago_delta=0,
            date_expr="ዛሬ",
            is_clear=True,
            transcript="ዛሬ ለታክሲ ከፈልኩ",
        )
        assert b == "ask-amount"
        assert t == "ask_for_clarification"

        # Unsupported date (e.g. ባለፈው ሳምንት)
        b, t, dr = determine_prediction_behavior_and_tool(
            intent="add_expense",
            amount=250.0,
            days_ago_delta=-7,
            date_expr="ባለፈው ሳምንት",
            is_clear=True,
            transcript="ባለፈው ሳምንት ለታክሲ ሁለት መቶ ሃምሳ ብር ከፈልኩ",
        )
        assert b == "ask-date"
        assert t == "ask_for_clarification"
        assert dr == "other"

        # Other / greeting
        b, t, dr = determine_prediction_behavior_and_tool(
            intent="other",
            amount=None,
            days_ago_delta=0,
            date_expr=None,
            is_clear=True,
            transcript="ሰላም እንዴት ነህ",
        )
        assert b == "refuse-no-tool"
        assert t == "none"


class TestEvaluationPipelineWithMockAgent:
    """Verifies end-to-end evaluation runner with mock agent and safety checks."""

    def test_run_evaluation_mocked(self, tmp_path):
        # Create a mini dataset CSV
        dataset_csv = tmp_path / "dataset.csv"
        dataset_csv.write_text(
            "id,audio_file,speaker_id,noise_level,reference_transcript,intent,expected_amount,expected_category,expected_date_ref,expected_date,expected_behavior\n"
            "A01,A01.amr,S1,quiet,ዛሬ ለታክሲ ሃያ ብር ከፈልኩ,add_expense,20,Transport,today,2026-10-25,save-after-confirm\n"
            "F01,F01.m4a,S3,quiet,ዛሬ ለትራንስፖርት ምን ያህል አወጣሁ?,query_spending,,Transport,today,2026-10-25,answer-query\n"
            "H01,H01.m4a,S1,noisy,ትናንት ለታክሲ ሃያ ብር ከፈልኩ,add_expense,20,Transport,yesterday,2026-10-24,save-after-confirm\n",
            encoding="utf-8",
        )

        # Audio directory with A01 and F01 present, H01 missing
        audio_dir = tmp_path / "voice_notes"
        audio_dir.mkdir()
        (audio_dir / "A01.amr").write_bytes(b"dummy_a01")
        (audio_dir / "F01.m4a").write_bytes(b"dummy_f01")

        results_csv = tmp_path / "results.csv"

        # Mock agent
        mock_agent = MagicMock()

        def side_effect(mp3_path):
            stem = Path(mp3_path).stem
            if "A01" in stem:
                return (
                    Expense(
                        amount=20.0,
                        currency="ETB",
                        category="Transport",
                        transcript="ዛሬ ለታክሲ ሃያ ብር ከፈልኩ",
                        date_expression="ዛሬ",
                        days_ago_delta=0,
                        transaction_date="2026-10-25",
                        description="Taxi",
                    ),
                    ExtractedExpensePayload(
                        transcript="ዛሬ ለታክሲ ሃያ ብር ከፈልኩ",
                        amount=20.0,
                        currency="ETB",
                        category="Transport",
                        date_expression="ዛሬ",
                        days_ago_delta=0,
                        intent="add_expense",
                        is_clear=True,
                    ),
                )
            else:
                return (
                    None,
                    ExtractedExpensePayload(
                        transcript="ዛሬ ለትራንስፖርት ምን ያህል አወጣሁ?",
                        amount=None,
                        currency="ETB",
                        category="Transport",
                        date_expression="ዛሬ",
                        days_ago_delta=0,
                        intent="query_spending",
                        is_clear=True,
                    ),
                )

        mock_agent.process_audio.side_effect = side_effect

        # Mock convert_to_temp_mp3 to return a dummy file without needing real ffmpeg on dummy bytes
        from unittest.mock import patch
        with patch("audio.convert_to_temp_mp3") as mock_convert, \
             patch("sheets_service.append_expense_row") as mock_sheets_write:
            mock_convert.side_effect = lambda input_path, **kwargs: Path(input_path).with_suffix(".mp3")

            summary = run_evaluation(
                dataset_path=dataset_csv,
                audio_dir=audio_dir,
                output_path=results_csv,
                delay=0.0,
                mock_agent=mock_agent,
            )

            # Safety check: zero writes to Sheets
            mock_sheets_write.assert_not_called()

            # Check summary numbers
            assert summary["total_samples"] == 3
            assert summary["evaluated_samples"] == 2
            assert summary["skipped_samples"] == 1
            assert summary["intent_accuracy"] == 1.0
            assert summary["tool_accuracy"] == 1.0
            assert summary["wer_strict_avg"] == 0.0
            assert results_csv.exists()
