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
LEGS_BY_KIND = {"big": 1, "pair": 2, "parlay3": 3}
REQUIRED = ("sport", "game", "pick", "market", "type", "odds", "modelProb", "source")
BANNED = re.compile(r"\b(lock(ed)? ?in|guarantee[ds]?|can'?t lose|sure thing|free money)\b", re.I)


def dec(am):
    am = float(am)
    if not (am >= 100 or am <= -100):
        raise ValueError(am)
    return am / 100 + 1 if am > 0 else 100 / -am + 1


def american(d):
    return round((d - 1) * 100) if d >= 2 else round(-100 / (d - 1))


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


def check_leg(r, l, where, never_against, now):
    for f in REQUIRED:
        if l.get(f) in (None, ""):
            r.fail("missing-field", f"{where}: missing `{f}`")
    try:
        d = dec(l["odds"])
    except (KeyError, ValueError, TypeError):
        r.fail("bad-odds", f"{where}: odds {l.get('odds')!r} is not a valid American price")
        return
    imp = 1 / d
    if l.get("impliedProb") is not None and abs(l["impliedProb"] - imp) > TOL_P:
        r.fail("implied-math", f"{where}: impliedProb {l['impliedProb']} but {l['odds']:+d} implies {imp:.3f}")
    p = l.get("modelProb")
    if p is not None:
        if not 0 < p <= 0.92:
            r.fail("model-cap", f"{where}: modelProb {p} outside (0, 0.92]")
        if l.get("edge") is not None and abs(l["edge"] - (p - imp)) > TOL_P:
            r.fail("edge-math", f"{where}: edge {l['edge']} but modelProb - implied = {p - imp:.3f}")
        if p - imp < 0:
            r.warn("negative-edge", f"{where}: priced worse than the model's estimate (edge {p - imp:+.3f})")
        if l.get("fairProb") is not None and abs(p - l["fairProb"]) > 0.0705:
            r.warn("far-from-market", f"{where}: model {p:.2f} is {abs(p - l['fairProb']):.2f} from the no-vig market {l['fairProb']:.2f}; double-check the stats")
    t = str(l.get("type", "")).lower()
    if t not in rules.NEUTRAL and not l.get("team"):
        r.fail("missing-team", f"{where}: non-total leg needs `team` so the loyalty rule can be checked")
    prot = rules.loyalty_violation(l, never_against)
    if prot:
        r.fail("loyalty", f"{where}: '{l.get('pick')}' bets against the {prot} (never-against rule)")
    src = str(l.get("source", ""))
    if src and ("fanduel" not in src.lower() or re.search(r"not fanduel", src, re.I)):
        r.warn("not-fanduel", f"{where}: price is not confirmed as FanDuel's ({src[:60]})")
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


def check_ticket(tid, t, never_against, now=None):
    r = Report(tid)
    kind = t.get("kind", "parlay3")
    legs = t.get("legs") or []
    want = LEGS_BY_KIND.get(kind)
    if want is None:
        r.fail("bad-kind", f"unknown kind {kind!r}")
    elif len(legs) != want:
        r.fail("leg-count", f"{kind} needs {want} legs, has {len(legs)}")
    for i, l in enumerate(legs):
        check_leg(r, l, f"leg {i + 1}", never_against, now)
    if t.get("backup"):
        check_leg(r, t["backup"], "backup", never_against, now)
    for i, j, why in rules.ticket_conflicts(legs):
        r.fail("conflict", f"legs {i + 1} and {j + 1}: {why}")
    if t.get("sport") and t.get("sport") != "Mix" and kind == "parlay3":
        other = {l.get("sport") for l in legs} - {t["sport"]}
        if other:
            r.fail("mixed-sport", f"{t['sport']} ticket has {other} legs")
    if BANNED.search(" ".join(str(t.get(f, "")) for f in ("title", "summary", "watch", "checkNote"))):
        r.fail("honesty", "ticket text promises a sure win")

    try:
        d = math.prod(dec(l["odds"]) for l in legs)
    except (KeyError, ValueError, TypeError):
        return r
    if not legs:
        return r
    if t.get("payout10") is not None and abs(t["payout10"] - 10 * d) > TOL_PAY:
        r.fail("payout-math", f"payout10 {t['payout10']} but the prices pay {10 * d:.2f}")
    if t.get("parlayOdds") is not None and len(legs) > 1 and abs(t["parlayOdds"] - american(d)) > 1:
        r.fail("odds-math", f"parlayOdds {t['parlayOdds']:+d} but the prices make {american(d):+d}")
    probs = [l.get("modelProb") for l in legs]
    if all(isinstance(p, (int, float)) for p in probs):
        sg = sum(1 for i in range(len(legs)) for j in range(i + 1, len(legs)) if legs[i].get("game") == legs[j].get("game"))
        win = math.prod(probs) * 0.97 ** sg
        if t.get("winChance") is not None and abs(t["winChance"] - win) > 0.004:
            r.fail("joint-math", f"winChance {t['winChance']} but legs multiply to {win:.3f}")
        e = win * d - 1
        if t.get("ev") is not None and abs(t["ev"] - e) > TOL_EV:
            r.fail("ev-math", f"ev {t['ev']} but winChance x payout - 1 = {e:+.3f}")
        if kind == "big":
            l = legs[0]
            if l["modelProb"] < 0.60 or l["modelProb"] - 1 / d < 0.025:
                r.fail("big-threshold", f"Big Bet needs win >= 60% and edge >= 2.5 pts (has {l['modelProb']:.0%}, {l['modelProb'] - 1 / d:+.3f})")
            if l.get("fairFlag"):
                r.fail("big-thin", "Big Bet can't rest on a thin fair price")
        if kind == "pair" and not (1.9 <= d <= 3.6 and win >= 0.42):
            r.fail("pair-threshold", f"Power Pair needs 1.9x-3.6x payout and 42%+ win (has {d:.2f}x, {win:.0%})")
        if kind == "pair" and e < 0.02:
            r.fail("pair-value", f"Power Pair model value {e:+.1%} is under +2%")
        if kind == "parlay3":
            if 10 * d < 18 - TOL_PAY:
                r.fail("parlay-payout", f"$10 pays {10 * d:.2f}, under the $18 floor")
            if e < 0:
                r.fail("parlay-value", f"negative model value {e:+.1%}")
            neg = sum(1 for l in legs if l["modelProb"] - 1 / dec(l["odds"]) < 0)
            if neg > 1:
                r.fail("filler-legs", f"{neg} legs with negative edge (max 1)")
    return r


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
        return out, obj.get("board") or []
    if isinstance(obj, list):
        return {d.get("_id") or d.get("id") or f"#{i}": d.get("data", d) for i, d in enumerate(obj)}, []
    return {k: v.get("data", v) if isinstance(v, dict) else v for k, v in obj.items()}, []


def audit(obj, never_against, now=None):
    tickets, board = tickets_from(obj)
    reports = [check_ticket(tid, t, never_against, now) for tid, t in tickets.items()]
    br = Report("board")
    for i, l in enumerate(board):
        check_leg(br, l, f"board {i + 1}", never_against, now)
        if l.get("modelProb") is not None and l.get("odds"):
            if l["modelProb"] * dec(l["odds"]) - 1 <= 0:
                br.fail("board-value", f"board {i + 1}: no positive value, shouldn't be listed")
    if board:
        reports.append(br)
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
    now = datetime.fromisoformat(a.now.replace("Z", "+00:00")) if a.now else datetime.now(timezone.utc)
    reports = audit(json.load(open(a.tickets)), settings.get("neverAgainst", []), now)
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
