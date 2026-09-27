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

from scoring import ORDER, TIER_COLOR, composite, tier_for_score

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


def wrap_text(d, text, font, max_width):
    """Greedy word-wrap; returns a list of lines that each fit max_width."""
    words = text.split(' ')
    lines = []
    cur = ''
    for w in words:
        trial = f'{cur} {w}'.strip()
        if d.textlength(trial, font=font) <= max_width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


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
    brandmark = parse_brandmark(html)

    W, H = 1200, 630
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # Top accent strip in the current tier's color
    d.rectangle([0, 0, W, 10], fill=fill)

    f_wordmark = ImageFont.truetype(f"{FONT_DIR}/DejaVuSansCondensed-Bold.ttf", 60)
    f_tagline = ImageFont.truetype(f"{FONT_DIR}/DejaVuSansMono-Bold.ttf", 19)
    f_headline = ImageFont.truetype(f"{FONT_DIR}/DejaVuSansCondensed-Bold.ttf", 33)
    f_body = ImageFont.truetype(f"{FONT_DIR}/DejaVuSansMono-Bold.ttf", 20)
    f_scale = ImageFont.truetype(f"{FONT_DIR}/DejaVuSansMono-Bold.ttf", 18)
    f_url = ImageFont.truetype(f"{FONT_DIR}/DejaVuSansMono-Bold.ttf", 20)

    # Three roughly-equal columns, sharing one vertical band: logo, title,
    # and an enticing description. The color bar spans beneath all three.
    col_top = 78
    margin = 72
    col_w = (W - 2 * margin) / 3
    logo_col_x = margin
    title_col_x = margin + col_w
    desc_col_x = margin + 2 * col_w + 24  # a little extra breathing room before the copy

    # --- Column 1: just the logo, large and centered in its column ---
    logo_size = 220
    title_lines = ["JOHNSON", "ECONOMIC", "TIER"]
    title_line_h = 70
    title_block_h = title_line_h * len(title_lines)

    logo_y = col_top + max(0, (title_block_h - logo_size) / 2)
    logo_x = logo_col_x + max(0, (col_w - 40 - logo_size) / 2)
    logo_img = render_brandmark(brandmark, logo_size, INK)
    img.paste(logo_img, (round(logo_x), round(logo_y)), logo_img)

    # --- Column 2: the title, forced onto its 3-line form, tagline below ---
    for i, line in enumerate(title_lines):
        d.text((title_col_x, col_top + i * title_line_h), line, font=f_wordmark, fill=INK)

    tagline_y = col_top + title_block_h + 20
    tagline_lines = wrap_text(d, "JET Index · U.S. market valuation gauge", f_tagline, col_w - 24)
    for i, line in enumerate(tagline_lines):
        d.text((title_col_x, tagline_y + i * 26), line, font=f_tagline, fill=SUB)

    mid_bottom = tagline_y + len(tagline_lines) * 26

    # --- Column 3: a topline pitch for the site, not the raw reading ---
    desc_w = W - margin - desc_col_x
    if pe_pct is not None:
        body_copy = (
            f"Right now, valuations are richer than {pe_pct}% of "
            "months since 1977. See the full picture and what it means."
        )
    else:
        body_copy = "A clear, honest read on U.S. market valuation — graded against 47 years of history."

    desc_y = col_top + max(0, (title_block_h - 220) / 2)
    for line in wrap_text(d, "How stretched are today's markets?", f_headline, desc_w):
        d.text((desc_col_x, desc_y), line, font=f_headline, fill=INK)
        desc_y += 40
    desc_y += 14
    for line in wrap_text(d, body_copy, f_body, desc_w):
        d.text((desc_col_x, desc_y), line, font=f_body, fill=SUB)
        desc_y += 27
    desc_y += 10
    cta = "Get today's reading →"
    d.text((desc_col_x, desc_y), cta, font=f_headline, fill=fill)
    desc_bottom = desc_y + 40

    # --- Full-width tier color bar, mirroring the site's meter, below all three columns ---
    bar_x, bar_w = margin, W - 2 * margin
    bar_y = max(logo_y + logo_size, mid_bottom, desc_bottom) + 40
    bar_h = 22
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
