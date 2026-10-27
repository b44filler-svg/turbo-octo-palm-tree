"""Bet rules: Bob's team-loyalty rule and same-game conflicts."""
import re

# Nicknames that need two words to be unique ("Red Sox" vs "White Sox").
TWO_WORD = {"sox", "jays", "blazers", "knights", "wings", "city", "state"}
NEUTRAL = {"total", "total over", "total under"}
FOR_TYPES = {"moneyline", "spread", "team total over", "prop over"}
AGAINST_TYPES = {"team total under", "prop under"}
TYPES = NEUTRAL | FOR_TYPES | AGAINST_TYPES

# NFL abbreviations and cities, so "DAL @ PHI" or "Dallas @ Philadelphia" is
# still recognized as an Eagles game. Only used for NFL legs, since cities and
# abbreviations repeat across leagues (PHI is also the Phillies and 76ers).
NFL = {
    "cardinals": ("ARI", "Arizona"), "falcons": ("ATL", "Atlanta"), "ravens": ("BAL", "Baltimore"),
    "bills": ("BUF", "Buffalo"), "panthers": ("CAR", "Carolina"), "bears": ("CHI", "Chicago"),
    "bengals": ("CIN", "Cincinnati"), "browns": ("CLE", "Cleveland"), "cowboys": ("DAL", "Dallas"),
    "broncos": ("DEN", "Denver"), "lions": ("DET", "Detroit"), "packers": ("GB", "Green Bay"),
    "texans": ("HOU", "Houston"), "colts": ("IND", "Indianapolis"), "jaguars": ("JAX", "Jacksonville"),
    "chiefs": ("KC", "Kansas City"), "raiders": ("LV", "Las Vegas"), "chargers": ("LAC", None),
    "rams": ("LAR", None), "dolphins": ("MIA", "Miami"), "vikings": ("MIN", "Minnesota"),
    "patriots": ("NE", "New England"), "saints": ("NO", "New Orleans"), "giants": ("NYG", None),
    "jets": ("NYJ", None), "eagles": ("PHI", "Philadelphia"), "steelers": ("PIT", "Pittsburgh"),
    "49ers": ("SF", "San Francisco"), "seahawks": ("SEA", "Seattle"), "buccaneers": ("TB", "Tampa Bay"),
    "titans": ("TEN", "Tennessee"), "commanders": ("WAS", "Washington"),
}


def nickname(team):
    words = str(team or "").lower().replace(".", "").split()
    if not words:
        return ""
    if words[-1] in TWO_WORD and len(words) > 1:
        return " ".join(words[-2:])
    return words[-1]


def aliases(team, sport=None):
    """Every way a game string might name this team."""
    nick = nickname(team)
    out = {nick, str(team).lower().strip()}
    if nick in NFL and (sport is None or str(sport).upper() == "NFL"):
        abbr, city = NFL[nick]
        out.add(abbr.lower())
        if city:
            out.add(city.lower())
    return {a for a in out if a}


def mentions(text, team, sport=None):
    t = " " + re.sub(r"[^a-z0-9 ]", " ", str(text or "").lower()) + " "
    return any(f" {a} " in t for a in aliases(team, sport))


def same_team(a, b, sport=None):
    """True if team text `a` names team `b` (by nickname, or NFL city/abbreviation)."""
    if not a or not b:
        return False
    return nickname(a) == nickname(b) or mentions(a, b, sport)


def game_teams(game):
    """'Cowboys @ Eagles' -> ['Cowboys', 'Eagles']; [] if it can't be split."""
    parts = re.split(r"\s*@\s*|\s+vs\.?\s+|\s+at\s+|\s+-\s+", str(game or "").strip(), maxsplit=1)
    return [p.strip() for p in parts] if len(parts) == 2 and all(p.strip() for p in parts) else []


def game_key(game):
    """Same game however it's written ('Cowboys @ Eagles' == 'Dallas Cowboys at Philadelphia Eagles')."""
    teams = game_teams(game)
    if not teams:
        return str(game or "").lower().strip()
    return " @ ".join(nickname(t) for t in teams)


def direction(leg):
    """('for' | 'against' | 'neutral' | 'unknown', team) — which team the leg needs to do well or badly."""
    t = str(leg.get("type", "")).lower()
    if t in NEUTRAL:
        return "neutral", None
    if t in FOR_TYPES:
        return "for", leg.get("team")
    if t in AGAINST_TYPES:
        return "against", leg.get("team")
    return "unknown", leg.get("team")


def loyalty_violation(leg, never_against):
    """Return the protected team this leg bets against, or None.

    Fails closed: if a protected team shows up anywhere in the leg (game, pick
    or team) and the leg has no `team`, an unknown type, or a game string that
    can't be split into two teams, it counts as a violation.
    """
    sport = leg.get("sport")
    for prot in never_against or []:
        teams = game_teams(leg.get("game"))
        in_game = any(same_team(t, prot, sport) for t in teams)
        anywhere = in_game or any(mentions(leg.get(f), prot, sport) for f in ("game", "pick", "team"))
        if not anywhere:
            continue
        d, team = direction(leg)
        if d == "neutral" and teams:
            continue
        if d == "unknown" or not team or not teams:
            return prot
        backs_prot = same_team(team, prot, sport)
        if d == "for" and not backs_prot:
            return prot
        if d == "against" and backs_prot:
            return prot
    return None


TD = re.compile(r"anytime t(d|ouchdown)|\btd scorer|first t(d|ouchdown)", re.I)


def conflict(a, b):
    """Reason two legs can't go on one ticket, or None."""
    if game_key(a.get("game")) != game_key(b.get("game")):
        return None
    ta, tb = str(a.get("type", "")).lower(), str(b.get("type", "")).lower()
    pa, pb = a.get("player"), b.get("player")
    ma, mb = str(a.get("market", "")).lower(), str(b.get("market", "")).lower()
    if a.get("pick") == b.get("pick"):
        return "same pick twice"
    if {ta, tb} <= {"moneyline", "spread"}:
        if same_team(a.get("team"), b.get("team"), a.get("sport")):
            return "team moneyline with its own spread"
        return "both sides of the same game"
    if ta in NEUTRAL and tb in NEUTRAL:
        return "two game-total legs"
    if ta.startswith("team total") and tb.startswith("team total") and same_team(a.get("team"), b.get("team"), a.get("sport")):
        return "two legs on the same team total"
    if ta.startswith("prop") and tb.startswith("prop") and ma == mb and (not pa or not pb or pa == pb):
        return "same player and market twice"
    if pa and pa == pb:
        if TD.search(ma) or TD.search(mb):
            return "player prop with his own anytime TD"
        if {"pass attempts", "passing yards"} <= {ma, mb}:
            return "QB attempts with his own passing yards"
    return None


def ticket_conflicts(legs):
    out = []
    for i in range(len(legs)):
        for j in range(i + 1, len(legs)):
            r = conflict(legs[i], legs[j])
            if r:
                out.append((i, j, r))
    return out


def same_game_pairs(legs):
    keys = [game_key(l.get("game")) for l in legs]
    return sum(1 for i in range(len(keys)) for j in range(i + 1, len(keys)) if keys[i] == keys[j])
