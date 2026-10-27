"""Bet rules: Bob's team-loyalty rule and same-game conflicts."""

# Nicknames that need two words to be unique ("Red Sox" vs "White Sox").
TWO_WORD = {"sox", "jays", "blazers", "knights", "wings", "city", "state"}
NEUTRAL = {"total", "total over", "total under"}
FOR_TYPES = {"moneyline", "spread", "team total over", "prop over"}
AGAINST_TYPES = {"team total under", "prop under"}


def nickname(team):
    words = str(team or "").lower().replace(".", "").split()
    if not words:
        return ""
    if words[-1] in TWO_WORD and len(words) > 1:
        return " ".join(words[-2:])
    return words[-1]


def same_team(a, b):
    return bool(a) and bool(b) and nickname(a) == nickname(b)


def game_teams(game):
    """'Cowboys @ Eagles' -> ['Cowboys', 'Eagles']."""
    for sep in (" @ ", " vs. ", " vs ", " at "):
        if sep in str(game):
            return [t.strip() for t in str(game).split(sep, 1)]
    return []


def direction(leg):
    """('for' | 'against' | 'neutral', team) — which team the leg needs to do well or badly."""
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

    Fails closed: a leg in a protected team's game with no `team` or an
    unknown type counts as a violation.
    """
    teams = game_teams(leg.get("game"))
    for prot in never_against or []:
        if not any(same_team(prot, t) for t in teams):
            continue
        d, team = direction(leg)
        if d == "neutral":
            continue
        if d == "unknown" or not team:
            return prot
        if d == "for" and not same_team(team, prot):
            return prot
        if d == "against" and same_team(team, prot):
            return prot
    return None


def conflict(a, b):
    """Reason two legs can't go on one ticket, or None."""
    if a.get("game") != b.get("game"):
        return None
    ta, tb = str(a.get("type", "")).lower(), str(b.get("type", "")).lower()
    pa, pb = a.get("player"), b.get("player")
    if a.get("pick") == b.get("pick"):
        return "same pick twice"
    if {ta, tb} <= {"moneyline", "spread"}:
        if same_team(a.get("team"), b.get("team")):
            return "team moneyline with its own spread"
        return "both sides of the same game"
    if ta in NEUTRAL and tb in NEUTRAL:
        return "two game-total legs"
    if pa and pa == pb:
        ma, mb = str(a.get("market", "")).lower(), str(b.get("market", "")).lower()
        if ma == mb:
            return "same player and market twice"
        if "td" in ma or "td" in mb or "touchdown" in ma + mb:
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
    return sum(1 for i in range(len(legs)) for j in range(i + 1, len(legs))
               if legs[i].get("game") == legs[j].get("game"))
