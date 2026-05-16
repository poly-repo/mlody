"""Runtime helpers for the Sonora Chatterbox Turbo speech tool."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from common.python.audio.playback import PlaybackSession, PlaybackTarget

DEFAULT_DEVICE = "cuda"


@dataclass(frozen=True)
class ChatterboxConfig:
    """Runtime configuration for one invocation."""

    device: str
    audio_prompt_path: Path | None
    output_file: Path | None
    sink: str | None


class ChatterboxSpeaker:
    """Synthesizes speech and handles playback/file output."""

    def __init__(self, *, config: ChatterboxConfig) -> None:
        self._config = config
        self._model = _load_model(device=config.device)
        self._sample_rate: int = self._model.sr
        target = PlaybackTarget(output_file=config.output_file, sink=config.sink)
        self._playback_session = PlaybackSession(target)

    def synthesize_text(self, text: str) -> Any:
        """Return synthesized audio tensor for one text segment."""
        stripped = text.strip()
        if not stripped:
            return None
        audio_prompt = (
            str(self._config.audio_prompt_path)
            if self._config.audio_prompt_path is not None
            else None
        )
        return self._model.generate(stripped, audio_prompt_path=audio_prompt)

    def write_wav(self, *, path: Path, audio: Any) -> None:
        """Write a WAV file from a torchaudio-compatible tensor."""
        import torchaudio

        path.parent.mkdir(parents=True, exist_ok=True)
        torchaudio.save(str(path), audio, self._sample_rate)

    def play_audio(self, *, audio: Any) -> None:
        """Play audio through paplay/aplay."""
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(prefix="sonora_cb_", suffix=".wav", delete=False) as tmp:
                temp_path = Path(tmp.name)
            self.write_wav(path=temp_path, audio=audio)
            self._playback_session.play(temp_path)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()


def run_once(*, config: ChatterboxConfig, text: str) -> None:
    """Run one-shot synthesis mode."""
    speaker = ChatterboxSpeaker(config=config)
    audio = speaker.synthesize_text(text)
    if audio is None:
        return
    if config.output_file is not None:
        speaker.write_wav(path=config.output_file, audio=audio)
        return
    speaker.play_audio(audio=audio)


def run_stdin(*, config: ChatterboxConfig, stream: TextIO) -> None:
    """Run line-by-line stdin mode until EOF."""
    import torch

    speaker = ChatterboxSpeaker(config=config)
    if config.output_file is not None:
        chunks: list[Any] = []
        for line in stream:
            audio = speaker.synthesize_text(line.rstrip("\n"))
            if audio is not None:
                chunks.append(audio)
        if chunks:
            full_audio = torch.cat(chunks, dim=-1)
            speaker.write_wav(path=config.output_file, audio=full_audio)
        return

    for line in stream:
        audio = speaker.synthesize_text(line.rstrip("\n"))
        if audio is not None:
            speaker.play_audio(audio=audio)


def _patch_ml_dtypes() -> None:
    """onnx>=1.16 accesses ml_dtypes.float4_e2m1fn at import time; installs
    older than ml_dtypes 0.5.0 lack it. Inject a stand-in so onnx loads.
    float4 tensors are never used in English TTS."""
    try:
        import ml_dtypes  # noqa: PLC0415

        if not hasattr(ml_dtypes, "float4_e2m1fn"):
            import numpy as np  # noqa: PLC0415

            ml_dtypes.float4_e2m1fn = np.dtype("float16")  # type: ignore[attr-defined]
    except ImportError:
        pass


def _patch_librosa_resample_dtype() -> None:
    """Chatterbox expects float32 conditioning audio; newer librosa resample
    paths can return float64 arrays."""
    try:
        import librosa  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415
    except ImportError:
        return

    original_resample = getattr(librosa, "resample", None)
    if original_resample is None or getattr(original_resample, "_sonora_fp32_patch", False):
        return

    def _resample_fp32(*args: Any, **kwargs: Any) -> Any:
        result = original_resample(*args, **kwargs)
        if isinstance(result, np.ndarray) and result.dtype != np.float32:
            return result.astype(np.float32, copy=False)
        return result

    setattr(_resample_fp32, "_sonora_fp32_patch", True)
    librosa.resample = _resample_fp32


def _load_model(device: str) -> Any:
    _patch_ml_dtypes()
    _patch_librosa_resample_dtype()
    import perth

    if getattr(perth, "PerthImplicitWatermarker", None) is None:
        class _DummyWatermarker:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            def apply_watermark(self, wav: Any, sample_rate: int) -> Any:
                return wav

            def get_watermark(self, wav: Any, sample_rate: int) -> None:
                return None

        perth.PerthImplicitWatermarker = _DummyWatermarker  # type: ignore[attr-defined]

    from chatterbox.tts_turbo import ChatterboxTurboTTS

    return ChatterboxTurboTTS.from_pretrained(device=device)
