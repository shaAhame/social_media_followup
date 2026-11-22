"""Regenerate the home-screen / notification icons from the iDealz logo (dev tool; needs Pillow).

    python scripts/make_icons.py [path/to/logo.png]

The logo should be a square image with the full blue background (no transparency).
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "assets" / "idealz-logo.png"
OUT = ROOT / "app" / "static" / "icons"


def square(img: Image.Image) -> Image.Image:
    side = min(img.size)
    left, top = (img.width - side) // 2, (img.height - side) // 2
    return img.crop((left, top, left + side, top + side))


def resized(img: Image.Image, size: int) -> Image.Image:
    return img.resize((size, size), Image.Resampling.LANCZOS)


def rounded(img: Image.Image, radius_ratio: float = 0.22) -> Image.Image:
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, img.width - 1, img.height - 1], radius=int(img.width * radius_ratio), fill=255
    )
    out = img.convert("RGBA")
    out.putalpha(mask)
    return out


def badge(img: Image.Image, size: int = 96) -> Image.Image:
    """Monochrome badge for the Android status bar: the white mark on transparent."""
    gray_full = img.convert("L")
    box = gray_full.point(lambda v: 255 if v > 200 else 0).getbbox() or (0, 0, *img.size)
    pad = int(max(box[2] - box[0], box[3] - box[1]) * 0.12)
    cx, cy = (box[0] + box[2]) // 2, (box[1] + box[3]) // 2
    half = max(box[2] - box[0], box[3] - box[1]) // 2 + pad
    mark = img.crop((cx - half, cy - half, cx + half, cy + half))  # just the white mark
    gray = resized(mark, size).convert("L")
    alpha = gray.point(lambda v: 255 if v > 200 else (0 if v < 150 else (v - 150) * 5))
    out = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    out.putalpha(alpha)
    return out


def main() -> None:
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else SOURCE
    logo = square(Image.open(source).convert("RGB"))
    OUT.mkdir(parents=True, exist_ok=True)
    rounded(resized(logo, 192)).save(OUT / "icon-192.png", optimize=True)
    rounded(resized(logo, 512)).save(OUT / "icon-512.png", optimize=True)
    # Full-bleed for Android adaptive icons (the OS applies its own mask shape)
    resized(logo, 512).save(OUT / "icon-maskable-512.png", optimize=True)
    # iPhone applies its own rounded corners; must be opaque
    resized(logo, 180).save(OUT / "apple-touch-icon.png", optimize=True)
    badge(logo).save(OUT / "badge-96.png", optimize=True)
    print("icons written to", OUT)


if __name__ == "__main__":
    main()
