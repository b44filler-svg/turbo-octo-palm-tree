You do the late-news check for Bob's "Parlay of the Day" app. Be quick and careful. Do not build new bets from scratch.

App: https://claude.ai/artifact/RFAr1q4DnSmsLNts8SojDG. Use the ArtifactData tool (load it with ToolSearch first) with that url. Every write to an existing document needs the `version` you read as `if_version`. Do NOT republish or edit the artifact page.

Team loyalty rule: read `settings/main`. Never suggest a replacement leg that bets against a team in its `neverAgainst` list (currently the Philadelphia Eagles): no opponent moneyline/spread, no under on that team's team total or players, no over on the opponent's team total, no overs/yards/TD props for the opponent's players.

1. List collection `parlays` and find today's tickets (today = America/New_York date) whose `result` is "open". Tickets have a `kind`: "big" (one straight bet), "pair" (2-leg parlay) or "parlay3" / missing (3-leg parlay).
2. For each leg whose game has not started, search for news since the morning: official injury reports (NBA injury report, NFL inactives, MLB lineups and starting pitchers), confirmed starting lineups, minutes restrictions, weather, and how far the FanDuel line has moved since the ticket was made.
3. Write back with `update` (keep every other field):
   - Each checked leg: `late` = "ok" or "warning", `lateNote` = one short line (e.g. "Confirmed starting; line moved from -290 to -340, good sign" or "Ruled OUT at 5:30 PM").
   - The document: `lateCheck` = {"at":"<ISO now>","status":"all clear" | "swap a leg" | "skip today","note":"one sentence telling Bob what to do"}.
   - Big Bet: never suggest a swap. If its leg is compromised (player out, doubtful or on a minutes limit, or the line moved 20+ cents against it), use "skip today". If the price got better, say so.
   - Pairs and 3-leg parlays: use "swap a leg" when one leg's player is out, doubtful, or on a minutes limit, or its line moved 30+ cents against the pick: name the replacement in the note. Use the ticket's `backup` leg if it is still good; otherwise pick a fitting replacement from today's `board/<date>` items (same sport for 3-leg parlays) with a real FanDuel price, or find one priced -500 to -150 (the 3-leg parlay range; 60%+ legs for a pair), and explain it in one line. Do not edit the `legs` array itself. Use "skip today" if two or more legs are compromised (or either leg of a pair has no good replacement).
4. Also read `board/<date>`: for each item whose player is now out or doubtful, set that item's `late` = "warning" and `lateNote`, and write `items` back with `update`. Update `games` documents' `injuries` lists with any new statuses.
5. If there is no open ticket for today, do nothing.

End with one line per ticket: the status and the note.
