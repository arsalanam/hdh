"""Reusable social cards for hdh — an agent-first EHR.

Run with ``uv run --with pillow python scripts/make_social_card.py``. Pillow
is not a project dependency: this is a one-off generator, not something the
package needs at runtime, and the fonts it reaches for are Windows'.

The visual argument is the verdict column: a note goes in, coded chart
entries come out, and one line REFUSES. That refusal is the thing worth
being known for, so it is the only element that isn't green.

Wide (1200x627) is the LinkedIn/OG card. Square (1080x1080) shows the
note as well as the verdicts, because a square has the height for it and
the transformation is the point.
"""

import pathlib

from PIL import Image, ImageDraw, ImageFont

F = "C:/Windows/Fonts/"
BG = (10, 14, 20)
PANEL = (17, 23, 32)
EDGE = (32, 42, 55)
INK = (232, 238, 245)
MUTED = (128, 145, 165)
DIM = (92, 106, 122)
TEAL = (56, 199, 173)
AMBER = (240, 175, 72)
BLUE = (96, 165, 250)

ROWS = [
    ("confirmed", TEAL, "hypertension", "already on the problem list"),
    ("updated", TEAL, "controlled", "well treated → flag set"),
    ("new", TEAL, "HbA1c", "lab order, due in 90 days"),
    ("review", AMBER, "unmapped term", "NOT charted — sent to a human"),
]

NOTE = [
    "68yo returns for chronic disease review.",
    "Well treated hypertension. Continue lisinopril",
    "10 mg daily. Repeat HbA1c in 3 months.",
]


def font(name, size):
    return ImageFont.truetype(F + name, size)


def mark(d, x, y, scale, ui, ui_b, ui_s):
    """The wordmark, its rule, and the two lines beside it. Returns bottom y."""
    wordmark = font("segoeuib.ttf", int(108 * scale))
    d.text((x, y), "hdh", font=wordmark, fill=INK)
    w = d.textlength("hdh", font=wordmark)
    bar_x = x + w + int(26 * scale)
    d.rounded_rectangle(
        [bar_x, y + int(14 * scale), bar_x + int(7 * scale), y + int(104 * scale)],
        radius=4,
        fill=TEAL,
    )
    tx = bar_x + int(26 * scale)
    d.text((tx, y + int(22 * scale)), "an", font=ui, fill=MUTED)
    d.text((tx + d.textlength("an ", font=ui), y + int(20 * scale)), "agent-first EHR", font=ui_b, fill=TEAL)
    d.text((tx, y + int(58 * scale)), "notes in · orders out · nothing guessed", font=ui_s, fill=DIM)
    return y + int(126 * scale)


def verdicts(d, x0, x1, y, mono, mono_b, step, pad_in, k=1.0):
    """The verdict panel. Returns bottom y. ``k`` scales the internals so a
    print-resolution render is drawn, not upscaled."""
    height = pad_in * 2 + int(40 * k) + step * len(ROWS)
    d.rounded_rectangle([x0, y, x1, y + height], radius=14, fill=PANEL, outline=EDGE, width=1)
    x, ty = x0 + int(32 * k), y + pad_in
    d.text((x, ty), "$ hdh comprehend --file note.txt --apply", font=mono, fill=DIM)
    ty += int(42 * k)
    label_w = max(d.textlength(r[0], font=mono_b) for r in ROWS) + int(26 * k)
    for verdict, colour, subject, detail in ROWS:
        d.text((x, ty), verdict, font=mono_b, fill=colour)
        d.text((x + label_w, ty), subject, font=mono_b, fill=INK)
        sx = x + label_w + d.textlength(subject, font=mono_b) + int(18 * k)
        d.text((sx, ty + 1), detail, font=mono, fill=MUTED if colour is TEAL else AMBER)
        ty += step
    return y + height


def footer(d, x0, x1, y, ui_s, k=1.0):
    d.text((x0, y), "SNOMED CT", font=ui_s, fill=BLUE)
    dx = x0 + d.textlength("SNOMED CT", font=ui_s)
    for label in ("ICD-10-CM", "LOINC", "RxNorm"):
        d.text((dx + int(12 * k), y), "·", font=ui_s, fill=EDGE)
        dx += int(12 * k) + d.textlength("·  ", font=ui_s)
        d.text((dx, y), label, font=ui_s, fill=BLUE)
        dx += d.textlength(label, font=ui_s)
    tail = "10,000 synthetic patients · zero PHI · MIT"
    d.text((x1 - d.textlength(tail, font=ui_s), y), tail, font=ui_s, fill=DIM)


def wide(path, k=1.0):
    """The banner card. ``k=2`` renders at print resolution for an A4 page —
    drawn at size rather than upscaled, so the mono stays crisp."""
    W, H, pad = int(1200 * k), int(627 * k), int(58 * k)
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    ui_b = font("segoeuib.ttf", int(25 * k))
    ui = font("segoeui.ttf", int(23 * k))
    ui_s = font("segoeui.ttf", int(19 * k))
    mono = font("CascadiaMono.ttf", int(20 * k))
    mono_b = font("CascadiaMono.ttf", int(21 * k))

    step, pad_in = int(44 * k), int(26 * k)
    block = int(126 * k) + (pad_in * 2 + int(40 * k) + step * len(ROWS)) + int(58 * k)
    y = (H - block) // 2
    y = mark(d, pad, y, k, ui, ui_b, ui_s)
    y = verdicts(d, pad, W - pad, y + int(8 * k), mono, mono_b, step, pad_in, k)
    footer(d, pad, W - pad, y + int(30 * k), ui_s, k)
    img.save(path)
    print("wrote", path, img.size)


def square(path):
    W, H, pad = 1080, 1080, 72
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    ui_b, ui, ui_s = font("segoeuib.ttf", 28), font("segoeui.ttf", 26), font("segoeui.ttf", 21)
    mono, mono_b = font("CascadiaMono.ttf", 22), font("CascadiaMono.ttf", 23)
    note_f = font("segoeuii.ttf", 25)

    # every band's height, so the stack can be centred instead of guessed
    note_h = 26 + 30 + 40 * len(NOTE) + 24
    verdict_h = 28 * 2 + 40 + 50 * len(ROWS)
    block = int(126 * 1.15) + 34 + note_h + 78 + verdict_h + 40 + 26
    y = mark(d, pad, (H - block) // 2, 1.15, ui, ui_b, ui_s)

    # what goes in — the square has the height, and the transformation is
    # the whole point, so show the note rather than implying it
    y += 34
    d.rounded_rectangle([pad, y, W - pad, y + note_h], radius=14, fill=PANEL, outline=EDGE, width=1)
    ty = y + 26
    d.text((pad + 32, ty), "THE NOTE", font=font("segoeuib.ttf", 15), fill=DIM)
    ty += 30
    for line in NOTE:
        d.text((pad + 32, ty), line, font=note_f, fill=MUTED)
        ty += 40
    y += note_h

    # the arrow that carries the argument
    cx = W // 2
    d.line([cx, y + 20, cx, y + 52], fill=EDGE, width=3)
    d.polygon([(cx - 9, y + 48), (cx + 9, y + 48), (cx, y + 64)], fill=TEAL)
    y += 78

    y = verdicts(d, pad, W - pad, y, mono, mono_b, 50, 28, 1.15)
    footer(d, pad, W - pad, y + 40, ui_s, 1.15)
    img.save(path)
    print("wrote", path, img.size)


OUT = pathlib.Path(__file__).resolve().parent.parent / "docs" / "assets"
OUT.mkdir(parents=True, exist_ok=True)
wide(str(OUT / "hdh-card-wide.png"))
wide(str(OUT / "hdh-card-wide@2x.png"), k=2)
square(str(OUT / "hdh-card-square.png"))
