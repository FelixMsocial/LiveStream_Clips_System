"""One-shot: strip the green background from the MojoOnPC brand logo PNG.

Reads the source PNG, samples the top-left pixel as the chroma color, sets
alpha to 0 for any pixel within a Euclidean distance threshold, and writes a
transparent PNG to the package assets directory. Re-run if the source asset
is replaced.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageFilter


SRC = Path(__file__).resolve().parent.parent / "Live-stream Logo-MojoOnPc_2.png"
DST = (
    Path(__file__).resolve().parent.parent
    / "gpu-worker"
    / "clipfactory_gpu"
    / "assets"
    / "brand_logo_mojoonpc.png"
)

# Inside the threshold = fully transparent. Between thresholds = feathered
# alpha so anti-aliased edges blend cleanly against the blurred background.
HARD_DIST = 90.0
SOFT_DIST = 150.0

# Soft drop shadow baked into the transparent PNG so the box lifts off busy
# or dark blurred backgrounds. Matches the hook_renderer shadow recipe at a
# slightly larger spread for the wider banner.
SHADOW_OFFSET_X = 0
SHADOW_OFFSET_Y = 4
SHADOW_BLUR = 10
SHADOW_ALPHA = 90


def main() -> int:
    if not SRC.exists():
        print(f"source not found: {SRC}", file=sys.stderr)
        return 1

    img = Image.open(SRC).convert("RGBA")
    px = img.load()
    w, h = img.size

    # Sample the four corners and average — robust to one-pixel speckle.
    samples = [px[0, 0], px[w - 1, 0], px[0, h - 1], px[w - 1, h - 1]]
    kr = sum(s[0] for s in samples) / len(samples)
    kg = sum(s[1] for s in samples) / len(samples)
    kb = sum(s[2] for s in samples) / len(samples)
    print(f"chroma key sampled (avg corners): rgb=({kr:.0f}, {kg:.0f}, {kb:.0f})")

    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            d = ((r - kr) ** 2 + (g - kg) ** 2 + (b - kb) ** 2) ** 0.5
            if d <= HARD_DIST:
                px[x, y] = (r, g, b, 0)
            elif d < SOFT_DIST:
                # Linear feather between HARD_DIST and SOFT_DIST.
                t = (d - HARD_DIST) / (SOFT_DIST - HARD_DIST)
                px[x, y] = (r, g, b, int(a * t))

    # Composite: shadow layer underneath, original keyed logo on top. The
    # canvas grows by the shadow spread so nothing gets clipped.
    pad_l = max(0, SHADOW_BLUR - SHADOW_OFFSET_X)
    pad_r = max(0, SHADOW_BLUR + SHADOW_OFFSET_X)
    pad_t = max(0, SHADOW_BLUR - SHADOW_OFFSET_Y)
    pad_b = max(0, SHADOW_BLUR + SHADOW_OFFSET_Y)
    canvas_w = w + pad_l + pad_r
    canvas_h = h + pad_t + pad_b

    # Shadow source = current alpha channel, tinted black, offset and blurred.
    alpha = img.split()[-1]
    shadow = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    shadow_alpha = Image.new("L", (canvas_w, canvas_h), 0)
    shadow_alpha.paste(alpha, (pad_l + SHADOW_OFFSET_X, pad_t + SHADOW_OFFSET_Y))
    shadow_alpha = shadow_alpha.filter(ImageFilter.GaussianBlur(radius=SHADOW_BLUR))
    shadow_alpha = shadow_alpha.point(lambda v: int(v * SHADOW_ALPHA / 255))
    shadow.putalpha(shadow_alpha)

    out = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    out.alpha_composite(shadow)
    out.alpha_composite(img, (pad_l, pad_t))

    DST.parent.mkdir(parents=True, exist_ok=True)
    out.save(DST, "PNG")
    print(f"wrote: {DST} ({out.size[0]}x{out.size[1]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
