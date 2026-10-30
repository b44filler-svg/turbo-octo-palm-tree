"""Turns a researched candidate leg into a scored leg.

A candidate is a dict collected during research. Fields the model reads:
  odds          FanDuel American price (required)
  sharpOdds     every side of the same market at a sharp book, the pick's side
                FIRST, e.g. [-160, +140]; or `fairProb` if already computed
  hitRates      {"season":[hit,games], "last10":[..], "last5":[..], "lastSeason":[..]}
  type          moneyline | spread | total over | total under | team total over |
                team total under | prop over | prop under
  star          true for popular players (public money inflates their overs)
  adjust        matchup/role adjustment in probability points (clipped to +/-0.05)
  newsAdjust    expert consensus / news adjustment (clipped to +/-0.03)
  lineMovedAgainst  true if the line moved 15+ cents against the pick since open
"""
from . import oddsmath as om

STAT_WEIGHTS = {"season": 0.35, "last10": 0.30, "last5": 0.15, "lastSeason": 0.20}
FULL_SAMPLE = 20      # games of history before the stat model gets its full weight
# Games a window needs before it gets its full weight: early in a season, a
# 3-game "season" rate shouldn't outweigh last season's 70 games.
WINDOW_FULL = {"season": 15, "last10": 10, "last5": 5, "lastSeason": 30}
CAP, FLOOR = 0.92, 0.02
# Closing lines are hard to beat by much; a model far from the no-vig market is
# far more often wrong than right, so it may sit at most this far from it.
MAX_DEVIATION = 0.07
# Tighter leash when the fair price comes from a real two-sided sharp market
# (e.g. Pinnacle). Scanning dozens of props and keeping the biggest model edges
# overstates those edges (the "optimizer's curse", Smith & Winkler 2006), and
# on 10/28 twelve Pinnacle-priced singles the model put ~6-7 pts above the
# market hit 6 of 12 (50%), right at Pinnacle's ~51%, not the model's ~59%.
# Through 10/29, 31 graded legs hit 58% against 65% expected, and the 50-60%
# bucket hit 43%, so the stats may now move a sharp price by 2.5 points at most.
SHARP_MAX_DEVIATION = 0.025


def stat_prob(hit_rates):
    """Weighted hit rate across windows, plus how many games stand behind it."""
    if not hit_rates:
        return None, 0
    num = den = 0.0
    games = 0
    for key, w in STAT_WEIGHTS.items():
        hr = hit_rates.get(key)
        if not hr or not hr[1]:
            continue
        hit, n = hr
        w *= min(1.0, n / WINDOW_FULL[key])
        num += w * hit / n
        den += w
        if key in ("season", "lastSeason"):
            games += n
    if not den:
        return None, 0
    if not games:
        games = max(hr[1] for hr in hit_rates.values() if hr and hr[1])
    return num / den, games


LABELS = (("last10", "last 10"), ("last5", "last 5"), ("season", "season"), ("lastSeason", "last season"))


def hit_rate_text(hit_rates):
    """{"last10":[7,10], ...} -> "7/10 last 10, 3/4 season" for the app."""
    parts = [f"{hit_rates[k][0]}/{hit_rates[k][1]} {lab}" for k, lab in LABELS
             if (hit_rates or {}).get(k) and hit_rates[k][1]]
    return ", ".join(parts) or None


def fair_of(c):
    """(fair probability, flagged-as-thin). Needs both sides of a market."""
    if len(c.get("sharpOdds") or []) >= 2:
        return om.fair_prob(c["sharpOdds"]), False
    if c.get("fairProb") is not None:
        # A hand-made fair estimate only counts as sharp if it names its source.
        return float(c["fairProb"]), bool(c.get("fairFlag", not c.get("sharpSource")))
    # No sharp price at all: fall back to FanDuel's own two sides if given.
    if len(c.get("bookOdds") or []) >= 2:
        return om.fair_prob(c["bookOdds"]), True
    return None, True


def bias_adjust(c):
    t = str(c.get("type", "")).lower()
    if t == "prop under":
        return 0.015
    if t == "prop over" and c.get("star"):
        return -0.01
    return 0.0


def clip(x, lim):
    return max(-lim, min(lim, float(x or 0)))


def score(c, shrink=0.5):
    """Return a copy of candidate `c` with model fields filled in."""
    leg = dict(c)
    odds = int(c["odds"])
    implied = om.implied(odds)
    fair, flagged = fair_of(c)
    stat, games = stat_prob(c.get("hitRates"))

    if fair is None and stat is None:
        raise ValueError(f"no fair price and no stats for {c.get('pick')}")
    if fair is None:
        base = stat
    elif stat is None:
        base = fair
    else:
        # Thin samples lean on the market instead of the box score.
        stat_w = (1 - shrink) * min(1.0, games / FULL_SAMPLE)
        base = (1 - stat_w) * fair + stat_w * stat

    p = base + bias_adjust(c) + clip(c.get("adjust"), 0.05) + clip(c.get("newsAdjust"), 0.03)
    if c.get("lineMovedAgainst"):
        p -= 0.02
    if fair is not None:
        lim = MAX_DEVIATION if flagged else SHARP_MAX_DEVIATION
        p = max(fair - lim, min(fair + lim, p))
    p = max(FLOOR, min(CAP, p))

    p = round(p, 3)   # every stored number is derived from the stored modelProb
    dec = om.am_to_dec(odds)
    leg.update({
        "odds": odds,
        "impliedProb": round(implied, 3),
        "fairProb": round(fair, 3) if fair is not None else None,
        "statProb": round(stat, 3) if stat is not None else None,
        "fairFlag": flagged,
        "modelProb": round(p, 3),
        "edge": round(p - implied, 3),
        "ev": round(om.ev_dec(p, dec), 3),
        "growth": round(om.growth(p, dec), 5),
        # What the price is worth by the market alone, no stats: the sharp
        # no-vig chance times the payout. Below zero, the book is charging more
        # than the bet is worth.
        "marketEv": round(fair * dec - 1, 3) if fair is not None else None,
        "result": c.get("result", "open"),
    })
    if c.get("hitRates") and not c.get("hitRate"):
        leg["hitRate"] = hit_rate_text(c["hitRates"])
    return leg
