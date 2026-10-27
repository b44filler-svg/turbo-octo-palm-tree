"""Odds conversions, no-vig fair prices, expected value and Kelly sizing.

American odds are ints like -150 or +130. Probabilities are floats in (0, 1).
"""
import math


def am_to_dec(am):
    am = float(am)
    if am == 0 or -100 < am < 100:
        raise ValueError(f"not a valid American price: {am}")
    return 1 + am / 100 if am > 0 else 1 + 100 / abs(am)


def dec_to_am(dec):
    if dec <= 1:
        raise ValueError(f"decimal odds must be above 1: {dec}")
    return round((dec - 1) * 100) if dec >= 2 else round(-100 / (dec - 1))


def implied(am):
    """Book's implied probability, vig included."""
    return 1 / am_to_dec(am)


def devig_multiplicative(prices):
    """Scale every side's implied probability so they sum to 1."""
    raw = [implied(p) for p in prices]
    total = sum(raw)
    return [r / total for r in raw]


def devig_power(prices, tol=1e-10):
    """Find k with sum(implied_i ** k) == 1.

    Takes more vig off longshots than favorites, which matches the
    favorite-longshot bias better than the multiplicative method.
    """
    raw = [implied(p) for p in prices]
    if sum(raw) <= 1:
        return devig_multiplicative(prices)
    lo, hi = 1.0, 10.0
    while hi - lo > tol:
        k = (lo + hi) / 2
        if sum(r ** k for r in raw) > 1:
            lo = k
        else:
            hi = k
    k = (lo + hi) / 2
    out = [r ** k for r in raw]
    total = sum(out)
    return [o / total for o in out]


def fair_prob(prices, method="power"):
    """No-vig probability of the FIRST price in `prices` (all sides of one market)."""
    if len(prices) < 2:
        raise ValueError("need every side of the market to remove the vig")
    fn = devig_power if method == "power" else devig_multiplicative
    return fn(prices)[0]


def parlay_dec(prices):
    return math.prod(am_to_dec(p) for p in prices)


def ev(p, am):
    """Expected profit per $1 staked."""
    return p * am_to_dec(am) - 1


def ev_dec(p, dec):
    return p * dec - 1


def kelly(p, dec):
    """Full-Kelly fraction of bankroll; 0 when the bet has no edge."""
    b = dec - 1
    return max(0.0, (b * p - (1 - p)) / b)


def growth(p, dec, frac=0.25):
    """Expected log growth per bet at a fraction of Kelly.

    One number that rewards both a high win chance and a high payout:
    a big edge at long odds and a small edge at short odds can score the same.
    """
    f = frac * kelly(p, dec)
    if f <= 0:
        return 0.0
    b = dec - 1
    return p * math.log(1 + f * b) + (1 - p) * math.log(1 - f)
