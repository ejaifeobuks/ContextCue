"""Laptop tests for the G1 benchmark's case set and reply screening."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps/llm-benchmark/python"))

from cases import CATEGORIES, build_cases  # noqa: E402
from validate import check  # noqa: E402


def reply(**fields):
    base = {"category": "preparation", "text": "Physics lecture starts in 15 minutes; rain is likely.",
            "evidence_ids": ["ev-cal-p1", "ev-wx-p1"]}
    base.update(fields)
    return json.dumps(base)


class CaseSetTests(unittest.TestCase):
    def test_thirty_cases_across_all_categories(self):
        cases = build_cases()
        self.assertEqual(len(cases), 30)
        self.assertEqual({c["snapshot"]["request"]["category"] for c in cases}, set(CATEGORIES))
        self.assertEqual(sum(not c["answerable"] for c in cases), 2)
        self.assertEqual(len({c["name"] for c in cases}), 30)


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = next(c for c in build_cases() if c["name"] == "prep-1")["snapshot"]

    def test_grounded_reply_passes(self):
        result = check(reply(), self.snapshot, True, 30)
        self.assertEqual(result, {"strict": True, "lenient": True, "problems": []})

    def test_code_fence_is_lenient_only(self):
        result = check("```json\n" + reply() + "\n```", self.snapshot, True, 30)
        self.assertEqual((result["strict"], result["lenient"]), (False, True))

    def test_invented_number_fails(self):
        result = check(reply(text="Physics lecture starts in 45 minutes."), self.snapshot, True, 30)
        self.assertIn("unsupported_numbers:45", result["problems"])

    def test_unknown_or_missing_evidence_fails(self):
        self.assertIn("unknown_evidence_id",
                      check(reply(evidence_ids=["ev-made-up"]), self.snapshot, True, 30)["problems"])
        self.assertIn("no_evidence_cited",
                      check(reply(evidence_ids=[]), self.snapshot, True, 30)["problems"])

    def test_wrong_category_extra_fields_and_length(self):
        self.assertIn("wrong_category", check(reply(category="morning"), self.snapshot, True, 30)["problems"])
        self.assertIn("wrong_fields", check(reply(confidence=0.9), self.snapshot, True, 30)["problems"])
        long_text = "One. Two. Three. Four."
        self.assertIn("too_many_sentences", check(reply(text=long_text), self.snapshot, True, 30)["problems"])
        self.assertIn("too_many_tokens", check(reply(), self.snapshot, True, 161)["problems"])

    def test_not_json(self):
        self.assertEqual(check("Sure! Here is your notice.", self.snapshot, True, 10)["problems"], ["not_json"])

    def test_digits_in_ids_do_not_support_numbers(self):
        # prep-1 has no "1" anywhere except inside IDs (ev-cal-p1, snap-prep-1).
        result = check(reply(text="You have 1 event: Physics lecture."), self.snapshot, True, 30)
        self.assertIn("unsupported_numbers:1", result["problems"])

    def test_ids_quoted_in_text_are_not_numbers(self):
        result = check(reply(text="Physics lecture (ev-cal-p1) starts in 15 minutes."),
                       self.snapshot, True, 30)
        self.assertEqual(result["problems"], [])

    def test_leading_zero_and_decimal_forms_match(self):
        # The snapshot has "08:15" and "09:00"; a reply may write them as 8:15 and 9.
        result = check(reply(text="It is 8:15; Physics lecture is at 9."), self.snapshot, True, 30)
        self.assertEqual(result["problems"], [])
        comfort = next(c for c in build_cases() if c["name"] == "comfort-2")["snapshot"]
        text = json.dumps({"category": "comfort", "text": "Humidity is 64%.",
                           "evidence_ids": ["ev-in-2"]})
        self.assertEqual(check(text, comfort, True, 20)["problems"], [])

    def test_unanswerable_must_cite_nothing(self):
        snapshot = next(c for c in build_cases() if c["name"] == "question-5")["snapshot"]
        text = json.dumps({"category": "context_answer",
                           "text": "I don't have your email in this snapshot.", "evidence_ids": []})
        self.assertTrue(check(text, snapshot, False, 20)["strict"])
        cited = json.dumps({"category": "context_answer",
                            "text": "I don't have your email in this snapshot.",
                            "evidence_ids": ["ev-cal-q"]})
        self.assertIn("evidence_cited_for_unanswerable", check(cited, snapshot, False, 20)["problems"])


if __name__ == "__main__":
    unittest.main()
