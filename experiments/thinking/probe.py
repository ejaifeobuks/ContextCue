"""Probe how this llama.cpp build handles Qwen3.5 thinking (no app changes).

Run inside the ContextCue app container against the isolated experiment
server (never the production one):

    docker exec contextcue-main-1 python /app/eval_tmp/probe.py http://llamacpp-models-runner:9998/v1

Each probe prints: finish_reason, token counts and timings, the reasoning
text length and the final content, so the mechanics are observed, not assumed.
"""

import json
import sys
import time
import urllib.request

URL = (sys.argv[1] if len(sys.argv) > 1 else "http://llamacpp-models-runner:9998/v1").rstrip("/")
sys.path[:0] = ["/app/python/services/application", "/app/python/apps/contextcue-hardware/python"]
from contextcue_app.recommendation import SYSTEM_PROMPT  # noqa: E402

USER = json.dumps({"request": {"category": "context_answer", "question": "Will it rain today?"},
                   "evidence": [{"id": "weather.now", "type": "forecast_next", "condition": "cloudy",
                                 "rain_chance_pct": 20, "temperature_c": 14},
                                {"id": "weather.later", "type": "forecast_later", "from": "3:00 PM",
                                 "until": "6:00 PM", "condition": "rain", "rain_chance_pct": 70}]},
                  separators=(",", ":"))
SCHEMA = {"type": "json_schema", "json_schema": {"name": "n", "schema": {
    "type": "object", "properties": {"text": {"type": "string", "minLength": 1, "maxLength": 400}},
    "required": ["text"], "additionalProperties": False}}}


def call(name, **extra):
    body = {"model": "exp", "temperature": 0, "max_tokens": 100,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": USER}]}
    body.update(extra)
    t0 = time.monotonic()
    req = urllib.request.Request(URL + "/chat/completions", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            p = json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"## {name}: HTTP {e.code} {e.read()[:300]!r}")
        return
    wall = time.monotonic() - t0
    c = p["choices"][0]
    m = c["message"]
    t = p.get("timings", {})
    reasoning = m.get("reasoning_content") or ""
    print(f"## {name}: wall {wall:.1f}s finish={c.get('finish_reason')} usage={p.get('usage')}")
    print(f"   timings prompt_n={t.get('prompt_n')} prompt_ms={t.get('prompt_ms', 0):.0f} "
          f"predicted_n={t.get('predicted_n')} predicted_ms={t.get('predicted_ms', 0):.0f}")
    print(f"   reasoning chars={len(reasoning)} first200={reasoning[:200]!r}")
    print(f"   content={m.get('content')!r}")


probes = sys.argv[2].split(",") if len(sys.argv) > 2 else ["off", "on", "on_schema", "on_small", "on_budget"]
for p in probes:
    if p == "off":
        call("A thinking off (template default), schema", response_format=SCHEMA)
    elif p == "on":
        call("B thinking on, no schema, max_tokens 1500",
             chat_template_kwargs={"enable_thinking": True}, max_tokens=1500)
    elif p == "on_schema":
        call("B thinking on + schema, max_tokens 1500",
             chat_template_kwargs={"enable_thinking": True}, max_tokens=1500, response_format=SCHEMA)
    elif p == "on_small":
        call("B thinking on, max_tokens 100 (does reasoning count?)",
             chat_template_kwargs={"enable_thinking": True}, max_tokens=100)
    elif p == "on_budget":
        call("B thinking on, per-request reasoning_budget 64",
             chat_template_kwargs={"enable_thinking": True}, max_tokens=1500, reasoning_budget=64)
    elif p == "on_budget2":
        call("B thinking on, per-request thinking_budget_tokens 64",
             chat_template_kwargs={"enable_thinking": True}, max_tokens=1500, thinking_budget_tokens=64)
    elif p == "on_qwen":
        # Qwen's recommended thinking sampling (model card): T 0.6, top_p 0.95, top_k 20.
        call("B thinking on, Qwen thinking sampling, reasoning_format none",
             chat_template_kwargs={"enable_thinking": True}, max_tokens=1500, reasoning_format="none",
             temperature=0.6, top_p=0.95, top_k=20, min_p=0, seed=1)
    elif p == "on_qwen_ask":
        # Same, plus an explicit instruction to reason first.
        global SYSTEM_PROMPT
        saved = SYSTEM_PROMPT
        SYSTEM_PROMPT = SYSTEM_PROMPT.replace("Reply with exactly one JSON object and nothing else",
                                              "Think it through first, then reply with exactly one JSON object")
        call("B thinking on, Qwen sampling, prompt asks to think",
             chat_template_kwargs={"enable_thinking": True}, max_tokens=1500, reasoning_format="none",
             temperature=0.6, top_p=0.95, top_k=20, min_p=0, seed=1)
        SYSTEM_PROMPT = saved
    elif p == "on_none":
        call("B thinking on, reasoning_format none",
             chat_template_kwargs={"enable_thinking": True}, max_tokens=1500, reasoning_format="none")
