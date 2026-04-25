"""Claude Sonnet × 3 for platform copy. Sequential with 10s spacing (HR learning)."""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from anthropic import Anthropic
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-5-20250929"


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=30))
def _call(client: Anthropic, prompt_body: str, ctx: dict[str, Any]) -> str:
    user_msg = (
        prompt_body
        + "\n\n---\nINPUT:\n"
        + json.dumps(ctx, ensure_ascii=False, indent=2)
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=400,
        temperature=0.7,
        messages=[{"role": "user", "content": user_msg}],
    )
    parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    return ("".join(parts)).strip()


def run_copy(
    api_key: str,
    prompts: dict[str, str],
    vision_analysis: dict[str, Any],
    transcript_excerpt: str,
    vibe: str,
    *,
    fallback_url: str | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Returns ({'instagram': str, 'youtube': str, 'tiktok': str}, failures)."""
    client = Anthropic(api_key=api_key)
    ctx = {
        "vision_analysis": vision_analysis,
        "transcript_excerpt": transcript_excerpt[:1500],
        "vibe": vibe,
    }
    out: dict[str, str] = {}
    failures: dict[str, str] = {}
    keys = ("instagram", "youtube", "tiktok")
    prompt_keys = {"instagram": "ig_copy", "youtube": "yt_copy", "tiktok": "tt_copy"}

    for i, k in enumerate(keys):
        try:
            text = _call(client, prompts[prompt_keys[k]], ctx)
            if k == "youtube":
                # yt prompt returns JSON { title, description }.
                try:
                    parsed = json.loads(text)
                    text = f"{parsed.get('title', '')}\n{parsed.get('description', '')}".strip()
                except Exception:
                    pass
            out[k] = text
        except Exception as e:  # noqa: BLE001
            log.warning("claude %s copy failed: %s", k, e)
            failures[k] = str(e)
            out[k] = fallback_url or "🔴 Live now"
        if i < len(keys) - 1:
            time.sleep(10)  # HR-learning spacing
    return out, failures
