"""picks.fetch parsing and matching, on small offline copies of each feed."""
from picks import build, fetch

MATCHUPS = [
    {"id": 1, "participants": [{"alignment": "home", "name": "Atlanta Hawks"},
                               {"alignment": "away", "name": "Miami Heat"}],
     "startTime": "2026-10-28T23:10:00Z"},
    {"id": 2, "parent": {"participants": [{"alignment": "home", "name": "Atlanta Hawks"},
                                          {"alignment": "away", "name": "Miami Heat"}],
                         "startTime": "2026-10-28T23:10:00Z"},
     "participants": [{"id": 21, "name": "Over"}, {"id": 22, "name": "Under"}],
     "special": {"description": "Jalen Johnson Total Rebounds"}},
]
MARKETS = [
    {"matchupId": 1, "period": 0, "type": "moneyline", "key": "s;0;m",
     "prices": [{"designation": "home", "price": -130}, {"designation": "away", "price": 110}]},
    {"matchupId": 1, "period": 0, "type": "total", "key": "s;0;ou;241.5",
     "prices": [{"designation": "over", "points": 241.5, "price": -105},
                {"designation": "under", "points": 241.5, "price": -115}]},
    {"matchupId": 1, "period": 1, "type": "moneyline", "key": "s;1;m",
     "prices": [{"designation": "home", "price": -120}, {"designation": "away", "price": 100}]},
    {"matchupId": 2, "period": 0, "type": "total", "key": "s;0;ou",
     "prices": [{"participantId": 21, "points": 9.5, "price": -143},
                {"participantId": 22, "points": 9.5, "price": 108}]},
]
TEAMS = [{"id": 159, "full_name": "Atlanta Hawks"}, {"id": 160, "full_name": "Miami Heat"}]
SCOREBOARD = {"games": [{
    "status": "scheduled", "start_time": "2026-10-28T23:00:00.000Z",
    "away_team_id": 160, "home_team_id": 159, "teams": TEAMS,
    "odds": [{"book_id": 15, "ml_away": 999, "ml_home": -999},
             {"book_id": 69, "ml_away": 106, "ml_home": -124, "spread_away": 2.5,
              "spread_home": -2.5, "spread_away_line": -120, "spread_home_line": -102,
              "total": 241.5, "over": -110, "under": -110}]}]}
PROP_FEED = {
    "games": [{"home_team": {"id": 159, "full_name": "Atlanta Hawks"},
               "away_team": {"id": 160, "full_name": "Miami Heat"}}],
    "players": [{"id": 7, "full_name": "Jalen Johnson", "team_id": 159}],
    "markets": {"69": {"event": {"core_bet_type_23_rebounds": [
        {"player_id": 7, "side": "over", "value": 9.5, "odds": -130},
        {"player_id": 7, "side": "under", "value": 9.5, "odds": 104},
        {"player_id": 7, "side": "over", "value": 12.5, "odds": 300, "is_alt_market": True}]}},
        "15": {"event": {"core_bet_type_23_rebounds": [
            {"player_id": 7, "side": "over", "value": 9.5, "odds": -500}]}}},
}


def logs(mins, rebs):
    return [{"date": f"2026-10-{20 + i:02d}", "MIN": m, "PTS": 10.0, "REB": r, "AST": 3.0, "3PM": 1.0}
            for i, (m, r) in enumerate(zip(mins, rebs))][::-1]


def test_pinnacle_keeps_full_game_and_names_sides():
    rows = fetch.parse_pinnacle(MATCHUPS, MARKETS, "NBA")
    assert len(rows) == 3                     # the first-half moneyline is skipped
    ml = next(r for r in rows if r["type"] == "moneyline")
    assert ml["game"] == "Miami Heat @ Atlanta Hawks"
    assert {p["name"]: p["price"] for p in ml["prices"]} == {"Atlanta Hawks": -130, "Miami Heat": 110}
    props = fetch.pinnacle_props(rows)
    assert props[(fetch.norm("Jalen Johnson"), "Rebounds", 9.5)] == (-143, 108, "Miami Heat @ Atlanta Hawks")


def test_fanduel_props_reads_only_book_69_main_lines():
    fd = fetch.fanduel_props(PROP_FEED, "Rebounds")
    assert fd == {("jalenjohnson", "Rebounds", 9.5):
                  {"player": "Jalen Johnson", "team": "Atlanta Hawks", "over": -130, "under": 104}}


def test_lines_pair_fanduel_with_matching_sharp_number():
    rows = fetch.parse_pinnacle(MATCHUPS, MARKETS, "NBA")
    legs = fetch.match_lines(SCOREBOARD, rows, "NBA")
    by_pick = {x["pick"]: x for x in legs}
    assert by_pick["Miami Heat ML"]["fd"] == 106 and by_pick["Miami Heat ML"]["sharp"] == [110, -130]
    assert by_pick["Under 241.5"]["sharp"] == [-115, -105]
    assert not any(x["type"] == "spread" for x in legs)   # Pinnacle has no 2.5 spread here


def test_line_candidates_build_and_use_true_tip_time():
    rows = fetch.parse_pinnacle(MATCHUPS, MARKETS, "NBA")
    cands = fetch.line_candidates(fetch.match_lines(SCOREBOARD, rows, "NBA"))
    assert all(c["start"] == "7:00 PM ET" and c["startISO"] == "2026-10-28T23:00:00Z" for c in cands)
    best, _, dropped = build.prepare(cands, 0.5, ["Philadelphia Eagles"], ["FanDuel"])
    assert best and not dropped


def test_hit_rates_and_median():
    cur = logs([30, 31, 29], [10, 11, 8])
    last = logs([30] * 6, [12, 9, 10, 8, 11, 10])
    hr, med, role = fetch.hit_rates(cur, last, "Rebounds", 9.5, "over")
    assert role is None
    assert hr["season"] == [2, 3] and hr["lastSeason"] == [4, 6] and hr["last5"][1] == 5
    assert med.startswith("median 10 reb (last 9)")


def test_role_change_drops_last_season():
    cur = logs([14, 15, 13], [4, 5, 3])
    last = logs([28] * 10, [9] * 10)
    hr, _, role = fetch.hit_rates(cur, last, "Rebounds", 4.5, "over")
    assert "28 -> 14" in role and "lastSeason" not in hr and hr["season"] == [1, 3]


def test_cameo_games_are_left_out():
    rows = logs([30, 30, 4, 31], [10, 10, 0, 10])
    assert len(fetch.normal_minutes(rows)) == 3


def test_injury_block():
    assert fetch.injury_block({"status": "Out", "note": ""}) == "out"
    assert fetch.injury_block({"status": "Day-To-Day", "note": "X is questionable for Wednesday."}) == "questionable"
    assert fetch.injury_block({"status": "Day-To-Day", "note": "X (knee) is probable for Wednesday."}) is None
    assert fetch.injury_block(None) is None


def test_prop_candidates_skip_injured_and_carry_context():
    rows = fetch.parse_pinnacle(MATCHUPS, MARKETS, "NBA")
    pin, fd = fetch.pinnacle_props(rows), fetch.fanduel_props(PROP_FEED, "Rebounds")
    tips = fetch.tip_times(SCOREBOARD)
    lg = {"jalenjohnson": {"cur": logs([30, 31], [10, 11]), "last": logs([30] * 8, [10] * 8)}}
    cands, dropped = fetch.prop_candidates(pin, fd, tips, lg, {}, {"Atlanta Hawks": ["Trae Young"]})
    # the Under at +104 is far worse than Pinnacle's +108 side, so only the Over is listed
    assert [c["pick"] for c in cands] == ["Jalen Johnson Over 9.5 Rebounds"]
    over = cands[0]
    assert over["sharpOdds"] == [-143, 108] and over["prices"] == {"FanDuel": -130}
    assert over["teammatesOut"] == ["Trae Young"] and over["hitRates"]["season"] == [2, 2]
    hurt = {"jalenjohnson": {"team": "Atlanta Hawks", "status": "Day-To-Day", "note": "Johnson is questionable."}}
    cands, dropped = fetch.prop_candidates(pin, fd, tips, lg, hurt, {})
    assert not cands and dropped == ["Jalen Johnson (questionable: Johnson is questionable.)"]


def test_parse_injuries():
    feed = {"injuries": [{"displayName": "Atlanta Hawks", "injuries": [
        {"status": "Out", "shortComment": "Young is out.", "athlete": {"displayName": "Trae Young"}},
        {"status": "Day-To-Day", "shortComment": "probable", "athlete": {"displayName": "Aaron Wiggins"}}]}]}
    players, out = fetch.parse_injuries(feed)
    assert out == {"Atlanta Hawks": ["Trae Young"]} and players["aaronwiggins"]["status"] == "Day-To-Day"
