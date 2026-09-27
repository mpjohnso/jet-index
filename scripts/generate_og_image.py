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

from scoring import TIER_COLOR, TIER_TEXT, TIER_WORD, composite, tier_for_score

INDEX_HTML = "index.html"
OUT_PATH = "og-image.png"
FONT_DIR = "assets/fonts"

BG = (250, 248, 245)
INK = (22, 24, 28)
SUB = (113, 117, 125)
LINE = (231, 226, 216)


def hex_to_rgb(h):
    h = h.lstrip('#')
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


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

    W, H = 1200, 630
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # Top accent strip in the current tier's color
    d.rectangle([0, 0, W, 10], fill=fill)

    f_wordmark = ImageFont.truetype(f"{FONT_DIR}/DejaVuSansCondensed-Bold.ttf", 38)
    f_tagline = ImageFont.truetype(f"{FONT_DIR}/DejaVuSansMono-Bold.ttf", 19)
    f_chip = ImageFont.truetype(f"{FONT_DIR}/DejaVuSansMono-Bold.ttf", 130)
    f_score = ImageFont.truetype(f"{FONT_DIR}/DejaVuSansCondensed-Bold.ttf", 58)
    f_tier_word = ImageFont.truetype(f"{FONT_DIR}/DejaVuSansCondensed-Bold.ttf", 32)
    f_detail = ImageFont.truetype(f"{FONT_DIR}/DejaVuSansMono-Bold.ttf", 23)
    f_url = ImageFont.truetype(f"{FONT_DIR}/DejaVuSansMono-Bold.ttf", 21)

    # Wordmark, top-left
    d.text((72, 54), "JOHNSON ECONOMIC TIER", font=f_wordmark, fill=INK)
    d.text((72, 104), "JET Index · U.S. market valuation gauge", font=f_tagline, fill=SUB)

    # Big tier chip
    chip_x, chip_y, chip_w, chip_h = 72, 190, 260, 260
    d.rounded_rectangle([chip_x, chip_y, chip_x + chip_w, chip_y + chip_h], radius=28, fill=fill)
    bbox = d.textbbox((0, 0), tier, font=f_chip)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(
        (chip_x + chip_w / 2 - tw / 2 - bbox[0], chip_y + chip_h / 2 - th / 2 - bbox[1]),
        tier, font=f_chip, fill=text_on_fill,
    )

    # Score + tier word, right of chip
    tx = chip_x + chip_w + 48
    d.text((tx, 208), f"{score:.2f} / 7", font=f_score, fill=INK)
    d.text((tx, 278), f"{TIER_WORD[tier]} tier", font=f_tier_word, fill=SUB)

    if pe_pct is not None:
        d.text((tx, 342), f"Valuations richer than {pe_pct}% of", font=f_detail, fill=INK)
        d.text((tx, 374), "months since 1977", font=f_detail, fill=INK)

    d.text((tx, 424), f"As of {as_of}", font=f_detail, fill=SUB)

    # Footer
    d.line([(72, H - 90), (W - 72, H - 90)], fill=LINE, width=2)
    d.text((72, H - 66), "jetindx.com", font=f_url, fill=INK)
    note = "Not investment advice"
    note_w = d.textlength(note, font=f_url)
    d.text((W - 72 - note_w, H - 66), note, font=f_url, fill=SUB)

    img.save(OUT_PATH)
    print(f"Wrote {OUT_PATH}: {tier} tier, score {score:.2f}, as of {as_of}")


if __name__ == "__main__":
    main()
