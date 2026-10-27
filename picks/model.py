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
CAP, FLOOR = 0.92, 0.02
# Closing lines are hard to beat by much; a model far from the no-vig market is
# far more often wrong than right, so it may sit at most this far from it.
MAX_DEVIATION = 0.07


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
        num += w * hit / n
        den += w
        if key in ("season", "lastSeason"):
            games += n
    if not den:
        return None, 0
    if not games:
        games = max(hr[1] for hr in hit_rates.values() if hr and hr[1])
    return num / den, games


def fair_of(c):
    if c.get("sharpOdds"):
        return om.fair_prob(c["sharpOdds"]), False
    if c.get("fairProb") is not None:
        return float(c["fairProb"]), bool(c.get("fairFlag"))
    # No sharp price at all: fall back to FanDuel's own two sides if given.
    if c.get("bookOdds"):
        return om.fair_prob(c["bookOdds"]), True
    return None, True


def bias_adjust(c):
    t = c.get("type", "")
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
        p = max(fair - MAX_DEVIATION, min(fair + MAX_DEVIATION, p))
    p = max(FLOOR, min(CAP, p))

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
        "result": c.get("result", "open"),
    })
    return leg
