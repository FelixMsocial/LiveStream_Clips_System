"""FFmpeg command builder — one invocation, NVENC encode.

9:16 reframe = blurred scaled background + centered scaled foreground.
Captions are burned in via libass `subtitles=` filter.
Hook overlay (the v1.1 on-video framing line) is composited as a second
`subtitles=` pass with a top-third style and a 0-4s display window.
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


def build_hook_overlay_ass(
    hook_text: str,
    *,
    visible_until_sec: float = 4.0,
    font_name: str = "Inter Black",
    font_size: int = 80,
    margin_v_top: int = 120,
) -> str:
    """Render an ASS file containing a single dialogue line for the hook overlay.

    Top-anchored (Alignment=8 = top-center), bold, with a thick outline so it
    reads on any background. Composes alongside the word-by-word caption ASS
    in a separate `subtitles=` filter pass.
    """
    text = (hook_text or "").strip()
    if not text:
        return ""
    # Hard-wrap to 2 lines if >9 words by inserting an ASS line break at the midpoint word.
    words = text.split()
    if len(words) > 9:
        mid = len(words) // 2
        text = " ".join(words[:mid]) + r"\N" + " ".join(words[mid:])
    # ASS escape commas (style/event field separator).
    text_safe = text.replace(",", "\\,")

    end_h = int(visible_until_sec // 3600)
    end_m = int((visible_until_sec % 3600) // 60)
    end_s = int(visible_until_sec % 60)
    end_cs = int(round((visible_until_sec - int(visible_until_sec)) * 100))
    if end_cs >= 100:
        end_cs = 99
    end_ts = f"{end_h:d}:{end_m:02d}:{end_s:02d}.{end_cs:02d}"

    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: TV.709\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Hook,{font_name},{font_size},"
        "&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,"
        "1,0,0,0,100,100,0,0,1,5,2,8,60,60,"
        f"{margin_v_top},1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        f"Dialogue: 0,0:00:00.00,{end_ts},Hook,,0,0,0,,{text_safe}\n"
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
    hook_overlay_path: Path | None = None,
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

    # Burn-in captions. Style (font, colors, MarginV=576 ≈ 70% from top of
    # the 1920px frame, karaoke palette) is owned by the ASS file produced
    # by deepgram.words_to_ass — no force_style override here.
    if subtitles_path and subtitles_path.exists() and subtitles_path.stat().st_size > 0:
        # Escape backslashes and colons for ffmpeg's filtergraph parser.
        sub_escaped = (
            str(subtitles_path).replace("\\", "/").replace(":", r"\:")
        )
        filter_parts.append(
            f"[{last}]subtitles='{sub_escaped}'[captioned]"
        )
        last = "captioned"

    # Hook overlay (Step 2 v1.1). Burned in as a second subtitles pass so it
    # shares libass with the word-level captions but uses its own top-anchored
    # style and 0-4s display window.
    if (
        hook_overlay_path
        and hook_overlay_path.exists()
        and hook_overlay_path.stat().st_size > 0
    ):
        hook_escaped = (
            str(hook_overlay_path).replace("\\", "/").replace(":", r"\:")
        )
        filter_parts.append(
            f"[{last}]subtitles='{hook_escaped}'[hooked]"
        )
        last = "hooked"

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
