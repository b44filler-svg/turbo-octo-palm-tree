"""Quick self-test that needs nothing but Python 3 (no pip installs).

    python3 -m picks.selftest

The scheduled picks task runs this before building. Exit code 1 on any failure.
The full suite lives in tests/ and needs pytest.
"""
import json
import sys
from pathlib import Path

from . import audit, build, oddsmath as om, rules

ROOT = Path(__file__).resolve().parent.parent
EAGLES = ["Philadelphia Eagles"]


def checks():
    yield "odds conversions", om.am_to_dec(-110) == 1 + 100 / 110 and om.dec_to_am(2.5) == 150
    yield "no-vig sums to 1", abs(sum(om.devig_power([-300, 240])) - 1) < 1e-9
    yield "Kelly", abs(om.kelly(0.6, 2.0) - 0.2) < 1e-9
    against = {"sport": "NFL", "game": "DAL @ PHI", "type": "moneyline", "team": "Dallas Cowboys", "pick": "Dallas ML"}
    backing = {**against, "team": "Philadelphia Eagles", "pick": "Eagles ML"}
    yield "Eagles rule blocks bets against", bool(rules.loyalty_violation(against, EAGLES))
    yield "Eagles rule allows bets for", not rules.loyalty_violation(backing, EAGLES)
    try:
        build.build_all([], "2026-01-01", {})
        yield "refuses to build without neverAgainst", False
    except ValueError:
        yield "refuses to build without neverAgainst", True
    cands = json.loads((ROOT / "tests" / "fixtures" / "candidates.json").read_text())
    res = build.build_all(cands, "2026-10-27", {"neverAgainst": EAGLES})
    yield "builds every bet type", bool(res["big"] and res["pairs"] and res["parlays"] and res["board"])
    fails = [r.tid for r in audit.audit(res, EAGLES, None) if r.status == "FAIL"]
    yield "build passes the independent audit", not fails


def main():
    bad = 0
    for name, ok in checks():
        print(("ok   " if ok else "FAIL ") + name)
        bad += not ok
    print(f"{'all passed' if not bad else str(bad) + ' failed'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
