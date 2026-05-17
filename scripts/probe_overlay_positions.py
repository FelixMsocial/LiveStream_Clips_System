"""Probe pixel coordinates for the brand-video repositioning math.

Reports:
  1. MP4 pill bounding box (non-green pixels) in source coords. Samples every
     0.5s and reports the union across the whole 11s clip.
  2. Brand-logo PNG: bottom y of the "MojoOnPC" text region by scanning the
     non-transparent pixels and finding where the dense text-stroke band ends.

Run once; use the printed numbers to set BRAND_VIDEO_* constants.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
MP4 = ROOT / "gpu-worker" / "clipfactory_gpu" / "assets" / "brand_video_mojoonpc.mp4"
LOGO = ROOT / "gpu-worker" / "clipfactory_gpu" / "assets" / "brand_logo_mojoonpc.png"
TMP = ROOT / "tmp_probe"


def probe_mp4() -> None:
    TMP.mkdir(exist_ok=True)
    timestamps = [round(t * 0.5, 1) for t in range(0, 22)]  # 0 .. 10.5s
    union = None  # (min_x, min_y, max_x, max_y)
    for t in timestamps:
        out = TMP / f"f_{t:.1f}.png"
        subprocess.run(
            [
                "ffmpeg", "-y", "-ss", f"{t}", "-i", str(MP4),
                "-frames:v", "1", "-update", "1", str(out),
            ],
            check=True, capture_output=True,
        )
        img = Image.open(out).convert("RGB")
        w, h = img.size
        px = img.load()
        bbox = None
        # Coarse scan: stride 4 in both axes for speed; pill is large enough.
        for y in range(0, h, 4):
            for x in range(0, w, 4):
                r, g, b = px[x, y]
                # "Non-green": substantially less green-dominant than the
                # screen color.
                if not (g > 180 and r < 120 and b < 120):
                    if bbox is None:
                        bbox = [x, y, x, y]
                    else:
                        bbox[0] = min(bbox[0], x)
                        bbox[1] = min(bbox[1], y)
                        bbox[2] = max(bbox[2], x)
                        bbox[3] = max(bbox[3], y)
        if bbox is None:
            continue
        if union is None:
            union = bbox[:]
        else:
            union[0] = min(union[0], bbox[0])
            union[1] = min(union[1], bbox[1])
            union[2] = max(union[2], bbox[2])
            union[3] = max(union[3], bbox[3])
        out.unlink()
    print("MP4 pill bbox (union of samples):", union)
    if union:
        cx = (union[0] + union[2]) // 2
        cy = (union[1] + union[3]) // 2
        print(f"  center: ({cx}, {cy})")
        print(f"  size:   {union[2] - union[0]} x {union[3] - union[1]}")


def probe_logo() -> None:
    img = Image.open(LOGO).convert("RGBA")
    w, h = img.size
    px = img.load()
    # For each row, count "high alpha" pixels — these are the visible box +
    # text strokes. We're looking for the y where the "MojoOnPC" text ends
    # (the row count drops below the text-line density once you pass the
    # text baseline, then rises again only for the box bottom border).
    row_density: list[int] = []
    for y in range(h):
        count = 0
        for x in range(w):
            if px[x, y][3] > 200:
                # Distinguish text/icon (bright) from dark box fill.
                r, g, b, _ = px[x, y]
                if r > 180 or g > 180 or b > 180:  # bright stroke
                    count += 1
        row_density.append(count)
    # Print summary every 10 rows to eyeball, and find the last row in the
    # top 60% where density > 5 (= bottom of text/icon strokes).
    cutoff = int(h * 0.6)
    text_bottom = 0
    for y, c in enumerate(row_density[:cutoff]):
        if c > 5:
            text_bottom = y
    print(f"Logo PNG: {w} x {h}")
    print(f"  text/icon stroke region ends at y = {text_bottom}")
    print(f"  empty band below text: y={text_bottom} .. y={h - 1}")
    print(f"  center of empty band:  y = {(text_bottom + h - 1) // 2}")


if __name__ == "__main__":
    probe_mp4()
    print()
    probe_logo()
