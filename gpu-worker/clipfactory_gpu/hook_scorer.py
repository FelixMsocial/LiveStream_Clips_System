"""Step 3 — Hook Overlay Scorer (Gemini Flash).

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

from google import genai
from google.genai import types
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)


def _extract_json(text: str) -> dict[str, Any]:
    s = (text or "").strip()
    if not s:
        raise ValueError("empty response from model")
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*\n?", "", s)
        s = re.sub(r"\n?```\s*$", "", s)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", s)
        if not m:
            raise
        return json.loads(m.group(0))


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=20))
def score(
    api_key: str,
    prompt_body: str,
    hook_output: dict[str, Any],
    substance: dict[str, Any],
    *,
    iteration: int = 1,
    previous_feedback: list[dict[str, Any]] | None = None,
    model: str = "gemini-2.5-flash",
    pass_threshold: int = 65,
    alignment_floor: int = 6,
) -> dict[str, Any]:
    client = genai.Client(api_key=api_key)
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

    resp = client.models.generate_content(
        model=model,
        config=types.GenerateContentConfig(
            system_instruction=prompt_body,
            temperature=0.2,
            response_mime_type="application/json",
        ),
        contents="INPUT:\n" + json.dumps(payload, ensure_ascii=False, indent=2),
    )
    data = _extract_json(resp.text or "")

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
