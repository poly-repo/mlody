"""Tests for PlaybackTarget and PlaybackSession — spec: playback-target/spec.md."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import call, patch

import pytest

from common.python.audio.playback import PlaybackSession, PlaybackTarget


# ---------------------------------------------------------------------------
# PlaybackTarget scenarios
# ---------------------------------------------------------------------------


def test_playback_target_output_file_mode() -> None:
    """PlaybackTarget constructed with output_file stores the fields correctly."""
    target = PlaybackTarget(output_file=Path("out.wav"), sink=None)
    assert target.output_file == Path("out.wav")
    assert target.sink is None


def test_playback_target_live_mode() -> None:
    """PlaybackTarget constructed for live playback stores output_file as None."""
    target = PlaybackTarget(output_file=None, sink=None)
    assert target.output_file is None


def test_playback_target_is_immutable() -> None:
    """PlaybackTarget raises FrozenInstanceError on field assignment."""
    target = PlaybackTarget(output_file=None, sink=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        target.output_file = Path("other.wav")  # type: ignore[misc]


# ---------------------------------------------------------------------------
# PlaybackSession construction scenarios
# ---------------------------------------------------------------------------


def test_playback_session_output_file_skips_binary_probe() -> None:
    """output_file mode: shutil.which is never called — spec scenario 1."""
    target = PlaybackTarget(output_file=Path("out.wav"), sink=None)
    with patch("shutil.which") as mock_which:
        PlaybackSession(target)
    mock_which.assert_not_called()


def test_playback_session_no_binary_raises_runtime_error() -> None:
    """Live mode with no binary found raises RuntimeError — spec scenario 2."""
    target = PlaybackTarget(output_file=None, sink=None)
    with patch("common.python.audio.playback.shutil.which", return_value=None):
        with pytest.raises(RuntimeError, match="no playback program found"):
            PlaybackSession(target)


def test_playback_session_sink_aplay_only_raises_value_error() -> None:
    """Sink with aplay-only raises ValueError — spec scenario 3."""
    target = PlaybackTarget(output_file=None, sink="alsa_out")

    def which_side_effect(prog: str) -> str | None:
        return None if prog == "paplay" else "/usr/bin/aplay"

    with patch("common.python.audio.playback.shutil.which", side_effect=which_side_effect):
        with pytest.raises(ValueError, match="sink requires paplay"):
            PlaybackSession(target)


def test_playback_session_sink_with_paplay_does_not_raise() -> None:
    """Sink with paplay available does not raise — spec scenario 4."""
    target = PlaybackTarget(output_file=None, sink="alsa_out")
    with patch("common.python.audio.playback.shutil.which", return_value="/usr/bin/paplay"):
        session = PlaybackSession(target)
    assert session is not None


# ---------------------------------------------------------------------------
# PlaybackSession.play() argv scenarios
# ---------------------------------------------------------------------------


def test_play_paplay_without_sink() -> None:
    """play() with paplay and no sink calls subprocess.run with correct argv."""
    target = PlaybackTarget(output_file=None, sink=None)
    with patch("common.python.audio.playback.shutil.which", return_value="/usr/bin/paplay"):
        session = PlaybackSession(target)

    with patch("common.python.audio.playback.subprocess.run") as mock_run:
        session.play(Path("/tmp/audio.wav"))

    mock_run.assert_called_once_with(["paplay", "/tmp/audio.wav"], check=True)


def test_play_paplay_with_sink() -> None:
    """play() with paplay and sink passes --device <sink> before the file path."""
    target = PlaybackTarget(output_file=None, sink="my_sink")
    with patch("common.python.audio.playback.shutil.which", return_value="/usr/bin/paplay"):
        session = PlaybackSession(target)

    with patch("common.python.audio.playback.subprocess.run") as mock_run:
        session.play(Path("/tmp/audio.wav"))

    mock_run.assert_called_once_with(["paplay", "--device", "my_sink", "/tmp/audio.wav"], check=True)


def test_play_aplay() -> None:
    """play() with aplay calls subprocess.run with correct argv — spec scenario 5."""
    target = PlaybackTarget(output_file=None, sink=None)

    def which_side_effect(prog: str) -> str | None:
        return None if prog == "paplay" else "/usr/bin/aplay"

    with patch("common.python.audio.playback.shutil.which", side_effect=which_side_effect):
        session = PlaybackSession(target)

    with patch("common.python.audio.playback.subprocess.run") as mock_run:
        session.play(Path("/tmp/audio.wav"))

    mock_run.assert_called_once_with(["aplay", "/tmp/audio.wav"], check=True)
