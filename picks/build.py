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
  boost    the best use of each sportsbook promo (profit boosts), with the
           boosted payout

Prices: a candidate can carry `prices` = {"FanDuel": -130, "DraftKings": -120, ...}.
Only books in settings `books` are used (empty = any book). Singles take the best
price; a parlay must be placed at one book, so each multi-leg ticket is built per
book and the book that pays most wins.
"""
import argparse
import itertools
import json
import math
import sys

from . import oddsmath as om
from . import rules
from .model import score

SAME_GAME_PENALTY = 0.97   # per same-game pair; only old tickets have these (combo_ok rejects them)

BIG = dict(min_prob=0.60, min_edge=0.025, min_ev=0.03, min_odds=-300, max_odds=150)
PAIR = dict(min_leg_prob=0.60, min_leg_edge=0.0, min_joint=0.42,
            min_dec=1.9, max_dec=3.6, min_ev=0.02, count=2)
PARLAY = dict(min_payout10=18.0, min_ev=0.0, min_leg_edge=0.0, soft_leg_edge=-0.01,
              min_odds=-500, max_odds=-150)
BOARD = dict(min_prob=0.50, min_ev=0.0, count=10, sweet_prob=0.55, sweet_dec=1.8)
BOOST = dict(min_joint=0.25, max_legs=4, min_leg_edge=-0.01, pool=25)

# Leans: used only when a bet type has nothing that clears its full bar, so the
# app is never empty. Still positive value, but a lower bar, and labeled as such.
LEAN_BIG = dict(min_prob=0.55, min_edge=0.005, min_ev=0.005, min_odds=-400, max_odds=200, allow_thin=True)
LEAN_PAIR = dict(min_leg_prob=0.55, min_leg_edge=-0.005, min_joint=0.35,
                 min_dec=1.7, max_dec=4.0, min_ev=0.005, count=1)
LEAN_PARLAY = dict(min_payout10=15.0, min_ev=0.0, min_leg_edge=-0.005, soft_leg_edge=-0.015,
                   min_odds=-600, max_odds=150)
# Payout plays: plus-money singles with real value, for a bigger payout.
LONGSHOT = dict(min_prob=0.35, min_ev=0.03, min_odds=120, count=5)


def joint(legs):
    p = math.prod(l["modelProb"] for l in legs)
    return p * SAME_GAME_PENALTY ** rules.same_game_pairs(legs)


def combo_ok(legs):
    # Same-game legs only go on a FanDuel Same Game Parlay, which has its own
    # (lower) price, so the product of the leg odds would overstate the payout.
    return not rules.ticket_conflicts(legs) and rules.same_game_pairs(legs) == 0


def boosted_dec(dec, pct):
    """A profit boost multiplies the winnings, not the stake."""
    return 1 + (dec - 1) * (1 + pct)


def ticket(kind, legs, date, sport=None, boost=None, **extra):
    base = om.parlay_dec([l["odds"] for l in legs])
    dec = boosted_dec(base, boost["pct"]) if boost else base
    win = joint(legs)
    books = {l.get("book") for l in legs}
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
    if len(books) == 1 and None not in books:
        doc["book"] = books.pop()
    if boost:
        doc["boost"] = boost
        doc["baseOdds"] = om.dec_to_am(base)
    doc.update(extra)
    return doc


def problem(c):
    """Why a candidate can't be used, or None. Bad legs are dropped, not fatal."""
    for f in ("sport", "game", "pick", "market", "type"):
        if c.get(f) in (None, ""):
            return f"missing `{f}`"
    if not c.get("prices") and (c.get("odds") in (None, "") or not c.get("source")):
        return "needs `prices` by book (or `odds` plus `source`)"
    t = str(c["type"]).lower()
    if t not in rules.TYPES:
        return f"unknown type {c['type']!r}"
    if t not in rules.NEUTRAL and not c.get("team"):
        return "missing `team` (needed for the never-against rule)"
    if t.startswith("prop") and not c.get("player"):
        return "prop without `player`"
    if not rules.game_teams(c["game"]):
        return f"game {c['game']!r} isn't 'Away @ Home'"
    for book, o in (c.get("prices") or {"": c.get("odds")}).items():
        try:
            om.am_to_dec(o)
        except (TypeError, ValueError):
            return f"bad odds {o!r}" + (f" at {book}" if book else "")
    return None


def book_of_source(source):
    s = str(source or "").lower()
    if "not fanduel" in s:
        return "Other"
    return "FanDuel" if "fanduel" in s else "Other"


def price_options(c, books):
    """{book: odds} this candidate can be bet at, limited to Bob's books."""
    if c.get("prices"):
        opts = dict(c["prices"])
    else:
        opts = {c.get("book") or book_of_source(c.get("source")): c["odds"]}
    if books:
        allowed = {b.lower(): b for b in books}
        opts = {allowed[b.lower()]: o for b, o in opts.items() if b.lower() in allowed}
    return {b: int(o) for b, o in opts.items()}


def prepare(candidates, shrink, never_against, books=()):
    """Score every usable candidate at every allowed book.

    Returns (best, by_book, dropped): `best` holds each leg at its best price,
    `by_book` maps each book to the legs priced there.
    """
    best, by_book, dropped = [], {}, []
    for c in candidates:
        why = problem(c)
        opts = {} if why else price_options(c, books)
        if not why and not opts:
            why = "not offered at your books (" + ", ".join(books) + ")"
        scored = {}
        if not why:
            try:
                for book, o in opts.items():
                    scored[book] = score({**c, "odds": o, "book": book,
                                          "source": c.get("source") or book}, shrink)
            except (TypeError, ValueError, KeyError) as e:
                why = f"can't score: {e}"
        top = max(scored.values(), key=lambda l: om.am_to_dec(l["odds"])) if scored else None
        if not why and top["fairProb"] is None:
            why = "no market price to anchor the model (stats only)"
        if not why:
            prot = rules.loyalty_violation(top, never_against)
            why = f"bets against {prot}" if prot else None
        if why:
            dropped.append((c, why))
            continue
        if len(scored) > 1:
            top["otherPrices"] = {b: l["odds"] for b, l in scored.items() if b != top["book"]}
        best.append(top)
        for book, l in scored.items():
            by_book.setdefault(book, []).append(l)
    return best, by_book, dropped


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


def build_big(legs, date, cfg=BIG):
    c = [l for l in legs
         if l["modelProb"] >= cfg["min_prob"] and l["edge"] >= cfg["min_edge"]
         and l["ev"] >= cfg["min_ev"] and cfg["min_odds"] <= l["odds"] <= cfg["max_odds"]
         and (cfg.get("allow_thin") or not l.get("fairFlag"))]
    if not c:
        return None
    best = max(c, key=lambda l: l["growth"])
    dec = om.am_to_dec(best["odds"])
    return ticket("big", [best], date, kellyFull=round(om.kelly(best["modelProb"], dec), 4))


def build_pairs(by_book, date, exclude=(), cfg=PAIR):
    found = []
    for legs in by_book.values():
        pool = [l for l in legs if l["modelProb"] >= cfg["min_leg_prob"]
                and l["edge"] >= cfg["min_leg_edge"] and l.get("pick") not in exclude]
        for a, b in itertools.combinations(pool, 2):
            if not combo_ok([a, b]):
                continue
            t = ticket("pair", [a, b], date)
            dec = t["payout10"] / 10
            if (t["winChance"] >= cfg["min_joint"] and cfg["min_dec"] <= dec <= cfg["max_dec"]
                    and t["ev"] >= cfg["min_ev"]):
                found.append(t)
    found.sort(key=lambda t: (t["growth"], diff_games(t)), reverse=True)
    out, used = [], set()
    for t in found:
        picks = {l["pick"] for l in t["legs"]}
        if picks & used:
            continue
        out.append(t)
        used |= picks
        if len(out) == cfg["count"]:
            break
    return out


def diff_games(t):
    return len({rules.game_key(l["game"]) for l in t["legs"]})


def build_parlay(legs, date, sport, cfg=PARLAY):
    pool = [l for l in legs if str(l["sport"]).upper() == sport and l["edge"] >= cfg["soft_leg_edge"]
            and cfg["min_odds"] <= l["odds"] <= cfg["max_odds"]]
    found = []
    for combo in itertools.combinations(pool, 3):
        if sum(1 for l in combo if l["edge"] < cfg["min_leg_edge"]) > 1:
            continue
        if not combo_ok(list(combo)):
            continue
        t = ticket("parlay3", list(combo), date, sport=sport)
        if t["payout10"] >= cfg["min_payout10"] and t["ev"] >= cfg["min_ev"]:
            found.append(t)
    if not found:
        return None, []
    found.sort(key=lambda t: t["winChance"], reverse=True)
    top = found[0]["winChance"]
    close = [t for t in found if top - t["winChance"] <= 0.02]
    best = max(close, key=lambda t: sum(l["edge"] for l in t["legs"]))
    chosen = {l["pick"] for l in best["legs"]}

    def swaps_in(l):
        # The backup must work in place of ANY of the three legs.
        for i in range(3):
            legs3 = best["legs"][:i] + best["legs"][i + 1:] + [l]
            if not combo_ok(legs3):
                return False
            t = ticket("parlay3", legs3, date, sport=sport)
            if t["payout10"] < cfg["min_payout10"] or t["ev"] < cfg["min_ev"]:
                return False
        return True

    backups = [l for l in pool if l["pick"] not in chosen and l["edge"] >= 0 and swaps_in(l)]
    if backups:
        best["backup"] = max(backups, key=lambda l: l["modelProb"])
    return best, found[:5]


def lean(t):
    """Mark a ticket that only cleared the lower 'lean' bar."""
    if t:
        t["tier"] = "lean"
    return t


def build_longshots(legs):
    """Plus-money singles with real value: lower win chance, bigger payout."""
    c = [l for l in legs if l["odds"] >= LONGSHOT["min_odds"] and l["modelProb"] >= LONGSHOT["min_prob"]
         and l["ev"] >= LONGSHOT["min_ev"]]
    return sorted(c, key=lambda l: l["growth"], reverse=True)[:LONGSHOT["count"]]


def build_boosts(by_book, boosts, date):
    """Best ticket for each promo: the combo with the most growth at the boosted price.

    A boost is {"id","book","title","pct":0.30,"minLegs":3,"maxLegs":4,
    "minLegOdds":-250,"minTotalOdds":200,"sport":"NBA","maxStake":25}; missing
    limits are open. Only legs at the boost's book can be used.
    """
    out = []
    for b in boosts:
        pct = float(b.get("pct") or 0)
        legs = by_book.get(b.get("book"), [])
        pool = [l for l in legs if l["edge"] >= BOOST["min_leg_edge"]
                and (not b.get("sport") or str(l["sport"]).upper() == str(b["sport"]).upper())
                and l["odds"] >= b.get("minLegOdds", -10000)]
        pool = sorted(pool, key=lambda l: l["modelProb"], reverse=True)[:BOOST["pool"]]
        lo, hi = int(b.get("minLegs", 1)), min(int(b.get("maxLegs", 3)), BOOST["max_legs"])
        best = None
        for n in range(max(1, lo), hi + 1):
            for combo in itertools.combinations(pool, n):
                if not combo_ok(list(combo)):
                    continue
                if b.get("minTotalOdds") is not None and \
                        om.dec_to_am(om.parlay_dec([l["odds"] for l in combo])) < b["minTotalOdds"]:
                    continue
                info = {k: b[k] for k in ("id", "book", "title", "maxStake", "expires") if k in b}
                info["pct"] = pct
                t = ticket("boost", list(combo), date, boost=info)
                if t["winChance"] < BOOST["min_joint"] or t["ev"] <= 0:
                    continue
                if best is None or t["growth"] > best["growth"]:
                    best = t
        if best:
            out.append(best)
    return out


def build_all(candidates, date, settings=None, stats=None, boosts_in=None):
    settings = settings or {}
    settings = settings.get("data", settings)   # accept a raw db export too
    if not isinstance(settings.get("neverAgainst"), list):
        # Never build without the loyalty rule; an empty list must be explicit.
        raise ValueError("settings need a `neverAgainst` list (use [] for none)")
    stats = (stats or {}).get("data", stats)
    shrink = float((stats or {}).get("shrink", 0.5))
    books = settings.get("books") or []
    legs, by_book, dropped = prepare(candidates, shrink, settings["neverAgainst"], books)
    board = build_board(legs)
    big = build_big(legs, date) or lean(build_big(legs, date, LEAN_BIG))
    pairs = build_pairs(by_book, date, exclude={big["legs"][0]["pick"]} if big else ())
    if not pairs:
        pairs = [lean(t) for t in build_pairs(by_book, date, {big["legs"][0]["pick"]} if big else (), LEAN_PAIR)]
    parlays, top5 = {}, {}
    for sport in sorted({str(l["sport"]).upper() for l in legs}):
        options = [r for r in (build_parlay(bl, date, sport) for bl in by_book.values()) if r[0]]
        if not options:
            options = [(lean(r[0]), r[1]) for r in
                       (build_parlay(bl, date, sport, LEAN_PARLAY) for bl in by_book.values()) if r[0]]
        if options:
            # Closest to a lock first; between books within 2 points, the bigger payout.
            top = max(o[0]["winChance"] for o in options)
            best, alts = max((o for o in options if top - o[0]["winChance"] <= 0.02),
                             key=lambda o: o[0]["payout10"])
            parlays[sport] = best
            top5[sport] = alts
    boosts = build_boosts(by_book, boosts_in, date) if boosts_in else []
    return {
        "date": date, "shrink": shrink, "candidates": len(candidates), "books": books,
        "dropped": [{"pick": l.get("pick"), "why": why} for l, why in dropped],
        "board": board, "big": big, "pairs": pairs, "parlays": parlays, "top5": top5,
        "boosts": boosts, "longshots": build_longshots(legs),
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("candidates")
    ap.add_argument("--date", required=True)
    ap.add_argument("--settings")
    ap.add_argument("--stats")
    ap.add_argument("--boosts", help="JSON list of today's promos (profit boosts)")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    load = lambda p: json.load(open(p)) if p else None
    res = build_all(load(a.candidates), a.date, load(a.settings), load(a.stats), load(a.boosts))
    text = json.dumps(res, indent=2)
    if a.out:
        open(a.out, "w").write(text)
    else:
        sys.stdout.write(text + "\n")
    show = lambda t: (f"{t['kind']:8} {t.get('book', '?'):10} {t['parlayOdds']:+5d}  win {t['winChance']:.0%}  "
                      f"ev {t['ev']:+.1%}  | " + " + ".join(f"{l['pick']} ({l['odds']:+d})" for l in t["legs"]))
    print(f"{len(res['board'])} board bets, {len(res['dropped'])} candidates dropped (see `dropped`)", file=sys.stderr)
    for t in ([res["big"]] if res["big"] else []) + res["pairs"] + list(res["parlays"].values()) + res["boosts"]:
        print(show(t), file=sys.stderr)


if __name__ == "__main__":
    main()
