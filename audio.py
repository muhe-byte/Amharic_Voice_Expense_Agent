"""
Audio Processing Module for Amharic Voice Expense Agent.

Handles downloading Telegram voice notes (.oga/.ogg) and mobile audio
recordings (.aac, .amr, .m4a), converting them to 128k mono MP3 format
via FFmpeg using either system FFmpeg or bundled imageio-ffmpeg.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Optional, Union

from error_handler import AudioConversionError

logger = logging.getLogger(__name__)

# Supported audio formats for ingestion & evaluation
SUPPORTED_AUDIO_EXTENSIONS = {
    ".aac",
    ".amr",
    ".m4a",
    ".oga",
    ".ogg",
    ".wav",
    ".mp3",
    ".flac",
    ".mp4",
    ".3gp",
}


def get_ffmpeg_path() -> str:
    """
    Locates the FFmpeg executable from system PATH or bundled imageio-ffmpeg.
    """
    # 1. Check system PATH
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg

    # 2. Check bundled imageio_ffmpeg
    try:
        import imageio_ffmpeg
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        if os.path.exists(ffmpeg_exe):
            return ffmpeg_exe
    except ImportError:
        pass

    raise AudioConversionError(
        "FFmpeg executable not found. Please install FFmpeg on system PATH or install imageio-ffmpeg."
    )


def convert_to_mp3(
    input_path: Union[str, Path],
    output_path: Optional[Union[str, Path]] = None,
    bitrate: str = "128k",
    sample_rate: int = 44100,
) -> Path:
    """
    Converts an audio file (.aac, .amr, .m4a, .oga, .ogg, .wav, etc.) to 128k mono MP3 using FFmpeg.

    Args:
        input_path: Path to input audio file.
        output_path: Path for converted .mp3. If None, created alongside input with .mp3 extension.
        bitrate: Audio bitrate, default '128k'.
        sample_rate: Audio sample rate, default 44100 Hz.

    Returns:
        Path to the converted MP3 file.

    Raises:
        AudioConversionError: If FFmpeg fails or output file is empty.
    """
    in_p = Path(input_path).resolve()
    if not in_p.exists():
        raise AudioConversionError(f"Input audio file does not exist: {in_p}")

    if in_p.stat().st_size == 0:
        raise AudioConversionError(f"Input audio file is empty (0 bytes): {in_p}")

    if output_path is None:
        out_p = in_p.with_suffix(".mp3")
    else:
        out_p = Path(output_path).resolve()

    ffmpeg_bin = get_ffmpeg_path()
    cmd = [
        ffmpeg_bin,
        "-y",               # Overwrite output without asking
        "-i", str(in_p),    # Input file
        "-vn",              # Disable video
        "-ar", str(sample_rate),
        "-ac", "1",         # Mono audio is ideal for speech transcription
        "-b:a", bitrate,
        str(out_p),
    ]

    logger.info("Converting audio via FFmpeg: %s -> %s", in_p.name, out_p.name)

    try:
        process = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,  # 30-second timeout
        )
    except subprocess.TimeoutExpired as err:
        logger.error("FFmpeg conversion timed out after 30s: %s", err)
        raise AudioConversionError("FFmpeg audio conversion timed out.") from err
    except Exception as err:
        logger.error("FFmpeg execution error: %s", err)
        raise AudioConversionError(f"FFmpeg execution failed: {err}") from err

    if process.returncode != 0:
        logger.error("FFmpeg error (code %d): %s", process.returncode, process.stderr)
        raise AudioConversionError(f"FFmpeg conversion failed: {process.stderr[-200:]}")

    if not out_p.exists() or out_p.stat().st_size == 0:
        raise AudioConversionError("FFmpeg produced an empty or missing MP3 file.")

    logger.info("Audio successfully converted to MP3: %s (%d bytes)", out_p.name, out_p.stat().st_size)
    return out_p


def convert_to_temp_mp3(
    input_path: Union[str, Path],
    temp_dir: Optional[Union[str, Path]] = None,
    bitrate: str = "128k",
    sample_rate: int = 44100,
) -> Path:
    """
    Automatically converts any supported audio file (.aac, .amr, .m4a, .oga, .ogg, .wav, etc.)
    into a temporary 128k mono .mp3 file for Gemini processing.

    Args:
        input_path: Path to the source audio file.
        temp_dir: Optional custom temporary directory. If None, uses default OS temp directory.
        bitrate: Audio bitrate (default '128k').
        sample_rate: Audio sampling rate (default 44100 Hz).

    Returns:
        Path to the generated temporary MP3 file. Caller is responsible for deleting it
        or using cleanup_files().
    """
    in_p = Path(input_path).resolve()
    if not in_p.exists():
        raise AudioConversionError(f"Audio file does not exist: {in_p}")

    ext = in_p.suffix.lower()
    if ext not in SUPPORTED_AUDIO_EXTENSIONS:
        logger.warning(
            "Audio extension '%s' not in known supported set %s; FFmpeg will attempt auto-detection.",
            ext,
            SUPPORTED_AUDIO_EXTENSIONS,
        )

    # Create temporary MP3 file
    if temp_dir is not None:
        td = Path(temp_dir)
        td.mkdir(parents=True, exist_ok=True)
        temp_file = tempfile.NamedTemporaryFile(
            prefix=f"eval_{in_p.stem}_", suffix=".mp3", dir=str(td), delete=False
        )
    else:
        temp_file = tempfile.NamedTemporaryFile(
            prefix=f"eval_{in_p.stem}_", suffix=".mp3", delete=False
        )

    temp_mp3_path = Path(temp_file.name)
    temp_file.close()

    try:
        return convert_to_mp3(
            input_path=in_p,
            output_path=temp_mp3_path,
            bitrate=bitrate,
            sample_rate=sample_rate,
        )
    except Exception:
        if temp_mp3_path.exists():
            try:
                temp_mp3_path.unlink()
            except Exception:
                pass
        raise


def cleanup_files(*paths: Optional[Union[str, Path]]) -> None:
    """Safely removes temporary files without raising errors."""
    for p in paths:
        if p is None:
            continue
        try:
            path_obj = Path(p)
            if path_obj.exists():
                path_obj.unlink()
        except Exception as err:
            logger.warning("Could not delete temporary file %s: %s", p, err)
