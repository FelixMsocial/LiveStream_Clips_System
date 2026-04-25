"""Step 4 — Per-Platform Post Text Generator (Claude Sonnet 4.5).

ONE Claude call returns three structurally-different captions (YouTube Shorts,
TikTok, Instagram Reels) in a single JSON response per the v1.1 prompt. Replaces
the old 3-call sequential pattern (which spaced calls 10s apart for "HR
learning") — net savings ~30s per clip.
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


def _build_user_message(
    substance: dict[str, Any],
    hook_output: dict[str, Any],
) -> str:
    extracted = substance.get("_extracted", {})
    payload = {
        "weighted_total": substance.get("weighted_total"),
        "peak_emotion": extracted.get("peak_emotion"),
        "extractable_element": extracted.get("extractable_element"),
        "context_summary": substance.get("context_summary"),
        "trigger_type": extracted.get("trigger_type"),
        "hook_text": hook_output.get("hook_text"),
        "primary_archetype": hook_output.get("primary_archetype"),
    }
    return "INPUT:\n" + json.dumps(payload, ensure_ascii=False, indent=2)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=30))
def _call(
    client: Anthropic,
    prompt_body: str,
    user_msg: str,
    model: str,
) -> str:
    resp = client.messages.create(
        model=model,
        max_tokens=1500,
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
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()


def run_copy(
    api_key: str,
    prompt_body: str,
    substance: dict[str, Any],
    hook_output: dict[str, Any],
    *,
    model: str = "claude-sonnet-4-5-20250929",
    fallback_url: str | None = None,
) -> tuple[dict[str, str], dict[str, Any], str | None]:
    """Generate IG/YT/TT captions in one call.

    Returns:
      captions: {"instagram", "youtube", "tiktok"} — strings ready for D1
      caption_scores: full JSON from the model (for `caption_scores_json` column)
      error: str | None if the call failed and fallbacks were used
    """
    client = Anthropic(api_key=api_key)
    user_msg = _build_user_message(substance, hook_output)

    try:
        raw = _call(client, prompt_body, user_msg, model)
        data = _extract_json(raw)
    except Exception as e:  # noqa: BLE001
        log.warning("per-platform caption call failed: %s", e)
        fallback = fallback_url or "🔴 Live now"
        return (
            {"instagram": fallback, "youtube": fallback, "tiktok": fallback},
            {},
            str(e),
        )

    captions_block = data.get("captions", {}) or {}
    yt = (captions_block.get("youtube_shorts") or {}).get("caption_text", "") or ""
    tt = (captions_block.get("tiktok") or {}).get("caption_text", "") or ""
    ig = (captions_block.get("instagram_reels") or {}).get("caption_text", "") or ""

    fallback = fallback_url or "🔴 Live now"
    return (
        {
            "instagram": ig.strip() or fallback,
            "youtube": yt.strip() or fallback,
            "tiktok": tt.strip() or fallback,
        },
        data,
        None,
    )
