"""ContextCue G1 LLM benchmark: runs fixed snapshot cases against the local
llama.cpp server provisioned by the arduino:llm Brick and prints JSON lines.

Talks to the server's OpenAI-compatible endpoint directly (standard library
only) so it can stream for time-to-first-token, read llama.cpp's own timings,
and try JSON-schema constrained output, which the Brick's chat() does not
expose. Never passes tools and never keeps conversation memory.
"""

import json
import os
import signal
import statistics
import sys
import time
import urllib.error
import urllib.request

from cases import build_cases
from validate import MAX_OUTPUT_TOKENS, RESPONSE_SCHEMA, check

BASE_URL = os.environ.get("LLM_BASE_URL", "http://llamacpp-models-runner:9999/v1")
REQUEST_TIMEOUT_S = 120
MODES = os.environ.get("BENCH_MODES", "prompt,schema").split(",")

SYSTEM_PROMPT = """You write short desk notices for ContextCue.
Use ONLY the facts in the snapshot JSON. Never invent times, numbers, names or events.
Reply with exactly one JSON object and nothing else:
{"category": "<copy request.category exactly>", "text": "<at most 2 short sentences>", "evidence_ids": ["<id values from the evidence list that you used>"]}
"category" must be exactly the value of request.category in the snapshot.
"evidence_ids" may contain only "id" values from the evidence list.
If request.question cannot be answered from the snapshot, say so in "text" and use an empty evidence_ids list."""


def log(record: dict) -> None:
    print(json.dumps(record), flush=True)


def get(path: str):
    with urllib.request.urlopen(BASE_URL + path, timeout=10) as response:
        return json.loads(response.read())


def wait_for_server(limit_s: float = 1800) -> str:
    start = time.monotonic()
    while True:
        try:
            models = [m["id"] for m in get("/models")["data"]]
            if models:
                return models[0]
        except (OSError, ValueError, KeyError):
            pass
        if time.monotonic() - start > limit_s:
            raise SystemExit("llama.cpp server did not become ready")
        time.sleep(5)


def generate(model: str, snapshot: dict, mode: str) -> dict:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(snapshot, separators=(",", ":"))},
        ],
        "max_tokens": MAX_OUTPUT_TOKENS,
        "temperature": 0.2,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if mode == "schema":
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": "notice", "schema": RESPONSE_SCHEMA}}
    request = urllib.request.Request(
        BASE_URL + "/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    started = time.monotonic()
    first_token = None
    parts, usage, timings = [], None, None
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
        for line in response:
            line = line.decode().strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            chunk = json.loads(payload)
            usage = chunk.get("usage") or usage
            timings = chunk.get("timings") or timings
            for choice in chunk.get("choices", []):
                piece = (choice.get("delta") or {}).get("content")
                if piece:
                    if first_token is None:
                        first_token = time.monotonic()
                    parts.append(piece)
    ended = time.monotonic()
    return {
        "text": "".join(parts),
        "total_s": ended - started,
        "ttft_s": (first_token - started) if first_token else None,
        "usage": usage,
        "timings": timings,
    }


def summarize(model: str, mode: str, results: list[dict]) -> dict:
    ok = [r for r in results if "error" not in r]
    totals = sorted(r["total_s"] for r in ok)
    ttfts = sorted(r["ttft_s"] for r in ok if r["ttft_s"] is not None)

    def pct(values, p):
        return round(values[min(len(values) - 1, int(round(p * (len(values) - 1))))], 2) if values else None

    gen = [r["timings"]["predicted_per_second"] for r in ok if r.get("timings")]
    prompt = [r["timings"]["prompt_per_second"] for r in ok if r.get("timings")]
    return {
        "summary": True, "model": model, "mode": mode, "cases": len(results),
        "errors": len(results) - len(ok),
        "strict_valid": sum(r["check"]["strict"] for r in ok),
        "lenient_valid": sum(r["check"]["lenient"] for r in ok),
        "total_s_p50": pct(totals, 0.5), "total_s_p95": pct(totals, 0.95),
        "total_s_max": pct(totals, 1.0),
        "ttft_s_p50": pct(ttfts, 0.5), "ttft_s_p95": pct(ttfts, 0.95),
        "gen_tok_s_mean": round(statistics.mean(gen), 1) if gen else None,
        "prompt_tok_s_mean": round(statistics.mean(prompt), 1) if prompt else None,
    }


def main() -> None:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    log({"event": "waiting_for_server", "base_url": BASE_URL})
    model = wait_for_server()
    cases = build_cases()
    # Cold call: the first request loads the model into memory.
    started = time.monotonic()
    warm = generate(model, cases[0]["snapshot"], "prompt")
    log({"event": "cold_call", "model": model, "total_s": round(time.monotonic() - started, 2),
         "ttft_s": warm["ttft_s"]})
    for mode in MODES:
        results = []
        for case in cases:
            try:
                out = generate(model, case["snapshot"], mode)
                tokens = (out["usage"] or {}).get("completion_tokens")
                out["check"] = check(out["text"], case["snapshot"], case["answerable"], tokens)
                record = {"case": case["name"], "mode": mode,
                          "request": case["snapshot"]["request"],
                          "total_s": round(out["total_s"], 2),
                          "ttft_s": round(out["ttft_s"], 2) if out["ttft_s"] is not None else None,
                          "usage": out["usage"], "timings": out["timings"],
                          "check": out["check"], "text": out["text"]}
            except (urllib.error.HTTPError) as exc:
                record = {"case": case["name"], "mode": mode,
                          "error": f"HTTP {exc.code}: {exc.read()[:300].decode(errors='replace')}"}
            except Exception as exc:  # noqa: BLE001 - benchmark records every failure
                record = {"case": case["name"], "mode": mode, "error": repr(exc)}
            results.append({**out, **record} if "error" not in record else record)
            log(record)
        log(summarize(model, mode, results))
    # Exit so the app stops by itself. App Lab sets no restart policy, so the
    # benchmark does not rerun, and the stopped container keeps its log.
    log({"event": "done"})


if __name__ == "__main__":
    main()
