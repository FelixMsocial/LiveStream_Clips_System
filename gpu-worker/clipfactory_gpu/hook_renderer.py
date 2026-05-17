"""Pillow-based hook overlay renderer.

Produces a transparent PNG with a rounded white background and centered
black bold text. Mixes a Latin text font (Arial Bold / Liberation Sans
Bold) with a color emoji font (Segoe UI Emoji on Windows, Noto Color
Emoji on Linux) so emoji codepoints render properly instead of as
missing-glyph "tofu" boxes.

The PNG is then composited over the base video by FFmpeg's `overlay`
filter, sidestepping all of drawtext's escaping limitations and
single-font constraints.
"""
from __future__ import annotations

import logging
import re
import sys
import unicodedata
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

log = logging.getLogger(__name__)


# Drop only the things no font can render: surrogates, private use,
# unassigned codepoints. Emoji (Symbol categories) are now KEPT and
# routed to the emoji font during draw.
_HOOK_STRIP_CATS = frozenset({"Cs", "Co", "Cn"})

# Collapse literal escape sequences and any whitespace run to one space.
_HOOK_WS_RE = re.compile(r"(?:\\[nNrt])|\s+")


def _is_emoji_codepoint(ch: str) -> bool:
    """True for codepoints that should be drawn with the emoji font.

    Covers the standard emoji blocks plus the joiners/variation selectors
    that bind multi-codepoint emoji sequences (e.g. flags, family ZWJ).
    """
    code = ord(ch)
    return (
        0x2600 <= code <= 0x27BF        # Misc Symbols + Dingbats
        or 0x1F000 <= code <= 0x1FAFF   # Emoji blocks (Pictographs, Emoticons, etc.)
        or 0x1F1E6 <= code <= 0x1F1FF   # Regional Indicator (flags)
        or code == 0xFE0F                # Variation Selector-16 (emoji style)
        or code == 0x200D                # Zero-Width Joiner
    )


def _split_runs(line: str) -> list[tuple[bool, str]]:
    """Split a line into alternating (is_emoji, segment) runs."""
    if not line:
        return []
    runs: list[tuple[bool, str]] = []
    cur_is_emoji = _is_emoji_codepoint(line[0])
    cur = line[0]
    for ch in line[1:]:
        is_emoji = _is_emoji_codepoint(ch)
        if is_emoji == cur_is_emoji:
            cur += ch
        else:
            runs.append((cur_is_emoji, cur))
            cur = ch
            cur_is_emoji = is_emoji
    runs.append((cur_is_emoji, cur))
    return runs


def _seg_width(font: ImageFont.FreeTypeFont, seg: str) -> int:
    try:
        return int(font.getlength(seg))
    except Exception:
        bbox = font.getbbox(seg)
        return int(bbox[2] - bbox[0])


def _measure_line(
    line: str,
    text_font: ImageFont.FreeTypeFont,
    emoji_font: ImageFont.FreeTypeFont | None,
    emoji_scale: float,
) -> int:
    """Pixel width of a line, summing per-run widths with the right font."""
    if not line:
        return 0
    total = 0
    for is_emoji, seg in _split_runs(line):
        if is_emoji and emoji_font is not None:
            total += int(_seg_width(emoji_font, seg) * emoji_scale)
        else:
            total += _seg_width(text_font, seg)
    return total


def _force_break_word(
    word: str,
    text_font: ImageFont.FreeTypeFont,
    emoji_font: ImageFont.FreeTypeFont | None,
    emoji_scale: float,
    max_width: int,
) -> list[str]:
    """Split a single overlong word into chunks that each fit within max_width.

    Walks the word character-by-character and starts a new chunk whenever
    appending the next char would exceed max_width. Guarantees no chunk
    is wider than max_width — guards against an unbroken URL or token
    that would otherwise force the box wider than the video frame.
    """
    chunks: list[str] = []
    cur = ""
    for ch in word:
        candidate = cur + ch
        if _measure_line(candidate, text_font, emoji_font, emoji_scale) <= max_width:
            cur = candidate
        else:
            if cur:
                chunks.append(cur)
            cur = ch
    if cur:
        chunks.append(cur)
    return chunks or [word]


def _wrap_to_pixels(
    cleaned: str,
    text_font: ImageFont.FreeTypeFont,
    emoji_font: ImageFont.FreeTypeFont | None,
    emoji_scale: float,
    max_width: int,
) -> list[str]:
    """Greedy word wrap by actual rendered pixel width.

    Single words wider than max_width are character-broken so no rendered
    line ever exceeds max_width — keeps the resulting PNG narrower than
    the video frame so FFmpeg's `(W-w)/2` overlay centering stays positive.
    """
    if not cleaned:
        return []
    lines: list[str] = []
    cur = ""
    for word in cleaned.split(" "):
        if not word:
            continue
        if _measure_line(word, text_font, emoji_font, emoji_scale) > max_width:
            # Flush whatever's accumulated, then emit char-broken chunks.
            if cur:
                lines.append(cur)
                cur = ""
            chunks = _force_break_word(
                word, text_font, emoji_font, emoji_scale, max_width
            )
            lines.extend(chunks[:-1])
            cur = chunks[-1]
            continue
        candidate = word if not cur else f"{cur} {word}"
        if _measure_line(candidate, text_font, emoji_font, emoji_scale) <= max_width:
            cur = candidate
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def _truncate_lines(
    lines: list[str],
    max_lines: int,
    text_font: ImageFont.FreeTypeFont,
    emoji_font: ImageFont.FreeTypeFont | None,
    emoji_scale: float,
    max_width: int,
) -> list[str]:
    """Trim to max_lines, ellipsizing the last line so cut content is signalled."""
    result = list(lines[: max_lines - 1])
    last = lines[max_lines - 1]
    words = last.split(" ")
    while words:
        candidate = " ".join(words) + "…"
        if _measure_line(candidate, text_font, emoji_font, emoji_scale) <= max_width:
            result.append(candidate)
            return result
        words.pop()
    result.append("…")
    return result


def _normalize(text: str) -> str:
    """Strip unrenderable categories and collapse whitespace/escape literals."""
    keep = "".join(c for c in text if unicodedata.category(c) not in _HOOK_STRIP_CATS)
    return _HOOK_WS_RE.sub(" ", keep).strip()


def _load_text_font(path: str, size: int) -> ImageFont.FreeTypeFont | None:
    try:
        return ImageFont.truetype(path, size)
    except OSError as e:
        log.warning("hook text font load failed at %r: %s", path, e)
        return None


def _load_emoji_font(
    path: str, target_size: int
) -> tuple[ImageFont.FreeTypeFont | None, float]:
    """Load emoji font, falling back to the fixed-size Noto Color Emoji.

    Returns (font, scale) where scale is the factor to apply to widths and
    rendered glyphs so a fixed-size emoji bitmap aligns visually with the
    target text font size. For scalable emoji fonts (Segoe UI Emoji) the
    scale is 1.0.
    """
    try:
        return ImageFont.truetype(path, target_size), 1.0
    except OSError:
        # NotoColorEmoji ships only at size 109 (CBDT bitmap).
        try:
            big = ImageFont.truetype(path, 109)
            return big, target_size / 109.0
        except OSError as e:
            log.warning("hook emoji font load failed at %r: %s", path, e)
            return None, 1.0


def render_hook_png(
    text: str,
    output_path: Path,
    *,
    text_font_path: str,
    emoji_font_path: str | None = None,
    font_size: int = 55,
    box_width: int = 990,
    max_lines: int = 2,
    pad_x: int = 28,
    pad_y: int = 18,
    corner_radius: int = 24,
    line_spacing: int = 10,
    bg_rgba: tuple[int, int, int, int] = (255, 255, 255, 255),
    text_rgba: tuple[int, int, int, int] = (0, 0, 0, 255),
) -> tuple[int, int] | None:
    """Render the hook to a transparent PNG: fixed-width white rounded rect,
    drop shadow, bold black text, capped at max_lines with ellipsis.

    Returns the rendered (width, height) in pixels on success, or None if
    there was nothing to render or the text font could not be loaded.
    The PNG is wider than box_width by the shadow spread so the white box
    left edge sits exactly at the FFmpeg overlay x offset (45px).
    """
    cleaned = _normalize(text)
    if not cleaned:
        return None

    text_font = _load_text_font(text_font_path, font_size)
    if text_font is None:
        return None

    emoji_font: ImageFont.FreeTypeFont | None = None
    emoji_scale = 1.0
    if emoji_font_path and Path(emoji_font_path).exists():
        emoji_font, emoji_scale = _load_emoji_font(emoji_font_path, font_size)

    max_text_width = box_width - 2 * pad_x
    lines = _wrap_to_pixels(cleaned, text_font, emoji_font, emoji_scale, max_text_width)
    if not lines:
        return None
    if len(lines) > max_lines:
        lines = _truncate_lines(lines, max_lines, text_font, emoji_font, emoji_scale, max_text_width)

    line_widths = [_measure_line(line, text_font, emoji_font, emoji_scale) for line in lines]

    asc, desc = text_font.getmetrics()
    line_h = asc + desc
    block_h = line_h * len(lines) + line_spacing * max(0, len(lines) - 1)
    box_h = block_h + 2 * pad_y

    # Shadow: offset (right, down) + gaussian blur.  The PNG is enlarged by
    # the shadow spread so nothing is clipped; the white box still starts at
    # (0, 0) matching the FFmpeg overlay x=45 anchor.
    _SH_X = 3
    _SH_Y = 5
    _SH_BLUR = 8
    _SH_ALPHA = 60

    extra_w = _SH_X + _SH_BLUR * 2
    extra_h = _SH_Y + _SH_BLUR * 2
    img_w = box_width + extra_w
    img_h = box_h + extra_h

    shadow_layer = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow_layer)
    shadow_draw.rounded_rectangle(
        [(_SH_X, _SH_Y), (_SH_X + box_width - 1, _SH_Y + box_h - 1)],
        radius=corner_radius,
        fill=(0, 0, 0, _SH_ALPHA),
    )
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=_SH_BLUR))

    img = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
    img.alpha_composite(shadow_layer)
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        [(0, 0), (box_width - 1, box_h - 1)],
        radius=corner_radius,
        fill=bg_rgba,
    )

    # Center each line within the white box (box_width), not the full image.
    y = pad_y
    for line, lw in zip(lines, line_widths):
        x = (box_width - lw) // 2
        _draw_line(img, draw, x, y, line, text_font, emoji_font, emoji_scale, text_rgba)
        y += line_h + line_spacing

    img.save(output_path, "PNG")
    return (img_w, img_h)


def _draw_line(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    line: str,
    text_font: ImageFont.FreeTypeFont,
    emoji_font: ImageFont.FreeTypeFont | None,
    emoji_scale: float,
    text_rgba: tuple[int, int, int, int],
) -> None:
    for is_emoji, seg in _split_runs(line):
        if is_emoji and emoji_font is not None:
            seg_w = _seg_width(emoji_font, seg)
            if emoji_scale == 1.0:
                # Scalable color emoji (Segoe UI Emoji): draw directly with
                # embedded_color so colors come from the font's CBDT/COLR tables.
                try:
                    draw.text((x, y), seg, font=emoji_font, embedded_color=True)
                except Exception:
                    draw.text((x, y), seg, font=emoji_font, fill=text_rgba)
                x += seg_w
            else:
                # Fixed-size emoji (Noto Color Emoji at 109px) — render to a
                # scratch image, then resize down to match the text font.
                tile = Image.new("RGBA", (max(1, seg_w), 109), (0, 0, 0, 0))
                tile_draw = ImageDraw.Draw(tile)
                try:
                    tile_draw.text((0, 0), seg, font=emoji_font, embedded_color=True)
                except Exception:
                    tile_draw.text((0, 0), seg, font=emoji_font, fill=text_rgba)
                scaled_w = max(1, int(seg_w * emoji_scale))
                scaled_h = max(1, int(109 * emoji_scale))
                tile = tile.resize((scaled_w, scaled_h), Image.LANCZOS)
                # Vertically align emoji baseline to text baseline (rough).
                asc, _ = text_font.getmetrics()
                paste_y = y + asc - scaled_h
                img.alpha_composite(tile, (x, max(y, paste_y)))
                x += scaled_w
        else:
            draw.text((x, y), seg, font=text_font, fill=text_rgba)
            x += _seg_width(text_font, seg)


def default_emoji_font_path() -> str:
    """Best-effort detection of a color emoji font on the host OS."""
    if sys.platform == "win32":
        win = Path(r"C:\Windows\Fonts\seguiemj.ttf")
        return str(win) if win.exists() else ""
    candidates = [
        "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
        "/usr/share/fonts/truetype/twemoji/TwitterColorEmoji-SVGinOT.ttf",
    ]
    for p in candidates:
        if Path(p).exists():
            return p
    return ""
