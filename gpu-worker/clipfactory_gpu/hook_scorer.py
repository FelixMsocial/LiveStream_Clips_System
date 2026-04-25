"""Step 3 — Hook Overlay Scorer (Claude Sonnet 4.5).

Scores the generator's output against the 8-rule rubric and either passes
(verdict=PASS) or returns structured `improvement_feedback` for the next
iteration. Rule 5 (Alignment) is a hard veto — alignment <6 forces FAIL
regardless of weighted_total.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from anthropic import Anthropic
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)


def _extract_json(text: str) -> dict[str, Any]:
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*\n?", "", s)
        s = re.sub(r"\n?```\s*$", "", s)
    return json.loads(s)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=20))
def score(
    api_key: str,
    prompt_body: str,
    hook_output: dict[str, Any],
    substance: dict[str, Any],
    *,
    iteration: int = 1,
    previous_feedback: list[dict[str, Any]] | None = None,
    model: str = "claude-sonnet-4-5-20250929",
    pass_threshold: int = 65,
    alignment_floor: int = 6,
) -> dict[str, Any]:
    client = Anthropic(api_key=api_key)
    extracted = substance.get("_extracted", {})
    payload: dict[str, Any] = {
        "iteration_number": iteration,
        "hook_output": hook_output,
        "substance_signals": {
            "peak_timestamp_seconds": extracted.get("peak_timestamp_seconds"),
            "peak_emotion": extracted.get("peak_emotion"),
            "extractable_element": extracted.get("extractable_element"),
            "context_summary": substance.get("context_summary"),
            "trigger_type": extracted.get("trigger_type"),
        },
    }
    if previous_feedback:
        payload["previous_feedback"] = previous_feedback

    resp = client.messages.create(
        model=model,
        max_tokens=2000,
        temperature=0.2,
        system=[
            {
                "type": "text",
                "text": prompt_body,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {
                "role": "user",
                "content": "INPUT:\n" + json.dumps(payload, ensure_ascii=False, indent=2),
            }
        ],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    data = _extract_json(text)

    # Apply hard-veto in code regardless of what the prompt returned, so the
    # contract is enforced even if the model drifts.
    rules = data.get("rule_scores", {}) or {}
    alignment = rules.get("5_alignment", {}) or {}
    align_score = int(alignment.get("score", 0))
    weighted = int(data.get("weighted_total", 0))

    if align_score < alignment_floor:
        data["verdict"] = "FAIL"
        if "5_alignment" in rules:
            rules["5_alignment"]["hard_veto_triggered"] = True
    elif weighted >= pass_threshold:
        data["verdict"] = "PASS"
    else:
        data["verdict"] = "FAIL"

    return data
