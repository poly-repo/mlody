"""Tests for Sonora speak runtime behavior."""

from __future__ import annotations

import io
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import ANY, patch

import numpy as np

import mlody.teams.sonora.speak.runtime as speak_runtime
from mlody.teams.sonora.speak.runtime import (
    DEFAULT_SAMPLE_RATE,
    SpeakConfig,
    _load_pipeline,
    _silence_audio,
    _validate_model_assets,
    run_stdin,
)


@dataclass
class _FakeSpeaker:
    config: SpeakConfig

    def __post_init__(self) -> None:
        self.calls: list[str] = []
        self.writes: list[np.ndarray] = []
        self.played: list[np.ndarray] = []

    def synthesize_text(self, text: str, *, blank_silence_ms: int = 0) -> np.ndarray:
        self.calls.append(text)
        if text.strip():
            return np.asarray([0.1, 0.2], dtype=np.float32)
        return np.zeros(int(DEFAULT_SAMPLE_RATE * (blank_silence_ms / 1000.0)), dtype=np.float32)

    def write_wav(self, *, path: Path, audio: np.ndarray) -> None:
        _ = path
        self.writes.append(audio)

    def play_audio(self, *, audio: np.ndarray) -> None:
        self.played.append(audio)


def _config(output_file: Path | None = None) -> SpeakConfig:
    return SpeakConfig(
        model_dir=Path("/tmp/model"),
        voice="af_heart",
        lang_code="a",
        speed=1.0,
        output_file=output_file,
        sink=None,
    )


def test_resolve_playback_program_not_in_module() -> None:
    # _resolve_playback_program was removed; playback logic lives in PlaybackSession.
    assert not hasattr(speak_runtime, "_resolve_playback_program")


def test_build_playback_command_not_in_module() -> None:
    # _build_playback_command was removed; it lives in common.python.audio.playback.
    assert not hasattr(speak_runtime, "_build_playback_command")


def test_run_stdin_writes_one_concatenated_file(tmp_path: Path) -> None:
    out_path = tmp_path / "out.wav"
    cfg = _config(output_file=out_path)
    speaker = _FakeSpeaker(config=cfg)

    with patch("mlody.teams.sonora.speak.runtime.KokoroSpeaker", return_value=speaker):
        run_stdin(config=cfg, stream=io.StringIO("hello\n\nworld\n"))

    assert len(speaker.writes) == 1
    merged = speaker.writes[0]
    # 2 voiced segments x 2 samples + one 500ms silence segment.
    assert merged.size == 4 + int(DEFAULT_SAMPLE_RATE * 0.5)


def test_run_stdin_plays_each_line_when_no_output_file() -> None:
    cfg = _config(output_file=None)
    speaker = _FakeSpeaker(config=cfg)

    with patch("mlody.teams.sonora.speak.runtime.KokoroSpeaker", return_value=speaker):
        run_stdin(config=cfg, stream=io.StringIO("a\nb\n"))

    assert speaker.calls == ["a", "b"]
    assert len(speaker.played) == 2


def test_validate_model_assets_raises_for_missing_voice(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    weights_path = tmp_path / "kokoro-v1_0.pth"
    voice_path = tmp_path / "voices" / "af_heart.pt"
    config_path.write_text("{}")
    weights_path.write_text("x")

    try:
        _validate_model_assets(
            config_path=config_path,
            weights_path=weights_path,
            voice_path=voice_path,
        )
    except ValueError as exc:
        assert "voice" in str(exc)
    else:
        raise AssertionError("expected missing voice error")


def test_validate_model_assets_raises_for_missing_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    weights_path = tmp_path / "kokoro-v1_0.pth"
    voice_path = tmp_path / "voices" / "af_heart.pt"
    weights_path.write_text("x")
    voice_path.parent.mkdir(parents=True, exist_ok=True)
    voice_path.write_text("x")

    try:
        _validate_model_assets(
            config_path=config_path,
            weights_path=weights_path,
            voice_path=voice_path,
        )
    except ValueError as exc:
        assert "config" in str(exc)
    else:
        raise AssertionError("expected missing config error")


def test_validate_model_assets_raises_for_missing_weights(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    weights_path = tmp_path / "kokoro-v1_0.pth"
    voice_path = tmp_path / "voices" / "af_heart.pt"
    config_path.write_text("{}")
    voice_path.parent.mkdir(parents=True, exist_ok=True)
    voice_path.write_text("x")

    try:
        _validate_model_assets(
            config_path=config_path,
            weights_path=weights_path,
            voice_path=voice_path,
        )
    except ValueError as exc:
        assert "weights" in str(exc)
    else:
        raise AssertionError("expected missing weights error")


def test_blank_silence_is_500ms_default() -> None:
    audio = _silence_audio(sample_rate=DEFAULT_SAMPLE_RATE, silence_ms=500)
    assert audio.size == 12_000


def test_load_pipeline_uses_local_assets_only() -> None:
    calls: dict[str, dict[str, object]] = {}

    class FakeKModel:
        def __init__(self, **kwargs: object) -> None:
            calls["model"] = kwargs

    class FakeKPipeline:
        def __init__(self, **kwargs: object) -> None:
            calls["pipeline"] = kwargs

    fake_kokoro = types.ModuleType("kokoro")
    fake_kokoro_model = types.ModuleType("kokoro.model")
    fake_kokoro.KPipeline = FakeKPipeline
    fake_kokoro_model.KModel = FakeKModel

    with patch.dict(
        sys.modules,
        {
            "kokoro": fake_kokoro,
            "kokoro.model": fake_kokoro_model,
        },
    ):
        _load_pipeline(
            lang_code="a",
            config_path=Path("/tmp/config.json"),
            weights_path=Path("/tmp/kokoro-v1_0.pth"),
        )

    assert calls["model"] == {
        "config": "/tmp/config.json",
        "model": "/tmp/kokoro-v1_0.pth",
    }
    assert calls["pipeline"] == {"lang_code": "a", "model": ANY}
