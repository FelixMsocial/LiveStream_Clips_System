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
import math
import re
import time
from pathlib import Path
from typing import Any

from tenacity import RetryError, retry, stop_after_attempt, wait_fixed

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
        "recommended_crop": {
            "horizontal_focus": 0.5,
            "rationale": "degraded fallback",
        },
        "rulebook_version": "1.2-degraded",
        "_extracted": {
            "peak_timestamp_seconds": mid,
            "peak_emotion": "low_arousal",
            "extractable_element": "",
            "trigger_type": "none",
            "horizontal_focus": 0.5,
        },
        "degraded": True,
    }


def _extract_json(text: str) -> dict[str, Any]:
    s = (text or "").strip()
    if not s:
        raise ValueError("substance scorer returned empty response text")

    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*\n?", "", s)
        s = re.sub(r"\n?```\s*$", "", s)

    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", s)
        if not m:
            raise
        data = json.loads(m.group(0))

    if not isinstance(data, dict):
        raise ValueError("substance scorer response must be a JSON object")
    return data


def _to_float(value: Any, *, field: str) -> float:
    try:
        out = float(value)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"{field} is not numeric: {value!r}") from e
    if not math.isfinite(out):
        raise ValueError(f"{field} is not finite: {value!r}")
    return out


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _normalize_trim_window(
    start: float,
    end: float,
    *,
    duration_sec: float,
    peak_ts: float,
) -> tuple[float, float, bool]:
    """Return a bounds-safe 10-40s trim window, preserving center when possible."""
    orig_start = start
    orig_end = end

    if end < start:
        start, end = end, start

    max_dur = max(10.0, min(40.0, duration_sec))
    dur = end - start
    center = _clamp(peak_ts, 0.0, duration_sec)
    if dur >= 10.0 and dur <= 40.0:
        center = (start + end) / 2.0
    target_dur = _clamp(dur if dur > 0 else 25.0, 10.0, max_dur)

    half = target_dur / 2.0
    start = center - half
    end = center + half

    if start < 0:
        end -= start
        start = 0.0
    if end > duration_sec:
        start -= (end - duration_sec)
        end = duration_sec
    start = max(0.0, start)
    end = min(duration_sec, end)

    if end - start < 10.0 and duration_sec >= 10.0:
        end = min(duration_sec, start + 10.0)
        start = max(0.0, end - 10.0)

    changed = abs(start - orig_start) > 0.01 or abs(end - orig_end) > 0.01
    return start, end, changed


def _root_error_message(err: Exception) -> str:
    if isinstance(err, RetryError):
        try:
            cause = err.last_attempt.exception()
        except Exception:  # noqa: BLE001
            cause = None
        if cause:
            return f"{type(cause).__name__}: {cause}"
    return f"{type(err).__name__}: {err}"


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
        "output_constraint: recommended_trim_window must be 10-40 seconds and inside clip bounds\n"
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
    data = _extract_json(text)
    return _validate_and_extract(data, duration_sec)


def _validate_and_extract(d: dict[str, Any], duration_sec: float) -> dict[str, Any]:
    """Validate the v1.1 schema shape and lift commonly-used fields to the top."""
    required = {"rule_scores", "weighted_total", "context_summary", "recommended_trim_window"}
    missing = required - set(d.keys())
    if missing:
        raise ValueError(f"substance scorer response missing keys: {missing}")

    rs = d.get("rule_scores", {}) or {}
    rule1 = rs.get("1_peak_moment_clarity", {}) or {}
    peak_ts = _to_float(
        rule1.get("peak_timestamp_seconds", duration_sec / 2.0),
        field="rule_scores.1_peak_moment_clarity.peak_timestamp_seconds",
    )

    rt = d["recommended_trim_window"]
    if not isinstance(rt, dict):
        raise ValueError("recommended_trim_window must be a JSON object")
    start = _to_float(rt.get("start_seconds", 0), field="recommended_trim_window.start_seconds")
    end = _to_float(rt.get("end_seconds", 0), field="recommended_trim_window.end_seconds")
    start, end, changed = _normalize_trim_window(
        start,
        end,
        duration_sec=duration_sec,
        peak_ts=peak_ts,
    )
    if changed:
        log.warning(
            "substance scorer returned invalid trim; normalized [%.3f, %.3f] -> [%.3f, %.3f]",
            _to_float(rt.get("start_seconds", 0), field="recommended_trim_window.start_seconds"),
            _to_float(rt.get("end_seconds", 0), field="recommended_trim_window.end_seconds"),
            start,
            end,
        )
    rt["start_seconds"] = round(start, 3)
    rt["end_seconds"] = round(end, 3)

    # Lift fields the rest of the pipeline needs from nested rule_scores.
    rule2 = rs.get("2_emotional_arousal", {})
    rule4 = rs.get("4_quotable_memorable_beat", {})
    rule6 = rs.get("6_share_trigger", {})

    # Optional crop hint — never raise; bad values fall back to 0.5 (current
    # center-crop behavior). Canonicalize on the returned blob so D1 stores
    # the clamped value.
    rc = d.get("recommended_crop")
    if not isinstance(rc, dict):
        rc = {"horizontal_focus": 0.5, "rationale": "missing; defaulted to center"}
        d["recommended_crop"] = rc
    try:
        focus = _to_float(rc.get("horizontal_focus", 0.5), field="recommended_crop.horizontal_focus")
        focus = _clamp(focus, 0.0, 1.0)
    except ValueError as e:
        log.warning("substance scorer returned bad horizontal_focus; defaulting to 0.5: %s", e)
        focus = 0.5
    rc["horizontal_focus"] = round(focus, 4)

    d["_extracted"] = {
        "peak_timestamp_seconds": peak_ts,
        "peak_emotion": rule2.get("peak_emotion", "mixed"),
        "extractable_element": rule4.get("extractable_element", ""),
        "trigger_type": rule6.get("trigger_type", "none"),
        "horizontal_focus": focus,
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
        detail = _root_error_message(e)
        log.warning("substance scorer failed, using degraded fallback: %s", detail)
        return _degraded(duration_sec), detail
