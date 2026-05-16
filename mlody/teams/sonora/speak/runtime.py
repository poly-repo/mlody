"""Runtime helpers for the Sonora Kokoro speech tool."""

from __future__ import annotations

import io
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import numpy as np
import numpy.typing as npt

from common.python.audio.playback import PlaybackSession, PlaybackTarget

DEFAULT_MODEL_DIR = Path(
    "/home/mav/.cache/mlody/artifacts/huggingface/hexgrad/Kokoro-82M/"
    "f3ff3571791e39611d31c381e3a41a3af07b4987"
)
DEFAULT_SAMPLE_RATE = 24_000
DEFAULT_BLANK_SILENCE_MS = 500


@dataclass(frozen=True)
class SpeakConfig:
    """Runtime configuration for one invocation."""

    model_dir: Path
    voice: str
    lang_code: str
    speed: float
    output_file: Path | None
    sink: str | None
    sample_rate: int = DEFAULT_SAMPLE_RATE
    blank_silence_ms: int = DEFAULT_BLANK_SILENCE_MS


class KokoroSpeaker:
    """Synthesizes speech and handles playback/file output."""

    def __init__(self, *, config: SpeakConfig) -> None:
        self._config = config
        self._config_path = config.model_dir / "config.json"
        self._weights_path = config.model_dir / "kokoro-v1_0.pth"
        self._voice_path = config.model_dir / "voices" / f"{config.voice}.pt"
        _validate_model_assets(
            config_path=self._config_path,
            weights_path=self._weights_path,
            voice_path=self._voice_path,
        )
        self._pipeline = _load_pipeline(
            lang_code=config.lang_code,
            config_path=self._config_path,
            weights_path=self._weights_path,
        )
        target = PlaybackTarget(output_file=config.output_file, sink=config.sink)
        self._playback_session = PlaybackSession(target)

    def synthesize_text(
        self,
        text: str,
        *,
        blank_silence_ms: int = 0,
    ) -> npt.NDArray[np.float32]:
        """Return synthesized audio for one text segment."""
        stripped = text.strip()
        if not stripped:
            return _silence_audio(
                sample_rate=self._config.sample_rate,
                silence_ms=blank_silence_ms,
            )

        chunks: list[npt.NDArray[np.float32]] = []
        for result in self._pipeline(
            stripped,
            voice=str(self._voice_path),
            speed=self._config.speed,
            split_pattern=r"\n+",
        ):
            audio_chunk = _extract_audio_chunk(result)
            if audio_chunk.size > 0:
                chunks.append(audio_chunk)
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(chunks, axis=0).astype(np.float32, copy=False)

    def write_wav(self, *, path: Path, audio: npt.NDArray[np.float32]) -> None:
        """Write one mono PCM16 WAV file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_pcm16_wav(path=path, audio=audio, sample_rate=self._config.sample_rate)

    def play_audio(self, *, audio: npt.NDArray[np.float32]) -> None:
        """Play audio through paplay/aplay."""
        if audio.size == 0:
            return

        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(prefix="sonora_", suffix=".wav", delete=False) as tmp:
                temp_path = Path(tmp.name)
            self.write_wav(path=temp_path, audio=audio)
            self._playback_session.play(temp_path)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()


def run_once(*, config: SpeakConfig, text: str) -> None:
    """Run one-shot synthesis mode."""
    speaker = KokoroSpeaker(config=config)
    audio = speaker.synthesize_text(text)
    if config.output_file is not None:
        speaker.write_wav(path=config.output_file, audio=audio)
        return
    speaker.play_audio(audio=audio)


def run_stdin(*, config: SpeakConfig, stream: TextIO) -> None:
    """Run line-by-line stdin mode until EOF."""
    speaker = KokoroSpeaker(config=config)
    if config.output_file is not None:
        merged: list[npt.NDArray[np.float32]] = []
        for line in stream:
            merged.append(
                speaker.synthesize_text(
                    line.rstrip("\n"),
                    blank_silence_ms=config.blank_silence_ms,
                )
            )
        if merged:
            full_audio = np.concatenate(merged, axis=0).astype(np.float32, copy=False)
        else:
            full_audio = np.zeros(0, dtype=np.float32)
        speaker.write_wav(path=config.output_file, audio=full_audio)
        return

    for line in stream:
        audio = speaker.synthesize_text(
            line.rstrip("\n"),
            blank_silence_ms=config.blank_silence_ms,
        )
        speaker.play_audio(audio=audio)


def _validate_model_assets(*, config_path: Path, weights_path: Path, voice_path: Path) -> None:
    for path, label in (
        (config_path, "config"),
        (weights_path, "weights"),
        (voice_path, "voice"),
    ):
        if not path.exists():
            raise ValueError(f"missing Kokoro {label} file: {path}")


def _load_pipeline(*, lang_code: str, config_path: Path, weights_path: Path) -> Any:
    from kokoro import KPipeline
    from kokoro.model import KModel

    model = KModel(
        config=str(config_path),
        model=str(weights_path),
    )
    return KPipeline(
        lang_code=lang_code,
        model=model,
    )


def _extract_audio_chunk(result: Any) -> npt.NDArray[np.float32]:
    if hasattr(result, "audio"):
        audio_value = result.audio
    elif isinstance(result, tuple) and len(result) >= 3:
        audio_value = result[2]
    else:
        audio_value = None
    if audio_value is None:
        return np.zeros(0, dtype=np.float32)

    if hasattr(audio_value, "detach"):
        tensor = audio_value.detach()
        if hasattr(tensor, "cpu"):
            tensor = tensor.cpu()
        if hasattr(tensor, "numpy"):
            array = tensor.numpy()
        else:
            array = np.asarray(tensor, dtype=np.float32)
    else:
        array = np.asarray(audio_value, dtype=np.float32)
    return np.asarray(array, dtype=np.float32).reshape(-1)


def _silence_audio(*, sample_rate: int, silence_ms: int) -> npt.NDArray[np.float32]:
    if silence_ms <= 0:
        return np.zeros(0, dtype=np.float32)
    samples = int(sample_rate * (silence_ms / 1000.0))
    return np.zeros(samples, dtype=np.float32)


def _write_pcm16_wav(
    *,
    path: Path,
    audio: npt.NDArray[np.float32],
    sample_rate: int,
) -> None:
    clipped = np.clip(audio, -1.0, 1.0)
    pcm16 = (clipped * 32767.0).astype(np.int16, copy=False)
    payload = io.BytesIO()
    with wave.open(payload, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm16.tobytes())
    path.write_bytes(payload.getvalue())
