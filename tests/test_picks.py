import copy
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import pytest

from picks import audit, build, model, oddsmath as om, rules

FIX = Path(__file__).parent / "fixtures"
EAGLES = ["Philadelphia Eagles"]


def load(name):
    return json.loads((FIX / name).read_text())


# ---------- odds math ----------

@pytest.mark.parametrize("am,dec", [(-110, 1.909091), (100, 2.0), (150, 2.5), (-300, 1.333333), (-500, 1.2)])
def test_conversions_round_trip(am, dec):
    assert om.am_to_dec(am) == pytest.approx(dec, abs=1e-6)
    assert om.dec_to_am(om.am_to_dec(am)) == am


@pytest.mark.parametrize("bad", [0, 50, -99])
def test_rejects_impossible_prices(bad):
    with pytest.raises(ValueError):
        om.am_to_dec(bad)


def test_devig_sums_to_one_and_power_shades_the_longshot():
    prices = [-300, 240]
    mult, power = om.devig_multiplicative(prices), om.devig_power(prices)
    assert sum(mult) == pytest.approx(1) and sum(power) == pytest.approx(1)
    # Power method takes more of the vig out of the underdog.
    assert power[1] < mult[1] and power[0] > mult[0]
    assert om.fair_prob([-110, -110]) == pytest.approx(0.5)


def test_fair_prob_needs_both_sides():
    with pytest.raises(ValueError):
        om.fair_prob([-150])


def test_ev_kelly_growth():
    assert om.ev(0.5, 100) == pytest.approx(0)
    assert om.ev(0.6, 100) == pytest.approx(0.2)
    assert om.kelly(0.6, 2.0) == pytest.approx(0.2)
    assert om.kelly(0.4, 2.0) == 0
    assert om.growth(0.4, 2.0) == 0
    # Growth balances win chance and payout: both of these beat a no-edge bet.
    assert om.growth(0.80, 1.4) > 0 and om.growth(0.45, 2.6) > 0


# ---------- model ----------

def test_stat_prob_weights_and_sample():
    p, n = model.stat_prob({"season": [8, 10], "last10": [8, 10], "last5": [4, 5], "lastSeason": [16, 20]})
    assert p == pytest.approx(0.8) and n == 30


def test_tiny_season_window_counts_less():
    # 3/3 this season vs 20/70 last season: the 70 games should dominate.
    p, _ = model.stat_prob({"season": [3, 3], "lastSeason": [20, 70]})
    assert p < 0.5


def test_thin_sample_leans_on_market():
    base = {"odds": -150, "sharpOdds": [-150, 130], "type": "prop over", "team": "X"}
    thin = model.score({**base, "hitRates": {"season": [3, 3]}})
    deep = model.score({**base, "hitRates": {"season": [30, 30]}})
    assert thin["modelProb"] < deep["modelProb"]


def test_model_stays_near_market_and_capped():
    c = {"odds": -400, "sharpOdds": [-420, 330], "type": "prop over", "team": "X",
         "hitRates": {"season": [40, 40], "lastSeason": [40, 40]}, "adjust": 0.5}
    s = model.score(c)
    assert s["modelProb"] <= s["fairProb"] + model.MAX_DEVIATION + 1e-9
    assert s["modelProb"] <= model.CAP
    assert s["edge"] == pytest.approx(s["modelProb"] - om.implied(-400), abs=0.001)


def test_under_bonus_and_star_over_penalty():
    base = {"odds": -110, "sharpOdds": [-110, -110], "team": "X"}
    under = model.score({**base, "type": "prop under"})["modelProb"]
    over = model.score({**base, "type": "prop over", "star": True})["modelProb"]
    assert under == pytest.approx(0.515) and over == pytest.approx(0.49)


# ---------- rules ----------

@pytest.mark.parametrize("leg,bad", [
    ({"type": "moneyline", "team": "Dallas Cowboys"}, True),          # opponent ML
    ({"type": "spread", "team": "Cowboys"}, True),                    # opponent spread
    ({"type": "prop over", "team": "Dallas Cowboys"}, True),          # opponent player over
    ({"type": "team total over", "team": "Dallas Cowboys"}, True),    # opponent team total over
    ({"type": "prop under", "team": "Philadelphia Eagles"}, True),    # Eagles player under
    ({"type": "team total under", "team": "Eagles"}, True),           # Eagles team total under
    ({"type": "moneyline"}, True),                                    # no team -> fail closed
    ({"type": "moneyline", "team": "Philadelphia Eagles"}, False),    # backing the Eagles
    ({"type": "prop over", "team": "Philadelphia Eagles"}, False),
    ({"type": "prop under", "team": "Dallas Cowboys"}, False),        # opponent under helps Philly
    ({"type": "total under"}, False),                                 # neutral game total
])
def test_loyalty_rule(leg, bad):
    leg = {"game": "Cowboys @ Eagles", **leg}
    assert bool(rules.loyalty_violation(leg, EAGLES)) is bad


def test_loyalty_ignores_other_games():
    assert rules.loyalty_violation({"game": "Bulls @ Heat", "type": "moneyline", "team": "Heat"}, EAGLES) is None


def test_two_word_nicknames():
    assert not rules.same_team("Boston Red Sox", "Chicago White Sox")
    assert rules.same_team("Boston Red Sox", "Red Sox")


@pytest.mark.parametrize("a,b,why", [
    ({"type": "moneyline", "team": "Heat", "pick": "Heat ML"}, {"type": "spread", "team": "Heat", "pick": "Heat -5"}, "own spread"),
    ({"type": "moneyline", "team": "Heat", "pick": "Heat ML"}, {"type": "moneyline", "team": "Bulls", "pick": "Bulls ML"}, "both sides"),
    ({"type": "total over", "pick": "O 220"}, {"type": "total under", "pick": "U 230"}, "game-total"),
    ({"type": "prop over", "player": "P", "market": "Rushing yards", "pick": "a"},
     {"type": "prop over", "player": "P", "market": "Anytime TD", "pick": "b"}, "anytime TD"),
])
def test_conflicts(a, b, why):
    g = {"game": "Bulls @ Heat"}
    assert why in rules.conflict({**g, **a}, {**g, **b})


def test_different_games_never_conflict():
    assert rules.conflict({"game": "A @ B", "type": "moneyline", "team": "A", "pick": "x"},
                          {"game": "C @ D", "type": "moneyline", "team": "C", "pick": "y"}) is None


# ---------- build ----------

@pytest.fixture
def built():
    return build.build_all(load("candidates.json"), "2026-10-27", {"neverAgainst": EAGLES})


def test_build_makes_every_bet_type(built):
    assert built["big"]["kind"] == "big" and len(built["big"]["legs"]) == 1
    assert 1 <= len(built["pairs"]) <= 2 and all(len(p["legs"]) == 2 for p in built["pairs"])
    assert set(built["parlays"]) <= {"NFL", "NBA", "MLB"} and built["parlays"]
    assert built["board"] and all(l["ev"] > 0 for l in built["board"])


def test_build_respects_loyalty_rule(built):
    assert any("Dallas" in d["pick"] for d in built["dropped"])
    every = [l for t in [built["big"], *built["pairs"], *built["parlays"].values()] for l in t["legs"]]
    every += built["board"]
    assert not [l for l in every if rules.loyalty_violation(l, EAGLES)]


def test_build_thresholds(built):
    big = built["big"]["legs"][0]
    assert big["modelProb"] >= build.BIG["min_prob"] and big["edge"] >= build.BIG["min_edge"]
    for p in built["pairs"]:
        assert build.PAIR["min_dec"] <= p["payout10"] / 10 <= build.PAIR["max_dec"]
        assert p["winChance"] >= build.PAIR["min_joint"] and p["ev"] >= build.PAIR["min_ev"]
    picks = [l["pick"] for p in built["pairs"] for l in p["legs"]] + [big["pick"]]
    assert len(picks) == len(set(picks)), "pairs and big bet should not reuse legs"
    for t in built["parlays"].values():
        assert t["payout10"] >= 18 and t["ev"] >= 0
        assert len({l["sport"] for l in t["legs"]}) == 1


def test_board_is_ranked_by_growth(built):
    g = [l["growth"] for l in built["board"]]
    assert g == sorted(g, reverse=True)


def test_parlay_payout_math(built):
    for t in built["parlays"].values():
        d = math.prod(om.am_to_dec(l["odds"]) for l in t["legs"])
        assert t["payout10"] == pytest.approx(10 * d, abs=0.006)
        assert t["parlayOdds"] == om.dec_to_am(d)


# ---------- audit ----------

NOW = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)


def test_build_output_passes_audit(built):
    reports = audit.audit(built, EAGLES, NOW)
    assert [r.tid for r in reports if r.status == "FAIL"] == []


def codes(reports):
    return {i["code"] for r in reports for i in r.items if i["level"] == "FAIL"}


def test_audit_catches_tampered_math(built):
    bad = copy.deepcopy(built)
    t = next(iter(bad["parlays"].values()))
    t["payout10"] += 3
    t["winChance"] += 0.1
    t["legs"][0]["edge"] += 0.05
    assert {"payout-math", "joint-math", "edge-math"} <= codes(audit.audit(bad, EAGLES, NOW))


def test_audit_catches_started_games_and_honesty(built):
    bad = copy.deepcopy(built)
    bad["big"]["legs"][0]["startISO"] = "2026-10-27T11:00:00Z"
    bad["big"]["summary"] = "This is a guaranteed winner"
    assert {"started", "honesty"} <= codes(audit.audit(bad, EAGLES, NOW))


def test_audit_catches_loyalty_break_in_real_ticket():
    # The 2026-10-26 NFL ticket had three legs backing Dallas against the Eagles.
    t = load("ticket_2026-10-26-nfl.json")
    reports = audit.audit({"2026-10-26-nfl": t}, EAGLES, NOW)
    loyal = [i for r in reports for i in r.items if i["code"] == "loyalty"]
    assert len(loyal) >= 3


def test_audit_catches_conflicts_and_wrong_leg_count(built):
    bad = copy.deepcopy(built)
    p = bad["pairs"][0]
    p["legs"].append(dict(p["legs"][0]))
    assert {"leg-count", "conflict"} <= codes(audit.audit(bad, EAGLES, NOW))


def test_audit_math_is_independent_of_oddsmath(monkeypatch, built):
    # If the builder's math were broken, the audit must still use its own.
    monkeypatch.setattr(om, "am_to_dec", lambda am: 99.0)
    assert audit.dec(-110) == pytest.approx(1.909091, abs=1e-6)


# ---------- regressions from the independent review ----------

@pytest.mark.parametrize("game", ["DAL @ PHI", "Dallas @ Philadelphia", "Cowboys - Eagles",
                                  "Cowboys@Eagles", "Dallas Cowboys at Philadelphia Eagles", "Cowboys vs Eagles"])
def test_loyalty_rule_any_game_spelling(game):
    leg = {"sport": "NFL", "game": game, "type": "moneyline", "team": "Dallas Cowboys", "pick": "Dallas ML"}
    assert rules.loyalty_violation(leg, EAGLES)
    ok = {**leg, "team": "Philadelphia Eagles", "pick": "Eagles ML"}
    assert not rules.loyalty_violation(ok, EAGLES)


def test_loyalty_fails_closed_on_unreadable_game():
    leg = {"sport": "NFL", "game": "Eagles game", "type": "total under", "pick": "Under 44.5"}
    assert rules.loyalty_violation(leg, EAGLES)


def test_philly_city_does_not_trip_other_sports():
    leg = {"sport": "MLB", "game": "New York Mets @ Philadelphia Phillies", "type": "moneyline", "team": "New York Mets", "pick": "Mets ML"}
    assert not rules.loyalty_violation(leg, EAGLES)


def test_build_refuses_missing_loyalty_settings():
    with pytest.raises(ValueError):
        build.build_all(load("candidates.json"), "2026-10-27", {})
    with pytest.raises(ValueError):
        build.build_all(load("candidates.json"), "2026-10-27", {"monthlyBudget": 100})


def test_audit_cli_fails_without_settings(tmp_path, built):
    f = tmp_path / "d.json"
    f.write_text(json.dumps(built))
    assert audit.main([str(f)]) == 1


def test_bad_candidates_are_dropped_not_fatal():
    good = load("candidates.json")
    bad = [{**good[3], "pick": "one-sided", "sharpOdds": [-150]},
           {**good[3], "pick": "no sport", "sport": None},
           {**good[3], "pick": "bad odds", "odds": 50},
           {**good[5], "pick": "stats only", "sharpOdds": None}]
    res = build.build_all(good + bad, "2026-10-27", {"neverAgainst": EAGLES})
    why = {d["pick"]: d["why"] for d in res["dropped"]}
    assert {"no sport", "bad odds", "stats only"} <= set(why)
    assert res["big"] and res["parlays"]


def test_build_output_never_fails_audit_at_rounding_edges():
    # A leg whose edge sits right at the Big Bet 2.5-point line after rounding.
    base = {"sport": "NBA", "game": "Bulls @ Heat", "market": "Points", "type": "prop over", "team": "Heat",
            "source": "FanDuel Sportsbook", "sharpSource": "Pinnacle"}
    cands = []
    for i, fair in enumerate([0.6502, 0.6505, 0.651, 0.6498]):
        cands.append({**base, "player": f"P{i}", "pick": f"P{i} under", "odds": -167, "fairProb": fair, "fairFlag": False})
    res = build.build_all(cands, "2026-10-27", {"neverAgainst": []})
    assert [r.tid for r in audit.audit(res, [], NOW) if r.status == "FAIL"] == []


def test_audit_catches_threshold_tampering(built):
    bad = copy.deepcopy(built)
    big = bad["big"]["legs"][0]
    big["odds"] = -450
    big["impliedProb"] = round(1 / audit.dec(-450), 3)
    big["edge"] = round(big["modelProb"] - big["impliedProb"], 3)
    bad["big"]["payout10"] = round(10 * audit.dec(-450), 2)
    bad["big"]["parlayOdds"] = -450
    bad["big"]["ev"] = round(big["modelProb"] * audit.dec(-450) - 1, 3)
    bad["pairs"][0]["legs"][0]["modelProb"] = 0.55
    assert {"big-odds", "big-threshold", "pair-leg"} <= codes(audit.audit(bad, EAGLES, NOW))


def test_audit_thresholds_match_builder():
    assert audit.BIG == build.BIG
    assert {k: build.PAIR[k] for k in audit.PAIR} == audit.PAIR
    assert {k: build.PARLAY[k] for k in audit.PARLAY} == audit.PARLAY
    assert audit.BOARD["min_prob"] == build.BOARD["min_prob"]
    assert audit.BOOST_MIN_JOINT == build.BOOST["min_joint"]


def test_audit_survives_garbage(built):
    bad = copy.deepcopy(built)
    bad["board"][0]["odds"] = 50
    bad["big"]["parlayOdds"] = "-125"
    reps = audit.audit(bad, EAGLES, NOW)
    assert "bad-odds" in codes(reps)


def test_backup_fits_in_place_of_any_leg(built):
    for t in built["parlays"].values():
        if t.get("backup"):
            for i in range(3):
                legs = t["legs"][:i] + t["legs"][i + 1:] + [t["backup"]]
                assert not rules.ticket_conflicts(legs)
                d = math.prod(om.am_to_dec(l["odds"]) for l in legs)
                assert 10 * d >= 18


def test_same_game_matches_across_spellings():
    a = {"game": "Cowboys @ Eagles", "type": "total over", "pick": "O 44"}
    b = {"game": "Dallas Cowboys at Philadelphia Eagles", "type": "total under", "pick": "U 47"}
    assert rules.conflict(a, b)
    assert rules.same_game_pairs([a, b]) == 1


def test_multi_leg_tickets_never_share_a_game(built):
    tickets = built["pairs"] + list(built["parlays"].values()) + built["boosts"]
    assert tickets
    for t in tickets:
        assert rules.same_game_pairs(t["legs"]) == 0, t["legs"]


def test_audit_catches_same_game_legs(built):
    bad = copy.deepcopy(built)
    p = bad["pairs"][0]
    a = p["legs"][0]
    p["legs"][1] = {**a, "pick": "Someone Else Over 1.5 Assists", "player": "Someone Else",
                    "market": "Assists", "type": "prop over"}
    assert "same-game" in codes(audit.audit(bad, EAGLES, NOW))


def test_conflict_details():
    g = {"game": "Bulls @ Heat", "sport": "NBA"}
    assert rules.conflict({**g, "type": "team total over", "team": "Heat", "pick": "a"},
                          {**g, "type": "team total under", "team": "Heat", "pick": "b"})
    # Passing TDs is not an anytime-TD bet
    assert rules.conflict({**g, "type": "prop over", "player": "Q", "market": "Passing TDs", "pick": "a"},
                          {**g, "type": "prop over", "player": "Q", "market": "Passing Yards", "pick": "b"}) is None


def test_hit_rate_text_for_the_app(built):
    assert "last 10" in built["big"]["legs"][0]["hitRate"]


def test_legacy_ticket_without_kind():
    t = load("ticket_2026-10-26-nfl.json")
    rep = audit.check_ticket("old", t, [], NOW)
    assert "no-fair" not in {i["code"] for i in rep.items}


def test_selftest_passes():
    from picks import selftest
    assert selftest.main() == 0


# ---------- multiple sportsbooks and promos ----------

BOOKS = {"neverAgainst": EAGLES, "books": ["FanDuel", "Courtside", "DraftKings"]}


def multibook():
    """The fixture candidates, each priced at three books (FanDuel's price kept as given)."""
    out = []
    for i, c in enumerate(load("candidates.json")):
        c = dict(c)
        fd = c.pop("odds")
        c.pop("source")
        bump = 10 if i % 3 == 0 else -5   # some legs better elsewhere, some worse
        alt = fd + bump if not (-100 < fd + bump < 100) else fd
        c["prices"] = {"FanDuel": fd, "Courtside": alt, "BetMGM": fd + 20 if fd + 20 >= 100 or fd + 20 <= -100 else fd}
        out.append(c)
    return out


def test_singles_take_the_best_price_at_bobs_books():
    res = build.build_all(multibook(), "2026-10-27", BOOKS)
    for l in res["board"]:
        assert l["book"] in ("FanDuel", "Courtside")       # BetMGM isn't one of Bob's books
        others = l.get("otherPrices", {})
        assert all(om.am_to_dec(l["odds"]) >= om.am_to_dec(o) for o in others.values())


def test_parlays_stay_at_one_book():
    res = build.build_all(multibook(), "2026-10-27", BOOKS)
    for t in res["pairs"] + list(res["parlays"].values()):
        assert len({l["book"] for l in t["legs"]}) == 1 and t["book"] == t["legs"][0]["book"]
    assert [r.tid for r in audit.audit(res, EAGLES, NOW, BOOKS["books"]) if r.status == "FAIL"] == []


def test_legs_not_at_bobs_books_are_dropped():
    c = dict(load("candidates.json")[3]); c.pop("odds"); c.pop("source")
    c["prices"] = {"BetMGM": -300}
    res = build.build_all([c], "2026-10-27", {"neverAgainst": EAGLES, "books": ["FanDuel"]})
    assert "not offered at your books" in res["dropped"][0]["why"]


def test_audit_catches_mixed_books_and_wrong_book():
    res = build.build_all(multibook(), "2026-10-27", BOOKS)
    bad = copy.deepcopy(res)
    p = bad["pairs"][0]
    p["legs"][1]["book"] = "Courtside" if p["legs"][0]["book"] == "FanDuel" else "FanDuel"
    bad["board"][0]["book"] = "BetMGM"
    assert {"mixed-books", "wrong-book"} <= codes(audit.audit(bad, EAGLES, NOW, BOOKS["books"]))


BOOST = {"id": "fd-nba-30", "book": "FanDuel", "title": "30% profit boost, NBA parlay",
         "pct": 0.30, "minLegs": 2, "maxLegs": 3, "sport": "NBA", "maxStake": 25}


def test_boost_math_and_rules():
    res = build.build_all(multibook(), "2026-10-27", BOOKS, None, [BOOST])
    assert len(res["boosts"]) == 1
    t = res["boosts"][0]
    assert t["kind"] == "boost" and t["book"] == "FanDuel" and 2 <= len(t["legs"]) <= 3
    assert all(l["book"] == "FanDuel" and l["sport"] == "NBA" for l in t["legs"])
    base = math.prod(om.am_to_dec(l["odds"]) for l in t["legs"])
    assert t["payout10"] == pytest.approx(10 * (1 + (base - 1) * 1.3), abs=0.006)
    assert t["baseOdds"] == om.dec_to_am(base) and t["ev"] > 0
    assert [r.tid for r in audit.audit(res, EAGLES, NOW, BOOKS["books"]) if r.status == "FAIL"] == []


def test_audit_catches_unboosted_payout_on_boost():
    res = build.build_all(multibook(), "2026-10-27", BOOKS, None, [BOOST])
    bad = copy.deepcopy(res)
    t = bad["boosts"][0]
    t["boost"]["pct"] = 0.5          # claims a bigger boost than the payout reflects
    assert "payout-math" in codes(audit.audit(bad, EAGLES, NOW, BOOKS["books"]))


def test_boost_at_a_book_with_no_legs_gives_nothing():
    res = build.build_all(multibook(), "2026-10-27", BOOKS, None, [{**BOOST, "book": "Caesars"}])
    assert res["boosts"] == []


# ---------- leans and payout plays (the app is never empty) ----------

def thin_day():
    """Small edges only: nothing clears the full bar, but some bets have positive value."""
    base = {"sport": "NBA", "market": "Points", "type": "prop under", "source": "FanDuel Sportsbook",
            "sharpSource": "consensus"}
    games = ["Bulls @ Heat", "Jazz @ Nuggets", "Celtics @ Knicks", "Lakers @ Suns"]
    out = []
    for i, g in enumerate(games):
        home = g.split(" @ ")[1]
        out.append({**base, "game": g, "team": home, "player": f"P{i}", "pick": f"P{i} under",
                    "odds": -150, "sharpOdds": [-165, 140]})
        out.append({**base, "game": g, "type": "moneyline", "market": "Moneyline", "team": home,
                    "pick": f"{home} ML", "odds": -260, "sharpOdds": [-280, 230]})
    return out


def test_thin_day_still_posts_leans():
    res = build.build_all(thin_day(), "2026-10-27", {"neverAgainst": EAGLES})
    strict = build.build_big(build.prepare(thin_day(), 0.5, EAGLES)[0], "d")
    assert strict is None                                  # nothing clears the full Big Bet bar
    assert res["big"] and res["big"]["tier"] == "lean" and res["big"]["ev"] > 0
    tickets = [res["big"], *res["pairs"], *res["parlays"].values()]
    assert res["pairs"] and res["pairs"][0]["tier"] == "lean"
    assert all(t["ev"] >= 0 for t in tickets)              # leans are still positive value
    assert [r.tid for r in audit.audit(res, EAGLES, NOW) if r.status == "FAIL"] == []


def test_leans_never_replace_full_picks(built):
    assert "tier" not in built["big"]
    assert all("tier" not in t for t in built["pairs"] + list(built["parlays"].values()))


def test_lean_is_still_checked():
    res = build.build_all(thin_day(), "2026-10-27", {"neverAgainst": EAGLES})
    bad = copy.deepcopy(res)
    l = bad["big"]["legs"][0]
    l["modelProb"] = 0.50                                  # below even the lean bar
    l["edge"] = round(0.50 - l["impliedProb"], 3)
    bad["big"]["winChance"] = 0.5
    bad["big"]["ev"] = round(0.5 * audit.dec(l["odds"]) - 1, 3)
    assert "big-threshold" in codes(audit.audit(bad, EAGLES, NOW))


def test_payout_plays():
    c = load("candidates.json") + [
        {"sport": "NBA", "game": "Warriors @ Kings", "pick": "Kings ML", "market": "Moneyline",
         "type": "moneyline", "team": "Sacramento Kings", "odds": 150, "sharpOdds": [125, -145],
         "source": "FanDuel Sportsbook", "sharpSource": "Pinnacle", "adjust": 0.03}]
    res = build.build_all(c, "2026-10-27", {"neverAgainst": EAGLES})
    assert res["longshots"] and all(l["odds"] >= 120 and l["ev"] >= 0.03 for l in res["longshots"])
    assert [r.tid for r in audit.audit(res, EAGLES, NOW) if r.status == "FAIL"] == []


def test_lean_bars_match_builder():
    for name in ("min_prob", "min_edge", "min_ev", "min_odds", "max_odds"):
        assert audit.LEAN_BIG[name] == build.LEAN_BIG[name]
    for name in audit.LEAN_PAIR:
        assert audit.LEAN_PAIR[name] == build.LEAN_PAIR[name]
    for name in audit.LEAN_PARLAY:
        assert audit.LEAN_PARLAY[name] == build.LEAN_PARLAY[name]
    for name in audit.LONGSHOT:
        assert audit.LONGSHOT[name] == build.LONGSHOT[name]
