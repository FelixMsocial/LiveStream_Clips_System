"""Step 2 — Hook Overlay Generator (Claude Sonnet 4.5).

Produces the on-video hook text from the Substance Scorer's structured output.
On iterations 2-3 receives `previous_feedback` from the Hook Scorer so it can
address specific rule failures rather than regenerate blindly.

The system prompt is sent with `cache_control` so iterations 2-3 reuse the
cached prefix (the prompt body is unchanged across iterations within one clip).
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


def _build_user_message(
    substance: dict[str, Any],
    *,
    iteration: int,
    previous_feedback: list[dict[str, Any]] | None,
) -> str:
    extracted = substance.get("_extracted", {})
    payload: dict[str, Any] = {
        "iteration_number": iteration,
        "weighted_total": substance.get("weighted_total"),
        "peak_timestamp_seconds": extracted.get("peak_timestamp_seconds"),
        "peak_emotion": extracted.get("peak_emotion"),
        "extractable_element": extracted.get("extractable_element"),
        "context_summary": substance.get("context_summary"),
        "trigger_type": extracted.get("trigger_type"),
        "recommended_trim_window": substance.get("recommended_trim_window"),
    }
    if iteration > 1 and previous_feedback:
        payload["previous_feedback"] = previous_feedback
    return "INPUT:\n" + json.dumps(payload, ensure_ascii=False, indent=2)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=20))
def generate(
    api_key: str,
    prompt_body: str,
    substance: dict[str, Any],
    *,
    iteration: int = 1,
    previous_feedback: list[dict[str, Any]] | None = None,
    model: str = "claude-sonnet-4-5-20250929",
) -> dict[str, Any]:
    client = Anthropic(api_key=api_key)
    user_msg = _build_user_message(
        substance,
        iteration=iteration,
        previous_feedback=previous_feedback,
    )
    resp = client.messages.create(
        model=model,
        max_tokens=1200,
        temperature=0.7,
        system=[
            {
                "type": "text",
                "text": prompt_body,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_msg}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    data = _extract_json(text)

    if "hook_text" not in data or not data["hook_text"]:
        raise ValueError("hook generator returned no hook_text")
    return data
