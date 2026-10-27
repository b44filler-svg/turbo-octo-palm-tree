You do the overnight grading for Bob's "Parlay of the Day" app, so the morning picks run can start building right away. Be careful and quick. Do not build new bets.

The app is the artifact https://claude.ai/artifact/RFAr1q4DnSmsLNts8SojDG. Use the ArtifactData tool (load it with ToolSearch first) with that url. Every write to an existing document needs the `version` you read as `if_version`; prefer `batch` writes. Do NOT republish or edit the artifact page. Do all math in Python.

JOB 1 - Grade old picks and measure closing line value.
List `parlays`. For every document whose `result` is "open" and whose games have all finished:
- Look up final scores and box-score stats. Set each leg's `result` ("won", "lost" or "void") and `actual` (e.g. "14 reb").
- Look up each leg's closing price for the same line at the book the leg names (`book`, FanDuel if missing), or the closing consensus price if that book's close isn't findable; say which in `closeSource`. Set `closeOdds` and `clv` = implied_prob(closeOdds) - implied_prob(odds), rounded to 3 decimals (positive means the pick beat the closing line).
- Set the document's `result` ("won" if every non-void leg won, "lost" if any leg lost; "void" if every leg is void; a pair or parlay with some void legs is graded on the rest, so a pair left with one winning leg is "won") and `finalNote` (one short line).
- Write the whole `legs` array with `update`, keeping every other field (like `placed`, `stake`, `lateCheck`, `audit`).
Also grade every item in `board` documents (both `items` and `longshots`) whose games have finished: set each item's `result` and `actual`, and write those arrays back with `update`.
Also set `result` ("final score, e.g. PHI 27-20 DAL") on finished `games` documents.

JOB 2 - Update the learning record.
Read `model/stats` (create it if missing). From every graded leg in `parlays` and graded `board` item that has `modelProb`, compute with Python and `set` (keep the existing `auditFixes` value):
{"updatedAt":"<ISO>","legsGraded":N,"legsHit":N,"avgModelProb":x,"avgClv":x,"beatCloseRate":x,
 "buckets":[{"range":"50-60","legs":N,"expected":x,"actual":x},{"range":"60-70",...},{"range":"70-80",...},{"range":"80-92",...}],
 "byType":[{"type":"prop over","legs":N,"hit":N},{"type":"prop under",...},{"type":"spread",...},{"type":"moneyline",...},{"type":"total over",...},{"type":"total under",...}],
 "bySport":[{"sport":"NBA","legs":N,"hit":N,"tickets":N,"ticketsWon":N},...],
 "byKind":[{"kind":"big","tickets":N,"won":N,"expected":x},{"kind":"pair",...},{"kind":"parlay3",...},{"kind":"boost",...},{"kind":"lean","tickets":N,"won":N,"expected":x},{"kind":"longshot",...},{"kind":"board","tickets":N,"won":N,"expected":x}],
 "auditFixes":N,"shrink":<market weight to use next>,"lesson":"one sentence on what the results say so far"}
shrink starts at 0.5. Once a bucket has 30+ legs: if actual trails expected by more than 5 points, raise shrink by 0.05 (max 0.8); if actual beats expected by more than 5 points and avgClv > 0, lower it by 0.05 (min 0.35). Never change it by more than 0.05 per day.

UPDATES FEED. The app shows a timeline of what each run did. At the end, `set` one document in collection `updates` with id `<date>-<HHMM ET>-grading`: {"at":"<ISO now>","date":"<today ET>","kind":"grading","title":"short, plain words","detail":"one or two plain sentences Bob can act on"}. Title like "Graded last night: 3 of 5 bets won"; detail names the winners and losers and the model/stats lesson. If nothing needed grading, still write one short update.

End with one line per graded ticket and the lesson.
