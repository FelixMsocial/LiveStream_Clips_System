"""Gemini 2.5 Pro video analysis.

Uploads the raw MP4 via the Files API and asks for the structured JSON defined
in the `gemini_analysis` prompt. Falls back to a degraded midpoint trim on
repeated failure.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from tenacity import retry, stop_after_attempt, wait_fixed

log = logging.getLogger(__name__)

MODEL = "gemini-2.5-pro"


def _degraded(duration_sec: float) -> dict[str, Any]:
    mid = duration_sec / 2.0
    start = max(0.0, mid - 10.0)
    end = start + min(25.0, max(15.0, duration_sec - start))
    return {
        "peak_timestamp_sec": mid,
        "vibe": "unknown",
        "key_elements": [],
        "quotes": [],
        "recommended_trim": {"start_sec": start, "end_sec": end},
        "degraded": True,
    }


@retry(stop=stop_after_attempt(3), wait=wait_fixed(10))
def analyze(
    api_key: str,
    video_path: Path,
    prompt_body: str,
) -> dict[str, Any]:
    # google-genai is the new SDK; imported lazily so tests without it still import.
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    uploaded = client.files.upload(file=str(video_path))
    # Poll until ACTIVE.
    deadline = time.time() + 180
    while uploaded.state.name != "ACTIVE":
        if time.time() > deadline:
            raise TimeoutError("gemini file never became ACTIVE")
        time.sleep(2)
        uploaded = client.files.get(name=uploaded.name)

    resp = client.models.generate_content(
        model=MODEL,
        contents=[
            types.Part.from_uri(file_uri=uploaded.uri, mime_type=uploaded.mime_type),
            prompt_body,
        ],
        config=types.GenerateContentConfig(
            temperature=0.2,
            response_mime_type="application/json",
        ),
    )
    text = resp.text or ""
    data = json.loads(text)
    return _validate(data)


def _validate(d: dict[str, Any]) -> dict[str, Any]:
    required = {"peak_timestamp_sec", "vibe", "recommended_trim"}
    missing = required - set(d.keys())
    if missing:
        raise ValueError(f"gemini response missing keys: {missing}")
    rt = d["recommended_trim"]
    start = float(rt["start_sec"])
    end = float(rt["end_sec"])
    dur = end - start
    if dur < 10 or dur > 40:
        raise ValueError(f"gemini trim out of 10–40s range: {dur}")
    return d


def analyze_with_fallback(
    api_key: str, video_path: Path, prompt_body: str, duration_sec: float
) -> dict[str, Any]:
    try:
        return analyze(api_key, video_path, prompt_body)
    except Exception as e:  # noqa: BLE001
        log.warning("gemini failed, falling back to degraded midpoint trim: %s", e)
        return _degraded(duration_sec)
