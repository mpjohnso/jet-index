"""
Shared scoring logic for the Python side of the pipeline (update_data.py,
generate_og_image.py). Mirrors the tier-scoring JS in index.html
(scoreYC / scorePE / scoreEY / composite / TIER_COLOR / TIER_TEXT / ORDER)
exactly. If those thresholds or colors ever change in the JS, update them
here too -- there's no single source of truth shared between the two
languages, so this is the one place on the Python side to keep in sync.
"""

TIER_COLOR = {
    'S': '#e0473d', 'A': '#ef8f1e', 'B': '#f3cf3d', 'C': '#4fae59',
    'D': '#63b8de', 'E': '#2f6fb0', 'F': '#c92f86',
}
TIER_TEXT = {
    'S': '#ffffff', 'A': '#ffffff', 'B': '#2b2200', 'C': '#ffffff',
    'D': '#0b2733', 'E': '#ffffff', 'F': '#ffffff',
}
ORDER = ['F', 'E', 'D', 'C', 'B', 'A', 'S']
TIER_WORD = {
    'S': 'excellent', 'A': 'strong', 'B': 'good', 'C': 'fair',
    'D': 'stretched', 'E': 'poor', 'F': 'extreme',
}


# Thresholds updated 2026-10-02 -- must match scoreYC/scorePE/scoreEY in index.html and
# about.html exactly.
def score_yc(v):
    if v >= 2.5: return 7
    if v >= 2: return 6
    if v >= 1: return 5
    if v >= 0.5: return 4
    if v >= 0: return 3
    if v >= -1: return 2
    return 1


def score_pe(v):
    if v <= 10: return 7
    if v <= 16: return 6
    if v <= 22: return 5
    if v <= 28: return 4
    if v <= 34: return 3
    if v <= 40: return 2
    return 1


def score_ey(v):
    if v >= 9: return 7
    if v >= 7: return 6
    if v >= 5: return 5
    if v >= 3: return 4
    if v >= 1: return 3
    if v >= 0: return 2
    return 1


def composite(yc, pe, ecy):
    return 0.25 * score_yc(yc) + 0.375 * score_pe(pe) + 0.375 * score_ey(ecy)


def tier_for_score(s):
    idx = max(0, min(6, round(s) - 1))
    return ORDER[idx]
