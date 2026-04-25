"""Step 1 — Substance Scorer (Gemini 2.5 Pro, native video).

Replaces the freeform `vision_analysis` step. Uploads the raw 90s mp4 via the
Files API with the v1.1 Substance Scorer system prompt and returns the 8-rule
weighted JSON. The substance score is a flag, not a gate — low scores still
continue down the pipeline and only set `low_potential_flag` for the human
approver.

On repeated failure we degrade to a midpoint trim with a neutral score so the
clip can still render and reach approval.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from tenacity import retry, stop_after_attempt, wait_fixed

log = logging.getLogger(__name__)


def _degraded(duration_sec: float) -> dict[str, Any]:
    """Neutral output when Gemini is unavailable. Pipeline still proceeds."""
    mid = duration_sec / 2.0
    start = max(0.0, mid - 12.5)
    end = start + min(25.0, max(15.0, duration_sec - start))
    return {
        "rule_scores": {f"{i}_unknown": {"score": 5, "reasoning": "degraded fallback"} for i in range(1, 9)},
        "coherence_bonus": {"score": 0, "reasoning": "degraded fallback"},
        "weighted_total": 0,
        "interpretation": "very_weak",
        "primary_strength": "—",
        "primary_weakness": "Gemini analysis unavailable",
        "context_summary": "Substance Scorer unavailable; using midpoint trim. Human review required.",
        "recommended_trim_window": {
            "start_seconds": start,
            "end_seconds": end,
            "rationale": "midpoint fallback (Gemini unavailable)",
        },
        "rulebook_version": "1.1-degraded",
        "_extracted": {
            "peak_timestamp_seconds": mid,
            "peak_emotion": "low_arousal",
            "extractable_element": "",
            "trigger_type": "none",
        },
        "degraded": True,
    }


@retry(stop=stop_after_attempt(3), wait=wait_fixed(10))
def score(
    api_key: str,
    video_path: Path,
    prompt_body: str,
    *,
    model: str = "gemini-2.5-pro",
    streamer: str = "Jordy",
    transcript_excerpt: str = "",
    duration_sec: float = 90.0,
) -> dict[str, Any]:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    uploaded = client.files.upload(file=str(video_path))
    deadline = time.time() + 180
    while uploaded.state.name != "ACTIVE":
        if time.time() > deadline:
            raise TimeoutError("gemini file never became ACTIVE")
        time.sleep(2)
        uploaded = client.files.get(name=uploaded.name)

    user_context = (
        f"streamer: {streamer}\n"
        f"clip_duration_seconds: {duration_sec:.1f}\n"
        f"trigger_context: a trusted moderator typed !clip near T+85s of this window\n"
        f"transcript:\n{transcript_excerpt[:6000]}"
    )

    resp = client.models.generate_content(
        model=model,
        contents=[
            types.Part.from_uri(file_uri=uploaded.uri, mime_type=uploaded.mime_type),
            prompt_body,
            user_context,
        ],
        config=types.GenerateContentConfig(
            temperature=0.2,
            response_mime_type="application/json",
        ),
    )
    text = resp.text or ""
    data = json.loads(text)
    return _validate_and_extract(data, duration_sec)


def _validate_and_extract(d: dict[str, Any], duration_sec: float) -> dict[str, Any]:
    """Validate the v1.1 schema shape and lift commonly-used fields to the top."""
    required = {"rule_scores", "weighted_total", "context_summary", "recommended_trim_window"}
    missing = required - set(d.keys())
    if missing:
        raise ValueError(f"substance scorer response missing keys: {missing}")

    rt = d["recommended_trim_window"]
    start = float(rt.get("start_seconds", 0))
    end = float(rt.get("end_seconds", 0))
    dur = end - start
    if dur < 10 or dur > 40:
        raise ValueError(f"substance scorer trim out of 10-40s range: {dur}")
    if start < 0 or end > duration_sec + 0.5:
        raise ValueError(f"trim out of clip bounds [0, {duration_sec}]: [{start}, {end}]")

    # Lift fields the rest of the pipeline needs from nested rule_scores.
    rs = d.get("rule_scores", {})
    rule1 = rs.get("1_peak_moment_clarity", {})
    rule2 = rs.get("2_emotional_arousal", {})
    rule4 = rs.get("4_quotable_memorable_beat", {})
    rule6 = rs.get("6_share_trigger", {})

    d["_extracted"] = {
        "peak_timestamp_seconds": float(rule1.get("peak_timestamp_seconds", duration_sec / 2.0)),
        "peak_emotion": rule2.get("peak_emotion", "mixed"),
        "extractable_element": rule4.get("extractable_element", ""),
        "trigger_type": rule6.get("trigger_type", "none"),
    }
    return d


def score_with_fallback(
    api_key: str,
    video_path: Path,
    prompt_body: str,
    duration_sec: float,
    *,
    model: str = "gemini-2.5-pro",
    streamer: str = "Jordy",
    transcript_excerpt: str = "",
) -> tuple[dict[str, Any], str | None]:
    try:
        return score(
            api_key,
            video_path,
            prompt_body,
            model=model,
            streamer=streamer,
            transcript_excerpt=transcript_excerpt,
            duration_sec=duration_sec,
        ), None
    except Exception as e:  # noqa: BLE001
        log.warning("substance scorer failed, using degraded fallback: %s", e)
        return _degraded(duration_sec), str(e)
