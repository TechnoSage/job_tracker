"""
make_icon.py — Generate build/builder_icon.ico for the Build Dashboard shortcut.

Draws a gear / cog icon on a dark rounded-square background using Pillow.

Requires Pillow (already a dev dependency):
    pip install Pillow

Run once from anywhere:
    python build/make_icon.py
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw
except ImportError:
    print("[ERROR] Pillow is required:  pip install Pillow")
    sys.exit(1)

BUILD_DIR = Path(__file__).parent.resolve()
OUT_FILE  = BUILD_DIR / "builder_icon.ico"

# ── Colour palette (matches the Build Dashboard dark UI) ─────────────────────

_BG   = (30,  41,  59, 255)   # slate-800  — rounded background square
_GEAR = (245, 158,  11, 255)  # amber-500  — gear body
_HUB  = (15,  23,  42, 255)   # slate-950  — hub / centre hole

# ── Drawing helpers ───────────────────────────────────────────────────────────

def _gear(
    draw: ImageDraw.ImageDraw,
    cx: float, cy: float,
    outer_r: float, inner_r: float, hub_r: float,
    n_teeth: int,
) -> None:
    """
    Draw a cog using a polygon whose vertices alternate between outer_r (tooth
    tips) and inner_r (valley bottoms).  Four polygon points per tooth:
    two on outer_r, two on inner_r, stepped evenly around the full circle.
    """
    pts = []
    steps = n_teeth * 4
    for i in range(steps):
        angle = 2 * math.pi * i / steps - math.pi / 2   # start at top
        r = outer_r if (i % 4 < 2) else inner_r
        pts.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
    draw.polygon(pts, fill=_GEAR)

    # Punch out the centre hub
    draw.ellipse(
        [cx - hub_r, cy - hub_r, cx + hub_r, cy + hub_r],
        fill=_HUB,
    )


def _frame(size: int) -> Image.Image:
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    s    = size / 256.0                        # scale relative to 256-px master

    pad  = max(1, int(size * 0.055))
    rad  = max(2, int(size * 0.20))

    # Rounded dark background
    try:
        draw.rounded_rectangle(
            [pad, pad, size - pad, size - pad],
            radius=rad,
            fill=_BG,
        )
    except AttributeError:
        # Pillow < 8.2 has no rounded_rectangle — fall back to plain rectangle
        draw.rectangle([pad, pad, size - pad, size - pad], fill=_BG)

    cx = cy = size / 2.0
    _gear(
        draw, cx, cy,
        outer_r = 88 * s,
        inner_r = 66 * s,
        hub_r   = 27 * s,
        n_teeth = 8 if size >= 32 else 6,
    )
    return img


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    sizes  = [256, 128, 64, 48, 32, 16]
    frames = [_frame(s) for s in sizes]

    frames[0].save(
        OUT_FILE,
        format="ICO",
        sizes=[(s, s) for s in sizes],
        append_images=frames[1:],
    )
    print(f"  [OK] Icon written: {OUT_FILE}")


if __name__ == "__main__":
    main()
