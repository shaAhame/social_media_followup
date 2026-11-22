"""Regenerate the home-screen / notification icons (dev tool; needs Pillow).

python scripts/make_icons.py
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parents[1] / "app" / "static" / "icons"
BLUE = (31, 79, 216)
WHITE = (255, 255, 255)
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def lettermark(size: int, padding: float, rounded: bool, bg=BLUE, fg=WHITE, transparent=False):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0) if transparent or rounded else bg + (255,))
    draw = ImageDraw.Draw(img)
    if rounded and not transparent:
        draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(size * 0.22), fill=bg)
    text = "iD"
    font = ImageFont.truetype(FONT, int(size * (1 - 2 * padding) * 0.62))
    box = draw.textbbox((0, 0), text, font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    origin = ((size - w) / 2 - box[0], (size - h) / 2 - box[1] - size * 0.02)
    draw.text(origin, text, font=font, fill=fg)
    # small "follow-up" tick under the letters
    y = size * (0.5 + (1 - 2 * padding) * 0.30)
    draw.rounded_rectangle(
        [size * 0.36, y, size * 0.64, y + size * 0.045], radius=int(size * 0.02), fill=fg
    )
    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    lettermark(192, 0.12, rounded=True).save(OUT / "icon-192.png")
    lettermark(512, 0.12, rounded=True).save(OUT / "icon-512.png")
    # full-bleed with safe-zone padding, for Android adaptive icons
    lettermark(512, 0.22, rounded=False).save(OUT / "icon-maskable-512.png")
    lettermark(180, 0.10, rounded=False).convert("RGB").save(OUT / "apple-touch-icon.png")
    lettermark(96, 0.08, rounded=False, transparent=True).save(OUT / "badge-96.png")  # mono badge
    print("icons written to", OUT)


if __name__ == "__main__":
    main()
