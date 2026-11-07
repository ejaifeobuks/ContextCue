"""APP-12 to APP-17: notice identity, priority, Focus, mute, quiet hours,
acknowledgement, late callbacks, audio retry and persistence."""

import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402

from contextcue_app.contracts import Lifecycle  # noqa: E402
from contextcue_app.notifications import NotificationService  # noqa: E402
from contextcue_app.repository import DataRepository  # noqa: E402
from contextcue_app.settings import Settings  # noqa: E402

# 09:38 Chicago, outside quiet hours.
START = datetime(2026, 11, 16, 15, 38, tzinfo=timezone.utc)


class FakeOutputs:
    def __init__(self):
        self.commands = []

    async def execute(self, command):
        from contextcue_contracts import CommandResult
        self.commands.append(command)
        return CommandResult("1.0", command.command_id, "accepted", None, "2026-11-16T15:38:00.000Z")


class Base(unittest.IsolatedAsyncioTestCase):
    def make(self, settings=None, repo=None, speech=None, start=START):
        self.clock = support.frozen_clock(start)
        self.settings = settings or Settings()
        self.repo = repo or DataRepository()
        self.addCleanup(self.repo.close)
        self.speech = speech or support.FakeSpeech()
        self.ns = NotificationService(now=self.clock.now, monotonic=self.clock.monotonic,
                                      settings=lambda: self.settings, repository=self.repo,
                                      speech=self.speech)
        return self.ns

    def candidate(self, ns=None, **kw):
        ns = ns or self.ns
        args = dict(category="comfort", dedupe_key="comfort:hot:1", priority=0,
                    evidence={"derived.comfort.hot": 1}, required=["derived.comfort.hot"],
                    source_mode="simulated")
        args.update(kw)
        return ns.create_candidate(**args)

    def activate(self, notice, text="Room is warm."):
        self.ns.apply_text(notice.notice_id, text=text, origin="factual_fallback",
                           request_outcome="error", validation="not_applicable",
                           evidence=None, generation={})


class IdentityAndPriority(Base):
    async def test_priority_then_expiry_then_creation_then_id(self):
        ns = self.make()
        low = self.candidate(dedupe_key="comfort:hot:1")
        prep = self.candidate(category="preparation", dedupe_key="prep:a", priority=1,
                              latest_expiry=START + timedelta(minutes=20))
        urgent = self.candidate(category="preparation", dedupe_key="prep:b", priority=2,
                                latest_expiry=START + timedelta(minutes=9))
        for n in (low, prep, urgent):
            self.activate(n)
        self.assertEqual(ns.select_priority_notice().notice_id, urgent.notice_id)
        ns.acknowledge(urgent.notice_id)
        self.assertEqual(ns.select_priority_notice().notice_id, prep.notice_id)
        early = self.candidate(category="preparation", dedupe_key="prep:c", priority=1,
                               latest_expiry=START + timedelta(minutes=5))
        self.activate(early)
        self.assertEqual(ns.select_priority_notice().notice_id, early.notice_id)

    async def test_dedupe_and_tombstone(self):
        ns = self.make()
        first = self.candidate()
        self.assertIsNone(self.candidate())  # same key while live
        self.activate(first)
        ns.acknowledge(first.notice_id)
        self.assertIsNone(self.candidate())  # tombstoned
        # Tombstones survive a restart (same database).
        ns2 = NotificationService(now=self.clock.now, monotonic=self.clock.monotonic,
                                  settings=lambda: self.settings, repository=self.repo)
        self.assertIsNone(self.candidate(ns2))

    async def test_acknowledge_clears_only_its_notice(self):
        ns = self.make()
        a, b = self.candidate(), self.candidate(dedupe_key="comfort:dry:2")
        self.activate(a)
        self.activate(b)
        ns.acknowledge(a.notice_id)
        self.assertEqual(a.lifecycle, Lifecycle.ACKNOWLEDGED)
        self.assertEqual(b.lifecycle, Lifecycle.ACTIVE)
        self.assertIsNone(ns.acknowledge(a.notice_id))

    async def test_late_generation_cannot_revive(self):
        ns = self.make()
        notice = self.candidate()
        ns.acknowledge(notice.notice_id)
        self.assertFalse(ns.apply_text(notice.notice_id, text="late", origin="generated",
                                       request_outcome="generated", validation="passed",
                                       evidence=None, generation={}))
        self.assertEqual(notice.lifecycle, Lifecycle.ACKNOWLEDGED)
        self.assertEqual(notice.text, "")

    async def test_no_valid_facts_expires_instead_of_inventing(self):
        ns = self.make()
        notice = self.candidate()
        ns.apply_text(notice.notice_id, text=None, origin="status", request_outcome="error",
                      validation="not_applicable", evidence=None, generation={})
        self.assertEqual(notice.lifecycle, Lifecycle.EXPIRED)
        self.assertEqual(notice.ended_reason, "no_valid_facts")

    async def test_expiry_and_evidence_invalidation(self):
        ns = self.make()
        a = self.candidate()
        b = self.candidate(dedupe_key="comfort:dry:2")
        self.activate(a)
        self.activate(b)
        ns.cancel_expired_outputs(lambda n: "derived: cleared" if n is a else None)
        self.assertEqual(a.lifecycle, Lifecycle.EXPIRED)
        self.assertEqual(b.lifecycle, Lifecycle.ACTIVE)
        self.clock.advance(5 * 60)
        ns.cancel_expired_outputs(lambda n: None)
        self.assertEqual(b.ended_reason, "expired")

    async def test_preparation_expires_at_event_start(self):
        self.make()
        n = self.candidate(category="preparation", dedupe_key="prep:x", priority=1,
                           latest_expiry=START + timedelta(minutes=3))
        self.assertEqual(n.expires_at, "2026-11-16T15:41:00.000Z")
        self.assertIsNone(self.candidate(category="preparation", dedupe_key="prep:y", priority=1,
                                         latest_expiry=START - timedelta(minutes=1)))


class FocusMuteQuiet(Base):
    async def test_focus_defers_unsolicited_morning_and_releases(self):
        ns = self.make()
        ns.set_focus(True)
        morning = self.candidate(category="morning", dedupe_key="morning:2026-11-16")
        self.assertEqual(morning.lifecycle, Lifecycle.DEFERRED)
        explicit = self.candidate(category="morning", dedupe_key="briefing:1", explicit=True, priority=2)
        self.assertEqual(explicit.lifecycle, Lifecycle.GENERATING)
        comfort = self.candidate()
        self.assertEqual(comfort.lifecycle, Lifecycle.GENERATING)  # quiet visuals kept
        released = ns.set_focus(False) or ns.recheck_deferred()
        self.assertEqual(morning.lifecycle, Lifecycle.GENERATING)

    async def test_mute_stops_audio_keeps_visuals_and_never_replays(self):
        speech = support.FakeSpeech()
        speech.block = asyncio.Event()
        ns = self.make(settings=Settings(audio_policy="automatic"), speech=speech)
        notice = self.candidate()
        self.activate(notice)
        self.assertEqual(notice.routes["speech"].status, "permitted")
        await ns.dispatch(None, fault=False, pixel_level=0.2, outputs_ready=False)
        await asyncio.sleep(0)
        self.assertEqual(len(speech.played), 1)
        ns.set_mute(True)
        await ns.dispatch(None, fault=False, pixel_level=0.2, outputs_ready=False)
        self.assertEqual(len(speech.stopped), 1)
        self.assertEqual(notice.lifecycle, Lifecycle.ACTIVE)
        self.assertEqual(notice.routes["speech"].status, "stopped")
        ns.set_mute(False)
        await ns.dispatch(None, fault=False, pixel_level=0.2, outputs_ready=False)
        await asyncio.sleep(0)
        self.assertEqual(len(speech.played), 1)  # no auto replay on unmute
        self.assertEqual(ns.request_play(notice.notice_id).status, "permitted")
        ns.set_mute(True)
        self.assertEqual(ns.request_play(notice.notice_id).reason, "muted")

    async def test_quiet_hours_block_automatic_even_p2_but_manual_overrides(self):
        late = datetime(2026, 11, 17, 4, 30, tzinfo=timezone.utc)  # 22:30 Chicago
        ns = self.make(settings=Settings(audio_policy="automatic", buzzer_enabled=True), start=late)
        ns.availability.buzzer_ready = True
        notice = self.candidate(category="preparation", dedupe_key="prep:z", priority=2,
                                latest_expiry=late + timedelta(minutes=8))
        self.activate(notice)
        self.assertTrue(ns.quiet_hours())
        self.assertEqual(notice.routes["speech"].reason, "quiet_hours")
        self.assertEqual(notice.routes["buzzer"].reason, "quiet_hours")
        self.assertEqual(ns.request_play(notice.notice_id).status, "permitted")

    async def test_focus_blocks_automatic_speech_and_p2_exception_is_opt_in(self):
        ns = self.make(settings=Settings(audio_policy="automatic"))
        ns.set_focus(True)
        n = self.candidate(category="preparation", dedupe_key="prep:1", priority=2,
                           latest_expiry=START + timedelta(minutes=8))
        self.activate(n)
        self.assertEqual(n.routes["speech"].reason, "focus")
        self.settings = Settings(audio_policy="automatic", focus_p2_speech=True)
        m = self.candidate(category="preparation", dedupe_key="prep:2", priority=2,
                           latest_expiry=START + timedelta(minutes=8))
        self.activate(m)
        self.assertEqual(m.routes["speech"].status, "permitted")

    async def test_visual_only_blocks_manual_play(self):
        ns = self.make()
        n = self.candidate()
        self.activate(n)
        result = ns.request_play(n.notice_id)
        self.assertEqual((result.status, result.reason), ("blocked", "audio_policy_visual_only"))
        self.assertEqual(n.routes["speech"].reason, "audio_policy_visual_only")

    async def test_long_idle_suppresses_automatic_audio(self):
        ns = self.make(settings=Settings(audio_policy="automatic", long_idle_s=600))
        ns.availability.distance_state = "clear"
        ns.availability.clear_since = self.clock.monotonic()
        ns.availability.last_input = self.clock.monotonic()
        self.clock.advance(601)
        n = self.candidate()
        self.activate(n)
        self.assertEqual(n.routes["speech"].reason, "long_idle_clear_distance")
        self.assertEqual(n.lifecycle, Lifecycle.ACTIVE)  # visual stays


class AudioDispatch(Base):
    async def test_one_retry_maximum(self):
        speech = support.FakeSpeech(outcomes=["failed: link_lost", "failed: link_lost", "completed"])
        ns = self.make(settings=Settings(audio_policy="on_request"), speech=speech)
        n = self.candidate()
        self.activate(n)
        ns.request_play(n.notice_id)
        for _ in range(4):
            await ns.dispatch(None, fault=False, pixel_level=0.2, outputs_ready=False)
            await asyncio.sleep(0.01)
        self.assertEqual(len(speech.played), 2)
        self.assertEqual(n.routes["speech"].status, "failed")
        self.assertEqual(n.lifecycle, Lifecycle.ACTIVE)  # failed audio keeps visuals

    async def test_ack_during_playback_stops_it_and_ignores_completion(self):
        speech = support.FakeSpeech()
        speech.block = asyncio.Event()
        ns = self.make(settings=Settings(audio_policy="on_request"), speech=speech)
        n = self.candidate()
        self.activate(n)
        ns.request_play(n.notice_id)
        await ns.dispatch(None, fault=False, pixel_level=0.2, outputs_ready=False)
        await asyncio.sleep(0)
        ns.acknowledge(n.notice_id)
        await ns.dispatch(None, fault=False, pixel_level=0.2, outputs_ready=False)
        speech.block.set()
        await asyncio.sleep(0.01)
        self.assertEqual(len(speech.stopped), 1)
        self.assertEqual(n.lifecycle, Lifecycle.ACKNOWLEDGED)
        self.assertEqual(n.routes["speech"].status, "stopped")

    async def test_buzzer_needs_opt_in_and_pixels_follow_notice(self):
        outputs = FakeOutputs()
        ns = self.make(settings=Settings(audio_policy="automatic"))
        ns.availability.buzzer_ready = True
        n = self.candidate()
        self.activate(n)
        self.assertEqual(n.routes["buzzer"].reason, "buzzer_not_enabled")
        await ns.dispatch(outputs, fault=False, pixel_level=0.25, outputs_ready=True)
        self.assertEqual([(c.target, c.action, c.parameters.get("pattern")) for c in outputs.commands],
                         [("pixels", "set_pattern", "notice")])
        self.assertEqual(outputs.commands[0].notice_id, n.notice_id)
        await ns.dispatch(outputs, fault=False, pixel_level=0.25, outputs_ready=True)
        self.assertEqual(len(outputs.commands), 1)  # unchanged state is not resent
        self.settings = Settings(audio_policy="automatic", buzzer_enabled=True)
        m = self.candidate(dedupe_key="comfort:dry:2")
        self.activate(m)
        await ns.dispatch(outputs, fault=False, pixel_level=0.25, outputs_ready=True)
        tones = [c for c in outputs.commands if c.target == "buzzer"]
        self.assertEqual(len(tones), 1)
        self.assertLessEqual(tones[0].parameters["duration_ms"], 250)
        ns.acknowledge(n.notice_id)
        ns.acknowledge(m.notice_id)
        await ns.dispatch(outputs, fault=False, pixel_level=0.25, outputs_ready=True)
        self.assertEqual(outputs.commands[-1].parameters.get("pattern"), "ready")

    async def test_refused_pixel_command_backs_off(self):
        from contextcue_contracts import CommandResult

        class Refusing(FakeOutputs):
            async def execute(self, command):
                self.commands.append(command)
                return CommandResult("1.0", command.command_id, "rejected", "pixels_unavailable", "t")
        outputs = Refusing()
        ns = self.make()
        for _ in range(20):
            await ns.dispatch(outputs, fault=False, pixel_level=0.25, outputs_ready=True)
        self.assertEqual(len(outputs.commands), 1)
        self.clock.advance(5)
        await ns.dispatch(outputs, fault=False, pixel_level=0.25, outputs_ready=True)
        self.assertEqual(len(outputs.commands), 2)

    async def test_tones_never_replay_after_outputs_return(self):
        outputs = FakeOutputs()
        ns = self.make(settings=Settings(audio_policy="automatic", buzzer_enabled=True))
        ns.availability.buzzer_ready = True
        n = self.candidate()
        self.activate(n)
        await ns.dispatch(outputs, fault=False, pixel_level=0.25, outputs_ready=False)
        self.assertEqual(n.routes["buzzer"].status, "failed")
        await ns.dispatch(outputs, fault=False, pixel_level=0.25, outputs_ready=True)
        self.assertEqual([c.target for c in outputs.commands], ["pixels"])


if __name__ == "__main__":
    unittest.main()
