"""
Correction / false-signal backtest.

Explores, for each tier of each metric (and for the composite), how long it
tends to take before the market suffers a "material correction" (a >=10%
drawdown from the running forward price high), and how often a tier turns
out to be a "false signal" (no correction within a fixed look-ahead window).

This is exploratory research tooling, NOT part of the live site or the daily
email pipeline. Nothing here feeds index.html, about.html, or scoring.py.
It exists so the analysis done in chat on 2026-10-0x can be re-run and
extended later instead of being trapped in one-off heredocs.

Usage:
    python3 scripts/analysis/correction_backtest.py

Data is parsed directly out of the live index.html's embedded JS objects
(`monthly` and `SP500`), so this always reflects whatever is currently
checked into the repo -- no separate data file to keep in sync.

Methodology notes (see chat history / session summary for full derivation):
  - "Material correction" = first future month where price has fallen >=10%
    from the running max price (from the observation month forward).
  - A month is right-censored (excluded, not "no correction") if there isn't
    enough future data left to know whether a correction would occur.
  - "False signal %" = share of (non-censored) months in a tier where no
    correction occurs within FALSE_SIGNAL_WINDOW months.
  - Per-metric breakdowns group months by that metric's OWN tier (via
    score_yc / score_pe / score_ey), not the composite tier -- this is the
    extension requested on 2026-10-03 to check whether individual metrics
    carry as much signal as the composite.

IMPORTANT DATA-QUALITY NOTE (found 2026-10-03): the `SP500` object embedded
in index.html is NOT real historical price data. It's a straight-line
interpolation between annual anchor points -- every single year (596 of 597
months) sits in a perfectly linear 12-month run, so it has zero intra-year
volatility and completely misses every real crash, including Oct 1987,
2000-02, 2008-09, and the Mar 2020 COVID crash (that last one actually goes
*up* month-over-month in the embedded data). It's fine for the site's rough
"S&P, Nmo after" display, but useless for drawdown/correction detection.
This script therefore uses `sp500_real_monthly.py` (real monthly closes,
sourced from multpl.com) instead of the embedded placeholder.
"""

import re
import sys
from pathlib import Path
from statistics import mean

REPO_ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = REPO_ROOT / "index.html"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from scoring import score_yc, score_pe, score_ey, composite, tier_for_score, ORDER  # noqa: E402
from sp500_real_monthly import SP500_REAL  # noqa: E402

CORRECTION_THRESHOLD = 0.10   # 10% drawdown
FALSE_SIGNAL_WINDOW = 24      # months


def load_monthly_and_sp500():
    html = INDEX_HTML.read_text()

    m = re.search(r"const monthly = \[(.*?)\n\];", html, re.S)
    if not m:
        raise RuntimeError("couldn't find `const monthly = [...]` in index.html")
    rows = []
    entry_re = re.compile(
        r"\[\s*['\"]([\d-]+)['\"]\s*,\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\]"
    )
    for ym, yc, pe, ecy, _credit in entry_re.findall(m.group(1)):
        rows.append((ym, float(yc), float(pe), float(ecy)))

    # NOTE: deliberately NOT parsing index.html's `const SP500 = {...}` here --
    # see the module docstring. We use real monthly closes instead.
    return rows, dict(SP500_REAL)


def months_to_correction(prices, i, threshold=CORRECTION_THRESHOLD):
    """Months from index i until price has fallen >=threshold from the
    running max (tracked forward from i). Returns None if censored (no
    correction found before the data runs out)."""
    n = len(prices)
    running_max = prices[i]
    for t in range(i + 1, n):
        if prices[t] > running_max:
            running_max = prices[t]
        dd = (running_max - prices[t]) / running_max
        if dd >= threshold:
            return t - i
    return None


def run_length_episodes(tier_seq):
    """Given a list of tier letters (one per month, in order), return a list
    of (start_index, tier, length) for each contiguous same-tier run."""
    episodes = []
    i = 0
    n = len(tier_seq)
    while i < n:
        j = i
        while j + 1 < n and tier_seq[j + 1] == tier_seq[i]:
            j += 1
        episodes.append((i, tier_seq[i], j - i + 1))
        i = j + 1
    return episodes


def summarize_tier_group(indices, correction_months, is_censored, window=FALSE_SIGNAL_WINDOW):
    """indices: month indices in this tier group.
    correction_months[i] / is_censored[i] indexed by month index i."""
    usable = [i for i in indices if not is_censored[i]]
    if not usable:
        return None
    months_vals = [correction_months[i] for i in usable]
    false_signals = sum(1 for i in usable if correction_months[i] is None or correction_months[i] > window)
    avg_months = mean(m for m in months_vals if m is not None) if any(m is not None for m in months_vals) else None
    return {
        "n_months": len(indices),
        "n_usable": len(usable),
        "n_censored": len(indices) - len(usable),
        "avg_months_to_correction": avg_months,
        "false_signal_pct": 100.0 * false_signals / len(usable),
    }


def build_correction_fields(sp500_prices_by_ym, rows):
    """Returns parallel arrays: correction_months[i], is_censored[i] for each
    row in `rows`, using the SP500 series aligned by year-month."""
    yms = [r[0] for r in rows]
    prices = []
    for ym in yms:
        if ym not in sp500_prices_by_ym:
            prices.append(None)
        else:
            prices.append(sp500_prices_by_ym[ym])

    # Only compute over the span where we actually have SP500 prices.
    valid_idx = [i for i, p in enumerate(prices) if p is not None]
    price_list = [prices[i] for i in valid_idx]
    n = len(price_list)

    correction_months = [None] * len(rows)
    is_censored = [True] * len(rows)

    max_month_in_data = n - 1
    for local_i, global_i in enumerate(valid_idx):
        c = months_to_correction(price_list, local_i)
        correction_months[global_i] = c
        if c is not None:
            is_censored[global_i] = False
        else:
            # censored only if there isn't enough runway left to be sure
            is_censored[global_i] = (max_month_in_data - local_i) < FALSE_SIGNAL_WINDOW

    return correction_months, is_censored


def main():
    rows, sp500 = load_monthly_and_sp500()
    correction_months, is_censored = build_correction_fields(sp500, rows)

    composite_tier = []
    yc_tier = []
    pe_tier = []
    ey_tier = []
    for (ym, yc, pe, ecy) in rows:
        syc, spe, sey = score_yc(yc), score_pe(pe), score_ey(ecy)
        comp = composite(yc, pe, ecy)
        composite_tier.append(tier_for_score(comp))
        yc_tier.append(ORDER[syc - 1])
        pe_tier.append(ORDER[spe - 1])
        ey_tier.append(ORDER[sey - 1])

    def table_for(tier_seq, label):
        print(f"\n=== {label} ===")
        print(f"{'Tier':<6}{'n_months':>10}{'n_usable':>10}{'avg_mo_to_corr':>16}{'false_signal_%':>16}")
        groups = {}
        for i, t in enumerate(tier_seq):
            groups.setdefault(t, []).append(i)
        for t in ORDER:
            if t not in groups:
                continue
            stats = summarize_tier_group(groups[t], correction_months, is_censored)
            if stats is None:
                print(f"{t:<6}{len(groups[t]):>10}{'(no usable months)':>26}")
                continue
            avg_str = f"{stats['avg_months_to_correction']:.1f}" if stats['avg_months_to_correction'] is not None else "n/a"
            print(f"{t:<6}{stats['n_months']:>10}{stats['n_usable']:>10}{avg_str:>16}{stats['false_signal_pct']:>15.1f}%")

    table_for(composite_tier, "COMPOSITE tier")
    table_for(yc_tier, "YIELD CURVE tier alone")
    table_for(pe_tier, "SHILLER PE tier alone")
    table_for(ey_tier, "EXCESS CAPE YIELD tier alone")

    # Episode-level view (de-autocorrelated) for composite, for reference.
    print("\n=== COMPOSITE tier, episode-start months only (de-autocorrelated) ===")
    episodes = run_length_episodes(composite_tier)
    ep_groups = {}
    for (start, tier, length) in episodes:
        ep_groups.setdefault(tier, []).append(start)
    print(f"{'Tier':<6}{'n_episodes':>12}{'n_usable':>10}{'avg_mo_to_corr':>16}{'false_signal_%':>16}")
    for t in ORDER:
        if t not in ep_groups:
            continue
        stats = summarize_tier_group(ep_groups[t], correction_months, is_censored)
        if stats is None:
            continue
        avg_str = f"{stats['avg_months_to_correction']:.1f}" if stats['avg_months_to_correction'] is not None else "n/a"
        print(f"{t:<6}{len(ep_groups[t]):>12}{stats['n_usable']:>10}{avg_str:>16}{stats['false_signal_pct']:>15.1f}%")


if __name__ == "__main__":
    main()
