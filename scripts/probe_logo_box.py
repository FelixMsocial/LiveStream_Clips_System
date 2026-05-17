"""Find the dark rounded rectangle's inner bounds in brand_logo_mojoonpc.png.

We want the band where the box is opaque-and-dark (the visible chrome), not
the glow padding around it. Reports inner top/bottom and bright-stroke bottom
in PNG coordinates plus their projection into the 1920-px output frame.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image

LOGO = (
    Path(__file__).resolve().parent.parent
    / "gpu-worker" / "clipfactory_gpu" / "assets" / "brand_logo_mojoonpc.png"
)

# Final-frame placement parameters (must match ffmpeg_recipes.py constants).
TARGET_WIDTH = 960
BAND_CENTER_Y = 1765


def main() -> None:
    img = Image.open(LOGO).convert("RGBA")
    w, h = img.size
    px = img.load()

    # For each row, count "fully opaque" pixels (the visible chrome) and
    # "bright stroke" pixels (text/icons that draw on top of the chrome).
    opaque_count = []
    bright_count = []
    for y in range(h):
        oc = bc = 0
        for x in range(w):
            r, g, b, a = px[x, y]
            if a >= 240:
                oc += 1
                if r > 180 or g > 180 or b > 180:
                    bc += 1
        opaque_count.append(oc)
        bright_count.append(bc)

    threshold = max(opaque_count) // 2
    inner_top = next(y for y in range(h) if opaque_count[y] > threshold)
    inner_bottom = next(y for y in range(h - 1, -1, -1) if opaque_count[y] > threshold)
    bright_bottom_in_text = next(
        y for y in range(int(h * 0.6), -1, -1) if bright_count[y] > 5
    )

    print(f"Logo PNG: {w}x{h}")
    print(f"  inner box top y    = {inner_top}")
    print(f"  inner box bottom y = {inner_bottom}")
    print(f"  text bright bottom = {bright_bottom_in_text}")
    empty_top = bright_bottom_in_text
    empty_bot = inner_bottom
    empty_center = (empty_top + empty_bot) // 2
    print(f"  empty band         = y {empty_top}..{empty_bot} (center {empty_center})")

    scale = TARGET_WIDTH / w
    scaled_h = int(h * scale)
    frame_top = BAND_CENTER_Y - scaled_h // 2
    print(
        f"\nScaled into frame (TARGET_WIDTH={TARGET_WIDTH}, "
        f"BAND_CENTER_Y={BAND_CENTER_Y}):"
    )
    print(f"  scale factor         = {scale:.4f}")
    print(f"  scaled height        = {scaled_h}")
    print(f"  scaled box top y     = {frame_top + int(inner_top * scale)}")
    print(f"  scaled box bottom y  = {frame_top + int(inner_bottom * scale)}")
    print(f"  scaled text bottom   = {frame_top + int(bright_bottom_in_text * scale)}")
    print(f"  scaled empty center  = {frame_top + int(empty_center * scale)}")


if __name__ == "__main__":
    main()
