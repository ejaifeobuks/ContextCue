"""Thinking-mode benchmark for ContextCue (isolated; no app or Brick changes).

Configurations (same model file, same server, same scenarios):
  A   production request, byte for byte: app prompt, greedy, max_tokens 100,
      JSON schema; thinking off (template default)
  B   A + chat_template_kwargs.enable_thinking=true, greedy, max_tokens 1024
  Bs  B with Qwen's recommended thinking sampling (T 0.6, top_p 0.95,
      top_k 20, min_p 0), seeds 1 and 2
  C   thinking off, greedy, max_tokens 100, JSON schema, richer context:
      number-free derived hints and a list of unavailable sources

Run inside the app container against the experiment server only:

    docker exec contextcue-main-1 python /app/eval_tmp/bench.py \
        --url http://llamacpp-models-runner:9998/v1 --out /app/eval_tmp/results.jsonl

One JSON line per request: config, scenario, timings, tokens, reasoning,
reply, the app validator's verdict and the grounding checks.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
for p in ("/app/python/services/application", "/app/python/apps/contextcue-hardware/python",
          str(HERE.parents[1] / "services/application"),
          str(HERE.parents[1] / "apps/contextcue-hardware/python")):
    if Path(p).is_dir() and p not in sys.path:
        sys.path.insert(0, p)
sys.path.insert(0, str(HERE))

from contextcue_app.contracts import ModelResponse  # noqa: E402
from contextcue_app.recommendation import (SYSTEM_PROMPT, LlamaCppModel, model_evidence_ids,  # noqa: E402
                                           model_view, response_schema, validate_response)
from scenarios import MISSING, SCENARIOS  # noqa: E402

QWEN_THINKING = {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0}

# C: one extra rule; the snapshot gains number-free hints only, so the app's
# number validator is exactly as strict as for A.
C_RULE = ("\nhints are code-checked conclusions; use them. If something the request needs is listed in "
          "unavailable, say you don't have that information instead of guessing.")

SOURCE_NAME = {"schedule_today": "calendar", "event": "calendar", "temperature": "room temperature",
               "humidity": "room humidity", "forecast_next": "weather forecast",
               "forecast_later": "weather forecast", "forecast_event": "weather forecast"}


def enriched_view(snapshot) -> dict:
    view = model_view(snapshot)
    hints = {}
    unavailable = sorted({SOURCE_NAME.get(e.metric, e.metric) for e in snapshot.evidence if e.quality != "valid"})
    by_metric = {e.metric: e for e in snapshot.evidence if e.quality == "valid"}
    sched = by_metric.get("schedule_today")
    if sched:
        v = sched.value
        hints["has_next_event"] = bool(v.get("next_event"))
        if v.get("current_events"):
            hints["happening_now"] = [e["title"] for e in v["current_events"]]
        if v.get("all_day"):
            hints["all_day_events_have_no_start_time"] = True
        nxt = v.get("next_event") or {}
        hints["next_event_location_known"] = bool(nxt.get("location")) if nxt else None
    fcs = [e.value for m, e in by_metric.items() if m.startswith("forecast")]
    if fcs:
        hints["rain_likely_today"] = any((f.get("precipitation_probability") or 0) >= 50 for f in fcs)
    later = by_metric.get("forecast_later")
    if later and sched and sched.value.get("next_event"):
        hints["next_event_during_later_forecast"] = later.value["start_local"] <= \
            sched.value["next_event"]["start_local"] < later.value["end_local"]
    t, rng = by_metric.get("temperature"), by_metric.get("preferred_temperature_range")
    if t and rng:
        lo, hi = rng.value
        hints["room_temperature_vs_preference"] = "above" if t.value > hi else "below" if t.value < lo else "within"
    view["hints"] = {k: v for k, v in hints.items() if v is not None}
    if unavailable:
        view["unavailable"] = unavailable
    return view


def body_for(config: str, snapshot, seed: int | None) -> dict:
    model = LlamaCppModel("http://unused/v1", "exp")
    messages = model._prompt(snapshot)
    body = {"model": "exp", "messages": messages, "max_tokens": 100, "temperature": 0,
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "contextcue_notice", "schema": response_schema(snapshot)}}}
    if config in ("B", "Bs"):
        body["chat_template_kwargs"] = {"enable_thinking": True}
        body["max_tokens"] = 1024
    if config == "Bs":
        body.update(QWEN_THINKING, seed=seed)
    if config == "C":
        body["messages"] = [{"role": "system", "content": SYSTEM_PROMPT + C_RULE},
                            {"role": "user", "content": json.dumps(enriched_view(snapshot), separators=(",", ":"),
                                                                   default=str)}]
    return body


def grade(sc, text: str | None) -> dict:
    t = (text or "").lower()
    must_ok = [any(re.search(alt, t, re.I) for alt in group) for group in sc.must]
    forbid_hits = [f for f in sc.forbid if re.search(f, t, re.I)]
    ack = bool(re.search(MISSING, t, re.I)) if sc.missing else None
    correct = bool(text) and all(must_ok) and not forbid_hits and (ack is not False)
    return {"must_ok": must_ok, "forbid_hits": forbid_hits, "acknowledged_missing": ack, "correct": correct}


def run_one(url: str, config: str, sc, seed: int | None, timeout: float) -> dict:
    snap = sc.snapshot()
    body = body_for(config, snap, seed)
    rec = {"config": config, "scenario": sc.sid, "group": sc.group, "seed": seed,
           "max_tokens": body["max_tokens"]}
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(url + "/chat/completions", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.loads(r.read())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        rec.update(wall_s=round(time.monotonic() - t0, 2), error=f"{type(exc).__name__}: {exc}"[:200],
                   valid=False, reason="request_failed", **grade(sc, None))
        return rec
    rec["wall_s"] = round(time.monotonic() - t0, 2)
    choice = payload["choices"][0]
    msg = choice["message"]
    timings, usage = payload.get("timings", {}), payload.get("usage", {})
    reasoning = msg.get("reasoning_content") or ""
    content = msg.get("content") or ""
    rec.update(
        finish=choice.get("finish_reason"),
        prompt_tokens=usage.get("prompt_tokens"),
        cached_tokens=(usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        reasoning_tokens=(usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
        prompt_n=timings.get("prompt_n"), prompt_ms=timings.get("prompt_ms"),
        predicted_n=timings.get("predicted_n"), predicted_ms=timings.get("predicted_ms"),
        gen_tps=timings.get("predicted_per_second"), prompt_tps=timings.get("prompt_per_second"),
        reasoning_chars=len(reasoning), reasoning=reasoning[:2000], raw=content[:1000],
        think_tag_in_content="<think>" in content or "</think>" in content,
    )
    text, reason = None, None
    if choice.get("finish_reason") == "length":
        reason = "truncated_model_output"
    else:
        try:
            data = json.loads(content.split("</think>")[-1].strip())
            text = str(data.get("text", "")) if isinstance(data, dict) else None
            if text is None:
                reason = "malformed_model_output"
        except json.JSONDecodeError:
            reason = "malformed_model_output"
    if text is not None:
        resp = ModelResponse(snap.request_id, snap.snapshot_id, snap.category, text, model_evidence_ids(snap))
        reason = validate_response(resp, snap)
    rec.update(text=text, valid=reason is None, reason=reason, **grade(sc, text))
    return rec


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://llamacpp-models-runner:9998/v1")
    ap.add_argument("--out", default=str(HERE / "results.jsonl"))
    ap.add_argument("--configs", default="A,C,B,Bs")
    ap.add_argument("--repeats", type=int, default=2, help="runs per scenario for greedy configs")
    ap.add_argument("--only", default="", help="comma-separated scenario ids")
    ap.add_argument("--timeout", type=float, default=600)
    args = ap.parse_args()
    url = args.url.rstrip("/")
    if "9999" in url:
        sys.exit("Refusing to benchmark the production server (port 9999).")
    scenarios = [s for s in SCENARIOS if not args.only or s.sid in args.only.split(",")]
    with open(args.out, "a") as out:
        # Config-major order: C's system prompt differs, so interleaving would
        # evict the cached prefix on every request. In blocks, the first request
        # of each block is the cold case and the rest match production's warm cache.
        for rep in range(args.repeats):
            for config in args.configs.split(","):
                for sc in scenarios:
                    seeds = [rep + 1] if config == "Bs" else [None]
                    for seed in seeds:
                        rec = run_one(url, config, sc, seed, args.timeout)
                        rec["repeat"] = rep
                        out.write(json.dumps(rec) + "\n")
                        out.flush()
                        print(f"{config:2} {sc.sid:24} {rec.get('wall_s', 0):6.1f}s valid={rec['valid']!s:5} "
                              f"correct={rec['correct']!s:5} {rec.get('reason') or ''} | {rec.get('text')!r}",
                              flush=True)


if __name__ == "__main__":
    main()
