"""Spoken notices: Piper text to speech, played through Arduino's Speaker.

`PiperSpeech` implements the NotificationService `SpeechPort`. The service
already decides *whether* a notice may be spoken (audio policy, mute, Focus,
quiet hours, once per notice, the Play button); this module only turns text
into sound.

* Voice: a Piper voice (ONNX), runs offline on the UNO Q CPU. Fetched once
  from the official rhasspy/piper-voices release into the app's data/ folder
  and checked against a pinned SHA-256.
* Output: `arduino.app_peripherals.speaker.Speaker("pipewire")`, the desktop
  audio service, so a USB, Bluetooth or built-in speaker all work and the
  speaker chosen in the board's sound settings is the one used.
* Every blocking step (download, model load, synthesis, playback) runs in a
  worker thread; `stop()` interrupts playback within about 0.1 s.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

# Pinned voice: en_US "lessac" medium (22.05 kHz). Measured on the UNO Q with
# the kiosk running: 4.2 s to synthesise 7.4 s of speech, first sentence ready
# in 1.7 s, 13 s to load. "low" was only 1 s faster and sounds duller.
VOICE = "en_US-lessac-medium"
VOICE_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium/"
VOICE_SHA256 = {
    f"{VOICE}.onnx": "5efe09e69902187827af646e1a6e9d269dee769f9877d17b16b1b46eeaaf019f",
    f"{VOICE}.onnx.json": "efe19c417bed055f2d69908248c6ba650fa135bc868b0e6abb3da181dab690a0",
}
DOWNLOAD_TIMEOUT_S = 60
CHUNK_S = 0.1          # playback slice: how quickly Stop takes effect
TAIL_S = 0.25          # let the speaker's buffer finish before closing it


@dataclass(frozen=True)
class Voice:
    """What playback needs from a loaded voice."""
    sample_rate: int
    synthesize: Callable[[str], Iterable]  # text -> int16 arrays, one per sentence, lazily


def speakable(text: str) -> str:
    """Notice text as it should sound. The screen text uses symbols and
    abbreviations that a voice reads badly ("27.4 °C" -> "degrees C")."""
    s = text
    s = re.sub(r"\s*°\s*C\b", " degrees Celsius", s)
    s = re.sub(r"\s*°\s*F\b", " degrees Fahrenheit", s)
    s = re.sub(r"\bdegC\b", "degrees Celsius", s)
    s = re.sub(r"%\s*RH\b", " percent humidity", s)
    s = re.sub(r"\s*%", " percent", s)
    s = re.sub(r"(\d)\s*[-–]\s*(\d)", r"\1 to \2", s)       # "20-26" ranges
    s = re.sub(r"\b(\d{1,2}):00\s*(AM|PM)\b", r"\1 \2", s)   # "9:00 AM" -> "9 AM"
    s = re.sub(r"\bAM\b", "a.m.", s)
    s = re.sub(r"\bPM\b", "p.m.", s)
    s = re.sub(r"\.\.(?!\.)", ".", s)                        # "p.m.." at a sentence end
    return re.sub(r"\s+", " ", s).strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _download(url: str, target: Path) -> None:
    partial = target.with_suffix(target.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "ContextCue"})
    with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_S) as response, partial.open("wb") as f:
        shutil.copyfileobj(response, f)
    partial.replace(target)


def ensure_voice(voice_dir: Path, *, download: Callable[[str, Path], None] = _download) -> Path:
    """The voice model path, downloading the pinned files once if missing."""
    voice_dir.mkdir(parents=True, exist_ok=True)
    for name, expected in VOICE_SHA256.items():
        path = voice_dir / name
        if not path.exists():
            download(VOICE_BASE + name, path)
        if expected is not None and _sha256(path) != expected:
            path.unlink(missing_ok=True)
            raise RuntimeError(f"voice_checksum_mismatch:{name}")
    return voice_dir / f"{VOICE}.onnx"


def load_piper(model: Path) -> Voice:
    from piper import PiperVoice  # imported here: optional dependency

    voice = PiperVoice.load(str(model))

    def synthesize(text: str):
        # A generator: each sentence plays while the next one is synthesised.
        return (chunk.audio_int16_array for chunk in voice.synthesize(text))

    return Voice(voice.config.sample_rate, synthesize)


def pipewire_speaker(sample_rate: int):
    import numpy as np
    from arduino.app_peripherals.speaker import Speaker

    return Speaker("pipewire", sample_rate=sample_rate, channels=1, format=np.int16)


class PiperSpeech:
    engine = "piper"

    def __init__(self, voice_dir: Path, *, load: Callable[[Path], Voice] = load_piper,
                 speaker: Callable[[int], object] = pipewire_speaker,
                 ensure: Callable[[Path], Path] = ensure_voice,
                 monotonic: Callable[[], float] = time.monotonic):
        self.voice_dir = Path(voice_dir)
        self._load, self._speaker, self._ensure, self._mono = load, speaker, ensure, monotonic
        self._voice: Voice | None = None
        self._reason: str | None = "voice_loading"
        self._stops: dict[str, threading.Event] = {}
        self._lock = threading.Lock()  # one speaker user at a time
        self.last_error: str | None = None

    # ---------------------------------------------------------------- setup
    async def prepare(self) -> None:
        """Fetch and load the voice in the background; status() reports it."""
        try:
            model = await asyncio.to_thread(self._ensure, self.voice_dir)
            self._voice = await asyncio.to_thread(self._load, model)
            self._reason = None
        except Exception as exc:  # noqa: BLE001 - reported, never raised into the app
            self._reason = "voice_unavailable"
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]

    # ------------------------------------------------------------ SpeechPort
    def status(self) -> dict:
        return {"engine": f"Piper ({VOICE})", "endpoint": "Board sound output (PipeWire)",
                "paired": False, "connected": self._voice is not None,
                "ready": self._voice is not None, "reason": self._reason}

    async def play(self, playback_id: str, text: str, volume: int) -> str:
        if self._voice is None:
            return f"unavailable: {self._reason or 'voice_loading'}"
        stop = self._stops.setdefault(playback_id, threading.Event())
        try:
            return await asyncio.to_thread(self._speak, speakable(text), volume, stop)
        except asyncio.CancelledError:
            # Cancelling the task does not stop its thread: silence it here.
            stop.set()
            raise
        finally:
            self._stops.pop(playback_id, None)

    async def stop(self, playback_id: str) -> None:
        event = self._stops.get(playback_id)
        if event is not None:
            event.set()

    # -------------------------------------------------------------- worker
    def _speak(self, text: str, volume: int, stop: threading.Event) -> str:
        voice = self._voice
        if voice is None:
            return "unavailable: voice_loading"
        with self._lock:
            if stop.is_set():
                return "stopped"
            try:
                chunks = voice.synthesize(text)
            except Exception as exc:  # noqa: BLE001
                return f"failed: synthesis_error {type(exc).__name__}"
            if stop.is_set():
                return "stopped"
            try:
                speaker = self._speaker(voice.sample_rate)
                speaker.start()
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"[:200]
                return "failed: speaker_unavailable"
            try:
                speaker.volume = max(0, min(100, int(volume)))
                # When the queued sound ends. Writes return once buffered, and
                # a slow sentence leaves a gap, so track it per slice.
                end = self._mono()
                step = max(1, int(voice.sample_rate * CHUNK_S))
                for pcm in chunks:
                    for i in range(0, len(pcm), step):
                        if stop.is_set():
                            return "stopped"
                        piece = pcm[i:i + step]
                        end = max(end, self._mono()) + len(piece) / voice.sample_rate
                        speaker.play(piece)
                end += TAIL_S
                while self._mono() < end:
                    if stop.wait(min(CHUNK_S, max(0.0, end - self._mono()))):
                        return "stopped"
                return "completed"
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"[:200]
                return "failed: playback_error"
            finally:
                try:
                    speaker.stop()
                except Exception:  # noqa: BLE001
                    pass
