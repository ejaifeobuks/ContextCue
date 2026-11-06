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
        with tempfile.TemporaryDirectory(prefix="contextcue-firmware-") as directory:
            binary = Path(directory) / "snapshot_test"
            subprocess.run(
                [compiler, "-std=c++17", "-pthread", "-Wall", "-Wextra", "-Werror",
                 "-I", str(fixtures / "stubs"), str(fixtures / "snapshot_test.cpp"),
                 "-o", str(binary)],
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
        for label in ("success", "recovery"):
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


if __name__ == "__main__":
    unittest.main()
