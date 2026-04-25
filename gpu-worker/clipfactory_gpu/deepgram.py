"""Deepgram Nova-3 transcription → SRT + ASS (word-by-word karaoke).

Captions are a nice-to-have. On failure we return empty outputs and continue.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)

URL = "https://api.deepgram.com/v1/listen"


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def transcribe(
    api_key: str, media_path: Path, content_type: str = "audio/mp4"
) -> tuple[str, list[dict[str, Any]]]:
    """Returns (srt_text, words). Caller passes content_type matching the file."""
    params = {
        "model": "nova-3",
        "smart_format": "true",
        "punctuate": "true",
        "utterances": "true",
        "word_timestamps": "true",
    }
    headers = {
        "Authorization": f"Token {api_key}",
        "Content-Type": content_type,
    }
    with open(media_path, "rb") as f:
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


def transcribe_safe(
    api_key: str, media_path: Path, content_type: str = "audio/mp4"
) -> tuple[str, list[dict[str, Any]], str | None]:
    try:
        srt, words = transcribe(api_key, media_path, content_type)
        return srt, words, None
    except Exception as e:  # noqa: BLE001
        log.warning("deepgram failed — continuing with empty captions: %s", e)
        return "", [], str(e)


def _words_to_srt(
    words: list[dict[str, Any]],
    *,
    max_chars: int = 32,
    max_duration: float = 2.5,
) -> str:
    """Rolling cue generator for the D1 transcript record. ≤32 chars or ≤2.5s."""
    cues = _group_cues(words, max_chars=max_chars, max_duration=max_duration)
    if not cues:
        return ""
    out: list[str] = []
    for i, cue in enumerate(cues, 1):
        text = " ".join(_word_text(w) for w in cue).strip()
        s = float(cue[0].get("start", 0.0))
        e = float(cue[-1].get("end", s))
        out.append(f"{i}\n{_srt_ts(s)} --> {_srt_ts(e)}\n{text}\n")
    return "\n".join(out)


def words_to_ass(
    words: list[dict[str, Any]],
    *,
    max_chars: int = 32,
    max_duration: float = 2.5,
    font_name: str = "Inter Black",
    font_size: int = 18,
    margin_v: int = 576,
) -> str:
    """Render an ASS subtitle file with per-word karaoke (\\k) timing.

    Each cue is a phrase (≤max_chars / ≤max_duration). Within a cue, every
    word is wrapped in {\\k<centiseconds>} so the active word flips from
    SecondaryColour (yellow) to PrimaryColour (white) as it is spoken.
    """
    cues = _group_cues(words, max_chars=max_chars, max_duration=max_duration)
    if not cues:
        return ""

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: TV.709\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font_name},{font_size},"
        "&H00FFFFFF,&H0000FFFF,&H00000000,&H00000000,"
        "1,0,0,0,100,100,0,0,1,3,0,2,40,40,"
        f"{margin_v},1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )

    events: list[str] = []
    for cue in cues:
        cue_start = float(cue[0].get("start", 0.0))
        cue_end = float(cue[-1].get("end", cue_start))
        text = _karaoke_text(cue, cue_start)
        events.append(
            f"Dialogue: 0,{_ass_ts(cue_start)},{_ass_ts(cue_end)},"
            f"Default,,0,0,0,,{text}"
        )
    return header + "\n".join(events) + "\n"


def _group_cues(
    words: list[dict[str, Any]],
    *,
    max_chars: int,
    max_duration: float,
) -> list[list[dict[str, Any]]]:
    if not words:
        return []
    cues: list[list[dict[str, Any]]] = []
    cue: list[dict[str, Any]] = []
    cue_start = float(words[0].get("start", 0.0))
    cue_text_len = 0
    for w in words:
        piece = _word_text(w)
        add = len(piece) + (1 if cue_text_len else 0)
        w_start = float(w.get("start", cue_start))
        w_end = float(w.get("end", w_start))
        if not cue:
            cue_start = w_start
            cue_text_len = 0
        if cue and (
            cue_text_len + add > max_chars
            or (w_end - cue_start) > max_duration
        ):
            cues.append(cue)
            cue = []
            cue_start = w_start
            cue_text_len = 0
        cue.append(w)
        cue_text_len += add
    if cue:
        cues.append(cue)
    return cues


def _karaoke_text(cue_words: list[dict[str, Any]], cue_start: float) -> str:
    """Build the karaoke-tagged dialogue text for one cue.

    Each word's \\k duration covers (word.end - prev_end) so silence/gaps
    between words are absorbed into the next word's highlight window.
    """
    parts: list[str] = []
    prev_end = cue_start
    for i, w in enumerate(cue_words):
        w_end = float(w.get("end", prev_end))
        dur_cs = max(1, int(round((w_end - prev_end) * 100)))
        word = _word_text(w)
        space = " " if i > 0 else ""
        parts.append(f"{space}{{\\k{dur_cs}}}{word}")
        prev_end = w_end
    return "".join(parts)


def _word_text(w: dict[str, Any]) -> str:
    return w.get("punctuated_word") or w.get("word", "")


def _srt_ts(sec: float) -> str:
    if sec < 0:
        sec = 0.0
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    if ms >= 1000:
        ms = 999
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _ass_ts(sec: float) -> str:
    if sec < 0:
        sec = 0.0
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    cs = int(round((sec - int(sec)) * 100))
    if cs >= 100:
        cs = 99
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"
