"""Deepgram Nova-3 transcription → SRT.

Captions are a nice-to-have. On failure we return an empty SRT and continue.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

URL = "https://api.deepgram.com/v1/listen"


def transcribe(api_key: str, video_path: Path) -> tuple[str, list[dict[str, Any]]]:
    """Returns (srt_text, words)."""
    params = {
        "model": "nova-3",
        "smart_format": "true",
        "punctuate": "true",
        "utterances": "true",
        "word_timestamps": "true",
    }
    headers = {
        "Authorization": f"Token {api_key}",
        "Content-Type": "video/mp4",
    }
    with open(video_path, "rb") as f:
        r = httpx.post(URL, params=params, headers=headers, content=f.read(), timeout=60.0)
    r.raise_for_status()
    data = r.json()
    words = (
        data.get("results", {})
        .get("channels", [{}])[0]
        .get("alternatives", [{}])[0]
        .get("words", [])
    )
    return _words_to_srt(words), words


def transcribe_safe(api_key: str, video_path: Path) -> tuple[str, list[dict[str, Any]]]:
    try:
        return transcribe(api_key, video_path)
    except Exception as e:  # noqa: BLE001
        log.warning("deepgram failed — continuing with empty captions: %s", e)
        return "", []


def _words_to_srt(
    words: list[dict[str, Any]],
    *,
    max_chars: int = 32,
    max_duration: float = 2.5,
) -> str:
    """Rolling cue generator. Matches burn-in style: ≤32 chars or ≤2.5s."""
    if not words:
        return ""
    cues: list[tuple[float, float, str]] = []
    cue_words: list[dict[str, Any]] = []
    cue_start = float(words[0].get("start", 0.0))
    cue_text_len = 0

    def flush(end: float) -> None:
        nonlocal cue_words, cue_start, cue_text_len
        if not cue_words:
            return
        text = " ".join(w.get("punctuated_word") or w["word"] for w in cue_words).strip()
        cues.append((cue_start, end, text))
        cue_words = []
        cue_text_len = 0

    for w in words:
        piece = w.get("punctuated_word") or w["word"]
        add = len(piece) + (1 if cue_text_len else 0)
        w_start = float(w.get("start", cue_start))
        w_end = float(w.get("end", w_start))
        if not cue_words:
            cue_start = w_start
            cue_text_len = 0
        if (
            cue_text_len + add > max_chars
            or (w_end - cue_start) > max_duration
        ):
            flush(float(cue_words[-1].get("end", w_start)))
            cue_start = w_start
            cue_text_len = 0
        cue_words.append(w)
        cue_text_len += add
    if cue_words:
        flush(float(cue_words[-1].get("end", cue_start)))

    out: list[str] = []
    for i, (s, e, t) in enumerate(cues, 1):
        out.append(f"{i}\n{_ts(s)} --> {_ts(e)}\n{t}\n")
    return "\n".join(out)


def _ts(sec: float) -> str:
    if sec < 0:
        sec = 0.0
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    if ms >= 1000:
        ms = 999
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
