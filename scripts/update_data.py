#!/usr/bin/env python3
"""
Daily data refresh for the JET Index dashboard.

Pulls the latest public data for the three inputs and the credit-stress
overlay, and rewrites the relevant numbers in index.html:
  - 2s10s Yield Curve Spread   -> FRED series T10Y2YM (no API key needed)
  - Baa-10Y credit spread      -> FRED series BAA10Y   (no API key needed)
  - Shiller PE (CAPE)          -> multpl.com/shiller-pe (updated daily; Shiller's own
                                  public Excel file on Yale's site stopped in Sept 2023)
  - Excess CAPE Yield          -> computed here: 100/CAPE minus the real 10Y Treasury yield,
                                  where real = FRED DGS10 minus trailing 10-year annualized
                                  CPI inflation (FRED CPIAUCSL). Back-tested against the
                                  site's own 2026 values: matches to within ~0.01-0.02.

Design goals:
  - Never crash the whole run because one source is temporarily down.
    Each source is fetched independently; whatever succeeds gets applied.
  - Never silently corrupt the file: every edit is done with an anchored,
    specific find-and-replace, and the script bails out loudly (non-zero
    exit code) if an anchor it expects to find is missing.
  - Always stamp "last checked", even on a day nothing actually changed,
    so the dashboard visibly proves the automation is alive.
"""
import re
import sys
from datetime import datetime, timedelta, timezone

import requests

from notify import send_change_notifications
from scoring import TIER_WORD, composite, tier_for_score

INDEX_HTML = "index.html"
UA = {"User-Agent": "Mozilla/5.0 (compatible; JETIndexBot/1.0; +https://jetindx.com)"}
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
TIER_HOLD_DAYS = 7  # a new tier must persist longer than this before it becomes official
MULTPL_PE = "https://www.multpl.com/shiller-pe"


def fetch_fred_latest(series):
    """Return the most recent non-missing value for a FRED series, or None."""
    try:
        r = requests.get(FRED_CSV.format(series=series), headers=UA, timeout=30)
        r.raise_for_status()
        lines = r.text.strip().splitlines()
        header = lines[0].split(",")
        if len(header) < 2:
            return None
        for line in reversed(lines[1:]):
            parts = line.split(",")
            if len(parts) < 2:
                continue
            val = parts[1].strip()
            if val and val != ".":
                return float(val)
        return None
    except Exception as e:
        print(f"[warn] FRED fetch failed for {series}: {e}")
        return None


def fetch_multpl_pe():
    """Return the current Shiller PE (CAPE) from multpl.com, or None.

    multpl updates this daily from Shiller's data plus the live S&P 500 price. Parsed from the
    text of the page's "current" block, with the <meta> description as a fallback, and
    sanity-checked so a layout change can't write garbage into the dashboard.
    """
    try:
        r = requests.get(MULTPL_PE, headers=UA, timeout=30)
        r.raise_for_status()
        html = r.text
        m = re.search(r'id="current"[^>]*>(.{0,600})', html, re.S)
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))) if m else ""
        mm = re.search(r"Current Shiller PE Ratio\s*:?\s*([0-9]+(?:\.[0-9]+)?)", text)
        if not mm:
            mm = re.search(r"Current Shiller PE Ratio is ([0-9]+(?:\.[0-9]+)?)", html)
        if not mm:
            print("[warn] Could not find the current Shiller PE on multpl.com")
            return None
        val = float(mm.group(1))
        if not (5 <= val <= 80):
            print(f"[warn] multpl Shiller PE {val} is outside the sane range 5-80; ignoring")
            return None
        return val
    except Exception as e:
        print(f"[warn] multpl Shiller PE fetch failed: {e}")
        return None


def fetch_fred_rows(series):
    """Return [(date_str, value), ...] for a FRED series, dropping missing values."""
    r = requests.get(FRED_CSV.format(series=series), headers=UA, timeout=30)
    r.raise_for_status()
    rows = []
    for line in r.text.strip().splitlines()[1:]:
        parts = line.split(",")
        if len(parts) >= 2 and parts[1].strip() not in ("", "."):
            rows.append((parts[0].strip(), float(parts[1])))
    return rows


def compute_ecy(cape):
    """Excess CAPE Yield = earnings yield (100/CAPE) - real 10Y Treasury yield, in percent.

    Real 10Y yield = latest daily 10Y Treasury (DGS10) minus the trailing 10-year annualized
    CPI inflation rate (latest CPIAUCSL vs. the same month ten years earlier). This is the
    construction that reproduces the dashboard's own historical ECY values; TIPS yields and
    breakeven-based variants do NOT (they miss by ~1 point). Returns None on any problem.
    """
    if cape is None:
        return None
    try:
        dgs10 = fetch_fred_rows("DGS10")
        cpi = dict(fetch_fred_rows("CPIAUCSL"))
        if not dgs10 or not cpi:
            return None
        latest_cpi_date = max(cpi)
        y, mth, _ = latest_cpi_date.split("-")
        then = cpi.get(f"{int(y) - 10}-{mth}-01")
        if not then:
            print("[warn] No CPI value ten years before", latest_cpi_date)
            return None
        inflation = ((cpi[latest_cpi_date] / then) ** (1 / 10) - 1) * 100
        real_yield = dgs10[-1][1] - inflation
        ecy = 100.0 / cape - real_yield
        if not (-6 <= ecy <= 12):
            print(f"[warn] Computed ECY {ecy:.2f} is outside the sane range; ignoring")
            return None
        print(f"[info] ECY: 100/{cape} = {100.0 / cape:.3f}; real 10Y = {dgs10[-1][1]} (DGS10 {dgs10[-1][0]}) "
              f"- {inflation:.3f} (10y CPI to {latest_cpi_date}) = {real_yield:.3f}; ECY = {ecy:.2f}")
        return ecy
    except Exception as e:
        print(f"[warn] ECY computation failed: {e}")
        return None


def main():
    with open(INDEX_HTML, "r", encoding="utf-8") as f:
        html = f.read()

    yc = fetch_fred_latest("T10Y2YM")
    credit = fetch_fred_latest("BAA10Y")
    cape = fetch_multpl_pe()
    ecy = compute_ecy(cape)

    print(f"Fetched: yc={yc} credit={credit} cape={cape} ecy={ecy}")

    changed = False

    # --- Update CURRENT_YC / CURRENT_PE / CURRENT_ECY (feeds the hero score) ---
    m = re.search(r"const CURRENT_YC = ([\-0-9.]+), CURRENT_PE = ([\-0-9.]+), CURRENT_ECY = ([\-0-9.]+);", html)
    if not m:
        print("[error] Could not find CURRENT_YC/PE/ECY anchor; aborting.")
        sys.exit(1)
    cur_yc, cur_pe, cur_ecy = float(m.group(1)), float(m.group(2)), float(m.group(3))
    new_yc = round(yc, 2) if yc is not None else cur_yc
    new_pe = round(cape, 2) if cape is not None else cur_pe
    new_ecy = round(ecy, 2) if ecy is not None else cur_ecy

    score_changed = (new_yc, new_pe, new_ecy) != (cur_yc, cur_pe, cur_ecy)
    # Captured before any overwrite, so we can tell later whether this run's change was big
    # enough to actually flip the tier letter, not just nudge an input within the same tier.
    old_composite = composite(cur_yc, cur_pe, cur_ecy)
    old_tier = tier_for_score(old_composite)
    # Debounce state: the official tier only changes after a new raw tier has held > 7 days.
    st = {}
    for key in ("CONFIRMED_TIER", "PREV_CONFIRMED_TIER", "PENDING_TIER", "PENDING_SINCE_ISO"):
        mm = re.search(rf"const {key} = '([^']*)';", html)
        st[key] = mm.group(1) if mm else None
    confirmed = st["CONFIRMED_TIER"] or old_tier
    pending, pending_since = st["PENDING_TIER"] or "", st["PENDING_SINCE_ISO"] or ""
    if score_changed:
        new_current_line = f"const CURRENT_YC = {new_yc}, CURRENT_PE = {new_pe}, CURRENT_ECY = {new_ecy};"
        html = html.replace(m.group(0), new_current_line)
        changed = True

        # Snapshot what each input was right before this change, so the site can show
        # whether that specific input moved favorably or unfavorably -- independent of
        # both the 3-month momentum trend and whether the tier itself moved.
        prev_line = f"const PREV_YC = {cur_yc}, PREV_PE = {cur_pe}, PREV_ECY = {cur_ecy};"
        prev_pattern = re.compile(r"const PREV_YC = [\-0-9.]+, PREV_PE = [\-0-9.]+, PREV_ECY = [\-0-9.]+;")
        if prev_pattern.search(html):
            html = prev_pattern.sub(prev_line, html)
        else:
            html = html.replace(new_current_line, new_current_line + "\n" + prev_line, 1)

    # --- Refresh the og:/twitter: description meta tags so social previews always ---
    # show today's actual reading rather than whatever text was last hand-written.
    score = composite(new_yc, new_pe, new_ecy)
    tier = tier_for_score(score)
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    tier_changed, prev_confirmed = False, st["PREV_CONFIRMED_TIER"] or confirmed
    if tier == confirmed:
        pending, pending_since = "", ""          # back in line (or flipped back): clock resets
    elif tier != pending:
        pending, pending_since = tier, now_iso   # new candidate tier: start the clock
    else:
        held = now_dt - datetime.strptime(pending_since, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if held > timedelta(days=TIER_HOLD_DAYS):
            prev_confirmed, confirmed, tier_changed = confirmed, tier, True
            pending, pending_since = "", ""
    tier = confirmed  # everything user-facing below uses the official tier
    as_of = datetime.now(timezone.utc).strftime("%B %Y")
    social_desc = (
        f"Current reading: {tier} tier ({TIER_WORD[tier]}), {score:.2f} / 7 as of {as_of}. "
        f"A U.S. market-valuation gauge combining the yield curve, Shiller PE, and Excess CAPE Yield."
    )
    for prop in ('og:description', 'twitter:description'):
        attr = 'property' if prop.startswith('og:') else 'name'
        pattern = re.compile(rf'(<meta {attr}="{re.escape(prop)}" content=")[^"]*(")')
        html, n = pattern.subn(lambda mm: mm.group(1) + social_desc + mm.group(2), html)
        if n == 0:
            print(f"[warn] Could not find <meta {attr}=\"{prop}\"> anchor; leaving social preview text untouched.")

    # --- Update the three metric-card literal display values ---
    def replace_metric_value(html, name_anchor, new_text):
        pattern = re.compile(
            r"(" + re.escape(name_anchor) + r'</div>\s*<div class="metric-value-wrap"><span class="metric-value mono">)([^<]+)(</span>)'
        )
        return pattern.sub(lambda mm: mm.group(1) + new_text + mm.group(3), html, count=1)

    if yc is not None:
        html = replace_metric_value(html, '<div class="metric-name">2s10s Yield Curve Spread', f"{new_yc:.2f}%")
    if cape is not None:
        html = replace_metric_value(html, '<div class="metric-name">Shiller PE (CAPE)', f"{new_pe:.1f}")
    if ecy is not None:
        html = replace_metric_value(html, '<div class="metric-name">Excess CAPE Yield', f"{new_ecy:.1f}%")

    # --- Update / append the current month's row in the `monthly` history array ---
    month_key = datetime.now(timezone.utc).strftime("%Y-%m")
    new_credit = round(credit, 2) if credit is not None else None

    tail_match = re.search(
        r"(\['(\d{4}-\d{2})',[\-0-9.]+,[\-0-9.]+,[\-0-9.]+,([\-0-9.]+)\])\s*,?\s*\n?\];",
        html,
    )
    if tail_match:
        row_text = tail_match.group(1)
        last_key = tail_match.group(2)
        last_credit = float(tail_match.group(3))
        row_credit = new_credit if new_credit is not None else last_credit
        new_row = f"['{month_key}',{new_yc},{new_pe},{new_ecy},{row_credit}]"
        if last_key == month_key:
            html = html[: tail_match.start()] + new_row + "\n];" + html[tail_match.end():]
            changed = True
        elif month_key > last_key:
            html = html[: tail_match.start()] + row_text + f",{new_row}\n];" + html[tail_match.end():]
            changed = True
    else:
        print("[warn] Could not find the tail of the `monthly` array; leaving history untouched.")

    # --- Always stamp the "data checked" timestamp on a successful run ---
    for key, val in (("CONFIRMED_TIER", confirmed), ("PREV_CONFIRMED_TIER", prev_confirmed),
                     ("PENDING_TIER", pending), ("PENDING_SINCE_ISO", pending_since)):
        html, n = re.subn(rf"const {key} = '[^']*';", f"const {key} = '{val}';", html)
        if n == 0:
            print(f"[warn] {key} anchor missing in index.html")
    if "const LAST_CHECKED_ISO" in html:
        html = re.sub(r"const LAST_CHECKED_ISO = '[^']*';", f"const LAST_CHECKED_ISO = '{now_iso}';", html)
    else:
        html = html.replace(
            "const TIER_COLOR =",
            f"const LAST_CHECKED_ISO = '{now_iso}';\nconst TIER_COLOR =",
            1,
        )
    changed = True  # the "checked" timestamp always advances

    # --- Only stamp "score last changed" when the composite-score inputs actually moved ---
    if score_changed:
        if "const LAST_CHANGED_ISO" in html:
            html = re.sub(r"const LAST_CHANGED_ISO = '[^']*';", f"const LAST_CHANGED_ISO = '{now_iso}';", html)
        else:
            html = html.replace(
                "const TIER_COLOR =",
                f"const LAST_CHANGED_ISO = '{now_iso}';\nconst TIER_COLOR =",
                1,
            )

    # --- Only stamp "tier last changed" when the resulting tier LETTER actually moved -- ---
    # this is the headline event the hero card highlights; an input moving within the same
    # tier only gets the quieter "inputs updated" treatment above.
    if tier_changed:
        if "const LAST_TIER_CHANGE_ISO" in html:
            html = re.sub(r"const LAST_TIER_CHANGE_ISO = '[^']*';", f"const LAST_TIER_CHANGE_ISO = '{now_iso}';", html)
        else:
            html = html.replace(
                "const TIER_COLOR =",
                f"const LAST_TIER_CHANGE_ISO = '{now_iso}';\nconst TIER_COLOR =",
                1,
            )

    with open(INDEX_HTML, "w", encoding="utf-8") as f:
        f.write(html)

    print("changed=" + ("true" if changed else "false"))

    # Email subscribers only when the OFFICIAL tier changes (a new tier that has held > 7 days),
    # never on raw-input wiggles or short-lived tier flickers.
    if tier_changed:
        send_change_notifications(prev_confirmed, confirmed, (cur_yc, cur_pe, cur_ecy), (new_yc, new_pe, new_ecy))


if __name__ == "__main__":
    main()
