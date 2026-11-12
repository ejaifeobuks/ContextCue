"""Spoken notices (PiperSpeech): text clean-up, voice download, playback and
stop. No audio hardware or Piper needed: the voice and speaker are stubs."""

import asyncio
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401

from contextcue_app.speech import PiperSpeech, Voice, ensure_voice, speakable  # noqa: E402


class FakeSpeaker:
    def __init__(self, rate, log, block=None):
        self.rate, self.log, self.block = rate, log, block
        self.volume = None

    def start(self):
        self.log.append("start")

    def play(self, pcm):
        if self.block is not None:
            self.block.wait(2)
        self.log.append(len(pcm))

    def stop(self):
        self.log.append("stop")


def voice(samples=1600, rate=16000):
    return Voice(rate, lambda text: [[0] * samples])


class SpeakableTests(unittest.TestCase):
    def test_symbols_read_as_words(self):
        self.assertEqual(speakable("Room temperature is 27.4 °C, above your preferred 20-26 °C."),
                         "Room temperature is 27.4 degrees Celsius, above your preferred 20 to 26 degrees Celsius.")
        self.assertEqual(speakable("Indoors 45% RH; 70% chance of rain."),
                         "Indoors 45 percent humidity; 70 percent chance of rain.")
        self.assertEqual(speakable("Team stand-up at 9:00 AM, review at 10:30 PM."),
                         "Team stand-up at 9 a.m., review at 10:30 p.m.")


class VoiceFileTests(unittest.TestCase):
    def test_downloads_once_and_checks_the_pinned_checksum(self):
        import hashlib
        from unittest import mock

        import contextcue_app.speech as speech
        calls = []

        def download(url, target):
            calls.append(url)
            target.write_bytes(b"corrupt" if "bad" in url else url.encode())

        pins = {name: hashlib.sha256((speech.VOICE_BASE + name).encode()).hexdigest()
                for name in speech.VOICE_SHA256}
        with TemporaryDirectory() as tmp, mock.patch.dict(speech.VOICE_SHA256, pins):
            model = ensure_voice(Path(tmp), download=download)
            ensure_voice(Path(tmp), download=download)
            self.assertTrue(model.exists())
            self.assertEqual(len(calls), 2)  # model and its config, first time only
            self.assertTrue(all(u.startswith("https://huggingface.co/rhasspy/piper-voices/") for u in calls))
            # A corrupt file is deleted and reported, never loaded.
            with mock.patch.object(speech, "VOICE_BASE", "https://example.invalid/bad/"):
                with self.assertRaisesRegex(RuntimeError, "voice_checksum_mismatch"):
                    ensure_voice(Path(tmp) / "other", download=download)
            self.assertFalse(any((Path(tmp) / "other").iterdir()))


class PlaybackTests(unittest.IsolatedAsyncioTestCase):
    def speech(self, log, block=None, load=voice):
        self.mono = [0.0]

        def mono():
            self.mono[0] += 0.05  # time moves on each look
            return self.mono[0]

        return PiperSpeech(Path("/nonexistent"), load=lambda model: load(),
                           speaker=lambda rate: FakeSpeaker(rate, log, block),
                           ensure=lambda d: d / "voice.onnx", monotonic=mono)

    async def test_not_ready_until_the_voice_loads(self):
        log = []
        speech = self.speech(log)
        self.assertEqual((speech.status()["ready"], speech.status()["reason"]), (False, "voice_loading"))
        self.assertEqual(await speech.play("p1", "Hello", 50), "unavailable: voice_loading")
        await speech.prepare()
        self.assertEqual((speech.status()["ready"], speech.status()["reason"]), (True, None))

    async def test_failed_voice_is_reported_not_raised(self):
        def broken():
            raise OSError("offline")
        speech = self.speech([], load=broken)
        await speech.prepare()
        self.assertEqual(speech.status()["reason"], "voice_unavailable")
        self.assertIn("offline", speech.last_error)

    async def test_plays_in_slices_at_the_volume_then_closes(self):
        log = []
        speech = self.speech(log)
        await speech.prepare()
        self.assertEqual(await speech.play("p1", "Hello", 70), "completed")
        self.assertEqual(log, ["start", 1600, "stop"])  # 0.1 s slices at 16 kHz

    async def test_stop_interrupts_playback(self):
        log, gate = [], threading.Event()
        speech = self.speech(log, block=gate, load=lambda: voice(samples=16000))  # 1 s
        await speech.prepare()
        task = asyncio.create_task(speech.play("p1", "Hello", 50))
        await asyncio.sleep(0.05)
        await speech.stop("p1")
        gate.set()
        self.assertEqual(await task, "stopped")
        self.assertLess(len([x for x in log if isinstance(x, int)]), 10)
        self.assertEqual(log[-1], "stop")

    async def test_cancelling_the_task_silences_the_thread(self):
        log, gate = [], threading.Event()
        speech = self.speech(log, block=gate, load=lambda: voice(samples=16000))
        await speech.prepare()
        task = asyncio.create_task(speech.play("p1", "Hello", 50))
        await asyncio.sleep(0.05)
        task.cancel()
        gate.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        for _ in range(40):  # the worker thread finishes on its own
            if log and log[-1] == "stop":
                break
            await asyncio.sleep(0.05)
        self.assertEqual(log[-1], "stop")
        self.assertLess(len([x for x in log if isinstance(x, int)]), 10)


class AppWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_app_prepares_the_voice_and_speaks_an_allowed_notice(self):
        from datetime import datetime, timezone

        from contextcue_app import ContextCueApp
        from contextcue_app.dev_gateway import SimulatedGateway
        from contextcue_app.repository import DataRepository
        from contextcue_app.sources import load_scenarios

        spoken = []

        class Recorder(PiperSpeech):
            def _speak(self, text, volume, stop):
                spoken.append((text, volume))
                return "completed"

        speech = Recorder(Path("/nonexistent"), load=lambda m: voice(), ensure=lambda d: d)
        clock = support.frozen_clock(datetime(2026, 11, 16, 15, 38, tzinfo=timezone.utc))
        repo = DataRepository()
        self.addCleanup(repo.close)
        app = ContextCueApp(gateway=SimulatedGateway(monotonic=clock.monotonic, utcnow=clock.now),
                            clock=clock, repository=repo, gateway_mode="simulated",
                            scenario_id="event_rain", scenarios=load_scenarios(), speech=speech)
        await app.start()
        await app._voice_task
        self.assertTrue(app.view_model()["audio"]["ready"])
        result = await app.action("volume", {"volume": 40})
        self.assertEqual((result["applied"], result["reason"]), (True, None))
        await app.action("settings", {"changes": {"audio_policy": "on_request"}})
        notice = app.view_model()["notice"]
        if notice is not None:  # play the selected notice on request
            await app.action("play", {"notice_id": notice["notice_id"]})
            for _ in range(20):
                await app._tick()
                if spoken:
                    break
                await asyncio.sleep(0.05)
            self.assertTrue(spoken)
            self.assertEqual(spoken[0][1], 40)
        await app.stop()


if __name__ == "__main__":
    unittest.main()
