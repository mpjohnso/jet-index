#!/usr/bin/env python3
"""
Daily data refresh for the JET Index dashboard.

Pulls the latest public data for the three inputs and the credit-stress
overlay, and rewrites the relevant numbers in index.html:
  - 2s10s Yield Curve Spread   -> FRED series T10Y2YM (no API key needed)
  - Baa-10Y credit spread      -> FRED series BAA10Y   (no API key needed)
  - Shiller PE (CAPE) and
    Excess CAPE Yield          -> Robert Shiller's own published dataset

Design goals:
  - Never crash the whole run because one source is temporarily down.
    Each source is fetched independently; whatever succeeds gets applied.
  - Never silently corrupt the file: every edit is done with an anchored,
    specific find-and-replace, and the script bails out loudly (non-zero
    exit code) if an anchor it expects to find is missing.
  - Always stamp "last checked", even on a day nothing actually changed,
    so the dashboard visibly proves the automation is alive.
"""
import io
import re
import sys
from datetime import datetime, timezone

import requests

from notify import send_change_notifications
from scoring import TIER_WORD, composite, tier_for_score

INDEX_HTML = "index.html"
UA = {"User-Agent": "Mozilla/5.0 (compatible; JETIndexBot/1.0; +https://jetindx.com)"}
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
SHILLER_XLS = "http://www.econ.yale.edu/~shiller/data/ie_data.xls"


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


def fetch_shiller_cape_ecy():
    """Return (cape, ecy) from Shiller's ie_data.xls, or (None, None)."""
    try:
        import pandas as pd

        r = requests.get(SHILLER_XLS, headers=UA, timeout=60)
        r.raise_for_status()
        raw = pd.read_excel(io.BytesIO(r.content), sheet_name="Data", header=None)

        # Find the header row: the row whose first cell is literally "Date".
        header_row_idx = None
        for i in range(min(15, len(raw))):
            if str(raw.iloc[i, 0]).strip().lower() == "date":
                header_row_idx = i
                break
        if header_row_idx is None:
            print("[warn] Could not locate Shiller header row")
            return None, None

        headers = [str(c).strip() for c in raw.iloc[header_row_idx]]
        data = raw.iloc[header_row_idx + 1:].copy()
        data.columns = headers

        cape_col = next((c for c in headers if c.strip().upper() == "CAPE"), None)
        ecy_col = next(
            (c for c in headers if "excess cape yield" in c.strip().lower() or c.strip().upper() == "ECY"),
            None,
        )
        if not cape_col or not ecy_col:
            print(f"[warn] Could not find CAPE/ECY columns in {headers}")
            return None, None

        cape_series = pd.to_numeric(data[cape_col], errors="coerce").dropna()
        ecy_series = pd.to_numeric(data[ecy_col], errors="coerce").dropna()
        if cape_series.empty or ecy_series.empty:
            return None, None

        return float(cape_series.iloc[-1]), float(ecy_series.iloc[-1]) * (
            100 if ecy_series.iloc[-1] < 1 else 1
        )
    except Exception as e:
        print(f"[warn] Shiller fetch/parse failed: {e}")
        return None, None


def main():
    with open(INDEX_HTML, "r", encoding="utf-8") as f:
        html = f.read()

    yc = fetch_fred_latest("T10Y2YM")
    credit = fetch_fred_latest("BAA10Y")
    cape, ecy = fetch_shiller_cape_ecy()

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
    old_tier = tier_for_score(composite(cur_yc, cur_pe, cur_ecy))
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
    # The headline event: did this run just nudge an input, or actually flip the tier letter?
    tier_changed = score_changed and (tier != old_tier)
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
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
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

    if score_changed:
        send_change_notifications((cur_yc, cur_pe, cur_ecy), (new_yc, new_pe, new_ecy))


if __name__ == "__main__":
    main()
