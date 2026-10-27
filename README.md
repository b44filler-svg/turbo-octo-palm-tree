# Parlay of the Day: picks engine

The math and rules behind Bob's Parlay of the Day app
(https://claude.ai/artifact/RFAr1q4DnSmsLNts8SojDG). The daily research task
clones this repo, collects odds and stats, and lets this code do all the math.
Then an independent auditor agent checks the result before anything posts.

```
research (web) ──► candidates.json ──► picks.build ──► draft.json ──► picks.audit ──► picks-auditor agent ──► app
                                         (model, rules,                (math & rules,    (re-checks prices,
                                          bet builder)                  independent)      news, stats by search)
```

## Bet types

| Type | What it is | Built for | Thresholds (`picks/build.py`) |
|---|---|---|---|
| **Best Bets board** | Top 10 singles with positive value, ranked | Win chance **and** payout together | 50%+ win, value > 0, ranked by quarter-Kelly growth; ★ sweet spot = 55%+ and pays $18+ on $10 |
| **Big Bet** | One straight bet | A bigger stake | 60%+ win, edge 2.5+ pts, value +3%+, -300 to +150, sharp fair price required |
| **Power Pair** | 2-leg parlay | Strong legs that are too juiced alone | each leg 60%+, joint 42%+, value +2%+, pays 1.9x-3.6x; no leg reused from the Big Bet |
| **3-leg parlay** | One per sport | Closest to a lock that still pays | legs -500 to -150, max one leg with negative edge, $18+ per $10, value >= 0 |

**Why rank by Kelly growth?** Expected log growth at a quarter of the Kelly
stake is one number that rises with both a higher win chance and a bigger
payout. A +150 bet with a 4-point edge and a -200 bet with a 3-point edge can
land side by side, which is the "high win % *and* high payout" trade-off.

## How a leg is scored (`picks/model.py`)

1. **Fair price**: the sharp book's prices for both sides, vig removed with the
   **power method** (takes more vig off longshots than favorites, matching the
   favorite-longshot bias).
2. **Stat price**: weighted hit rate vs the line (season 35%, last 10 30%,
   last 5 15%, last season 20%).
3. **Blend**: `shrink * fair + (1 - shrink) * stat`, where the stat weight is
   cut for thin samples (full weight at 20+ games). `shrink` comes from the
   app's learning record and rises when results trail the model.
4. **Adjustments**: prop unders +1.5 pts, star overs -1 pt (public money bias),
   matchup `adjust` ±5, expert/news `newsAdjust` ±3, line moved against -2.
5. **Guard rails**: the model may sit at most **7 points** from the no-vig
   market (a model far from the market is usually wrong), and never above 92%.

## The checks

- `picks/audit.py`: recomputes implied probability, edge, payout, parlay odds,
  joint chance and value **with its own code** (it does not import the builder's
  math), and checks leg counts, conflicts, the never-against rule, honesty
  wording ("lock", "guaranteed"), games already started, non-FanDuel prices and
  thin fair prices. Exit code 1 on any FAIL.
- `.claude/agents/picks-auditor.md`: the auditor agent. It runs `picks.audit`,
  then re-verifies every price, injury and stat claim with its own searches,
  argues the case against each ticket, and returns APPROVE / FIX / REJECT.
  The daily task must fix every blocking finding before posting.

## Candidate format

```json
{
  "sport": "NBA", "game": "Lakers @ Suns", "start": "10:00 PM ET", "startISO": "2026-10-28T02:00:00Z",
  "pick": "Austin Reaves Under 5.5 Assists", "market": "Assists",
  "type": "prop under",
  "team": "Los Angeles Lakers", "player": "Austin Reaves",
  "odds": -125,
  "sharpOdds": [-145, 125], "sharpSource": "Pinnacle",
  "source": "FanDuel Sportsbook",
  "hitRates": {"season": [3, 4], "last10": [7, 10], "last5": [4, 5], "lastSeason": [45, 70]},
  "median": "median 4 ast vs line 5.5",
  "star": false, "adjust": 0.01, "newsAdjust": 0.0, "lineMovedAgainst": false,
  "why": "Under in 45 of 70 last season; Suns allow the 4th-fewest assists."
}
```

`type` is one of `moneyline`, `spread`, `total over`, `total under`,
`team total over`, `team total under`, `prop over`, `prop under`. `team` is the
side for moneylines and spreads and the player's team for props; it is required
(except game totals) so the never-against rule can be enforced. Put the pick's
side **first** in `sharpOdds`. Without a sharp price, give `fairProb` and
`fairFlag: true`, or FanDuel's own two sides as `bookOdds` (flagged as thin).

## Run it

```bash
python -m picks.build tests/fixtures/candidates.json --date 2026-10-27 --settings tests/fixtures/settings.json --out draft.json
python -m picks.audit draft.json --settings tests/fixtures/settings.json
python3 -m picks.selftest                           # quick check, no installs (the daily task runs this)
python -m pytest -q                                 # full engine + auditor tests
NODE_PATH=$(npm root -g) node tests/page_test.js    # app page in Chromium with a mocked database
```

## Repo layout

- `picks/`: engine (`oddsmath`, `model`, `rules`, `build`) and the independent `audit`
- `.claude/agents/picks-auditor.md`: the auditor agent
- `prompts/`: the scheduled task prompts (the copies live in the Routines)
- `app/parlay-of-the-day.html`: source of the app page (the published artifact is the live copy)
- `tests/`: pytest suite, fixtures, and the Playwright page test
