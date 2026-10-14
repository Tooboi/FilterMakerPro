"""Build README / listing media for Filter Maker Pro and Lite from the showcase renders.

    python tools/make_media.py --pro results/showcase --lite results/showcase_lite \
        --out-pro docs/images --out-lite ../FilterMakerLite/docs --version 0.7.0

Every render is a transparent PNG from tests/render_showcase.py. It is laid on a dark
card (gradient, faint grid, warm glow) so the images read the same on GitHub's light and
dark themes, and the white die-cut furnace filter doesn't vanish on a white page.
Needs Pillow and the Inter font (falls back to DejaVu).
"""
import argparse
import os

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

BG_TOP = (21, 26, 36)
BG_BOT = (12, 15, 21)
GRID = (255, 255, 255, 9)
ORANGE = (255, 140, 31)
WHITE = (236, 241, 248)
GREY = (150, 160, 175)

FONT_DIRS = ("/usr/share/fonts/opentype/inter", "/usr/share/fonts/truetype/inter",
             "C:/Windows/Fonts", "/Library/Fonts")


def font(name, size):
    for d in FONT_DIRS:
        p = os.path.join(d, name)
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    for fallback in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(fallback, size)
        except OSError:
            pass
    return ImageFont.load_default()


def mono(size):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", "C:/Windows/Fonts/consola.ttf"):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return font("Inter-Regular.otf", size)


def background(w, h, glow=True, grid=40, radius=0):
    """Dark gradient card with a faint grid and a warm glow in the top-right corner."""
    t = np.linspace(0.0, 1.0, h)[:, None, None]
    top, bot = np.array(BG_TOP + (255,), float), np.array(BG_BOT + (255,), float)
    arr = np.broadcast_to(top * (1 - t) + bot * t, (h, w, 4)).astype(np.uint8)
    img = Image.fromarray(np.ascontiguousarray(arr), "RGBA")
    if glow:
        g = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(g)
        r = int(max(w, h) * 0.45)
        d.ellipse((w - r * 0.9, -r * 1.1, w + r * 1.1, r * 0.7), fill=(255, 140, 31, 46))
        d.ellipse((w * 0.25, h * 0.3, w * 0.25 + r * 1.6, h * 0.3 + r * 1.2), fill=(70, 95, 140, 30))
        g = g.filter(ImageFilter.GaussianBlur(r * 0.35))
        img = Image.alpha_composite(img, g)
    if grid:
        gl = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(gl)
        for x in range(0, w, grid):
            d.line((x, 0, x, h), fill=GRID)
        for y in range(0, h, grid):
            d.line((0, y, w, y), fill=GRID)
        img = Image.alpha_composite(img, gl)
    if radius:
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius, fill=255)
        out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        out.paste(img, (0, 0), mask)
        border = ImageDraw.Draw(out)
        border.rounded_rectangle((0, 0, w - 1, h - 1), radius, outline=(255, 255, 255, 22), width=2)
        img = out
    return img


def trim(im):
    """Crop a transparent render to its visible content."""
    box = im.getchannel("A").point(lambda a: 255 if a > 6 else 0).getbbox()
    return im.crop(box) if box else im


def shadow(im, blur=26, opacity=110, offset=(0, 18)):
    a = im.getchannel("A").point(lambda v: int(v * opacity / 255))
    sh = Image.new("RGBA", (im.width + blur * 4, im.height + blur * 4), (0, 0, 0, 0))
    black = Image.new("RGBA", im.size, (0, 0, 0, 255))
    sh.paste(black, (blur * 2 + offset[0], blur * 2 + offset[1]), a)
    return sh.filter(ImageFilter.GaussianBlur(blur)), blur * 2


def place(canvas, render, box, align="center"):
    """Fit a (trimmed) transparent render into box = (x0, y0, x1, y1) with a soft shadow."""
    r = trim(render)
    x0, y0, x1, y1 = box
    s = min((x1 - x0) / r.width, (y1 - y0) / r.height)
    r = r.resize((max(1, int(r.width * s)), max(1, int(r.height * s))), Image.LANCZOS)
    x = x0 + ((x1 - x0) - r.width) // 2 if align == "center" else (x1 - r.width if align == "right" else x0)
    y = y0 + ((y1 - y0) - r.height) // 2
    sh, pad = shadow(r)
    canvas.alpha_composite(sh, (max(0, x - pad), max(0, y - pad)))
    canvas.alpha_composite(r, (x, y))
    return canvas


def pill(img, x, y, text, f, accent=False):
    """Outlined pill, drawn on its own layer so the translucent fill blends."""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    w = d.textlength(text, font=f)
    h = f.size + 22
    col = ORANGE if accent else (210, 216, 226)
    d.rounded_rectangle((x, y, x + w + 40, y + h), h // 2, outline=col + (220,) if accent else (255, 255, 255, 80),
                        width=2, fill=(255, 140, 31, 22) if accent else (255, 255, 255, 10))
    d.text((x + 20, y + 10), text, font=f, fill=col + (255,))
    img.alpha_composite(layer)
    return x + w + 40 + 14


def card(render, w, h, pad=0.085):
    c = background(w, h, radius=26)
    p = int(min(w, h) * pad)
    return place(c, render, (p, p, w - p, h - p))


def headline(img, kicker, title, sub, pills, x, y, title_size=120, width=None):
    d = ImageDraw.Draw(img)
    fk = font("Inter-SemiBold.otf", int(title_size * 0.24))
    ft = font("Inter-Bold.otf", title_size)
    fs = font("Inter-Regular.otf", int(title_size * 0.3))
    fp = mono(int(title_size * 0.2))
    if kicker:
        k = " ".join(kicker.upper())                       # letter-spaced kicker
        d.text((x, y), k, font=fk, fill=ORANGE)
        y += int(fk.size * 1.9)
    d.text((x, y - int(title_size * 0.12)), title, font=ft, fill=WHITE)
    y += int(title_size * 1.12)
    if sub:
        for line in wrap(d, sub, fs, width or img.width * 0.45):
            d.text((x, y), line, font=fs, fill=GREY)
            y += int(fs.size * 1.45)
        y += int(fs.size * 0.6)
    px = x
    for i, t in enumerate(pills or ()):
        px = pill(img, px, y, t, fp, accent=(i == 0))
    return y


def wrap(d, text, f, width):
    words, lines, cur = text.split(), [], ""
    for wd in words:
        t = (cur + " " + wd).strip()
        if d.textlength(t, font=f) > width and cur:
            lines.append(cur)
            cur = wd
        else:
            cur = t
    if cur:
        lines.append(cur)
    return lines


def listing_slide(render, title, sub, w=1920, h=1080):
    img = background(w, h)
    d = ImageDraw.Draw(img)
    d.text((96, 70), title, font=font("Inter-Bold.otf", 64), fill=WHITE)
    d.text((98, 160), sub, font=font("Inter-Regular.otf", 32), fill=GREY)
    place(img, render, (96, 250, w - 96, h - 80))
    return img.convert("RGB")


def load(folder, name):
    p = os.path.join(folder, name + ".png")
    return Image.open(p).convert("RGBA") if os.path.exists(p) else None


def save(img, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path, optimize=True)
    print("wrote", path, img.size)


def build_pro(src, out, version):
    hero = load(src, "hero")
    b = background(2560, 800, radius=28)
    headline(b, "Blender add-on", "Filter Maker Pro",
             "Pleated air filters from a line, a curve or any faces: die-cut faces, wire mesh, "
             "built-in materials, hair & dust and game-ready bakes. All from the N-panel.",
             ["v" + version, "Blender 4.2+", "GPL-3.0"], 130, 190, title_size=128, width=1240)
    if hero is not None:
        place(b, hero, (1500, 40, 2500, 760))
    save(b, os.path.join(out, "banner.png"))
    s = background(1280, 640)
    headline(s, "Blender add-on", "Filter Maker Pro", "Pleated filters, die-cut faces, wire, "
             "materials, hair & dust, game-ready.", ["v" + version], 70, 120, title_size=70, width=640)
    if hero is not None:
        place(s, hero, (760, 40, 1250, 600))
    save(s.convert("RGB"), os.path.join(out, "social-preview.png"))
    sizes = {"shapes": (1800, 1150), "faces": (1500, 1300), "materials": (2000, 700), "dirt": (1600, 700),
             "game_ready": (1600, 900), "furnace_front": (1000, 1300), "furnace_macro": (1400, 900),
             "furnace_print": (1400, 900), "hero_dusty": (1800, 1000), "macro": (1400, 900),
             "boolean": (1400, 900), "cylinder": (1200, 900), "box_wrap": (1200, 900), "topology": (1200, 900),
             "bevels": (1500, 650), "borders": (1600, 600), "presets": (1600, 640), "noise": (1600, 640),
             "angle": (1600, 640), "sources": (1800, 640)}
    full_bleed = {"furnace_macro", "macro"}                 # close-ups fill the frame: no trimming
    for name, (w, h) in sizes.items():
        r = load(src, name)
        if r is None:
            print("missing", name)
            continue
        if name in full_bleed:
            c = background(w, h, radius=26)
            c.alpha_composite(r.resize((w, h), Image.LANCZOS))
            mask = Image.new("L", (w, h), 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), 26, fill=255)
            out_img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            out_img.paste(c, (0, 0), mask)
            save(out_img, os.path.join(out, name + ".png"))
        else:
            save(card(r, w, h), os.path.join(out, name + ".png"))


def build_lite(src, out, version):
    hero = load(src, "hero")
    img_dir, lst = os.path.join(out, "images"), os.path.join(out, "listing")
    b = background(2560, 800, radius=28)
    headline(b, "Free Blender add-on", "Filter Maker Lite",
             "Pleated filters from a line, a curve or any faces, with a clean border and the Boolean "
             "tool. Free, and all from the N-panel.", ["v" + version, "Free", "Blender 4.2+"],
             130, 190, title_size=128, width=1240)
    if hero is not None:
        place(b, hero, (1500, 60, 2500, 740))
    save(b, os.path.join(img_dir, "banner.png"))
    for name, (w, h) in {"sources": (1800, 640), "bevels": (1500, 650), "topology": (1200, 900),
                         "cylinder": (1200, 900), "boolean": (1400, 900)}.items():
        r = load(src, name)
        if r is not None:
            save(card(r, w, h), os.path.join(img_dir, name + ".png"))
    # Blender Extensions listing: featured image, previews and icon
    f = background(1920, 1080)
    headline(f, "Free Blender add-on", "Filter Maker Lite",
             "Pleated filters from a line, a curve or any faces. Free, and all from the N-panel.",
             ["v" + version, "Free", "Blender 4.2+"], 120, 150, title_size=118, width=900)
    src_r = load(src, "sources")
    if src_r is not None:
        place(f, src_r, (140, 540, 1780, 960))
    save(f.convert("RGB"), os.path.join(lst, "featured_1920x1080.png"))
    slides = (("sources", "preview_1_sources.png", "Four ways to start",
               "A new line, any curve, mesh edges or faces: one click each."),
              ("bevels", "preview_2_bevel.png", "Shape every pleat",
               "Density, depth, angle, thickness and a ridge / valley bevel."),
              ("topology", "preview_3_topology.png", "Clean quad topology",
               "A structured grid that follows folds and corners, with welded seams."),
              ("boolean", "preview_4_boolean.png", "Set it into your model",
               "The Boolean tool cuts the source mesh, so the filter sits in its housing."))
    for name, fname, title, sub in slides:
        r = load(src, name)
        if r is not None:
            save(listing_slide(r, title, sub), os.path.join(lst, fname))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pro")
    ap.add_argument("--lite")
    ap.add_argument("--out-pro", default="docs/images")
    ap.add_argument("--out-lite", default="../FilterMakerLite/docs")
    ap.add_argument("--version", default="0.7.0")
    a = ap.parse_args()
    if a.pro:
        build_pro(a.pro, a.out_pro, a.version)
    if a.lite:
        build_lite(a.lite, a.out_lite, a.version)
