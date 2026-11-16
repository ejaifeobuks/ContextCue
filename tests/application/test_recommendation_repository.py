"""APP-06 to APP-08 validation/fallback/queue, and DataRepository (section 8)."""

import asyncio
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401

from contextcue_app.contracts import ContextEvent, ContextSnapshot, ModelResponse  # noqa: E402
from contextcue_app.recommendation import (RecommendationService, factual_fallback,  # noqa: E402
                                           validate_response)
from contextcue_app.repository import DataRepository  # noqa: E402
from contextcue_app.settings import Settings  # noqa: E402

NOW = datetime(2026, 11, 16, 15, 38, tzinfo=timezone.utc)


def evidence(eid, metric, value, quality="valid", unit=None):
    return ContextEvent(eid, 1, "src", "sensor", metric, value, unit, None, None, None, None,
                        "simulated", quality, "test", "Test")


def snapshot(category="comfort", ev=None, facts=None, question=None, request_id="r1"):
    ev = ev if ev is not None else (
        evidence("sensor.temperature", "temperature", 27.4, unit="degC"),
        evidence("pref.temperature_range", "preferred_temperature_range", [20.0, 26.0]),
        evidence("derived.comfort.hot", "comfort_hot", "sustained_above_preferred_temperature"))
    return ContextSnapshot(request_id, "s1", 1, category, "test", tuple(ev), facts or {"condition": "hot"},
                           {}, "normal", "visual_only", "x", "y", question)


def response(text, ids=("sensor.temperature",), category="comfort", request_id="r1"):
    return ModelResponse(request_id, "s1", category, text, tuple(ids))


class ValidationTests(unittest.TestCase):
    def test_accepts_supported_numbers(self):
        snap = snapshot()
        ok = response("It is 27.4 degC, above your 26 degC limit.",
                      ("sensor.temperature", "pref.temperature_range"))
        self.assertIsNone(validate_response(ok, snap))

    def test_numbers_are_compared_by_value(self):
        # Board test: "20.0 degC" was rejected because evidence 20.0 became "20".
        snap = snapshot()
        ok = response("It is 27.4 degC, above your preferred 20.0 to 26.0 degC.",
                      ("sensor.temperature", "pref.temperature_range"))
        self.assertIsNone(validate_response(ok, snap))

    def test_model_view_is_compact_and_focused(self):
        from contextcue_app.recommendation import model_view, response_schema
        schedule = {"event_count": 2, "busy_ms": 1, "next_event": {"event_id": "a", "title": "Stand-up",
                    "start_local": "09:00", "start": "2026-11-16T15:00:00Z"},
                    "remaining_events": [{"event_id": "a", "title": "Stand-up", "start_local": "09:00"},
                                         {"event_id": "b", "title": "Review", "start_local": "10:00"}]}
        ev = (evidence("calendar.schedule", "schedule_today", schedule),
              evidence("calendar.event.b", "event", {"title": "Review", "start_local": "10:00",
                                                     "start": "2026-11-16T16:00:00Z"}),
              evidence("weather.event", "forecast_event", {"condition": "rainy",
                                                           "precipitation_probability": 70}))
        morning = model_view(snapshot("morning", ev, {"local_time": "07:45"}))
        [sched] = [e for e in morning["evidence"] if e["id"] == "calendar.schedule"]
        self.assertEqual(morning["request"], {"category": "briefing"})  # any hour, not "morning"
        self.assertEqual(sched["next_event"], {"title": "Stand-up", "start": "9:00 AM"})
        # A briefing names only the next event: each input token costs about
        # 0.1 s on the board, and a real calendar briefing hit the deadline.
        self.assertNotIn("later", sched)
        busy = model_view(snapshot("context_answer", ev, {}, "schedule_today"))
        [sched] = [e for e in busy["evidence"] if e["id"] == "calendar.schedule"]
        self.assertEqual(sched["later"], [{"title": "Review", "start": "10:00 AM"}])
        self.assertNotIn("local_time", morning.get("facts", {}))  # quoted as an event time on the board
        prep = snapshot("preparation", ev, {"minutes_until_event": 22, "event_in_ms": 1320000})
        view = model_view(prep)
        self.assertEqual([e["id"] for e in view["evidence"]], ["calendar.event.b", "weather.event"])
        self.assertEqual(view["facts"], {"minutes_until_event": 22})
        # The model writes only the text; code fills in the category and the
        # evidence it was shown.
        self.assertEqual(list(response_schema(prep)["properties"]), ["text"])

    def test_evening_and_night_wording(self):
        from contextcue_app.recommendation import model_view
        ended = {"event_count": 4, "busy_ms": 11700000, "next_event": None,
                 "current_events": [], "remaining_events": [], "all_day": ["Demo day"]}
        night = {"condition": "sunny", "precipitation_probability": 0, "temperature_c": 7.9,
                 "location": "Huntsville, Alabama", "night": True, "start_local": "21:00"}
        ev = (evidence("calendar.schedule", "schedule_today", ended),
              evidence("weather.now", "forecast_next", night))
        view = model_view(snapshot("morning", ev, {}))
        sched, wx = view["evidence"]
        self.assertEqual(sched.get("events_left_today", 0), 0)
        self.assertTrue(sched["no_more_events_today"])
        self.assertNotIn("events_today", sched)  # never "4 events today" after they all ended
        self.assertEqual(wx["condition"], "clear")
        self.assertNotIn("from", wx)  # the current period's start read as the time of day
        # Board: "Team stand-up at 9:00 in Huntsville, Alabama".
        self.assertNotIn("location", wx)
        text = factual_fallback(snapshot("morning", ev, {})).text
        self.assertIn("No more events today", text)
        self.assertIn("clear", text)
        self.assertNotIn("sunny", text)

    def test_times_and_percentages_read_as_on_screen(self):
        from contextcue_app.recommendation import model_view
        later = {"condition": "rainy", "precipitation_probability": 35.0, "temperature_c": 12.0,
                 "start_local": "21:00", "end_local": "00:00"}
        now = {"condition": "cloudy", "precipitation_probability": 1.0, "temperature_c": 12.4,
               "start_local": "18:00"}
        ev = (evidence("weather.now", "forecast_next", now), evidence("weather.later", "forecast_later", later),
              evidence("calendar.event.b", "event", {"title": "Review", "start_local": "19:30"}))
        view = {e["id"]: e for e in model_view(snapshot("context_answer", ev, {}, "rain_today"))["evidence"]}
        self.assertEqual(view["weather.now"]["rain_chance_pct"], 1)  # board: "1.0%"
        self.assertEqual((view["weather.later"]["from"], view["weather.later"]["until"]), ("9:00 PM", "12:00 AM"))
        self.assertEqual(view["calendar.event.b"]["start"], "7:30 PM")  # board: "19:00"
        # The validator accepts the 12-hour hour it was shown, and still checks it.
        ids = ("weather.later", "calendar.event.b")
        snap = snapshot("context_answer", ev, {}, "rain_today")
        ok = response("Review is at 7:30 PM; 35% chance of rain from 9:00 PM.", ids, "context_answer")
        self.assertIsNone(validate_response(ok, snap))
        bad = response("Review is at 8:30 PM.", ids, "context_answer")
        self.assertEqual(validate_response(bad, snap), "unsupported_number:8")

    def test_advice_follows_the_evidence(self):
        from contextcue_app.engine import advice
        rain = evidence("weather.event", "forecast_event", {"condition": "rainy", "precipitation_probability": 70,
                                                            "temperature_c": 10})
        dry = evidence("weather.now", "forecast_next", {"condition": "cloudy", "precipitation_probability": 10,
                                                        "temperature_c": 12})
        cold = evidence("weather.now", "forecast_next", {"condition": "sunny", "precipitation_probability": 0,
                                                         "temperature_c": -2})
        snow = evidence("weather.now", "forecast_next", {"condition": "snowy", "precipitation_probability": 60,
                                                         "temperature_c": -2})
        self.assertEqual(advice("preparation", (rain,)), ["Bring an umbrella"])
        self.assertEqual(advice("morning", (dry,)), [])
        self.assertEqual(advice("morning", (cold,)), ["Wear a warm coat"])
        self.assertEqual(advice("morning", (snow,)), ["Bring an umbrella", "Dress for snow"])
        self.assertEqual(advice("comfort", (), "hot"), ["Open a window or use a fan"])
        self.assertEqual(advice("context_answer", (rain,), question="next_event"), [])
        self.assertEqual(advice("context_answer", (rain,), question="rain_today"), ["Bring an umbrella"])

    def test_rejections(self):
        snap = snapshot()
        cases = {
            "unsupported_number:31": response("It is 31 degC."),
            "unknown_or_invalid_evidence": response("Warm.", ("sensor.humidity",)),
            "no_evidence": response("Warm.", ()),
            # Stray tokens seen from the 0.8B model on the board (G1 benchmark).
            "unexpected_characters": response("The room temperature is 27.无反顾"),
            "wrong_category": response("Warm.", category="morning"),
            "wrong_request": response("Warm.", request_id="other"),
            "empty_text": response("   "),
            "too_long": response("One. Two. Three. Four."),
        }
        for reason, resp in cases.items():
            with self.subTest(reason=reason):
                self.assertEqual(validate_response(resp, snap), reason)

    def test_invalid_evidence_cannot_be_cited(self):
        snap = snapshot(ev=(evidence("sensor.temperature", "temperature", None, quality="stale"),))
        self.assertEqual(validate_response(response("Warm."), snap), "unknown_or_invalid_evidence")


class FallbackTests(unittest.TestCase):
    def test_comfort_fallback_cites_inputs(self):
        fb = factual_fallback(snapshot())
        self.assertIn("27.4 °C", fb.text)
        self.assertIn("20-26", fb.text)
        self.assertEqual(set(fb.evidence_ids), {"sensor.temperature", "pref.temperature_range"})

    def test_no_valid_facts_means_no_fallback(self):
        snap = snapshot(ev=(evidence("sensor.temperature", "temperature", None, quality="unavailable"),))
        self.assertIsNone(factual_fallback(snap))

    def test_unsupported_question_has_no_answer(self):
        snap = snapshot(category="context_answer", ev=(), question="stock_price")
        self.assertIsNone(factual_fallback(snap))


class SlowModel:
    execution_location = "test"
    model_version = "fake"

    def __init__(self, delay=0.05, text="It is 27.4 degC."):
        self.delay = delay
        self.text = text
        self.calls = []

    async def complete(self, snap):
        self.calls.append(snap.request_id)
        await asyncio.sleep(self.delay)
        return ModelResponse(snap.request_id, snap.snapshot_id, snap.category, self.text,
                             ("sensor.temperature",))


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def run_service(self, model, timeout=1.0):
        svc = RecommendationService(model, now=lambda: NOW, timeout_s=timeout)
        results = []

        async def collect(r):
            results.append(r)
        svc.on_result(collect)
        return svc, results

    async def test_generated_and_validated(self):
        svc, results = await self.run_service(SlowModel(0.01))
        svc.submit(snapshot())
        await asyncio.sleep(0.05)
        self.assertEqual(results[0].outcome, "generated")

    async def test_timeout(self):
        svc, results = await self.run_service(SlowModel(0.5), timeout=0.05)
        svc.submit(snapshot())
        await asyncio.sleep(0.15)
        self.assertEqual(results[0].outcome, "timed_out")

    async def test_rejects_unsupported_output(self):
        svc, results = await self.run_service(SlowModel(0.01, text="It is 35 degC."))
        svc.submit(snapshot())
        await asyncio.sleep(0.05)
        self.assertEqual((results[0].outcome, results[0].reason), ("rejected", "unsupported_number:35"))
        self.assertIsNone(results[0].response)

    async def test_one_active_one_coalesced_waiting(self):
        model = SlowModel(0.05)
        svc, results = await self.run_service(model)
        svc.submit(snapshot(request_id="a"))
        svc.submit(snapshot(request_id="b"))
        svc.submit(snapshot(request_id="c"))  # replaces b
        await asyncio.sleep(0.2)
        self.assertEqual(model.calls, ["a", "c"])
        outcomes = {r.snapshot.request_id: r.outcome for r in results}
        self.assertEqual(outcomes, {"a": "generated", "b": "cancelled", "c": "generated"})

    async def test_cancel_discards_late_result(self):
        model = SlowModel(0.05)
        svc, results = await self.run_service(model)
        svc.submit(snapshot(request_id="a"))
        await asyncio.sleep(0.01)
        svc.cancel("a")
        await asyncio.sleep(0.1)
        self.assertEqual(results[0].outcome, "cancelled")
        self.assertIsNone(results[0].response)


class LlamaClientTests(unittest.IsolatedAsyncioTestCase):
    """LlamaCppModel against a local stand-in for the OpenAI-compatible endpoint."""

    async def asyncSetUp(self):
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        self.requests = []
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                test.requests.append((self.path, None))
                data = json.dumps({"data": [{"id": "stub-model"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                test.requests.append((self.path, body))
                content = test.reply
                finish = getattr(test, "finish_reason", "stop")
                data = json.dumps({"choices": [{"message": {"content": content},
                                                "finish_reason": finish}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    async def asyncTearDown(self):
        await asyncio.to_thread(self.httpd.shutdown)
        self.httpd.server_close()

    async def test_generated_response_is_parsed_and_validated(self):
        from contextcue_app.recommendation import LlamaCppModel
        self.reply = ('{"category": "comfort", "text": "It is 27.4 degC in here.", '
                      '"evidence_ids": ["sensor.temperature"]}')
        model = LlamaCppModel(self.url, "qwen-test", timeout_s=2)
        snap = snapshot()
        response = await model.complete(snap)
        self.assertIsNone(validate_response(response, snap))
        path, body = self.requests[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(body["max_tokens"], 100)
        self.assertNotIn("tools", body)  # the model never gets hardware tools
        self.assertIn("sensor.temperature", body["messages"][1]["content"])

    async def test_reply_cut_off_at_max_tokens_is_rejected(self):
        # Board, 12 November: a reply drifted into stray words ("11:0 abajo ...
        # iszlich") until max_tokens; plain letters pass the character check.
        from contextcue_app.recommendation import LlamaCppModel
        self.reply = '{"text": "Next event is PHL 201 at 11:0 abajo, the weather iszlich'
        self.finish_reason = "length"
        model = LlamaCppModel(self.url, "qwen-test", timeout_s=2)
        with self.assertRaisesRegex(ValueError, "truncated_model_output"):
            await model.complete(snapshot())

    async def test_request_is_compact_and_schema_constrained(self):
        import json
        from contextcue_app.recommendation import SYSTEM_PROMPT, LlamaCppModel
        self.reply = ('{"category": "comfort", "text": "It is 27.4 degC in here.", '
                      '"evidence_ids": ["sensor.temperature"]}')
        model = LlamaCppModel(self.url, timeout_s=2)
        snap = snapshot()
        await model.complete(snap)
        # The model name comes from the server, as the Brick serves one model.
        self.assertEqual([p for p, _ in self.requests], ["/v1/models", "/v1/chat/completions"])
        body = self.requests[1][1]
        self.assertEqual(body["model"], "stub-model")
        self.assertEqual(model.model_version, "stub-model")
        # Fixed instructions first, unchanged, so the server can reuse them.
        self.assertEqual(body["messages"][0], {"role": "system", "content": SYSTEM_PROMPT})
        view = json.loads(body["messages"][1]["content"])
        self.assertEqual(view["request"], {"category": snap.category})
        self.assertEqual([e["id"] for e in view["evidence"]],
                         [e.evidence_id for e in snap.evidence if e.quality == "valid"])
        schema = body["response_format"]["json_schema"]["schema"]
        self.assertEqual(schema["required"], ["text"])
        self.assertFalse(schema["additionalProperties"])

    async def test_warm_up_processes_the_instructions_and_never_raises(self):
        from contextcue_app.recommendation import SYSTEM_PROMPT, LlamaCppModel
        self.reply = ""
        await LlamaCppModel(self.url, timeout_s=2).warm_up()
        self.assertEqual(self.requests[-1][1]["messages"][0]["content"], SYSTEM_PROMPT)
        self.assertEqual(self.requests[-1][1]["max_tokens"], 1)
        await LlamaCppModel("http://127.0.0.1:9/v1", timeout_s=1).warm_up()  # unreachable: no error

    async def test_malformed_output_is_an_error(self):
        from contextcue_app.recommendation import LlamaCppModel
        self.reply = "Sure! It is warm."
        svc = RecommendationService(LlamaCppModel(self.url, timeout_s=2), now=lambda: NOW)
        results = []

        async def collect(r):
            results.append(r)
        svc.on_result(collect)
        svc.submit(snapshot())
        for _ in range(50):
            await asyncio.sleep(0.02)
            if results:
                break
        self.assertEqual((results[0].outcome, results[0].reason), ("error", "malformed_model_output"))

    async def test_unreachable_endpoint_is_reported(self):
        from contextcue_app.recommendation import LlamaCppModel, ModelUnavailable
        model = LlamaCppModel("http://127.0.0.1:9/v1", timeout_s=1)
        with self.assertRaises(ModelUnavailable):
            await model.complete(snapshot())


class RepositoryTests(unittest.TestCase):
    def test_settings_roundtrip_and_defaults(self):
        tmp = support.temp_dir(self)
        path = Path(tmp) / "cc.db"
        repo = DataRepository(path)
        self.assertEqual(repo.restore_settings(), (Settings(), None))
        repo.save_settings(Settings().updated({"audio_policy": "on_request", "focus": True}), NOW)
        repo.close()
        repo = DataRepository(path)
        self.addCleanup(repo.close)
        restored, problem = repo.restore_settings()
        self.assertIsNone(problem)
        self.assertEqual((restored.audio_policy, restored.focus), ("on_request", True))

    def test_corrupt_database_is_kept_and_replaced(self):
        tmp = support.temp_dir(self)
        path = Path(tmp) / "cc.db"
        path.write_bytes(b"this is not a sqlite database" * 100)
        repo = DataRepository(path)
        self.addCleanup(repo.close)
        self.assertIsNotNone(repo.recovered_from)
        self.assertEqual(len(list(Path(tmp).glob("cc.corrupt-*"))), 1)
        repo.save_settings(Settings(), NOW)

    def test_newer_schema_refused_visibly(self):
        tmp = support.temp_dir(self)
        path = Path(tmp) / "cc.db"
        DataRepository(path).close()
        db = sqlite3.connect(path)
        db.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
        db.commit()
        db.close()
        repo = DataRepository(path)
        self.addCleanup(repo.close)
        self.assertIn("newer", repo.recovered_from)

    def test_retention_and_redaction(self):
        repo = DataRepository()
        self.addCleanup(repo.close)
        repo.write_diagnostic("old", {"x": 1}, NOW - timedelta(days=8))
        repo.write_diagnostic("new", {"api_token": "secret-value", "nested": {"password": "p"}}, NOW)
        repo.remove_expired_data(NOW)
        diags = repo.diagnostics()
        self.assertEqual([d["kind"] for d in diags], ["new"])
        self.assertEqual(diags[0]["api_token"], "[redacted]")
        self.assertEqual(diags[0]["nested"]["password"], "[redacted]")

    def test_acknowledgement_is_transactional_tombstone(self):
        repo = DataRepository()
        self.addCleanup(repo.close)
        notice = {"notice_id": "n1", "dedupe_key": "k", "revision": 2, "lifecycle": "acknowledged",
                  "created_at": "2026-11-16T15:38:00.000Z"}
        repo.save_acknowledgement(notice, "acknowledged", NOW)
        self.assertEqual(repo.tombstones()["k"]["notice_id"], "n1")
        self.assertEqual(repo.recent_notices()[0]["lifecycle"], "acknowledged")


if __name__ == "__main__":
    unittest.main()
