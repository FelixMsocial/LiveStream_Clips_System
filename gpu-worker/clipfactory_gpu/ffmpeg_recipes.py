"""FFmpeg command builder — one invocation, NVENC encode.

9:16 reframe = blurred scaled background + centered scaled foreground.
Captions are burned in via libass `subtitles=` filter.
Hook overlay uses FFmpeg drawtext: white box (box=1, boxcolor=white@0.9,
boxborderw=25), black bold text, centered, full clip duration.
Sponsor overlay (optional) is positioned bottom-right with configurable opacity.
Video sponsor assets can optionally apply chroma-key pre-processing.
"""
from __future__ import annotations

import logging
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


def _wrap_hook_text(text: str) -> str:
    """Split hook into 2-3 lines so drawtext doesn't overflow the frame width."""
    words = text.split()
    n = len(words)
    if n <= 4:
        return text
    if n <= 8:
        mid = (n + 1) // 2
        return " ".join(words[:mid]) + "\n" + " ".join(words[mid:])
    # 3 lines for long hooks
    sz = (n + 2) // 3
    return (
        " ".join(words[:sz]) + "\n"
        + " ".join(words[sz: 2 * sz]) + "\n"
        + " ".join(words[2 * sz:])
    )


def _drawtext_hook_filter(
    label_in: str,
    label_out: str,
    hook_text: str,
    font_path: str,
    *,
    font_size: int = 55,
    margin_top: int = 150,
) -> str:
    """Build one drawtext filtergraph segment for the hook overlay.

    Produces: white box (box=1, boxcolor=white@0.9, boxborderw=25),
    pure-black bold text, horizontally centered, top-anchored.
    Visible for the full clip (no enable= needed since video length is the clip).
    """
    # FFmpeg single-quoted strings cannot contain a literal apostrophe, and
    # backslash-escapes inside them are limited — the cleanest workaround is
    # to swap any apostrophe for a typographic right-single-quote. Visually
    # identical, no escaping required.
    wrapped = _wrap_hook_text(hook_text).replace("'", "’")

    # Filter-arg level-2 escaping (inside single quotes):
    #   backslash → \\  (literal backslash)
    #   colon     → \:  (option separator)
    #   comma     → \,  (filterchain separator at level 1, safe to escape)
    #   newline   → \n  (drawtext interprets the two-char sequence as newline)
    text_esc = (
        wrapped
        .replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )

    # Normalize font path separators and escape the drive-letter colon
    # (`C:/Windows/...` → `C\:/Windows/...`) so the filter parser doesn't
    # treat it as an option separator.
    font_esc = font_path.replace("\\", "/").replace(":", r"\:")

    return (
        f"[{label_in}]drawtext="
        f"fontfile='{font_esc}':"
        f"text='{text_esc}':"
        f"fontcolor=black:"
        f"fontsize={font_size}:"
        f"x=(w-text_w)/2:"
        f"y={margin_top}:"
        f"box=1:"
        f"boxcolor=white@0.9:"
        f"boxborderw=25"
        f"[{label_out}]"
    )


@dataclass(frozen=True)
class SponsorConfig:
    path: Path            # local sponsor asset (PNG/MP4)
    opacity: float = 0.85
    scale_pct: float = 0.15
    position: str = "bottom-right"
    is_video: bool = False
    remove_green: bool = False
    chroma_color: str = "0x00FF00"
    chroma_similarity: float = 0.28
    chroma_blend: float = 0.0


def build_cmd(
    *,
    ffmpeg_bin: str,
    input_path: Path,
    output_path: Path,
    trim_start: float,
    trim_end: float,
    subtitles_path: Path | None,
    sponsor: SponsorConfig | None,
    hook_text: str | None = None,
    hook_font_path: str | None = None,
) -> list[str]:
    duration = max(1.0, trim_end - trim_start)

    inputs: list[str] = ["-y", "-hwaccel", "cuda", "-ss", f"{trim_start:.3f}", "-t", f"{duration:.3f}", "-i", str(input_path)]

    filter_parts: list[str] = []
    # Base 9:16 reframe — blurred bg + centered fg.
    filter_parts.append(
        "[0:v]split=2[a][b];"
        "[a]scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,boxblur=40:1[bg];"
        "[b]scale=1080:-2[fg];"
        "[bg][fg]overlay=0:(H-h)/2[framed]"
    )
    last = "framed"

    # Burn-in captions. Style (font=65pt, PlayRes 1080×1920, MarginV=576 = 30%
    # from bottom, karaoke palette) is owned by the ASS from deepgram.words_to_ass.
    if subtitles_path and subtitles_path.exists() and subtitles_path.stat().st_size > 0:
        # Escape backslashes and colons for ffmpeg's filtergraph parser.
        sub_escaped = (
            str(subtitles_path).replace("\\", "/").replace(":", r"\:")
        )
        filter_parts.append(
            f"[{last}]subtitles='{sub_escaped}'[captioned]"
        )
        last = "captioned"

    # Hook overlay — drawtext with white box background, black bold text,
    # horizontally centered, top-anchored, visible for the full clip duration.
    if hook_text and hook_font_path and Path(hook_font_path).exists():
        filter_parts.append(
            _drawtext_hook_filter(last, "hooked", hook_text, hook_font_path)
        )
        last = "hooked"
    elif hook_text and hook_font_path:
        log.warning("hook font not found at %r — skipping hook overlay", hook_font_path)

    # Sponsor overlay.
    if sponsor is not None:
        if sponsor.is_video:
            inputs += ["-stream_loop", "-1", "-i", str(sponsor.path)]
            filter_parts.append(
                "[1:v]"
                "scale=1000:-1,"
                "format=rgba,"
                f"chromakey={sponsor.chroma_color}:{sponsor.chroma_similarity}:{sponsor.chroma_blend},"
                "geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='if(gt(alpha(X,Y)\\,20)\\,255\\,0)',"
                "eq=saturation=1.06:contrast=1.03:brightness=0.01,"
                "unsharp=3:3:0.50:3:3:0.00,"
                f"colorchannelmixer=aa={sponsor.opacity}[sp]"
            )
            pos = "(W-w)/2:H-h-8+870-90:shortest=1"
        else:
            inputs += ["-i", str(sponsor.path)]
            sw = int(1080 * sponsor.scale_pct)
            sponsor_src = "1:v"

            if sponsor.remove_green:
                prep = ["format=rgba"]
                prep.append(
                    "chromakey="
                    f"{sponsor.chroma_color}:{sponsor.chroma_similarity}:{sponsor.chroma_blend}"
                )
                filter_parts.append(f"[1:v]{','.join(prep)}[spbase]")
                sponsor_src = "spbase"

            filter_parts.append(
                f"[{sponsor_src}]scale={sw}:-1,format=rgba,colorchannelmixer=aa={sponsor.opacity}[sp]"
            )
            pos = {
                "bottom-right": "W-w-40:H-h-40",
                "bottom-left": "40:H-h-40",
                "top-right": "W-w-40:40",
                "top-left": "40:40",
            }.get(sponsor.position, "W-w-40:H-h-40")

        filter_parts.append(f"[{last}][sp]overlay={pos}[outv]")
        last = "outv"

    filter_complex = ";".join(filter_parts)

    encode = [
        "-map", f"[{last}]",
        "-map", "0:a?",
        "-c:v", "h264_nvenc",
        "-preset", "p5",
        "-b:v", "8M",
        "-maxrate", "10M",
        "-bufsize", "12M",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "160k",
        "-movflags", "+faststart",
        str(output_path),
    ]

    return [ffmpeg_bin, *inputs, "-filter_complex", filter_complex, *encode]


def run(cmd: list[str]) -> None:
    log.info("ffmpeg: %s", " ".join(shlex.quote(c) for c in cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed (rc={proc.returncode}): {proc.stderr[-2000:]}"
        )
