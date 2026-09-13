"""Render icon design candidates to /tmp/icon_variants/ for review."""
from pathlib import Path

from PIL import Image, ImageDraw

S = 4
PX = 256 * S
RAD = 56 * S
WHITE = (255, 255, 255, 255)
ORANGE = (245, 130, 11, 255)
OUT = Path("/tmp/icon_variants")
OUT.mkdir(exist_ok=True)


def rounded_gradient(top, bottom):
    grad = Image.new("RGBA", (PX, PX))
    dg = ImageDraw.Draw(grad)
    for y in range(PX):
        f = y / (PX - 1)
        dg.line([(0, y), (PX, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * f) for i in range(3)) + (255,))
    mask = Image.new("L", (PX, PX), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, PX - 1, PX - 1], RAD, fill=255)
    img = Image.new("RGBA", (PX, PX), (0, 0, 0, 0))
    img.paste(grad, (0, 0), mask)
    return img


def bolt(cx, cy, w, h, fill):
    """Lightning bolt polygon centred on (cx, cy) sized w x h."""
    pts = [(0.58, 0.0), (0.12, 0.55), (0.44, 0.55), (0.30, 1.0),
           (0.90, 0.40), (0.56, 0.40), (0.78, 0.0)]
    return [(cx - w / 2 + px * w, cy - h / 2 + py * h) for px, py in pts], fill


def shield(cx, top, halfw, height):
    return [
        (cx - halfw, top + 12 * S), (cx - halfw, top),  # rounded-ish top corners approximated
        (cx + halfw, top), (cx + halfw, top + height * 0.55),
        (cx, top + height), (cx - halfw, top + height * 0.55),
    ]


# --- Variant A: teal shield + orange bolt -------------------------------------
a = rounded_gradient((20, 184, 166), (13, 118, 110))  # teal
da = ImageDraw.Draw(a)
da.polygon(shield(PX // 2, 46 * S, 78 * S, 168 * S), fill=WHITE)
poly, _ = bolt(PX // 2, 132 * S, 78 * S, 118 * S, ORANGE)
da.polygon(poly, fill=ORANGE)
a.resize((256, 256), Image.LANCZOS).save(OUT / "A_shield_bolt.png")

# --- Variant B: navy + orange sync ring + white bolt --------------------------
b = rounded_gradient((51, 65, 85), (15, 23, 42))  # slate/navy
db = ImageDraw.Draw(b)
cx = cy = PX // 2
r = 88 * S
wdt = 16 * S
db.arc([cx - r, cy - r, cx + r, cy + r], start=35, end=300, fill=ORANGE, width=wdt)
# arrow head at the ring end (~35deg)
db.polygon([(cx + r + 6 * S, cy + 6 * S), (cx + int(r * 0.78), cy + 40 * S),
            (cx + r + 22 * S, cy + 40 * S)], fill=ORANGE)
poly, _ = bolt(cx, cy, 70 * S, 108 * S, WHITE)
db.polygon(poly, fill=WHITE)
b.resize((256, 256), Image.LANCZOS).save(OUT / "B_sync_bolt.png")

# --- Variant C: orange + white hexagon + teal bolt ----------------------------
c = rounded_gradient((251, 146, 60), (234, 88, 12))  # orange
dc = ImageDraw.Draw(c)
import math
cx = cy = PX // 2
hr = 92 * S
hexpts = [(cx + hr * math.cos(math.radians(60 * k - 90)),
           cy + hr * math.sin(math.radians(60 * k - 90))) for k in range(6)]
dc.polygon(hexpts, fill=WHITE)
poly, _ = bolt(cx, cy, 66 * S, 104 * S, (13, 148, 136, 255))
dc.polygon(poly, fill=(13, 148, 136, 255))
c.resize((256, 256), Image.LANCZOS).save(OUT / "C_hex_bolt.png")

print("wrote", *[p.name for p in sorted(OUT.glob("*.png"))])
