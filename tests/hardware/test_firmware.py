"""Compile the real sketch with host hardware stubs; no board is required."""

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class FirmwareSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which("g++")
        if compiler is None:
            raise unittest.SkipTest("g++ is required for firmware regression tests")
        fixtures = Path(__file__).parent / "firmware"
        sketch = Path(__file__).resolve().parents[2] / "apps/contextcue-hardware/sketch"
        # Arduino compiles every .cpp under the sketch's src/ folder; mirror that.
        sources = sorted(str(path) for path in (sketch / "src").rglob("*.cpp"))
        with tempfile.TemporaryDirectory(prefix="contextcue-firmware-") as directory:
            binary = Path(directory) / "snapshot_test"
            subprocess.run(
                [compiler, "-std=c++17", "-pthread", "-Wall", "-Wextra", "-Werror",
                 "-I", str(fixtures / "stubs"), str(fixtures / "snapshot_test.cpp"),
                 *sources, "-o", str(binary)],
                check=True, capture_output=True, text=True, timeout=60,
            )
            result = subprocess.run(
                [str(binary)], check=True, capture_output=True, text=True, timeout=30,
            )
        cls.records = {}
        cls.stress = []
        for line in result.stdout.splitlines():
            label, payload = line.split("\t", 1)
            if len(payload.encode("utf-8")) > 224:
                raise ValueError(f"Oversized MCU response: {label}")
            # Python otherwise permits nonstandard uppercase NaN/Infinity.
            data = json.loads(payload, parse_constant=cls.reject_nonfinite)
            if label == "stress":
                cls.stress.append(data)
            else:
                cls.records[label] = data

    @staticmethod
    def reject_nonfinite(value):
        raise ValueError(f"Non-finite JSON token: {value}")

    def test_polling_during_acquisition_preserves_completed_sample(self):
        previous = self.records["initial"]
        for label in ("success", "disconnect", "recovery", "bad_read",
                      "out_of_range", "final_recovery"):
            for phase in ("begin", "temperature", "humidity"):
                key = f"{label}_during_{phase}"
                if key in self.records:
                    during = self.records[key]
                    self.assertEqual(
                        {k: v for k, v in during.items() if k != "up"},
                        {k: v for k, v in previous.items() if k != "up"},
                    )
            after = self.records[f"{label}_after"]
            self.assertEqual(after["seq"], previous["seq"] + 1)
            previous = after
        self.assertEqual(self.records["success_after"]["t"], 26.5)

    def test_failures_and_recovery_publish_complete_records(self):
        for label, quality, error in (
            ("disconnect", "unavailable", "thermo_disconnected"),
            ("bad_read", "invalid", "thermo_read_failed"),
            ("out_of_range", "invalid", "thermo_out_of_range"),
        ):
            sample = self.records[f"{label}_after"]
            self.assertEqual((sample["q"], sample["err"]), (quality, error))
            self.assertIsNone(sample["t"])
            self.assertIsNone(sample["h"])
        for label in ("recovery", "final_recovery"):
            self.assertEqual(self.records[f"{label}_after"]["q"], "valid")

    def test_distance_success_timeout_range_and_recovery(self):
        initial = self.records["initial_distance"]
        self.assertEqual((initial["q"], initial["err"]), ("unavailable", "not_sampled"))
        # The first poll after begin() reads nothing: the library would return
        # its previous (or zeroed) result before a fresh measurement exists.
        for label in ("success", "distance_reconnect"):
            warmup = self.records[f"{label}_distance_after"]
            self.assertEqual((warmup["q"], warmup["err"]), ("unavailable", "distance_timeout"))
            self.assertIsNone(warmup["mm"])
        for label in ("recovery", "final_recovery"):
            sample = self.records[f"{label}_distance_after"]
            self.assertEqual(sample["q"], "valid")
            self.assertTrue(sample["available"])
            self.assertGreaterEqual(sample["mm"], 0)
        timeout = self.records["distance_timeout_distance_after"]
        self.assertEqual((timeout["q"], timeout["err"]), ("unavailable", "distance_timeout"))
        self.assertIsNone(timeout["mm"])
        out_of_range = self.records["distance_out_of_range_distance_after"]
        self.assertEqual(
            (out_of_range["q"], out_of_range["err"]),
            ("invalid", "distance_out_of_range"),
        )
        self.assertIsNone(out_of_range["mm"])
        disconnected = self.records["distance_disconnect_distance_after"]
        self.assertEqual(
            (disconnected["q"], disconnected["err"]),
            ("unavailable", "distance_disconnected"),
        )
        recovered = self.records["distance_recovery_distance_after"]
        self.assertEqual((recovered["q"], recovered["mm"]), ("valid", 425))

    def test_distance_begin_runs_only_at_boot_and_reconnect(self):
        # begin() leaks driver objects in Arduino_Modulino 0.10.0, so a present
        # sensor must never be re-initialized: once at boot, once on re-plug,
        # regardless of timeouts, out-of-range results or 1000+ stress polls.
        self.assertEqual(self.records["distance_begin_calls"]["count"], 2)

    def test_sensor_rpc_responses_are_compact_and_independent(self):
        light = self.records["initial_light"]
        distance = self.records["distance_after"]
        self.assertEqual((light["q"], light["lux"], light["err"]),
                         ("unavailable", None, "light_not_found"))
        self.assertEqual((distance["q"], distance["mm"], distance["err"]),
                         ("valid", 430, None))
        for sample in (light, distance):
            self.assertEqual(sample["boot"], self.records["initial"]["boot"])
            self.assertLessEqual(len(json.dumps(sample, separators=(",", ":")).encode()),
                                 224)
        self.assertEqual(self.records["light_after"]["q"], "unavailable")
        self.assertEqual(distance["q"], "valid")

    def test_serializer_rejects_nan_and_infinity_for_both_metrics(self):
        guards = [v for k, v in self.records.items() if k.startswith("guard_")]
        self.assertEqual(len(guards), 6)
        for sample in guards:
            self.assertEqual(sample["q"], "invalid")
            self.assertEqual(sample["err"], "thermo_read_failed")
            self.assertIsNone(sample["t"])
            self.assertIsNone(sample["h"])

    def test_concurrent_handshakes_retain_one_boot_identity(self):
        tokens = {v["boot"] for v in self.records.values() if "boot" in v}
        self.assertEqual(len(tokens), 1)
        self.assertIn(tokens.pop(), {"a" * 32, "b" * 32})
        self.assertEqual(self.records["initial_open"]["ok"], True)
        self.assertEqual(self.records["disconnect_open_begin"]["ok"], True)
        self.assertEqual(self.records["recovery_open_begin"]["ok"], False)

    def test_handshake_reports_light_and_distance_availability(self):
        # setup() samples only Thermo; the stub Light module is never found.
        initial = self.records["initial_open"]
        self.assertEqual((initial["light"], initial["distance"]), (False, False))
        # By then a completed Distance reading has been published.
        later = self.records["final_recovery_open_begin"]
        self.assertEqual((later["light"], later["distance"]), (False, True))
        self.assertEqual(initial["fw"], "0.2.0")

    def test_concurrent_publication_preserves_sequence_content_and_uptime(self):
        self.assertGreaterEqual(len(self.stress), 1000)
        fingerprints = {}
        previous_seq = 0
        previous_up = 0
        for sample in self.stress:
            self.assertGreaterEqual(sample["seq"], previous_seq)
            self.assertGreaterEqual(sample["up"], previous_up)
            self.assertLessEqual(sample["at"], sample["up"])
            fingerprint = {k: v for k, v in sample.items() if k != "up"}
            self.assertEqual(fingerprints.setdefault(sample["seq"], fingerprint), fingerprint)
            self.assertEqual(sample["q"], "valid")
            previous_seq, previous_up = sample["seq"], sample["up"]
        self.assertGreater(self.records["after_wrap"]["up"], 2**32)

    # Outputs (HW-03, HW-07). LED words are (r << 24 | g << 16 | b << 8 | brightness).
    AMBER = lambda brightness: (255 << 24) | (140 << 16) | brightness  # noqa: E731

    def test_outputs_boot_quiet(self):
        boot = self.records["out_boot_hw"]
        self.assertEqual(boot["leds"], [0] * 8)
        self.assertEqual((boot["shows"], boot["notones"], boot["tones"]), (1, 1, 0))
        initial = self.records["out_initial"]
        self.assertEqual((initial["px"], initial["bz"], initial["pat"], initial["lease"]),
                         (True, True, "off", False))
        self.assertIsNone(initial["last"])

    def test_outputs_require_a_lease_and_apply_allowlisted_pattern(self):
        self.assertEqual(self.records["out_before_lease"]["why"], "no_lease")
        self.assertTrue(self.records["out_lease"]["ok"])
        self.assertEqual(self.records["out_pattern"]["r"], "accepted")
        state = self.records["out_pattern_state"]
        self.assertEqual((state["pat"], state["b"], state["last"], state["o"]),
                         ("notice", 25, "c1", "completed"))
        self.assertEqual(self.records["out_pattern_hw"]["leds"], [type(self).AMBER(25)] * 8)

    def test_outputs_reject_every_out_of_bounds_command(self):
        expected = {
            "out_duplicate": "duplicate_command",
            "rej_brightness": "invalid_brightness",
            "rej_pattern": "invalid_pattern",
            "rej_duration": "invalid_duration",
            "rej_frequency": "invalid_frequency",
            "rej_ttl_zero": "invalid_ttl",
            "rej_ttl_long": "invalid_ttl",
            "rej_action": "unknown_action",
            "rej_extra": "unexpected_parameter",
            "rej_id": "invalid_command_id",
            "rej_session": "session_mismatch",
            "rej_long_session": "invalid_argument",
        }
        for label, reason in expected.items():
            with self.subTest(label):
                self.assertEqual(self.records[label]["r"], "rejected")
                self.assertEqual(self.records[label]["why"], reason)
                self.assertNotIn("id", self.records[label])  # rejected IDs are never echoed

    def test_outputs_bounded_tone(self):
        self.assertEqual(self.records["out_tone"]["r"], "accepted")
        hardware = self.records["out_tone_hw"]
        self.assertEqual((hardware["freq"], hardware["dur"], hardware["tones"]), (880, 200, 1))
        self.assertTrue(self.records["out_tone_state"]["tone"])
        self.assertFalse(self.records["out_tone_done"]["tone"])

    def test_outputs_expired_command_is_not_applied(self):
        self.assertEqual(self.records["out_short_ttl"]["r"], "accepted")
        state = self.records["out_expired_state"]
        self.assertEqual((state["last"], state["o"], state["why"]), ("e1", "expired", "ttl_elapsed"))
        self.assertEqual(state["pat"], "notice")  # the fault pattern never reached the strip

    def test_outputs_lease_loss_turns_everything_off(self):
        before = self.records["out_before_expiry_hw"]
        self.assertNotEqual(before["leds"], [0] * 8)
        after = self.records["out_after_expiry_hw"]
        self.assertEqual(after["leds"], [0] * 8)
        self.assertEqual(after["notones"], before["notones"] + 1)
        state = self.records["out_expired_lease"]
        self.assertEqual((state["lease"], state["pat"]), (False, "off"))
        self.assertEqual(self.records["out_after_expiry_cmd"]["why"], "no_lease")
        # Stops can only make outputs safer, so they are accepted without a lease.
        self.assertEqual(self.records["out_after_expiry_stop"]["r"], "accepted")
        self.assertEqual(self.records["out_after_expiry_stop_state"]["o"], "completed")

    def test_outputs_new_session_starts_fresh(self):
        self.assertTrue(self.records["out_new_session"]["ok"])
        self.assertEqual(self.records["out_new_session_reuse"]["r"], "accepted")
        self.assertEqual(self.records["out_new_session_state"]["pat"], "notice")

    def test_outputs_disconnect_and_reconnect(self):
        self.assertFalse(self.records["out_pixels_gone"]["px"])
        self.assertEqual(self.records["out_pixels_gone_cmd"]["why"], "pixels_unavailable")
        self.assertTrue(self.records["out_pixels_back"]["px"])
        self.assertEqual(self.records["out_pixels_back_hw"]["leds"], [type(self).AMBER(20)] * 8)

    def test_outputs_longest_status_fits_the_transport(self):
        state = self.records["out_long_id_state"]
        self.assertEqual(state["last"], "abcdefghijklmnopqrstuvwxyz012345")
        # setUpClass already rejects any record over 224 bytes.


if __name__ == "__main__":
    unittest.main()
