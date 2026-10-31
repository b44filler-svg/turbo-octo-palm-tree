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

Every multi-leg ticket (pairs, parlays, boosts) uses one leg per game: FanDuel
only takes same-game legs as a Same Game Parlay at its own price, so a product of
the leg odds would promise a payout that isn't on offer. `picks.audit` fails any
ticket that breaks this.

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
5. **Guard rails**: the model may sit at most **2.5 points** from a sharp two-sided no-vig price (7 from a thin, FanDuel-only one; picking the biggest edges out of dozens overstates them), and never above 92%.
6. **Market first**: each leg also gets `marketEv`, its value by the sharp
   no-vig price alone. No leg priced more than 2% worse than that is used
   anywhere (`MARKET_FLOOR`); full-tier tickets, the board and payout plays must
   be worth at least break-even by the market alone, leans included. Only
   real two-sided sharp prices (`sharpOdds`) count as sharp. Through 10/29
   the stat-driven picks hit 58% of legs against 65% expected, and FanDuel's
   juiced unders sat 4-5% worse than Pinnacle.

## Boost builder (`sgp`) and the price snapshot (`market`)

`build_sgp` picks, per game, the 3 FanDuel legs priced best against the sharp
market (one leg per player, no contradictions) and gives `fairJoint` (sharp
chances multiplied, as if independent) and `breakEven`: the lowest pre-boost
Same Game Parlay price worth taking with no boost and with 25/30/50% boosts,
`1 + (1/p - 1)/(1 + boost)`. `market` is every sharp-priced FanDuel leg; the app's
"check your own parlay" tool reads it from `market/<date>`. `picks.audit`
re-derives all of it from the raw prices. `prompts/price-scan.md` refreshes it
midday and adds new value bets the auditor clears.

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
  "prices": {"FanDuel": -125, "Courtside": -120},
  "sharpOdds": [-145, 125], "sharpSource": "Pinnacle",
  "source": "Covers odds grid",
  "hitRates": {"season": [3, 4], "last10": [7, 10], "last5": [4, 5], "lastSeason": [45, 70]},
  "median": "median 4 ast vs line 5.5",
  "star": false, "adjust": 0.01, "newsAdjust": 0.0, "lineMovedAgainst": false,
  "why": "Under in 45 of 70 last season; Suns allow the 4th-fewest assists."
}
```

`prices` holds the price at each of Bob's books (settings `books`); singles use the best one, and each parlay is built at the one book that pays most. A single `odds` + `source` still works.

Promos go in a separate `--boosts` file: `[{"id","book","title","pct":0.3,"minLegs":2,"maxLegs":3,"minLegOdds":-250,"sport":"NBA","maxStake":25}]`.

`type` is one of `moneyline`, `spread`, `total over`, `total under`,
`team total over`, `team total under`, `prop over`, `prop under`. `team` is the
side for moneylines and spreads and the player's team for props; it is required
(except game totals) so the never-against rule can be enforced. Put the pick's
side **first** in `sharpOdds`. Without a sharp price, give `fairProb` and
`fairFlag: true`, or FanDuel's own two sides as `bookOdds` (flagged as thin).

## Fetching prices

`python3 -m picks.fetch --date YYYY-MM-DD --out <folder>` writes `candidates.json`
straight from public feeds: Pinnacle's guest API for sharp two-sided prices,
Action Network's odds feed for FanDuel's prices (book 69, the NJ board; confirm
PA in the app) and true tip times, and ESPN for NBA and NFL game logs and injury
reports, plus NFL props (passing, rushing and receiving yards, receptions,
pass TDs, attempts, interceptions) with ESPN football game logs. It drops
players listed Out, Doubtful or questionable, ignores last season's
games when a player's minutes moved more than 25% (`roleNote`), and lists
teammates who are out (`teammatesOut`) so research can adjust for role
changes. `fetch_report.json` has counts, injury drops and any feed failures.

## Books, closing lines, market weight (10/31)

- `--books` reads each book's **Pennsylvania** board in the Action Network feed
  (FanDuel 255, DraftKings 1534, BetMGM 280, Caesars 1906, Fanatics 2791, bet365 3547, BetRivers 122);
  a leg's `prices` lists every book posting the same number Pinnacle prices, and
  singles take the best. Courtside has no public feed.
- `--close legs.json` records closing lines for posted legs whose game starts within
  70 minutes (`prompts/close-snapshot.md`, run hourly at :50): `clv` against the
  book's close and `clvSharp` against Pinnacle's no-vig close, the truer skill test.
- The market weight (`shrink`) defaults to 0.7: through 10/30 the stat-driven legs
  hit 52% against 63% expected. Plain 3-leg parlays have no lean tier.

## Run it

```bash
python -m picks.build tests/fixtures/candidates.json --date 2026-10-27 --settings tests/fixtures/settings.json --out draft.json
python -m picks.audit draft.json --settings tests/fixtures/settings.json
python3 -m picks.fetch --date 2026-10-28 --out /tmp/picks --books FanDuel,BetMGM   # live prices + stats
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
