"""FFmpeg command builder — one invocation, NVENC encode.

9:16 reframe = blurred scaled background + centered scaled foreground.
Captions are burned in via libass `subtitles=` filter.
Hook overlay is pre-rendered to a PNG by hook_renderer (Pillow) — rounded
white box, black bold text, mixed Latin + color-emoji fonts — then
composited via FFmpeg `overlay`. Sponsor overlay (optional) is positioned
bottom-right with configurable opacity. Video sponsor assets can optionally
apply chroma-key pre-processing.
"""
from __future__ import annotations

import logging
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .hook_renderer import render_hook_png

log = logging.getLogger(__name__)


# Vertical position of the hook PNG from the top of the 1920px frame.
HOOK_TOP_MARGIN = 155

# Brand logo overlay sits centered in the bottom blurred band. The foreground
# video occupies y=285..1610 (1325px tall, see the framed filter), so the
# blurred band underneath spans y=1610..1920 — center at y=1765.
BRAND_LOGO_TARGET_WIDTH = 1164    # ~42px overflow each side (clips corner tips)
BRAND_LOGO_BAND_CENTER_Y = 1765

# Animated MP4 brand overlay. Authored full-frame (1080x1920) with a bright
# green background; we scale + offset so the inner pill lands inside the
# brand-logo box, below the "MojoOnPC" text. Derivation (see scripts/
# probe_overlay_positions.py and probe_logo_box.py):
#   - Source pill center in MP4: (540, 958), size 408x108
#   - Scale 0.9028 (~9.7% from native) → scaled MP4 975x1733, pill center (488, 865)
#   - Logo box at 1164 wide spans frame y≈1593..1938; MojoOnPC text bottom
#     sits at frame y≈1757, box visible bottom at y≈1888
#   - Pill centered between text bottom and box bottom: y=1822
#   - Overlay offsets: x = (1080 - 975)/2 ≈ 53, y = 1822 - 865 = 957
BRAND_VIDEO_WIDTH = 975           # ~9.7% reduction from native 1080
BRAND_VIDEO_X = 53                # overlay x offset (keeps MP4 centered)
BRAND_VIDEO_Y = 957               # overlay y offset (pill center at y≈1822)
BRAND_VIDEO_CHROMA_COLOR = "0x00FF00"
BRAND_VIDEO_CHROMA_SIMILARITY = 0.30
BRAND_VIDEO_CHROMA_BLEND = 0.05
# Despill removes residual green tint left behind by chromakey on
# anti-aliased edges. mix=1.0 fully neutralizes; expand widens the
# detection range.
BRAND_VIDEO_DESPILL_MIX = 0.50
BRAND_VIDEO_DESPILL_EXPAND = 0.20


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
    hook_emoji_font_path: str | None = None,
    brand_logo_path: Path | None = None,
    brand_video_path: Path | None = None,
) -> list[str]:
    duration = max(1.0, trim_end - trim_start)

    inputs: list[str] = ["-y", "-hwaccel", "cuda", "-ss", f"{trim_start:.3f}", "-t", f"{duration:.3f}", "-i", str(input_path)]
    next_input = 1  # 0 is the main video

    filter_parts: list[str] = []
    # Base 9:16 reframe — blurred bg + centered fg.
    filter_parts.append(
        "[0:v]split=2[a][b];"
        "[a]scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,boxblur=40:1,eq=brightness=-0.08:contrast=1.05[bg];"
        "[b]scale=1080:1325:force_original_aspect_ratio=increase,"
        "crop=1080:1325[fg];"
        "[bg][fg]overlay=0:285[framed]"
    )
    last = "framed"

    # Burn-in captions. Style (font=65pt, PlayRes 1080×1920, MarginV=360,
    # karaoke palette) is owned by the ASS from deepgram.words_to_ass.
    if subtitles_path and subtitles_path.exists() and subtitles_path.stat().st_size > 0:
        # Escape backslashes and colons for ffmpeg's filtergraph parser.
        sub_escaped = (
            str(subtitles_path).replace("\\", "/").replace(":", r"\:")
        )
        filter_parts.append(
            f"[{last}]subtitles='{sub_escaped}'[captioned]"
        )
        last = "captioned"

    # Sponsor overlay (input added first so we keep its existing index
    # behavior; also lets the hook PNG sit on top in the filter chain).
    if sponsor is not None:
        sponsor_idx = next_input
        if sponsor.is_video:
            inputs += ["-stream_loop", "-1", "-i", str(sponsor.path)]
            filter_parts.append(
                f"[{sponsor_idx}:v]"
                "scale=1000:-1,"
                "format=rgba,"
                f"chromakey={sponsor.chroma_color}:{sponsor.chroma_similarity}:{sponsor.chroma_blend},"
                "geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='if(gt(alpha(X,Y)\\,20)\\,255\\,0)',"
                "eq=saturation=1.06:contrast=1.03:brightness=0.01,"
                "unsharp=3:3:0.50:3:3:0.00,"
                f"colorchannelmixer=aa={sponsor.opacity}[sp]"
            )
            pos = "(W-w)/2:1655:shortest=1"
        else:
            inputs += ["-i", str(sponsor.path)]
            sw = int(1080 * sponsor.scale_pct)
            sponsor_src = f"{sponsor_idx}:v"

            if sponsor.remove_green:
                prep = ["format=rgba"]
                prep.append(
                    "chromakey="
                    f"{sponsor.chroma_color}:{sponsor.chroma_similarity}:{sponsor.chroma_blend}"
                )
                filter_parts.append(f"[{sponsor_idx}:v]{','.join(prep)}[spbase]")
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

        filter_parts.append(f"[{last}][sp]overlay={pos}[sponsored]")
        last = "sponsored"
        next_input += 1

    # Hook overlay — Pillow renders a transparent PNG (rounded white box,
    # black bold text, color emoji), then FFmpeg overlays it. This bypasses
    # drawtext's escaping limits and lets us mix Latin + emoji fonts.
    if hook_text and hook_font_path and Path(hook_font_path).exists():
        hook_png = output_path.with_name("hook.png")
        try:
            rendered = render_hook_png(
                hook_text,
                hook_png,
                text_font_path=hook_font_path,
                emoji_font_path=hook_emoji_font_path or None,
                font_size=55,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("hook PNG render failed: %s — skipping hook overlay", e)
            rendered = None
        if rendered is not None:
            hook_idx = next_input
            inputs += ["-i", str(hook_png)]
            next_input += 1
            filter_parts.append(
                f"[{last}][{hook_idx}:v]overlay=x=45:y={HOOK_TOP_MARGIN}[hooked]"
            )
            last = "hooked"
    elif hook_text and hook_font_path:
        log.warning("hook font not found at %r — skipping hook overlay", hook_font_path)

    # Brand logo overlay — centered horizontally and vertically inside the
    # bottom blurred band. Source PNG is expected to be pre-keyed (transparent
    # background), so we just scale and composite.
    if brand_logo_path and Path(brand_logo_path).exists():
        brand_idx = next_input
        inputs += ["-i", str(brand_logo_path)]
        next_input += 1
        filter_parts.append(
            f"[{brand_idx}:v]scale={BRAND_LOGO_TARGET_WIDTH}:-1,format=rgba[brand]"
        )
        filter_parts.append(
            f"[{last}][brand]overlay=(W-w)/2:{BRAND_LOGO_BAND_CENTER_Y}-h/2[branded]"
        )
        last = "branded"
    elif brand_logo_path:
        log.warning("brand logo not found at %r — skipping brand overlay", brand_logo_path)

    # Animated MP4 brand overlay — rendered last so it sits on top of every
    # other layer. Looped via -stream_loop so its 11s asset covers longer
    # clips; shortest=1 on the overlay ends output with the main video.
    if brand_video_path and Path(brand_video_path).exists():
        bv_idx = next_input
        inputs += ["-stream_loop", "-1", "-i", str(brand_video_path)]
        next_input += 1
        scale = (
            f"scale={BRAND_VIDEO_WIDTH}:-1," if BRAND_VIDEO_WIDTH > 0 else ""
        )
        filter_parts.append(
            f"[{bv_idx}:v]"
            f"{scale}"
            "format=rgba,"
            f"chromakey={BRAND_VIDEO_CHROMA_COLOR}:"
            f"{BRAND_VIDEO_CHROMA_SIMILARITY}:{BRAND_VIDEO_CHROMA_BLEND},"
            f"despill=type=green:mix={BRAND_VIDEO_DESPILL_MIX}:"
            f"expand={BRAND_VIDEO_DESPILL_EXPAND}"
            "[bv]"
        )
        filter_parts.append(
            f"[{last}][bv]overlay={BRAND_VIDEO_X}:{BRAND_VIDEO_Y}:shortest=1[bvout]"
        )
        last = "bvout"
    elif brand_video_path:
        log.warning(
            "brand video not found at %r — skipping brand video overlay", brand_video_path
        )

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
