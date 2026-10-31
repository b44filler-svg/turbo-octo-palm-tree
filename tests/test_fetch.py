"""picks.fetch parsing and matching, on small offline copies of each feed."""
from datetime import datetime, timezone

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


FOOTBALL_LOG = {
    "labels": ["CMP", "ATT", "YDS", "TD", "CAR", "YDS"],
    "names": ["completions", "passingAttempts", "passingYards", "passingTouchdowns",
              "rushingAttempts", "rushingYards"],
    "events": {"1": {"gameDate": "2026-10-12T17:00Z"}, "2": {"gameDate": "2026-10-19T17:00Z"}},
    "seasonTypes": [{"displayName": "2026 Regular Season", "categories": [{"events": [
        {"eventId": "1", "stats": ["20", "31", "1,204", "2", "3", "12"]},
        {"eventId": "2", "stats": ["25", "40", "301", "3", "1", "-2"]}]}]},
                    {"displayName": "2026 Preseason", "categories": [{"events": [
                        {"eventId": "1", "stats": ["1", "1", "5", "0", "0", "0"]}]}]}],
}


def test_football_log_uses_stat_names_and_skips_preseason():
    rows = fetch.parse_gamelog(FOOTBALL_LOG)
    assert len(rows) == 2 and rows[0]["date"].startswith("2026-10-19")
    assert rows[0]["passingYards"] == 301 and rows[1]["passingYards"] == 1204
    assert rows[0]["rushingYards"] == -2       # the second YDS column is rushing, not passing


def test_football_hit_rates_without_minutes():
    rows = fetch.parse_gamelog(FOOTBALL_LOG)
    assert fetch.normal_minutes(rows) == rows
    hr, med, role = fetch.hit_rates(rows, [], "Touchdown Passes", 1.5, "over")
    assert hr == {"season": [2, 2]} and role is None and "pass TDs" in med


def test_nfl_questionable_status_is_blocked():
    assert fetch.injury_block({"status": "Questionable", "note": "Hamstring"}) == "questionable"
    assert fetch.injury_block({"status": "Doubtful", "note": ""}) == "questionable"


def test_star_flags():
    assert fetch.is_star("Passing Yards", 265.5) and fetch.is_star("Receiving Yards", 74.5)
    assert not fetch.is_star("Receptions", 6.5) and not fetch.is_star("Points", 12.5)


# ---------- several Pennsylvania books ----------

def v2_row(typ, side, odds, value=0, team_id=None):
    return {"type": typ, "side": side, "odds": odds, "value": value, "team_id": team_id,
            "is_alt_market": False, "is_live": False}


BOARD_V2 = {"games": [{
    "status": "scheduled", "start_time": "2026-10-28T23:00:00.000Z",
    "away_team_id": 160, "home_team_id": 159, "teams": TEAMS,
    "markets": {
        "255": {"event": {"moneyline": [v2_row("moneyline", "away", 106), v2_row("moneyline", "home", -124)],
                          "total": [v2_row("total", "over", -110, 241.5), v2_row("total", "under", -110, 241.5)]}},
        "3547": {"event": {"moneyline": [v2_row("moneyline", "away", 112), v2_row("moneyline", "home", -130)],
                           "total": [v2_row("total", "over", -105, 240.5), v2_row("total", "under", -115, 240.5)]}},
        "15": {"event": {"moneyline": [v2_row("moneyline", "away", 999)]}}}}]}


def test_lines_merge_prices_from_every_book_with_the_same_number():
    rows = fetch.parse_pinnacle(MATCHUPS, MARKETS, "NBA")
    legs = fetch.match_lines_books(BOARD_V2, rows, "NBA", {"FanDuel": "255", "bet365": "3547"})
    by_pick = {x["pick"]: x for x in legs}
    assert by_pick["Miami Heat ML"]["prices"] == {"FanDuel": 106, "bet365": 112}
    assert by_pick["Miami Heat ML"]["sharp"] == [110, -130]
    # bet365's 240.5 total has no Pinnacle match; FanDuel's 241.5 does
    assert by_pick["Under 241.5"]["prices"] == {"FanDuel": -110} and "Under 240.5" not in by_pick
    cands = fetch.line_candidates(legs)
    heat = next(c for c in cands if c["pick"] == "Miami Heat ML")
    assert heat["why"].endswith("vs bet365 +112.")          # the book that pays most
    best, _, _ = build.prepare(cands, 0.7, ["Philadelphia Eagles"], ["FanDuel", "bet365"])
    assert next(l for l in best if l["pick"] == "Miami Heat ML")["book"] == "bet365"
    fd_only, _, _ = build.prepare(cands, 0.7, ["Philadelphia Eagles"], ["FanDuel"])
    assert next(l for l in fd_only if l["pick"] == "Miami Heat ML")["odds"] == 106


def test_props_take_prices_from_other_books():
    rows = fetch.parse_pinnacle(MATCHUPS, MARKETS, "NBA")
    pin, tips = fetch.pinnacle_props(rows), fetch.tip_times(SCOREBOARD)
    key = ("jalenjohnson", "Rebounds", 9.5)
    other = {key: {"player": "Jalen Johnson", "team": "Atlanta Hawks", "over": -120, "under": 100}}
    cands, _ = fetch.prop_candidates(pin, {}, tips, {}, {}, {}, others={"Fanatics": other})
    assert [c["prices"] for c in cands] == [{"Fanatics": -120}]   # FanDuel doesn't post it
    fd = fetch.fanduel_props(PROP_FEED, "Rebounds")
    cands, _ = fetch.prop_candidates(pin, fd, tips, {}, {}, {}, others={"Fanatics": other})
    assert cands[0]["prices"] == {"FanDuel": -130, "Fanatics": -120}


def test_closing_lines_for_games_about_to_start():
    cands = [{"game": "Miami Heat @ Atlanta Hawks", "pick": "Miami Heat ML",
              "prices": {"FanDuel": 100}, "sharpOdds": [-105, -115]}]
    posted = [{"pick": "Miami Heat ML", "game": "Miami Heat @ Atlanta Hawks", "odds": 110,
               "book": "FanDuel", "startISO": "2026-10-28T23:00:00Z"},
              {"pick": "Gone Over 9.5", "game": "Miami Heat @ Atlanta Hawks", "odds": -110,
               "startISO": "2026-10-28T23:00:00Z"},
              {"pick": "Later ML", "game": "X @ Y", "odds": -110, "startISO": "2026-10-29T02:00:00Z"}]
    now = datetime(2026, 10, 28, 22, 20, tzinfo=timezone.utc)      # 40 minutes before tip
    rows = fetch.closing_lines(posted, cands, now)
    assert [r["pick"] for r in rows] == ["Miami Heat ML", "Gone Over 9.5"]   # the 10 PM game is too far off
    heat = rows[0]
    assert heat["closeOdds"] == 100 and heat["clv"] > 0              # took +110, closed +100
    assert heat["closeSharp"] == [-105, -115] and heat["clvSharp"] > 0 and heat["minutesBefore"] == 40
    assert "closeOdds" not in rows[1] and "moved" in rows[1]["closeNote"]
