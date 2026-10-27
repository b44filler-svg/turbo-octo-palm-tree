"""Builds the day's bets from researched candidates.

    python -m picks.build candidates.json --date 2026-10-27 \
        [--settings settings.json] [--stats stats.json] [--out tickets.json]

Bet types it produces:
  board    Best Bets: every single with positive value, ranked by Kelly growth,
           which rewards a high win chance AND a high payout together
  big      Big Bet: the one straight bet best suited to a larger stake
           (high win chance, real edge, low variance)
  pair     Power Pairs: 2-leg parlays of legs that are too juiced to bet alone
           but pay about even money or better together
  parlay3  the classic 3-leg parlay per sport, closest to a lock
"""
import argparse
import itertools
import json
import math
import sys

from . import oddsmath as om
from . import rules
from .model import score

SAME_GAME_PENALTY = 0.97   # per same-game pair; FanDuel reprices correlated SGPs

BIG = dict(min_prob=0.60, min_edge=0.025, min_ev=0.03, min_odds=-300, max_odds=150)
PAIR = dict(min_leg_prob=0.60, min_leg_edge=0.0, min_joint=0.42,
            min_dec=1.9, max_dec=3.6, min_ev=0.02, count=2)
PARLAY = dict(min_payout10=18.0, min_ev=0.0, min_leg_edge=0.0, soft_leg_edge=-0.01,
              min_odds=-500, max_odds=-150)
BOARD = dict(min_prob=0.50, min_ev=0.0, count=10, sweet_prob=0.55, sweet_dec=1.8)


def joint(legs):
    p = math.prod(l["modelProb"] for l in legs)
    return p * SAME_GAME_PENALTY ** rules.same_game_pairs(legs)


def combo_ok(legs):
    return not rules.ticket_conflicts(legs)


def ticket(kind, legs, date, sport=None, **extra):
    dec = om.parlay_dec([l["odds"] for l in legs])
    win = joint(legs)
    doc = {
        "kind": kind,
        "date": date,
        "sport": sport or (legs[0]["sport"] if len({l["sport"] for l in legs}) == 1 else "Mix"),
        "legs": legs,
        "parlayOdds": om.dec_to_am(dec),
        "payout10": round(10 * dec, 2),
        "winChance": round(win, 3),
        "ev": round(om.ev_dec(win, dec), 3),
        "growth": round(om.growth(win, dec), 5),
        "result": "open",
        "placed": False,
    }
    doc.update(extra)
    return doc


def eligible(scored, never_against):
    ok, dropped = [], []
    for l in scored:
        prot = rules.loyalty_violation(l, never_against)
        if prot:
            dropped.append((l, f"bets against {prot}"))
        else:
            ok.append(l)
    return ok, dropped


def build_board(legs):
    b = [l for l in legs if l["modelProb"] >= BOARD["min_prob"] and l["ev"] > BOARD["min_ev"]]
    b.sort(key=lambda l: l["growth"], reverse=True)
    out = []
    for l in b[:BOARD["count"]]:
        item = dict(l)
        item["sweetSpot"] = (l["modelProb"] >= BOARD["sweet_prob"]
                             and om.am_to_dec(l["odds"]) >= BOARD["sweet_dec"])
        out.append(item)
    return out


def build_big(legs, date):
    c = [l for l in legs
         if l["modelProb"] >= BIG["min_prob"] and l["edge"] >= BIG["min_edge"]
         and l["ev"] >= BIG["min_ev"] and BIG["min_odds"] <= l["odds"] <= BIG["max_odds"]
         and not l.get("fairFlag")]
    if not c:
        return None
    best = max(c, key=lambda l: l["growth"])
    dec = om.am_to_dec(best["odds"])
    return ticket("big", [best], date, kellyFull=round(om.kelly(best["modelProb"], dec), 4))


def build_pairs(legs, date, exclude=()):
    pool = [l for l in legs if l["modelProb"] >= PAIR["min_leg_prob"]
            and l["edge"] >= PAIR["min_leg_edge"] and l.get("pick") not in exclude]
    found = []
    for a, b in itertools.combinations(pool, 2):
        if not combo_ok([a, b]):
            continue
        t = ticket("pair", [a, b], date)
        dec = t["payout10"] / 10
        if (t["winChance"] >= PAIR["min_joint"] and PAIR["min_dec"] <= dec <= PAIR["max_dec"]
                and t["ev"] >= PAIR["min_ev"]):
            found.append(t)
    found.sort(key=lambda t: (t["growth"], diff_games(t)), reverse=True)
    out, used = [], set()
    for t in found:
        picks = {l["pick"] for l in t["legs"]}
        if picks & used:
            continue
        out.append(t)
        used |= picks
        if len(out) == PAIR["count"]:
            break
    return out


def diff_games(t):
    return len({l["game"] for l in t["legs"]})


def build_parlay(legs, date, sport):
    pool = [l for l in legs if l["sport"] == sport and l["edge"] >= PARLAY["soft_leg_edge"]
            and PARLAY["min_odds"] <= l["odds"] <= PARLAY["max_odds"]]
    found = []
    for combo in itertools.combinations(pool, 3):
        if sum(1 for l in combo if l["edge"] < PARLAY["min_leg_edge"]) > 1:
            continue
        if not combo_ok(list(combo)):
            continue
        t = ticket("parlay3", list(combo), date, sport=sport)
        if t["payout10"] >= PARLAY["min_payout10"] and t["ev"] >= PARLAY["min_ev"]:
            found.append(t)
    if not found:
        return None, []
    found.sort(key=lambda t: t["winChance"], reverse=True)
    top = found[0]["winChance"]
    close = [t for t in found if top - t["winChance"] <= 0.02]
    best = max(close, key=lambda t: sum(l["edge"] for l in t["legs"]))
    chosen = {l["pick"] for l in best["legs"]}
    backups = [l for l in pool if l["pick"] not in chosen and l["edge"] >= 0
               and combo_ok(best["legs"][:2] + [l])]
    if backups:
        best["backup"] = max(backups, key=lambda l: l["modelProb"])
    return best, found[:5]


def build_all(candidates, date, settings=None, stats=None):
    settings = settings or {}
    settings = settings.get("data", settings)   # accept a raw db export too
    stats = (stats or {}).get("data", stats)
    shrink = float((stats or {}).get("shrink", 0.5))
    scored = [score(c, shrink) for c in candidates]
    legs, dropped = eligible(scored, settings.get("neverAgainst", []))
    board = build_board(legs)
    big = build_big(legs, date)
    pairs = build_pairs(legs, date, exclude={big["legs"][0]["pick"]} if big else ())
    parlays, top5 = {}, {}
    for sport in sorted({l["sport"] for l in legs}):
        best, alts = build_parlay(legs, date, sport)
        if best:
            parlays[sport] = best
            top5[sport] = alts
    return {
        "date": date, "shrink": shrink, "candidates": len(candidates),
        "dropped": [{"pick": l.get("pick"), "why": why} for l, why in dropped],
        "board": board, "big": big, "pairs": pairs, "parlays": parlays, "top5": top5,
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("candidates")
    ap.add_argument("--date", required=True)
    ap.add_argument("--settings")
    ap.add_argument("--stats")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    load = lambda p: json.load(open(p)) if p else None
    res = build_all(load(a.candidates), a.date, load(a.settings), load(a.stats))
    text = json.dumps(res, indent=2)
    if a.out:
        open(a.out, "w").write(text)
    else:
        sys.stdout.write(text + "\n")
    show = lambda t: (f"{t['kind']:8} {t['parlayOdds']:+5d}  win {t['winChance']:.0%}  "
                      f"ev {t['ev']:+.1%}  | " + " + ".join(f"{l['pick']} ({l['odds']:+d})" for l in t["legs"]))
    print(f"{len(res['board'])} board bets, {len(res['dropped'])} legs dropped by loyalty rule", file=sys.stderr)
    for t in ([res["big"]] if res["big"] else []) + res["pairs"] + list(res["parlays"].values()):
        print(show(t), file=sys.stderr)


if __name__ == "__main__":
    main()
