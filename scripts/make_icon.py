"""Generate app/static/icon.png — the app favicon and the Unraid container icon.

Design "B": navy gradient rounded-square, an orange sync ring (the recurring-backup
cycle) around a white lightning bolt (Tasmota/power). Drawn at 4x and downscaled
for smooth edges. Re-run after tweaks:  python scripts/make_icon.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

S = 4  # supersampling factor
PX = 256 * S
RAD = 56 * S
WHITE = (255, 255, 255, 255)
ORANGE = (245, 130, 11, 255)


def rounded_gradient(top, bottom):
    grad = Image.new("RGBA", (PX, PX))
    dg = ImageDraw.Draw(grad)
    for y in range(PX):
        f = y / (PX - 1)
        dg.line([(0, y), (PX, y)],
                fill=tuple(int(top[i] + (bottom[i] - top[i]) * f) for i in range(3)) + (255,))
    mask = Image.new("L", (PX, PX), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, PX - 1, PX - 1], RAD, fill=255)
    img = Image.new("RGBA", (PX, PX), (0, 0, 0, 0))
    img.paste(grad, (0, 0), mask)
    return img


def bolt(cx, cy, w, h):
    pts = [(0.58, 0.0), (0.12, 0.55), (0.44, 0.55), (0.30, 1.0),
           (0.90, 0.40), (0.56, 0.40), (0.78, 0.0)]
    return [(cx - w / 2 + px * w, cy - h / 2 + py * h) for px, py in pts]


img = rounded_gradient((51, 65, 85), (15, 23, 42))  # slate -> navy
d = ImageDraw.Draw(img)
cx = cy = PX // 2
r = 88 * S
d.arc([cx - r, cy - r, cx + r, cy + r], start=35, end=300, fill=ORANGE, width=16 * S)
# arrowhead at the open end of the ring (~35°)
d.polygon([(cx + r + 6 * S, cy + 6 * S), (cx + int(r * 0.78), cy + 40 * S),
           (cx + r + 22 * S, cy + 40 * S)], fill=ORANGE)
d.polygon(bolt(cx, cy, 70 * S, 108 * S), fill=WHITE)

out = Path(__file__).resolve().parent.parent / "app" / "static" / "icon.png"
img.resize((256, 256), Image.LANCZOS).save(out)
print("wrote", out)
