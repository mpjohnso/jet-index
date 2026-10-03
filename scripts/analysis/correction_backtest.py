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


def find_drawdown_episodes(prices, yms, threshold=0.10):
    """Peak-to-trough-to-recovery cycles in a price series, independent of
    any tier/metric -- this is about the market's own history of drawdowns
    and how long each took to recover, not about what the index said at the
    time. A new episode opens the first time price falls >=threshold from
    the running all-time-high since the last recovery, and closes the first
    month price closes back at/above the peak it fell from. The last
    episode may be unresolved (recovery_idx=None) if the data ends mid-drop.
    """
    n = len(prices)
    episodes = []
    peak_val = prices[0]
    peak_idx = 0
    cur = None
    for i in range(1, n):
        if cur is None:
            if prices[i] > peak_val:
                peak_val = prices[i]
                peak_idx = i
            else:
                dd = (peak_val - prices[i]) / peak_val
                if dd >= threshold:
                    cur = {'peak_idx': peak_idx, 'peak_val': peak_val,
                           'trough_idx': i, 'trough_val': prices[i]}
        else:
            if prices[i] < cur['trough_val']:
                cur['trough_val'] = prices[i]
                cur['trough_idx'] = i
            if prices[i] >= cur['peak_val']:
                cur['recovery_idx'] = i
                episodes.append(cur)
                peak_val = prices[i]
                peak_idx = i
                cur = None
    if cur is not None:
        cur['recovery_idx'] = None
        episodes.append(cur)
    for e in episodes:
        e['max_drawdown'] = (e['peak_val'] - e['trough_val']) / e['peak_val']
        e['peak_ym'] = yms[e['peak_idx']]
        e['trough_ym'] = yms[e['trough_idx']]
        e['recovery_ym'] = yms[e['recovery_idx']] if e['recovery_idx'] is not None else None
        e['months_peak_to_trough'] = e['trough_idx'] - e['peak_idx']
        e['months_trough_to_recovery'] = (
            (e['recovery_idx'] - e['trough_idx']) if e['recovery_idx'] is not None else None)
        e['months_peak_to_recovery'] = (
            (e['recovery_idx'] - e['peak_idx']) if e['recovery_idx'] is not None else None)
    return episodes


def recovery_time_by_severity(buckets=((0.10, 0.20, '10-20%'), (0.20, 0.30, '20-30%'), (0.30, 1.0, '30%+'))):
    """For each severity bucket, how long (on average) did it take the
    market to get back to its old high, for drawdown episodes that actually
    reached that severity. This is about the market's own cycle history --
    not conditioned on any JET Index tier."""
    rows, sp500 = load_monthly_and_sp500()
    yms = [r[0] for r in rows]
    prices = [sp500.get(ym) for ym in yms]
    valid_idx = [i for i, p in enumerate(prices) if p is not None]
    price_list = [prices[i] for i in valid_idx]
    valid_yms = [yms[i] for i in valid_idx]

    episodes = find_drawdown_episodes(price_list, valid_yms)

    print("=== Drawdown episodes (>=10% from a running high), 1977-present ===")
    for e in episodes:
        rec = e['months_peak_to_recovery']
        rec_str = f"{rec}mo" if rec is not None else "ONGOING / not yet recovered"
        print(f"  peak {e['peak_ym']} -> trough {e['trough_ym']} (-{e['max_drawdown']*100:.1f}%)"
              f" -> recovery {e['recovery_ym'] or '?'}  [{rec_str}]")

    print(f"\n{'Severity':<10}{'n':>4}{'avg peak->trough':>18}{'avg trough->recov':>20}"
          f"{'avg peak->recovery':>20}{'n_unresolved':>14}")
    for lo, hi, label in buckets:
        group = [e for e in episodes if lo <= e['max_drawdown'] < hi]
        resolved = [e for e in group if e['recovery_idx'] is not None]
        n_unresolved = len(group) - len(resolved)
        if resolved:
            pt = mean(e['months_peak_to_trough'] for e in resolved)
            tr = mean(e['months_trough_to_recovery'] for e in resolved)
            pr = mean(e['months_peak_to_recovery'] for e in resolved)
            print(f"{label:<10}{len(group):>4}{pt:>18.1f}{tr:>20.1f}{pr:>20.1f}{n_unresolved:>14}")
        else:
            print(f"{label:<10}{len(group):>4}{'n/a':>18}{'n/a':>20}{'n/a':>20}{n_unresolved:>14}")
    return episodes


def bull_market_runway_by_tier():
    """Flip side of the correction analysis: instead of "how soon until a
    correction," this asks "how much further does the rally have to run
    before it tops out and a correction begins." For each month, find the
    next market peak (per find_drawdown_episodes) and measure the distance
    to it in months; a month inside an ongoing drawdown/recovery counts
    toward the NEXT future peak, not the one it just fell from. Grouped by
    composite tier, both at the raw month level and at the episode-start
    (de-autocorrelated) level, since consecutive months in the same tier
    give near-duplicate runway values that would otherwise dominate the
    average."""
    rows, sp500 = load_monthly_and_sp500()
    yms = [r[0] for r in rows]
    prices = [sp500.get(ym) for ym in yms]
    valid_idx = [i for i, p in enumerate(prices) if p is not None]
    price_list = [prices[i] for i in valid_idx]
    valid_yms = [yms[i] for i in valid_idx]

    episodes = find_drawdown_episodes(price_list, valid_yms)
    peak_idxs_global = sorted(valid_idx[e['peak_idx']] for e in episodes)

    runway = [None] * len(rows)
    censored = [True] * len(rows)
    for i in valid_idx:
        future_peaks = [p for p in peak_idxs_global if p >= i]
        if future_peaks:
            runway[i] = min(future_peaks) - i
            censored[i] = False

    composite_tier = []
    for (ym, yc, pe, ecy) in rows:
        comp = composite(yc, pe, ecy)
        composite_tier.append(tier_for_score(comp))

    def report(groups, label):
        print(f"--- {label} ---")
        print(f"{'Tier':<6}{'n':>8}{'n_usable':>10}{'avg_runway_to_next_peak(mo)':>30}")
        for t in ORDER:
            if t not in groups:
                continue
            idxs = groups[t]
            usable = [i for i in idxs if not censored[i]]
            if not usable:
                print(f"{t:<6}{len(idxs):>8}{0:>10}{'n/a':>30}")
                continue
            avg = mean(runway[i] for i in usable)
            print(f"{t:<6}{len(idxs):>8}{len(usable):>10}{avg:>30.1f}")
        print()

    month_groups = {}
    for i, t in enumerate(composite_tier):
        month_groups.setdefault(t, []).append(i)
    report(month_groups, "by raw month")

    episode_groups = {}
    for (s, t, _l) in run_length_episodes(composite_tier):
        episode_groups.setdefault(t, []).append(s)
    report(episode_groups, "by episode-start (de-autocorrelated)")


def risk_vs_opportunity_framing():
    """Split framing requested 2026-10-03: for the 'danger' tiers (F/E/D),
    report downside risk (months to a correction, false-signal %) -- same
    as main(). For the 'safe' tiers (C/B/A), report upside instead: % price
    gain to the next market peak, and how many months that takes. Both use
    the episode-start (de-autocorrelated) view since consecutive months in
    one tier stretch give near-duplicate values that would otherwise
    dominate a raw-month average."""
    rows, sp500 = load_monthly_and_sp500()
    yms = [r[0] for r in rows]
    prices = [sp500.get(ym) for ym in yms]
    valid_idx = [i for i, p in enumerate(prices) if p is not None]
    price_list = [prices[i] for i in valid_idx]
    valid_yms = [yms[i] for i in valid_idx]

    episodes = find_drawdown_episodes(price_list, valid_yms)
    peak_idxs_global = sorted(valid_idx[e['peak_idx']] for e in episodes)
    peak_price_by_global = {valid_idx[e['peak_idx']]: e['peak_val'] for e in episodes}
    price_by_global = {valid_idx[i]: price_list[i] for i in range(len(valid_idx))}

    upside_pct = [None] * len(rows)
    runway = [None] * len(rows)
    up_censored = [True] * len(rows)
    for i in valid_idx:
        future_peaks = [p for p in peak_idxs_global if p > i]
        if future_peaks:
            np_ = min(future_peaks)
            runway[i] = np_ - i
            upside_pct[i] = (peak_price_by_global[np_] / price_by_global[i] - 1) * 100
            up_censored[i] = False

    composite_tier = []
    for (ym, yc, pe, ecy) in rows:
        comp = composite(yc, pe, ecy)
        composite_tier.append(tier_for_score(comp))

    cm, cens = build_correction_fields(sp500, rows)
    tier_episodes = run_length_episodes(composite_tier)
    groups = {}
    for (s, t, _l) in tier_episodes:
        groups.setdefault(t, []).append(s)

    print("=== Danger tiers (F/E/D): downside risk, episode-start ===")
    print(f"{'Tier':<6}{'n_ep':>6}{'n_usable':>10}{'avg_mo_to_corr':>16}{'false_signal_%':>16}")
    for t in ['F', 'E', 'D']:
        if t not in groups:
            continue
        idxs = groups[t]
        s = summarize_tier_group(idxs, cm, cens)
        if s is None:
            continue
        avg = f"{s['avg_months_to_correction']:.1f}" if s['avg_months_to_correction'] is not None else "n/a"
        print(f"{t:<6}{len(idxs):>6}{s['n_usable']:>10}{avg:>16}{s['false_signal_pct']:>15.1f}%")

    print("\n=== Safe tiers (C/B/A): upside potential, episode-start ===")
    print(f"{'Tier':<6}{'n_ep':>6}{'n_usable':>10}{'avg_upside_%':>14}{'avg_mo_to_peak':>16}")
    for t in ['C', 'B', 'A']:
        if t not in groups:
            continue
        idxs = groups[t]
        usable = [i for i in idxs if not up_censored[i]]
        if not usable:
            continue
        avg_up = mean(upside_pct[i] for i in usable)
        avg_mo = mean(runway[i] for i in usable)
        print(f"{t:<6}{len(idxs):>6}{len(usable):>10}{avg_up:>13.1f}%{avg_mo:>16.1f}")


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


def build_correction_fields(sp500_prices_by_ym, rows, threshold=CORRECTION_THRESHOLD, window=FALSE_SIGNAL_WINDOW):
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
        c = months_to_correction(price_list, local_i, threshold=threshold)
        correction_months[global_i] = c
        if c is not None:
            is_censored[global_i] = False
        else:
            # censored only if there isn't enough runway left to be sure
            is_censored[global_i] = (max_month_in_data - local_i) < window

    return correction_months, is_censored


def composite_sensitivity_matrix(thresholds=(0.10, 0.20, 0.30), windows=(12, 24)):
    """Re-run the composite-tier table across a grid of correction
    thresholds (e.g. 10/20/30% drawdowns) and false-signal windows
    (e.g. 12/24 months). Returns nothing -- prints a table per combo,
    same shape as the single-threshold tables in main()."""
    rows, sp500 = load_monthly_and_sp500()
    composite_tier = []
    for (ym, yc, pe, ecy) in rows:
        comp = composite(yc, pe, ecy)
        composite_tier.append(tier_for_score(comp))
    groups = {}
    for i, t in enumerate(composite_tier):
        groups.setdefault(t, []).append(i)

    for threshold in thresholds:
        for window in windows:
            cm, cens = build_correction_fields(sp500, rows, threshold=threshold, window=window)
            print(f"=== COMPOSITE tier -- {int(threshold*100)}% correction, {window}mo false-signal window ===")
            print(f"{'Tier':<6}{'n_months':>10}{'n_usable':>10}{'avg_mo_to_corr':>16}{'false_signal_%':>16}")
            for t in ORDER:
                if t not in groups:
                    continue
                stats = summarize_tier_group(groups[t], cm, cens, window=window)
                if stats is None:
                    continue
                avg_str = (f"{stats['avg_months_to_correction']:.1f}"
                           if stats['avg_months_to_correction'] is not None else "n/a")
                print(f"{t:<6}{stats['n_months']:>10}{stats['n_usable']:>10}{avg_str:>16}"
                      f"{stats['false_signal_pct']:>15.1f}%")
            print()


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
    if "--matrix" in sys.argv:
        composite_sensitivity_matrix()
    elif "--recovery" in sys.argv:
        recovery_time_by_severity()
    elif "--runway" in sys.argv:
        bull_market_runway_by_tier()
    elif "--risk-opportunity" in sys.argv:
        risk_vs_opportunity_framing()
    else:
        main()
