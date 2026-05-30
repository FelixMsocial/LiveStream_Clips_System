"""Step 4 — Per-Platform Post Text Generator (Gemini Flash).

ONE call returns three structurally-different captions (YouTube Shorts,
TikTok, Instagram Reels) in a single JSON response. Gemini's native JSON
mode (response_mime_type) guarantees a parseable response every time.
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


_STRIP_CHARS = (
    '"\u201c\u201d\u201e\u201f\u2033\u2036'  # double-quote variants
    "'\u2018\u2019\u201a\u201b\u2032\u2035"  # single-quote variants
)
_DASH_MAP = str.maketrans("\u2013\u2014\u2015", "---")


def _sanitize(text: str) -> str:
    """Strip all double and single quote variants, normalize dashes.

    Metricool rejects captions containing quote chars -- they break its text field.
    """
    result = text.translate(str.maketrans("", "", _STRIP_CHARS))
    return result.translate(_DASH_MAP).strip()


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
    api_key: str,
    prompt_body: str,
    user_msg: str,
    model: str,
) -> str:
    client = genai.Client(api_key=api_key)
    resp = client.models.generate_content(
        model=model,
        config=types.GenerateContentConfig(
            system_instruction=prompt_body,
            temperature=0.7,
            response_mime_type="application/json",
        ),
        contents=user_msg,
    )
    return (resp.text or "").strip()


def run_copy(
    api_key: str,
    prompt_body: str,
    substance: dict[str, Any],
    hook_output: dict[str, Any],
    *,
    model: str = "gemini-2.5-flash",
    fallback_url: str | None = None,
) -> tuple[dict[str, str], dict[str, Any], str | None]:
    """Generate IG/YT/TT captions in one call.

    Returns:
      captions: {"instagram", "youtube", "tiktok"} — strings ready for D1
      caption_scores: full JSON from the model (for `caption_scores_json` column)
      error: str | None if the call failed and fallbacks were used
    """
    user_msg = _build_user_message(substance, hook_output)

    try:
        raw = _call(api_key, prompt_body, user_msg, model)
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
            "instagram": _sanitize(ig.strip()) or fallback,
            "youtube": _sanitize(yt.strip()) or fallback,
            "tiktok": _sanitize(tt.strip()) or fallback,
        },
        data,
        None,
    )
