"""Independent checker for tickets before they are written to the app.

    python -m picks.audit tickets.json [--settings settings.json] [--now ISO] [--json]

`tickets.json` is either a build output (from picks.build) or a list / dict of
ticket documents. Exit code 1 if anything FAILs.

This file deliberately does NOT import oddsmath, model or build: it redoes the
math from the raw prices on its own, so a bug in the builder can't hide here.
"""
import argparse
import json
import math
import re
import sys
from datetime import datetime, timezone

from . import rules  # rules are checked again from the leg fields, not trusted from the builder

TOL_P, TOL_PAY, TOL_EV = 0.0025, 0.02, 0.006
EPS = 0.0006   # stored numbers are rounded to 3 places; thresholds allow for that

# Thresholds, written out again here on purpose (tests check they match picks.build).
# If someone loosens the builder, this checker still holds the line.
MARKET_FLOOR = -0.02          # no leg priced more than 2% worse than the sharp no-vig price
SHARP_LEASH = 0.026           # model vs sharp no-vig price (0.025 plus rounding)
BIG = dict(min_prob=0.60, min_edge=0.025, min_ev=0.03, min_odds=-300, max_odds=150, min_market_ev=0.0)
PAIR = dict(min_leg_prob=0.60, min_joint=0.42, min_dec=1.9, max_dec=3.6, min_ev=0.02, min_market_ev=0.0)
PARLAY = dict(min_payout10=18.0, min_ev=0.0, min_odds=-500, max_odds=-150, min_market_ev=0.0)
BOARD = dict(min_prob=0.50, min_market_ev=0.0)
BOOST_MIN_JOINT = 0.25
BOOST_MIN_MARKET_EV = 0.0
# The lower "lean" bar, used only when nothing clears the full one (still positive value).
LEAN_BIG = dict(min_prob=0.55, min_edge=0.005, min_ev=0.005, min_odds=-400, max_odds=200, min_market_ev=-0.02)
LEAN_PAIR = dict(min_leg_prob=0.55, min_joint=0.35, min_dec=1.7, max_dec=4.0, min_ev=0.005, min_market_ev=-0.02)
LEAN_PARLAY = dict(min_payout10=15.0, min_ev=0.0, min_odds=-600, max_odds=150, min_leg_edge=-0.005,
                   min_market_ev=-0.02)
LONGSHOT = dict(min_prob=0.35, min_ev=0.03, min_odds=120, min_market_ev=0.0)
SGP_MIN_JOINT = 0.12
LEGS_BY_KIND = {"big": 1, "pair": 2, "parlay3": 3}
BOOST_LEGS = (1, 4)   # a boost ticket may have 1 to 4 legs
REQUIRED = ("sport", "game", "pick", "market", "type", "odds", "modelProb", "source")
BANNED = re.compile(r"\b(lock(ed)? ?in|guarantee[ds]?|can'?t lose|sure thing|free money)\b", re.I)


def dec(am):
    am = float(am)
    if not (am >= 100 or am <= -100):
        raise ValueError(am)
    return am / 100 + 1 if am > 0 else 100 / -am + 1


def safe_dec(am):
    try:
        return dec(am)
    except (TypeError, ValueError):
        return None


def sgn(x):
    try:
        return f"{int(round(float(x))):+d}"
    except (TypeError, ValueError):
        return repr(x)


def american(d):
    return round((d - 1) * 100) if d >= 2 else round(-100 / (d - 1))


def power_fair(odds):
    """No-vig chance of the first side, power method, solved here by bisection
    (its own code, not oddsmath's): find k with sum(implied_i ** k) = 1."""
    imps = [1 / dec(o) for o in odds]
    lo, hi = 0.5, 3.0
    for _ in range(80):
        k = (lo + hi) / 2
        if sum(p ** k for p in imps) > 1:
            lo = k
        else:
            hi = k
    return imps[0] ** ((lo + hi) / 2)


def sharp_fair(l):
    """(fair chance from the leg's own sharp prices, or None)."""
    if l.get("fairFlag"):
        return None
    so = l.get("sharpOdds") or []
    if len(so) < 2:
        # A sourced fair estimate without both raw prices: same as the builder.
        return l.get("fairProb") if l.get("fairFlag") is False else None
    try:
        return power_fair(so)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def market_value(l):
    """Value by the sharp market alone, recomputed from the raw prices."""
    f, d = sharp_fair(l), safe_dec(l.get("odds"))
    return None if f is None or d is None else f * d - 1


class Report:
    def __init__(self, tid):
        self.tid, self.items = tid, []

    def fail(self, code, msg):
        self.items.append({"level": "FAIL", "code": code, "msg": msg})

    def warn(self, code, msg):
        self.items.append({"level": "WARN", "code": code, "msg": msg})

    @property
    def status(self):
        lv = {i["level"] for i in self.items}
        return "FAIL" if "FAIL" in lv else "WARN" if "WARN" in lv else "PASS"


def check_leg(r, l, where, never_against, now, legacy=False, books=None):
    for f in REQUIRED:
        if l.get(f) in (None, ""):
            r.fail("missing-field", f"{where}: missing `{f}`")
    d = safe_dec(l.get("odds"))
    if d is None:
        r.fail("bad-odds", f"{where}: odds {l.get('odds')!r} is not a valid American price")
        return
    imp = 1 / d
    if l.get("impliedProb") is not None and abs(l["impliedProb"] - imp) > TOL_P:
        r.fail("implied-math", f"{where}: impliedProb {l['impliedProb']} but {sgn(l['odds'])} implies {imp:.3f}")
    p = l.get("modelProb")
    if p is not None:
        if not 0 < p <= 0.92:
            r.fail("model-cap", f"{where}: modelProb {p} outside (0, 0.92]")
        if l.get("edge") is not None and abs(l["edge"] - (p - imp)) > TOL_P:
            r.fail("edge-math", f"{where}: edge {l['edge']} but modelProb - implied = {p - imp:.3f}")
        if p - imp < -EPS:
            r.warn("negative-edge", f"{where}: priced worse than the model's estimate (edge {p - imp:+.3f})")
        # Own copy of the leash (not imported): 2.5 pts from a sharp two-sided price.
        if l.get("fairProb") is not None and l.get("fairFlag") is False and abs(p - l["fairProb"]) > SHARP_LEASH:
            r.fail("sharp-leash", f"{where}: model {p:.3f} is {abs(p - l['fairProb']):.3f} from the sharp no-vig price {l['fairProb']:.3f}; max 0.025")
        elif l.get("fairProb") is not None and abs(p - l["fairProb"]) > 0.071:
            r.warn("far-from-market", f"{where}: model {p:.2f} is {abs(p - l['fairProb']):.2f} from the no-vig market {l['fairProb']:.2f}; double-check the stats")
    if l.get("fairProb") is None and not legacy:
        r.fail("no-fair", f"{where}: no market fair price; stats-only legs can't post")
    f = sharp_fair(l)
    if f is not None and not legacy:
        if l.get("fairProb") is not None and abs(l["fairProb"] - f) > 0.003:
            r.fail("fair-math", f"{where}: fairProb {l['fairProb']} but the sharp prices {l.get('sharpOdds')} make {f:.3f}")
        mv = f * d - 1
        if l.get("marketEv") is not None and abs(l["marketEv"] - mv) > TOL_EV:
            r.fail("market-math", f"{where}: marketEv {l['marketEv']} but sharp fair x payout - 1 = {mv:+.3f}")
        if mv < MARKET_FLOOR - EPS:
            r.fail("overpriced", f"{where}: priced {mv:+.1%} against the sharp market (floor {MARKET_FLOOR:+.0%})")
    t = str(l.get("type", "")).lower()
    if t not in rules.NEUTRAL and not l.get("team"):
        r.fail("missing-team", f"{where}: non-total leg needs `team` so the loyalty rule can be checked")
    prot = rules.loyalty_violation(l, never_against)
    if prot:
        r.fail("loyalty", f"{where}: '{l.get('pick')}' bets against the {prot} (never-against rule)")
    book = l.get("book")
    if book and books and book.lower() not in {b.lower() for b in books}:
        r.fail("wrong-book", f"{where}: priced at {book}, which isn't one of Bob's books ({', '.join(books)})")
    elif not book and not legacy:
        r.warn("no-book", f"{where}: doesn't say which sportsbook the price is from")
    sharp = str(l.get("sharpSource", ""))
    if l.get("fairFlag") or re.search(r"thin|one-sided|fanduel only", sharp, re.I):
        r.warn("thin-fair", f"{where}: fair price is from a thin or one-sided market")
    for f in ("why", "articleNote"):
        if BANNED.search(str(l.get(f, ""))):
            r.fail("honesty", f"{where}: `{f}` promises a sure win")
    st = l.get("startISO")
    if st and now:
        try:
            if datetime.fromisoformat(st.replace("Z", "+00:00")) <= now:
                r.fail("started", f"{where}: game already started at {st}")
        except ValueError:
            r.warn("bad-start", f"{where}: startISO {st!r} is not ISO time")


def check_ticket(tid, t, never_against, now=None, books=None):
    r = Report(tid)
    legs = t.get("legs") or []
    legacy = "kind" not in t   # tickets from before bet types existed
    kind = t.get("kind") or {1: "big", 2: "pair"}.get(len(legs), "parlay3")
    want = LEGS_BY_KIND.get(kind)
    boost = t.get("boost") if kind == "boost" else None
    if kind == "boost":
        if not (BOOST_LEGS[0] <= len(legs) <= BOOST_LEGS[1]):
            r.fail("leg-count", f"boost tickets need 1-4 legs, has {len(legs)}")
        if not boost or not isinstance(boost.get("pct"), (int, float)) or not boost.get("book"):
            r.fail("boost-info", "boost ticket needs `boost` with `book` and `pct`")
            boost = None
        elif any(l.get("book") != boost["book"] for l in legs):
            r.fail("boost-book", f"every leg must be priced at {boost['book']}, the book offering the boost")
    elif want is None:
        r.fail("bad-kind", f"unknown kind {kind!r}")
    elif len(legs) != want:
        r.fail("leg-count", f"{kind} needs {want} legs, has {len(legs)}")
    leg_books = {l.get("book") for l in legs if l.get("book")}
    if len(legs) > 1 and len(leg_books) > 1:
        r.fail("mixed-books", f"a parlay has to be placed at one book, legs are at {sorted(leg_books)}")
    if t.get("book") and leg_books and leg_books != {t["book"]}:
        r.fail("ticket-book", f"ticket says {t['book']} but legs are at {sorted(leg_books)}")
    for i, l in enumerate(legs):
        check_leg(r, l, f"leg {i + 1}", never_against, now, legacy, books)
    if t.get("backup"):
        check_leg(r, t["backup"], "backup", never_against, now, legacy, books)
    for i, j, why in rules.ticket_conflicts(legs):
        r.fail("conflict", f"legs {i + 1} and {j + 1}: {why}")
    if not legacy and len(legs) > 1 and rules.same_game_pairs(legs):
        r.fail("same-game", "two legs from one game: the book only takes them as a Same Game Parlay, "
                            "at its own price, so the payout shown isn't on offer")
    if t.get("sport") and t.get("sport") != "Mix" and kind == "parlay3":
        other = {l.get("sport") for l in legs} - {t["sport"]}
        if other:
            r.fail("mixed-sport", f"{t['sport']} ticket has {other} legs")
    if BANNED.search(" ".join(str(t.get(f, "")) for f in ("title", "summary", "watch", "checkNote"))):
        r.fail("honesty", "ticket text promises a sure win")

    decs = [safe_dec(l.get("odds")) for l in legs]
    if not legs or None in decs:
        return r
    d = math.prod(decs)
    if boost:
        d = 1 + (d - 1) * (1 + boost["pct"])   # profit boost: winnings grow, stake doesn't
    if t.get("payout10") is not None and abs(t["payout10"] - 10 * d) > TOL_PAY:
        r.fail("payout-math", f"payout10 {t['payout10']} but the prices pay {10 * d:.2f}")
    if t.get("parlayOdds") is not None and (len(legs) > 1 or boost) and abs(float(t["parlayOdds"]) - american(d)) > 1:
        r.fail("odds-math", f"parlayOdds {sgn(t['parlayOdds'])} but the prices make {american(d):+d}")
    probs = [l.get("modelProb") for l in legs]
    if all(isinstance(p, (int, float)) for p in probs):
        sg = rules.same_game_pairs(legs)
        win = math.prod(probs) * 0.97 ** sg
        if t.get("winChance") is not None and abs(t["winChance"] - win) > 0.004:
            r.fail("joint-math", f"winChance {t['winChance']} but legs multiply to {win:.3f}")
        e = win * d - 1
        if t.get("ev") is not None and abs(t["ev"] - e) > TOL_EV:
            r.fail("ev-math", f"ev {t['ev']} but winChance x payout - 1 = {e:+.3f}")
        if legacy:
            return r
        lean = t.get("tier") == "lean"
        if t.get("tier") not in (None, "lean"):
            r.fail("bad-tier", f"unknown tier {t.get('tier')!r}")
        big_c, pair_c, parlay_c = (LEAN_BIG, LEAN_PAIR, LEAN_PARLAY) if lean else (BIG, PAIR, PARLAY)
        floor = {"big": big_c, "pair": pair_c, "parlay3": parlay_c}.get(kind, {}).get("min_market_ev")
        if kind == "boost":
            floor = BOOST_MIN_MARKET_EV
        fairs = [sharp_fair(l) for l in legs]
        if None in fairs:
            if floor is not None and floor >= 0:
                r.fail("market-value", "a full-tier ticket needs a sharp price on every leg")
        else:
            mwin = math.prod(fairs)
            me = mwin * d - 1
            if t.get("marketEv") is not None and abs(t["marketEv"] - me) > TOL_EV:
                r.fail("market-math", f"marketEv {t['marketEv']} but sharp chances x payout - 1 = {me:+.3f}")
            if floor is not None and me < floor - EPS:
                r.fail("market-value", f"worth {me:+.1%} by the sharp market alone; needs {floor:+.1%}")
        if kind == "big":
            l, o = legs[0], float(legs[0]["odds"])
            if l["modelProb"] < big_c["min_prob"] - EPS or l["modelProb"] - 1 / d < big_c["min_edge"] - EPS:
                r.fail("big-threshold", f"Big Bet needs win >= {big_c['min_prob']:.0%} and edge >= {big_c['min_edge']*100:.1f} pts (has {l['modelProb']:.0%}, {l['modelProb'] - 1 / d:+.3f})")
            if e < big_c["min_ev"] - EPS:
                r.fail("big-value", f"Big Bet model value {e:+.1%} is under {big_c['min_ev']:+.1%}")
            if not big_c["min_odds"] <= o <= big_c["max_odds"]:
                r.fail("big-odds", f"Big Bet price {sgn(o)} is outside {big_c['min_odds']} to +{big_c['max_odds']}")
            if l.get("fairFlag") and not lean:
                r.fail("big-thin", "Big Bet can't rest on a thin fair price")
        if kind == "pair":
            if not (pair_c["min_dec"] - 0.005 <= d <= pair_c["max_dec"] + 0.005 and win >= pair_c["min_joint"] - EPS):
                r.fail("pair-threshold", f"Power Pair needs {pair_c['min_dec']}x-{pair_c['max_dec']}x payout and {pair_c['min_joint']:.0%}+ win (has {d:.2f}x, {win:.0%})")
            if e < pair_c["min_ev"] - EPS:
                r.fail("pair-value", f"Power Pair model value {e:+.1%} is under {pair_c['min_ev']:+.1%}")
            weak = [l["pick"] for l in legs if l["modelProb"] < pair_c["min_leg_prob"] - EPS]
            if weak:
                r.fail("pair-leg", f"Power Pair legs must each be {pair_c['min_leg_prob']:.0%}+: {weak}")
        if kind == "parlay3":
            if 10 * d < parlay_c["min_payout10"] - TOL_PAY:
                r.fail("parlay-payout", f"$10 pays {10 * d:.2f}, under the ${parlay_c['min_payout10']:.0f} floor")
            if e < parlay_c["min_ev"] - EPS:
                r.fail("parlay-value", f"negative model value {e:+.1%}")
            neg = sum(1 for l in legs if l["modelProb"] - 1 / dec(l["odds"]) < parlay_c.get("min_leg_edge", 0) - EPS)
            if neg > 1:
                r.fail("filler-legs", f"{neg} legs with negative edge (max 1)")
            out = [sgn(l["odds"]) for l in legs if not parlay_c["min_odds"] <= float(l["odds"]) <= parlay_c["max_odds"]]
            if out:
                r.fail("parlay-odds", f"3-leg parlay legs must be {parlay_c['min_odds']} to {parlay_c['max_odds']}: {out}")
        if kind == "boost":
            if e <= 0:
                r.fail("boost-value", f"even with the boost, model value is {e:+.1%}")
            if win < BOOST_MIN_JOINT - EPS:
                r.fail("boost-longshot", f"win chance {win:.0%} is under {BOOST_MIN_JOINT:.0%}")
    return r


def check_sgp(r, t, where, never_against, now, books):
    """Same Game Parlay builds: one game, one book, real math, honest prices."""
    legs = t.get("legs") or []
    if len(legs) != 3:
        r.fail("sgp-legs", f"{where}: needs 3 legs, has {len(legs)}")
        return
    for i, l in enumerate(legs):
        check_leg(r, l, f"{where} leg {i + 1}", never_against, now, books=books)
    if len({rules.game_key(l.get("game", "")) for l in legs}) != 1:
        r.fail("sgp-games", f"{where}: legs must all come from one game")
    if len({l.get("book") for l in legs}) != 1:
        r.fail("sgp-book", f"{where}: legs must all be at one book")
    players = [l.get("player") for l in legs if l.get("player")]
    if len(players) != len(set(players)):
        r.fail("sgp-player", f"{where}: the same player twice")
    for i, j, why in rules.ticket_conflicts(legs):
        r.fail("conflict", f"{where} legs {i + 1} and {j + 1}: {why}")
    fairs = [sharp_fair(l) for l in legs]
    decs = [safe_dec(l.get("odds")) for l in legs]
    if None in fairs or None in decs:
        r.fail("sgp-market", f"{where}: every leg needs a sharp two-sided price")
        return
    win, d = math.prod(fairs), math.prod(decs)
    if abs(float(t.get("fairJoint", -1)) - win) > 0.004:
        r.fail("sgp-joint", f"{where}: fairJoint {t.get('fairJoint')} but the sharp chances multiply to {win:.3f}")
    if win < SGP_MIN_JOINT - EPS:
        r.fail("sgp-longshot", f"{where}: {win:.0%} to hit is under {SGP_MIN_JOINT:.0%}")
    if t.get("straightOdds") is not None and abs(float(t["straightOdds"]) - american(d)) > 1:
        r.fail("sgp-odds", f"{where}: straightOdds {sgn(t['straightOdds'])} but the legs multiply to {american(d):+d}")
    for pct, am in (t.get("breakEven") or {}).items():
        need = 1 + (1 / win - 1) / (1 + float(pct))
        if safe_dec(am) is None or abs(safe_dec(am) - need) > 0.02:
            r.fail("sgp-break-even", f"{where}: break-even at {float(pct):.0%} boost should be {american(need):+d}, not {sgn(am)}")


def tickets_from(obj):
    """Accept build output, a list of docs, or {id: doc}."""
    if isinstance(obj, dict) and "board" in obj:
        out = {}
        if obj.get("big"):
            out["big"] = obj["big"]
        for i, p in enumerate(obj.get("pairs") or [], 1):
            out[f"pair-{i}"] = p
        for s, p in (obj.get("parlays") or {}).items():
            out[s.lower()] = p
        for i, p in enumerate(obj.get("boosts") or [], 1):
            out[f"boost-{i}"] = p
        return out, obj.get("board") or []
    if isinstance(obj, list):
        return {d.get("_id") or d.get("id") or f"#{i}": d.get("data", d) for i, d in enumerate(obj)}, []
    return {k: v.get("data", v) if isinstance(v, dict) else v for k, v in obj.items()}, []


def audit(obj, never_against, now=None, books=None):
    tickets, board = tickets_from(obj)
    reports = [check_ticket(tid, t, never_against, now, books) for tid, t in tickets.items()]
    br = Report("board")
    for i, l in enumerate(board):
        check_leg(br, l, f"board {i + 1}", never_against, now, books=books)
        bd = safe_dec(l.get("odds"))
        if l.get("modelProb") is not None and bd:
            if l["modelProb"] * bd - 1 <= 0:
                br.fail("board-value", f"board {i + 1}: no positive value, shouldn't be listed")
            if l["modelProb"] < BOARD["min_prob"] - EPS:
                br.fail("board-prob", f"board {i + 1}: under 50% to win")
            mv = market_value(l)
            if mv is None or mv < BOARD["min_market_ev"] - EPS:
                br.fail("board-market", f"board {i + 1}: FanDuel's price isn't at least the sharp fair price")
    if board:
        reports.append(br)
    lr = Report("longshots")
    for i, l in enumerate(obj.get("longshots") or [] if isinstance(obj, dict) else []):
        check_leg(lr, l, f"payout play {i + 1}", never_against, now, books=books)
        ld = safe_dec(l.get("odds"))
        if ld and l.get("modelProb") is not None:
            if l["odds"] < LONGSHOT["min_odds"] or l["modelProb"] < LONGSHOT["min_prob"] - EPS \
                    or l["modelProb"] * ld - 1 < LONGSHOT["min_ev"] - EPS:
                lr.fail("longshot-bar", f"payout play {i + 1} needs +120 or longer, 35%+ to win and +3% value")
            mv = market_value(l)
            if mv is None or mv < LONGSHOT["min_market_ev"] - EPS:
                lr.fail("longshot-market", f"payout play {i + 1}: FanDuel's price isn't at least the sharp fair price")
    if lr.items:
        reports.append(lr)
    sgps = (obj.get("sgp") or []) if isinstance(obj, dict) else []
    sr = Report("sgp")
    for i, t in enumerate(sgps):
        check_sgp(sr, t, f"SGP {i + 1}", never_against, now, books)
    if sgps:
        reports.append(sr)
    # The same pick on several tickets stacks risk on one outcome.
    seen = {}
    for tid, t in tickets.items():
        for l in t.get("legs") or []:
            seen.setdefault(l.get("pick"), []).append(tid)
    for pick, where in seen.items():
        if len(where) > 2:
            reports.append(Report("exposure"))
            reports[-1].warn("exposure", f"'{pick}' is on {len(where)} tickets ({', '.join(where)})")
    return reports


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("tickets")
    ap.add_argument("--settings")
    ap.add_argument("--now", help="ISO time; games starting before it FAIL")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    settings = json.load(open(a.settings)) if a.settings else {}
    settings = settings.get("data", settings)
    if not isinstance(settings.get("neverAgainst"), list):
        print("[FAIL] settings: no `neverAgainst` list, so the loyalty rule can't be checked (pass --settings)")
        return 1
    now = datetime.fromisoformat(a.now.replace("Z", "+00:00")) if a.now else datetime.now(timezone.utc)
    reports = audit(json.load(open(a.tickets)), settings.get("neverAgainst", []), now, settings.get("books"))
    if a.json:
        print(json.dumps([{"ticket": r.tid, "status": r.status, "findings": r.items} for r in reports], indent=2))
    else:
        for r in reports:
            print(f"[{r.status}] {r.tid}")
            for i in r.items:
                print(f"   {i['level']:4} {i['code']}: {i['msg']}")
    fails = sum(r.status == "FAIL" for r in reports)
    print(f"\n{len(reports)} checked, {fails} failing", file=sys.stderr)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
