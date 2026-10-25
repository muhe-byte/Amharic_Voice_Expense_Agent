"""
Unit tests for audio module.
Covers FFmpeg path detection and error handling for missing/empty files.
"""

from pathlib import Path
import pytest

import audio
from error_handler import AudioConversionError


class TestAudioModule:
    """Tests for audio file conversion and validation."""

    def test_get_ffmpeg_path_returns_valid_binary(self):
        ffmpeg_exe = audio.get_ffmpeg_path()
        assert ffmpeg_exe is not None
        assert Path(ffmpeg_exe).exists()

    def test_convert_non_existent_file_raises_error(self, tmp_path):
        dummy_file = tmp_path / "does_not_exist.oga"
        with pytest.raises(AudioConversionError, match="does not exist"):
            audio.convert_to_mp3(dummy_file)

    def test_convert_empty_file_raises_error(self, tmp_path):
        empty_file = tmp_path / "empty.oga"
        empty_file.write_bytes(b"")
        with pytest.raises(AudioConversionError, match="is empty"):
            audio.convert_to_mp3(empty_file)

    def test_cleanup_files_ignores_non_existent(self, tmp_path):
        non_existent = tmp_path / "phantom.mp3"
        # Should not raise exception
        audio.cleanup_files(non_existent, None)

    def test_supported_audio_extensions(self):
        assert ".aac" in audio.SUPPORTED_AUDIO_EXTENSIONS
        assert ".amr" in audio.SUPPORTED_AUDIO_EXTENSIONS
        assert ".m4a" in audio.SUPPORTED_AUDIO_EXTENSIONS

    def test_convert_to_temp_mp3_non_existent(self, tmp_path):
        missing = tmp_path / "missing.aac"
        with pytest.raises(AudioConversionError, match="does not exist"):
            audio.convert_to_temp_mp3(missing)
