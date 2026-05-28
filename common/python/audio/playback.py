"""Playback destination and session abstractions for WAV audio output."""

from __future__ import annotations

import dataclasses
import shutil
import subprocess
from pathlib import Path


@dataclasses.dataclass(frozen=True)
class PlaybackTarget:
    """Immutable record of where synthesized audio should go.

    Set output_file to write to disk; leave it None for live playback via
    paplay or aplay.  The sink field selects a PulseAudio sink and is only
    meaningful when output_file is None and paplay is available.
    """

    output_file: Path | None
    sink: str | None


class PlaybackSession:
    """Resolves the playback binary at construction time and plays WAV files.

    Raises at construction rather than at play() time so that callers discover
    configuration errors before synthesising audio (matching the original eager
    behaviour of _resolve_playback_program in the sonora runtimes).
    """

    def __init__(self, target: PlaybackTarget) -> None:
        self._target = target
        # Binary probing is skipped when writing to a file; the caller is
        # responsible for writing the WAV and handling the file lifecycle.
        if target.output_file is not None:
            self._program: str | None = None
            return

        paplay = shutil.which("paplay")
        aplay = shutil.which("aplay")

        if paplay is None and aplay is None:
            raise RuntimeError("no playback program found (expected paplay or aplay)")

        if target.sink is not None and paplay is None:
            # aplay does not support device selection via --device; paplay does.
            raise ValueError("sink requires paplay; only aplay is available")

        self._program = "paplay" if paplay is not None else "aplay"

    def play(self, wav_path: Path) -> None:
        """Play the WAV file at wav_path through the resolved binary.

        Raises RuntimeError when called in output_file mode (callers should
        check target.output_file before calling play).
        """
        if self._program is None:
            raise RuntimeError("play() called in output_file mode; use target.output_file instead")

        argv = _build_argv(program=self._program, wav_path=wav_path, sink=self._target.sink)
        subprocess.run(argv, check=True)


def _build_argv(*, program: str, wav_path: Path, sink: str | None) -> list[str]:
    if program == "paplay":
        cmd: list[str] = ["paplay"]
        if sink:
            cmd.extend(["--device", sink])
        cmd.append(str(wav_path))
        return cmd
    # aplay does not support sink selection; the sink check happens in __init__.
    return ["aplay", str(wav_path)]
