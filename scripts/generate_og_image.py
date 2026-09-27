#!/usr/bin/env python3
"""
Generates a branded 1200x630 social-preview card (og-image.png) showing
the current JET Index reading -- tier, score, and a bit of context -- so
links to the site render a real preview on iMessage, Slack, X, etc.
instead of a blank box.

Run this right after update_data.py, against the index.html it just
wrote, so the image always matches whatever reading is actually live.
No separate "is this stale" bookkeeping is needed: it's regenerated from
scratch on every run of the daily automation.

Visual design mirrors the site itself: the actual brandmark (the
checkmark-in-a-circle logo, parsed straight out of index.html's inline
<symbol id="brandmark"> so the two never drift apart) next to the
wordmark, the tier chip and score, and the same F-E-D-C-B-A-S color bar
used for the on-site meter, with a marker at the current score.

Fonts: bundles its own copies of DejaVu Sans Condensed Bold and DejaVu
Sans Mono Bold (assets/fonts/) rather than relying on whatever happens
to be installed on the CI runner, so the card renders identically every
time. These aren't the site's actual web fonts (Oswald / JetBrains Mono)
-- just a condensed-bold-sans + monospace pairing in the same spirit,
bundled directly so there's no font-fetching step to fail in CI.

Scoring thresholds live in scoring.py, kept in sync with index.html's JS
by hand (see that file's docstring).
"""
import re
import sys
from datetime import datetime, timezone

from PIL import Image, ImageDraw, ImageFont

from scoring import ORDER, TIER_COLOR, TIER_TEXT, TIER_WORD, composite, tier_for_score

INDEX_HTML = "index.html"
OUT_PATH = "og-image.png"
FONT_DIR = "assets/fonts"

BG = (250, 248, 245)
INK = (22, 24, 28)
SUB = (113, 117, 125)
LINE = (231, 226, 216)

SUPERSAMPLE = 4  # draw the logo mark at 4x and downsample for smooth edges


def hex_to_rgb(h):
    h = h.lstrip('#')
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def parse_brandmark(html):
    """Pull the circle + polygon path straight out of index.html's inline
    <symbol id="brandmark">, so the OG-image logo can never drift out of
    sync with the one actually shown on the site."""
    sym = re.search(r'<symbol id="brandmark"[^>]*>([\s\S]*?)</symbol>', html)
    if not sym:
        return None
    block = sym.group(1)

    circ = re.search(
        r'<circle cx="([\-0-9.]+)" cy="([\-0-9.]+)" r="([\-0-9.]+)"[^>]*stroke-width="([\-0-9.]+)"',
        block,
    )
    circle = None
    if circ:
        circle = tuple(float(circ.group(i)) for i in (1, 2, 3, 4))  # cx, cy, r, stroke_width

    path_m = re.search(r'<path[^>]*d="([^"]+)"', block)
    polygons = []
    if path_m:
        d = path_m.group(1)
        # This path only ever uses M/L/Z (straight lines), so it can be
        # parsed as a set of closed polygons rather than needing a real
        # SVG/bezier engine.
        for sub in d.split('Z'):
            nums = [float(x) for x in re.findall(r'[\-0-9.]+', sub)]
            if len(nums) >= 6:
                polygons.append(list(zip(nums[0::2], nums[1::2])))

    return {'circle': circle, 'polygons': polygons}


def render_brandmark(mark, size, color, view_box=1147):
    """Rasterize the parsed brandmark into an RGBA image `size`x`size`,
    supersampled for clean edges."""
    hi = size * SUPERSAMPLE
    scale = hi / view_box
    img = Image.new("RGBA", (hi, hi), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    if mark and mark['circle']:
        cx, cy, r, sw = mark['circle']
        bbox = [(cx - r) * scale, (cy - r) * scale, (cx + r) * scale, (cy + r) * scale]
        d.ellipse(bbox, outline=color, width=max(1, round(sw * scale)))

    if mark:
        for poly in mark['polygons']:
            pts = [(x * scale, y * scale) for x, y in poly]
            d.polygon(pts, fill=color)

    return img.resize((size, size), Image.LANCZOS)


def main():
    with open(INDEX_HTML, "r", encoding="utf-8") as f:
        html = f.read()

    m = re.search(r"const CURRENT_YC = ([\-0-9.]+), CURRENT_PE = ([\-0-9.]+), CURRENT_ECY = ([\-0-9.]+);", html)
    if not m:
        print("[error] Could not find CURRENT_YC/PE/ECY; skipping OG image.")
        sys.exit(1)
    yc, pe, ecy = float(m.group(1)), float(m.group(2)), float(m.group(3))

    m2 = re.search(r"const monthly = (\[[\s\S]*?\]);", html)
    pe_history = []
    if m2:
        for row_m in re.finditer(
            r"\['(\d{4}-\d{2})',([\-0-9.]+),([\-0-9.]+),([\-0-9.]+),([\-0-9.]+)\]", m2.group(1)
        ):
            pe_history.append(float(row_m.group(3)))

    score = composite(yc, pe, ecy)
    tier = tier_for_score(score)

    pe_pct = None
    if pe_history:
        pe_pct = round(100 * sum(1 for v in pe_history if v < pe) / len(pe_history))

    as_of = datetime.now(timezone.utc).strftime("%B %Y")

    fill = hex_to_rgb(TIER_COLOR[tier])
    text_on_fill = hex_to_rgb(TIER_TEXT[tier])
    brandmark = parse_brandmark(html)

    W, H = 1200, 630
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # Top accent strip in the current tier's color
    d.rectangle([0, 0, W, 10], fill=fill)

    f_wordmark = ImageFont.truetype(f"{FONT_DIR}/DejaVuSansCondensed-Bold.ttf", 34)
    f_tagline = ImageFont.truetype(f"{FONT_DIR}/DejaVuSansMono-Bold.ttf", 17)
    f_chip = ImageFont.truetype(f"{FONT_DIR}/DejaVuSansMono-Bold.ttf", 118)
    f_score = ImageFont.truetype(f"{FONT_DIR}/DejaVuSansCondensed-Bold.ttf", 54)
    f_tier_word = ImageFont.truetype(f"{FONT_DIR}/DejaVuSansCondensed-Bold.ttf", 29)
    f_detail = ImageFont.truetype(f"{FONT_DIR}/DejaVuSansMono-Bold.ttf", 21)
    f_scale = ImageFont.truetype(f"{FONT_DIR}/DejaVuSansMono-Bold.ttf", 18)
    f_url = ImageFont.truetype(f"{FONT_DIR}/DejaVuSansMono-Bold.ttf", 20)

    # --- Header: real brandmark logo + wordmark, same pairing as the site ---
    logo_size = 68
    logo_x, logo_y = 72, 50
    logo_img = render_brandmark(brandmark, logo_size, INK)
    img.paste(logo_img, (logo_x, logo_y), logo_img)

    word_x = logo_x + logo_size + 20
    d.text((word_x, logo_y + 6), "JOHNSON ECONOMIC TIER", font=f_wordmark, fill=INK)
    d.text((word_x, logo_y + 46), "JET Index · U.S. market valuation gauge", font=f_tagline, fill=SUB)

    # --- Tier chip + score, mid-card ---
    chip_x, chip_y, chip_w, chip_h = 72, 178, 226, 226
    d.rounded_rectangle([chip_x, chip_y, chip_x + chip_w, chip_y + chip_h], radius=26, fill=fill)
    bbox = d.textbbox((0, 0), tier, font=f_chip)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(
        (chip_x + chip_w / 2 - tw / 2 - bbox[0], chip_y + chip_h / 2 - th / 2 - bbox[1]),
        tier, font=f_chip, fill=text_on_fill,
    )

    tx = chip_x + chip_w + 44
    d.text((tx, chip_y + 8), f"{score:.2f} / 7", font=f_score, fill=INK)
    d.text((tx, chip_y + 70), f"{TIER_WORD[tier]} tier", font=f_tier_word, fill=SUB)

    detail_y = chip_y + 118
    if pe_pct is not None:
        d.text((tx, detail_y), f"Valuations richer than {pe_pct}% of", font=f_detail, fill=INK)
        d.text((tx, detail_y + 28), "months since 1977", font=f_detail, fill=INK)
        detail_y += 62
    d.text((tx, detail_y), f"As of {as_of}", font=f_detail, fill=SUB)

    # --- Full-width tier color bar, mirroring the site's meter ---
    bar_x, bar_w = 72, W - 144
    bar_y, bar_h = 452, 22
    n = len(ORDER)
    seg_w = bar_w / n
    for i, letter in enumerate(ORDER):
        seg_fill = hex_to_rgb(TIER_COLOR[letter])
        x0 = bar_x + i * seg_w
        x1 = bar_x + (i + 1) * seg_w
        # rounded caps on the outer ends only, square joins between segments
        if i == 0:
            d.rounded_rectangle([x0, bar_y, x1 + 12, bar_y + bar_h], radius=bar_h / 2, fill=seg_fill)
        elif i == n - 1:
            d.rounded_rectangle([x0 - 12, bar_y, x1, bar_y + bar_h], radius=bar_h / 2, fill=seg_fill)
        else:
            d.rectangle([x0, bar_y, x1, bar_y + bar_h], fill=seg_fill)

    # Marker at the current score's position (same formula as the site's
    # hero meter: pct = (score - 1) / 6)
    marker_pct = max(0.0, min(1.0, (score - 1) / 6))
    marker_x = bar_x + marker_pct * bar_w
    d.rounded_rectangle(
        [marker_x - 3, bar_y - 9, marker_x + 3, bar_y + bar_h + 9], radius=3, fill=INK,
    )

    # Scale labels under each segment, centered
    for i, letter in enumerate(ORDER):
        cx = bar_x + (i + 0.5) * seg_w
        lw = d.textlength(letter, font=f_scale)
        d.text((cx - lw / 2, bar_y + bar_h + 14), letter, font=f_scale, fill=SUB)

    # --- Footer ---
    d.line([(72, H - 60), (W - 72, H - 60)], fill=LINE, width=2)
    d.text((72, H - 42), "jetindx.com", font=f_url, fill=INK)
    note = "Not investment advice"
    note_w = d.textlength(note, font=f_url)
    d.text((W - 72 - note_w, H - 42), note, font=f_url, fill=SUB)

    img.save(OUT_PATH)
    print(f"Wrote {OUT_PATH}: {tier} tier, score {score:.2f}, as of {as_of}")


if __name__ == "__main__":
    main()
