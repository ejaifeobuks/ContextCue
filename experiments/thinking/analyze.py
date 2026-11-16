"""Summarise results.jsonl from bench.py into the comparison tables.

    python3 experiments/thinking/analyze.py results.jsonl [res.log]
"""

import json
import statistics as st
import sys
from collections import defaultdict

rows = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
ORDER = ["A", "B", "Bs", "C"]
NAME = {"A": "A  production (thinking off)", "B": "B  thinking on, greedy",
        "Bs": "B' thinking on, Qwen sampling", "C": "C  richer context, thinking off"}


def pct(xs):
    return f"{100 * sum(xs) / len(xs):.0f}%" if xs else "-"


def med(xs):
    return f"{st.median(xs):.1f}" if xs else "-"


def mean(xs):
    return f"{st.mean(xs):.1f}" if xs else "-"


by = defaultdict(list)
for r in rows:
    by[r["config"]].append(r)

print("| Config | n | Correct | App-valid | Hallucinated | Admits missing | Truncated/failed | "
      "Latency mean / median / max (s) | Warm latency median (s) | Prompt tok | Output tok mean | Reasoning chars | Gen tok/s |")
print("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
for c in ORDER:
    rs = by.get(c)
    if not rs:
        continue
    lat = [r["wall_s"] for r in rs if "wall_s" in r]
    warm = [r["wall_s"] for r in rs if (r.get("cached_tokens") or 0) > 100]
    halluc = [bool(r.get("forbid_hits")) or str(r.get("reason", "")).startswith("unsupported_number") for r in rs]
    miss = [r["acknowledged_missing"] for r in rs if r.get("acknowledged_missing") is not None]
    bad = [r.get("reason") in ("truncated_model_output", "request_failed", "malformed_model_output") for r in rs]
    out_tok = [r["completion_tokens"] for r in rs if r.get("completion_tokens") is not None]
    ptok = [r["prompt_tokens"] for r in rs if r.get("prompt_tokens")]
    tps = [r["gen_tps"] for r in rs if r.get("gen_tps")]
    print(f"| {NAME[c]} | {len(rs)} | {pct([r['correct'] for r in rs])} | {pct([r['valid'] for r in rs])} | "
          f"{pct(halluc)} | {pct(miss)} | {pct(bad)} | {mean(lat)} / {med(lat)} / {max(lat):.1f} | {med(warm)} | "
          f"{med(ptok)} | {mean(out_tok)} | {max(r.get('reasoning_chars', 0) for r in rs)} | {mean(tps)} |")

print("\nCorrect by scenario group (all runs):\n")
groups = sorted({r["group"] for r in rows})
print("| Group | " + " | ".join(c for c in ORDER if c in by) + " |")
print("|---|" + "---|" * len([c for c in ORDER if c in by]))
for g in groups:
    cells = []
    for c in ORDER:
        if c in by:
            rs = [r for r in by[c] if r["group"] == g]
            cells.append(f"{sum(r['correct'] for r in rs)}/{len(rs)}")
    print(f"| {g} | " + " | ".join(cells) + " |")

print("\nRejection / failure reasons:\n")
for c in ORDER:
    reasons = defaultdict(int)
    for r in by.get(c, []):
        if r.get("reason"):
            reasons[r["reason"].split(":")[0]] += 1
    print(f"- {c}: {dict(reasons)}")

print("\nPer scenario (first repeat; Bs seed 1):\n")
for sid in dict.fromkeys(r["scenario"] for r in rows):
    print(f"### {sid}")
    for c in ORDER:
        rs = [r for r in by.get(c, []) if r["scenario"] == sid and r.get("repeat") == 0]
        for r in rs:
            mark = "OK " if r["correct"] else "BAD"
            print(f"- {c} {mark} {r.get('wall_s')}s {r.get('reason') or ''} {r.get('forbid_hits') or ''} "
                  f"think={r.get('reasoning_chars')} | {r.get('text') or r.get('raw', '')[:160]!r}")

if len(sys.argv) > 2:
    # res.log: epoch, server utime+stime (ticks), RSS kB, load1, MemAvailable kB, /proc/stat cpu fields
    samples = [line.split() for line in open(sys.argv[2]) if line.strip()]
    if len(samples) > 2:
        cpu = []
        for a, b in zip(samples, samples[1:]):
            dt = int(b[0]) - int(a[0])
            if dt > 0:
                cpu.append((int(b[1]) - int(a[1])) / 100 / dt)
        rss = [int(s[2]) / 1024 for s in samples]
        avail = [int(s[4]) / 1024 for s in samples]
        print(f"\nExperiment server: CPU mean {st.mean(cpu):.2f} cores (max {max(cpu):.2f}); "
              f"RSS {min(rss):.0f}-{max(rss):.0f} MB (includes the shared, memory-mapped model file); "
              f"MemAvailable min {min(avail):.0f} MB; load1 max {max(float(s[3]) for s in samples):.2f}")
