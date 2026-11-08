"""Automated screening of a model reply against its snapshot (baseline APP-08).

This is a screen, not proof of truth: T03 still needs independent human
review. It checks structure, category, evidence references, numeric claims,
and size. `strict` requires the reply to be exactly one JSON object;
`lenient` also accepts one wrapped in a Markdown code fence.
"""

import json
import re

MAX_SENTENCES = 3
MAX_OUTPUT_TOKENS = 160

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string",
                     "enum": ["morning", "preparation", "comfort", "context_answer"]},
        "text": {"type": "string"},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["category", "text", "evidence_ids"],
    "additionalProperties": False,
}

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def _numbers(text: str) -> set[str]:
    """Numeric claims in `text`, normalized so "08", "8" and "8.0" match."""
    found = set()
    for token in _NUMBER.findall(text):
        # A leading "-" is often a separator ("20-26"), so sign is not checked.
        value = float(token.lstrip("-"))
        found.add(str(int(value)) if value.is_integer() else repr(value))
    return found


def _identifiers(snapshot: dict) -> set[str]:
    """Snapshot and evidence IDs, whose digits are labels, not claims."""
    ids = {snapshot["snapshot_id"]} | {item["id"] for item in snapshot["evidence"]}
    ids |= {v for k, v in snapshot.get("calculated_facts", {}).items()
            if k.endswith("_id") and isinstance(v, str)}
    return ids


def _without(text: str, identifiers: set[str]) -> str:
    for ident in sorted(identifiers, key=len, reverse=True):
        text = text.replace(ident, " ")
    return text


def check(raw: str, snapshot: dict, answerable: bool, completion_tokens: int | None) -> dict:
    """Return {"strict": bool, "lenient": bool, "problems": [...]}."""
    problems = []
    text = raw.strip()
    fenced = _FENCE.match(text)
    strict_json = True
    if fenced:
        strict_json = False
        text = fenced.group(1)
    try:
        reply = json.loads(text)
    except ValueError:
        return {"strict": False, "lenient": False, "problems": ["not_json"]}
    if not isinstance(reply, dict):
        return {"strict": False, "lenient": False, "problems": ["not_object"]}
    if set(reply) != {"category", "text", "evidence_ids"}:
        problems.append("wrong_fields")
    category, message, ids = reply.get("category"), reply.get("text"), reply.get("evidence_ids")
    if category != snapshot["request"]["category"]:
        problems.append("wrong_category")
    if not isinstance(message, str) or not message.strip():
        problems.append("empty_text")
        message = ""
    known = {item["id"] for item in snapshot["evidence"]}
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        problems.append("bad_evidence_ids")
        ids = []
    if any(i not in known for i in ids):
        problems.append("unknown_evidence_id")
    if answerable and not ids:
        problems.append("no_evidence_cited")
    if not answerable and ids:
        problems.append("evidence_cited_for_unanswerable")
    # Every number in the text must appear somewhere in the snapshot. Digits
    # inside IDs are ignored on both sides: "ev-cal-p1" does not support "1".
    identifiers = _identifiers(snapshot)
    allowed = _numbers(_without(json.dumps(snapshot), identifiers))
    invented = sorted(_numbers(_without(message, identifiers)) - allowed, key=float)
    if invented:
        problems.append(f"unsupported_numbers:{','.join(invented)}")
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", message.strip()) if s]
    if len(sentences) > MAX_SENTENCES:
        problems.append("too_many_sentences")
    if completion_tokens is not None and completion_tokens > MAX_OUTPUT_TOKENS:
        problems.append("too_many_tokens")
    # Unanswerable questions must cite nothing; whether the text admits the
    # gap instead of inventing an answer is left to human review.
    lenient = not problems
    return {"strict": lenient and strict_json, "lenient": lenient,
            "problems": problems + ([] if strict_json else ["code_fence"])}
