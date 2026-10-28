"""Pulls today's prices and stats and writes candidates.json for picks.build.

Sources (all public JSON, stdlib only):
  Pinnacle guest API   sharp two-sided prices: game lines and NBA/MLB props
  Action Network feed  FanDuel prices (book 69) for game lines and NBA props,
                       plus true tip times
  ESPN                 NBA game logs (this season and last) and injury reports

    python3 -m picks.fetch --date 2026-10-28 --out /tmp/picks/2026-10-28

Writes <out>/candidates.json and <out>/fetch_report.json (counts, failures,
injuries). Research still adds news, expert views and role adjustments.
"""
import argparse
import json
import re
import statistics
import sys
import time
import unicodedata
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import oddsmath as om

ET = ZoneInfo("America/New_York")
PINNACLE = "https://guest.api.arcadia.pinnacle.com/0.1"
ACTION = "https://api.actionnetwork.com/web"
ESPN = "https://site.api.espn.com/apis/site/v2/sports"
ESPN_LOGS = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes"
FANDUEL_BOOK = "69"
LEAGUES = {"NBA": (487, "nba", "basketball/nba"),
           "MLB": (246, "mlb", "baseball/mlb"),
           "NFL": (889, "nfl", "football/nfl")}
# Action Network prop markets -> the stat name Pinnacle uses.
PROP_TYPES = {"NBA": {"core_bet_type_27_points": "Points",
                      "core_bet_type_23_rebounds": "Rebounds",
                      "core_bet_type_26_assists": "Assists",
                      "core_bet_type_21_3fgm": "Threes Made",
                      "core_bet_type_85_points_rebounds_assists": "Pts & Rebs & Asts"},
              "MLB": {"core_bet_type_37_strikeouts": "Strikeouts"}}
STAT_KEYS = {"Points": ("PTS",), "Rebounds": ("REB",), "Assists": ("AST",),
             "Threes Made": ("3PM",), "Pts & Rebs & Asts": ("PTS", "REB", "AST")}
STAT_ABBR = {"Points": "pts", "Rebounds": "reb", "Assists": "ast",
             "Threes Made": "3s", "Pts & Rebs & Asts": "PRA"}
ROLE_CHANGE = 0.25       # minutes moved this much: last season's games don't compare
MIN_PROP_EV = -0.05      # props this far under the sharp price aren't worth listing
SOURCE = "Action Network odds feed (FanDuel); sharp: Pinnacle"


def get(url, failures=None, tries=3):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                               "Accept": "application/json"})
    for n in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.load(r)
        except Exception as e:  # network, HTTP or JSON error: retry, then report
            if n == tries - 1:
                if failures is not None:
                    failures.append(f"{url.split('?')[0]}: {e}")
                return None
            time.sleep(2 * (n + 1))


def norm(name):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", "", s)
    return re.sub(r"[^a-z]", "", s)


def nickname(team):
    return team.split()[-1]


def game_key(game):
    away, home = game.split(" @ ")
    return nickname(away), nickname(home)


def to_et(iso):
    t = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ET)
    return t.strftime("%-I:%M %p ET")


def et_date(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ET).strftime("%Y-%m-%d")


def to_z(iso):
    t = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- Pinnacle ----------

def parse_pinnacle(matchups, markets, sport):
    """Full-game, pre-game markets as rows with named sides."""
    by_id = {m["id"]: m for m in matchups}
    rows = []
    for k in markets:
        m = by_id.get(k.get("matchupId"))
        if not m or k.get("period") != 0 or m.get("isLive"):
            continue
        parent = m.get("parent") or m
        teams = {p["alignment"]: p["name"] for p in parent.get("participants", [])}
        if "away" not in teams or "home" not in teams:
            continue
        named = {p["id"]: p.get("name") for p in m.get("participants", []) if p.get("id")}
        rows.append({
            "sport": sport, "game": f"{teams['away']} @ {teams['home']}",
            "start": parent.get("startTime"), "type": k["type"], "key": k.get("key", ""),
            "alt": bool(k.get("isAlternate")),
            "special": (m.get("special") or {}).get("description"),
            "prices": [{"name": named.get(p.get("participantId"))
                                or teams.get(p.get("designation"), p.get("designation")),
                        "points": p.get("points"), "price": p["price"]}
                       for p in k.get("prices", [])],
        })
    return rows


def pinnacle_props(rows):
    """{(player, stat, line): (over, under, game)} from 'X Total Points' specials."""
    out = {}
    for r in rows:
        s = r["special"]
        if not s or " Total " not in s or r["type"] != "total":
            continue
        name, stat = s.rsplit(" Total ", 1)
        sides = {p["name"].lower(): p for p in r["prices"] if p["name"]}
        if "over" in sides and "under" in sides:
            out[(norm(name), stat, sides["over"]["points"])] = (
                sides["over"]["price"], sides["under"]["price"], r["game"])
    return out


# ---------- Action Network (FanDuel) ----------

def fanduel_props(feed, stat):
    """{(player, stat, line): {"player","team","over","under"}} from one prop feed."""
    teams = {}
    for g in feed.get("games", []):
        for side in ("home_team", "away_team"):
            teams[g[side]["id"]] = g[side]["full_name"]
    players = {p["id"]: p for p in feed.get("players", [])}
    out = {}
    book = (feed.get("markets") or {}).get(FANDUEL_BOOK) or {}
    for rows in book.get("event", {}).values():
        for x in rows:
            p = players.get(x.get("player_id"))
            if not p or x.get("is_alt_market"):
                continue
            k = (norm(p["full_name"]), stat, x["value"])
            out.setdefault(k, {"player": p["full_name"], "team": teams.get(p["team_id"])})[x["side"]] = x["odds"]
    return out


def tip_times(scoreboard):
    """{(away nickname, home nickname): startISO} from an Action Network scoreboard."""
    out = {}
    for g in scoreboard.get("games", []):
        t = {x["id"]: x["full_name"] for x in g["teams"]}
        out[(nickname(t[g["away_team_id"]]), nickname(t[g["home_team_id"]]))] = g["start_time"]
    return out


def match_lines(scoreboard, pin_rows, sport):
    """Both sides of the moneyline, spread, total and team totals for every game
    where FanDuel and Pinnacle offer the same number."""
    by_game = {}
    for r in pin_rows:
        if not r["special"]:
            by_game.setdefault(game_key(r["game"]), []).append(r)
    legs = []
    for g in scoreboard.get("games", []):
        t = {x["id"]: x["full_name"] for x in g["teams"]}
        away, home = t[g["away_team_id"]], t[g["home_team_id"]]
        fd = next((o for o in g.get("odds", []) if str(o.get("book_id")) == FANDUEL_BOOK), None)
        pm = by_game.get((nickname(away), nickname(home)))
        if not fd or not pm:
            continue

        def sharp(typ, pts=None, side=None):
            for r in pm:
                if r["type"] != typ:
                    continue
                pr = {(p["name"] or "").lower(): p for p in r["prices"]}
                if typ == "moneyline":
                    return {k: v["price"] for k, v in pr.items()}
                if typ == "spread" and pr.get(away.lower(), {}).get("points") == pts:
                    return {k: v["price"] for k, v in pr.items()}
                if typ == "total" and r["prices"][0]["points"] == pts:
                    return {k: v["price"] for k, v in pr.items()}
                if (typ == "team_total" and r["key"].endswith(side)
                        and r["prices"][0]["points"] == pts):
                    return {k: v["price"] for k, v in pr.items()}
            return None

        base = {"sport": sport, "game": f"{away} @ {home}", "startISO": g["start_time"]}

        def leg(typ, team, pick, odds, pair):
            if odds is None or not pair or None in pair:
                return
            c = {**base, "type": typ, "pick": pick, "fd": odds, "sharp": list(pair)}
            if team:
                c["team"] = team
            legs.append(c)

        a, h = away.lower(), home.lower()
        ml = sharp("moneyline")
        if ml:
            leg("moneyline", away, f"{away} ML", fd.get("ml_away"), (ml.get(a), ml.get(h)))
            leg("moneyline", home, f"{home} ML", fd.get("ml_home"), (ml.get(h), ml.get(a)))
        if fd.get("spread_away") is not None:
            sp = sharp("spread", fd["spread_away"])
            if sp:
                leg("spread", away, f"{away} {fd['spread_away']:+g}", fd.get("spread_away_line"), (sp.get(a), sp.get(h)))
                leg("spread", home, f"{home} {fd['spread_home']:+g}", fd.get("spread_home_line"), (sp.get(h), sp.get(a)))
        if fd.get("total") is not None:
            tt = sharp("total", fd["total"])
            if tt:
                leg("total over", None, f"Over {fd['total']:g}", fd.get("over"), (tt.get("over"), tt.get("under")))
                leg("total under", None, f"Under {fd['total']:g}", fd.get("under"), (tt.get("under"), tt.get("over")))
        for side, team in (("away", away), ("home", home)):
            pts = fd.get(side + "_total")
            x = sharp("team_total", pts, side) if pts is not None else None
            if x:
                leg("team total over", team, f"{team} Over {pts:g} team pts", fd.get(side + "_over"), (x.get("over"), x.get("under")))
                leg("team total under", team, f"{team} Under {pts:g} team pts", fd.get(side + "_under"), (x.get("under"), x.get("over")))
    return legs


# ---------- ESPN ----------

def parse_gamelog(d):
    """Regular-season games, newest first: {"date","MIN","PTS","REB","AST","3PM"}."""
    rows = []
    if not d or "labels" not in d:
        return rows
    events = d.get("events", {})
    for st in d.get("seasonTypes", []):
        if "Regular Season" not in st.get("displayName", ""):
            continue
        for cat in st.get("categories", []):
            for e in cat.get("events", []):
                s = dict(zip(d["labels"], e["stats"]))
                try:
                    rows.append({"date": events.get(e["eventId"], {}).get("gameDate", ""),
                                 "MIN": float(s["MIN"] or 0), "PTS": float(s["PTS"]),
                                 "REB": float(s["REB"]), "AST": float(s["AST"]),
                                 "3PM": float(s["3PT"].split("-")[0])})
                except (KeyError, ValueError):
                    continue
    rows.sort(key=lambda r: r["date"], reverse=True)
    return rows


def normal_minutes(rows):
    """Drop cameo games (under 60% of the player's median minutes)."""
    if not rows:
        return []
    med = statistics.median(r["MIN"] for r in rows)
    return [r for r in rows if r["MIN"] > 0 and r["MIN"] >= 0.6 * med]


def hit_rates(cur, last, stat, line, side):
    """hitRates and median text for one prop from two seasons of game logs."""
    keys = STAT_KEYS[stat]

    def val(r):
        return sum(r[k] for k in keys)

    def hits(rows):
        won = sum(1 for r in rows if (val(r) > line if side == "over" else val(r) < line))
        return [won, len(rows)]

    cur, last = normal_minutes(cur), normal_minutes(last)
    note = role_change(cur, last)
    if note:
        last = []
    both = cur + last
    hr = {}
    if cur:
        hr["season"] = hits(cur)
    if len(both) >= 5:
        hr["last10"] = hits(both[:10])
        hr["last5"] = hits(both[:5])
    if last:
        hr["lastSeason"] = hits(last)
    if not hr:
        return None, None, note
    recent = both[:20]
    med = statistics.median(val(r) for r in recent)
    return hr, f"median {med:g} {STAT_ABBR[stat]} (last {len(recent)}) vs line {line:g}", note


def role_change(cur, last):
    """'minutes 27 -> 14' when this season's role differs from last season's."""
    if len(cur) < 2 or not last:
        return None
    now, before = statistics.median(r["MIN"] for r in cur), statistics.median(r["MIN"] for r in last)
    if before and abs(now - before) / before > ROLE_CHANGE:
        return f"minutes {before:.0f} -> {now:.0f} vs last season, so only this season's games count"
    return None


def parse_injuries(d):
    """{player: {"team","status","note"}} plus {team: [players out]}."""
    players, out = {}, {}
    for t in (d or {}).get("injuries", []):
        for i in t.get("injuries", []):
            name = i.get("athlete", {}).get("displayName")
            if not name:
                continue
            players[norm(name)] = {"team": t.get("displayName"), "status": i.get("status", ""),
                                   "note": i.get("shortComment", "")}
            if i.get("status") == "Out":
                out.setdefault(t.get("displayName"), []).append(name)
    return players, out


def injury_block(info):
    """Reason to leave a prop out, or None. Day-to-day players stay only when
    the report says probable/expected to play."""
    if not info:
        return None
    note = info["note"].lower()
    if info["status"] == "Out" or "out for" in note or "ruled out" in note:
        return "out"
    if re.search(r"\b(questionable|doubtful|game-time)\b", note):
        return "questionable"
    return None


# ---------- candidates ----------

def prop_candidates(pin_props, fd_props, tips, logs, injuries, team_out):
    cands, dropped = [], []
    for k, fd in fd_props.items():
        if k not in pin_props or "over" not in fd or "under" not in fd:
            continue
        p_over, p_under, game = pin_props[k]
        _, stat, line = k
        info = injuries.get(k[0])
        why_out = injury_block(info)
        if why_out:
            drop = f"{fd['player']} ({why_out}: {info['note']})"
            if drop not in dropped:
                dropped.append(drop)
            continue
        iso = tips.get(game_key(game))
        if not iso:
            continue
        for side, odds, sharp in (("over", fd["over"], [p_over, p_under]),
                                  ("under", fd["under"], [p_under, p_over])):
            if not -500 <= odds <= 200:
                continue
            fair = om.fair_prob(sharp)
            if om.ev(fair, odds) < MIN_PROP_EV:
                continue
            c = {"sport": "NBA" if stat in STAT_KEYS else "MLB", "game": game,
                 "start": to_et(iso), "startISO": to_z(iso),
                 "pick": f"{fd['player']} {side.title()} {line:g} {stat}", "market": stat,
                 "type": "prop " + side, "team": fd["team"], "player": fd["player"],
                 "prices": {"FanDuel": odds}, "sharpOdds": sharp, "sharpSource": "Pinnacle",
                 "source": SOURCE, "star": stat == "Points" and line >= 22.5, "adjust": 0.0}
            lg = logs.get(k[0])
            if lg and stat in STAT_KEYS:
                hr, med, role = hit_rates(lg["cur"], lg["last"], stat, line, side)
                if role:
                    c["roleNote"] = role
                if hr:
                    c["hitRates"], c["median"] = hr, med
                    l10, ls = hr.get("last10", [0, 0]), hr.get("lastSeason", [0, 0])
                    c["why"] = (f"{side.title()} hit {l10[0]}/{l10[1]} last 10, {ls[0]}/{ls[1]} "
                                f"last season; Pinnacle no-vig {fair * 100:.0f}%.")
            c.setdefault("why", f"Pinnacle no-vig {fair * 100:.0f}% vs FanDuel {odds:+d}.")
            if info:
                c["injuryNote"] = f"{info['status']}: {info['note']}"
            if team_out.get(fd["team"]):
                c["teammatesOut"] = team_out[fd["team"]]
            cands.append(c)
    return cands, dropped


def line_candidates(legs):
    market = {"moneyline": "Moneyline", "spread": "Spread", "total over": "Total",
              "total under": "Total", "team total over": "Team total",
              "team total under": "Team total"}
    out = []
    for x in legs:
        c = {"sport": x["sport"], "game": x["game"], "start": to_et(x["startISO"]),
             "startISO": to_z(x["startISO"]), "pick": x["pick"], "market": market[x["type"]],
             "type": x["type"], "prices": {"FanDuel": x["fd"]}, "sharpOdds": x["sharp"],
             "sharpSource": "Pinnacle", "source": SOURCE, "star": False, "adjust": 0.0,
             "why": f"Pinnacle no-vig {om.fair_prob(x['sharp']) * 100:.0f}% vs FanDuel {x['fd']:+d}."}
        if x.get("team"):
            c["team"] = x["team"]
        out.append(c)
    return out


def nba_logs(players, teams, failures):
    """{normalized name: {"cur": rows, "last": rows}} for players on these teams."""
    listing = get(f"{ESPN}/basketball/nba/teams", failures) or {}
    ids = {t["team"]["displayName"]: t["team"]["id"]
           for t in listing.get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", [])}
    pool = ThreadPoolExecutor(12)
    rosters = pool.map(lambda t: get(f"{ESPN}/basketball/nba/teams/{ids[t]}/roster", failures),
                       [t for t in teams if t in ids])
    athlete = {}
    for r in rosters:
        for a in (r or {}).get("athletes", []):
            athlete[norm(a["fullName"])] = a["id"]
    last_season = datetime.now(ET).year if datetime.now(ET).month >= 7 else datetime.now(ET).year - 1

    def one(name):
        aid = athlete.get(name)
        if not aid:
            return name, None
        cur = parse_gamelog(get(f"{ESPN_LOGS}/{aid}/gamelog"))
        last = parse_gamelog(get(f"{ESPN_LOGS}/{aid}/gamelog?season={last_season}"))
        return name, {"cur": cur, "last": last}

    return {n: v for n, v in pool.map(one, players) if v}


def fetch(date, sports):
    day = date.replace("-", "")
    failures, report, cands = [], {"date": date, "sports": {}}, []
    for sport in sports:
        league, an, espn = LEAGUES[sport]
        rep = report["sports"].setdefault(sport, {})
        board = get(f"{ACTION}/v1/scoreboard/{an}?period=game&date={day}", failures)
        matchups = get(f"{PINNACLE}/leagues/{league}/matchups", failures)
        markets = get(f"{PINNACLE}/leagues/{league}/markets/straight", failures)
        if not board or matchups is None or markets is None:
            rep["error"] = "price feed unavailable"
            continue
        board["games"] = [g for g in board.get("games", [])
                          if g.get("status") == "scheduled" and et_date(g["start_time"]) == date]
        rep["games"] = len(board["games"])
        if not board["games"]:
            continue
        pin = parse_pinnacle(matchups, markets, sport)
        legs = line_candidates(match_lines(board, pin, sport))
        if not legs:  # Pinnacle sometimes answers with an empty board; ask once more
            time.sleep(5)
            pin = parse_pinnacle(get(f"{PINNACLE}/leagues/{league}/matchups", failures) or [],
                                 get(f"{PINNACLE}/leagues/{league}/markets/straight", failures) or [], sport)
            legs = line_candidates(match_lines(board, pin, sport))
        cands += legs
        rep["lineLegs"] = len(legs)
        if sport not in PROP_TYPES:
            continue
        fd = {}
        for typ, stat in PROP_TYPES[sport].items():
            feed = get(f"{ACTION}/v2/scoreboard/{an}/markets?bookIds={FANDUEL_BOOK}"
                       f"&date={day}&customPickTypes={typ}", failures)
            if feed and "games" in feed:
                fd.update(fanduel_props(feed, stat))
        injuries, team_out = parse_injuries(get(f"{ESPN}/{espn}/injuries", failures))
        logs = {}
        if sport == "NBA":
            logs = nba_logs({k[0] for k in fd}, {v["team"] for v in fd.values() if v["team"]}, failures)
        props, dropped = prop_candidates(pinnacle_props(pin), fd, tip_times(board), logs, injuries, team_out)
        cands += props
        rep.update(propLegs=len(props), playersWithLogs=len(logs), injuryDrops=dropped)
    report["candidates"] = len(cands)
    report["failures"] = failures
    return cands, report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", required=True, help="America/New_York date, YYYY-MM-DD")
    ap.add_argument("--out", required=True, help="folder for candidates.json and fetch_report.json")
    ap.add_argument("--sports", default="NBA,MLB,NFL")
    a = ap.parse_args(argv)
    cands, report = fetch(a.date, [s.strip().upper() for s in a.sports.split(",") if s.strip()])
    with open(f"{a.out}/candidates.json", "w") as f:
        json.dump(cands, f, indent=1)
    with open(f"{a.out}/fetch_report.json", "w") as f:
        json.dump(report, f, indent=1)
    for sport, rep in report["sports"].items():
        print(f"{sport}: " + ", ".join(f"{k} {v if not isinstance(v, list) else len(v)}" for k, v in rep.items()))
    print(f"{report['candidates']} candidates, {len(report['failures'])} fetch failures")
    return 0 if cands else 1


if __name__ == "__main__":
    sys.exit(main())
