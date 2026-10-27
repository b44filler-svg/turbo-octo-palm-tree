// Loads the app page with a mocked database and checks every tab at phone widths.
//   node tests/page_test.js [path/to/page.html] [path/to/build-output.json]
// Fails (exit 1) on console errors, sideways scrolling or missing sections.
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const root = path.join(__dirname, "..");
const pagePath = process.argv[2] || path.join(root, "app", "parlay-of-the-day.html");
const buildPath = process.argv[3] || path.join(__dirname, "fixtures", "build_output.json");
const today = new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York" }).format(new Date());
const built = JSON.parse(fs.readFileSync(buildPath, "utf8"));
const stamp = new Date().toISOString();

const parlays = {};
const add = (id, t, extra = {}) => { parlays[id] = { ...t, date: today, createdAt: stamp, title: t.title || id, ...extra }; };
if (built.big) add(`${today}-big`, built.big, { audit: { status: "PASS", note: "prices re-checked at FanDuel" } });
(built.pairs || []).forEach((p, i) => add(`${today}-pair-${i + 1}`, p, { audit: { status: "WARN", note: "one price from Covers" } }));
Object.entries(built.parlays || {}).forEach(([s, p]) => add(`${today}-${s.toLowerCase()}`, p));
(built.boosts || []).forEach((p, i) => add(`${today}-boost-${i + 1}`, p));
// A lean ticket (only cleared the lower bar) must be labeled as one.
if (built.pairs && built.pairs[0]) add(`${today}-pair-9`, { ...built.pairs[0], tier: "lean" });
// A graded, placed ticket from an earlier day for the Profit, Calendar and History tabs.
const old = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures", "ticket_2026-10-26-nfl.json"), "utf8"));
parlays["2026-10-20-nfl"] = { ...old, date: "2026-10-20", createdAt: "2026-10-20T15:00:00Z", result: "won", placed: true, stake: 20 };
parlays["2026-10-21-big"] = { ...built.big, date: "2026-10-21", createdAt: "2026-10-21T15:00:00Z", result: "lost", placed: true, stake: 50 };

const gamesSet = new Set();
Object.values(parlays).forEach(p => (p.legs || []).forEach(l => gamesSet.add(l.sport + "|" + l.game)));
const slug = s => String(s).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
const games = {};
[...gamesSet].forEach(k => {
  const [sport, game] = k.split("|");
  games[`${today}-${sport.toLowerCase()}-${slug(game)}`] = {
    date: today, sport, game, start: "7:30 PM ET", venue: "Home Arena", tv: "ESPN",
    lines: { spread: "HOME -4.5 (-110)", total: "O/U 221.5", moneyline: "AWAY +165 / HOME -195" },
    fairWin: { away: 0.36, home: 0.64 },
    injuries: [{ team: "Home", player: "Some Player With A Long Name", status: "Out", note: "ankle, ruled out Monday" },
               { team: "Away", player: "Other Player", status: "Questionable", note: "game-time decision" }],
    matchup: ["Home allows the 3rd-most points in the paint", "Away 28th in pace"],
    trends: ["Home 7-3 ATS last 10", "Under 6-1 in Away's road games"],
    weather: "Indoors", rest: "Home 2 days, Away back-to-back", lineMove: "HOME -3.5 → -4.5 since open",
    lean: "Home side and the under; Away on a back-to-back without its starting center.",
    sources: [{ title: "Covers matchup", url: "https://www.covers.com/" }],
  };
});
const longshots = built.board.slice(0, 2).map(l => ({ ...l, odds: 150 }));
const board = { [today]: { date: today, createdAt: stamp, items: built.board, longshots, note: "Ranked by quarter-Kelly growth." } };
const settings = { main: { defaultStake: 10, monthlyBudget: 200, neverAgainst: ["Philadelphia Eagles"] } };
const model = { stats: { legsGraded: 12, legsHit: 9, avgClv: 0.012, beatCloseRate: 0.58, shrink: 0.5, auditFixes: 3, lesson: "Unders are hitting." } };
const changelog = { [today]: { at: stamp, title: "Revamp", detail: "New bet types.", kind: "feature" } };
const updates = {
  [`${today}-0741-picks`]: { at: stamp, date: today, kind: "picks", title: "Today's picks are up", detail: "Big Bet, 2 pairs, NBA and MLB parlays." },
  [`${today}-1143-check`]: { at: stamp, date: today, kind: "check", title: "11:43 AM check: all clear", detail: "No injury news on any leg." },
};
const DATA = { parlays, games, board, settings, model, changelog, updates };

function mock(DATA) {
  const writes = [];
  const snapDocs = col => Object.entries(DATA[col] || {}).map(([id, d]) => ({ id, data: () => d, exists: true }));
  const coll = name => {
    const q = {
      orderBy: () => q, limit: () => q,
      onSnapshot: (cb) => { setTimeout(() => cb({ docs: snapDocs(name), empty: !snapDocs(name).length }), 10); return () => {}; },
      doc: id => ({
        update: async patch => { writes.push([name, id, patch]); Object.assign(DATA[name][id], patch); },
        set: async d => { writes.push([name, id, d]); DATA[name][id] = d; },
      }),
    };
    return q;
  };
  window.__writes = writes;
  window.claude = { use: async cap => cap === "db" ? {
    collection: coll,
    doc: p => { const [c, id] = p.split("/"); return { onSnapshot: cb => { setTimeout(() => cb({ exists: !!(DATA[c] || {})[id], data: () => DATA[c][id] }), 10); return () => {}; } }; },
  } : null };
}

(async () => {
  const html = "<!doctype html><html><head><meta charset=utf8><meta name=viewport content='width=device-width,initial-scale=1'></head><body>" +
    fs.readFileSync(pagePath, "utf8") + "</body></html>";
  const browser = await chromium.launch();
  let failures = 0;
  const fail = m => { failures++; console.log("FAIL", m); };
  for (const [width, scheme] of [[400, "light"], [360, "dark"], [600, "light"]]) {
    const ctx = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: scheme });
    const page = await ctx.newPage();
    const errors = [];
    page.on("console", m => { if (m.type() === "error" && !/fonts\.g/.test(m.text())) errors.push(m.text()); });
    page.on("pageerror", e => errors.push(e.message));
    await page.route("https://fonts.googleapis.com/**", r => r.fulfill({ body: "", contentType: "text/css" }));
    await page.addInitScript(mock, DATA);
    await page.route("https://app.test/", r => r.fulfill({ body: html, contentType: "text/html" }));
    await page.goto("https://app.test/");
    await page.waitForTimeout(300);
    const overflow = async tab => {
      const o = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      if (o > 0) fail(`${width}px ${tab}: page scrolls sideways by ${o}px`);
    };
    // Today
    const counts = await page.evaluate(() => ({
      big: document.querySelectorAll(".ticket.k-big").length, pair: document.querySelectorAll(".ticket.k-pair").length,
      parlay: document.querySelectorAll(".ticket.k-parlay3").length, board: document.querySelectorAll(".board li").length,
      sizing: document.querySelector(".sizing")?.textContent || "", audit: document.querySelectorAll(".audit").length,
      glance: document.querySelectorAll(".glance li").length,
      slate: document.querySelectorAll("#s-slate li").length,
      lean: document.querySelectorAll(".tag.lean").length,
      payout: !!document.getElementById("s-payout"),
      lastNight: [...document.querySelectorAll("#todayBox .label")].some(l => /won|waiting/.test(l.textContent)),
      tagBook: [...document.querySelectorAll(".ticket .tag")].some(t => /FanDuel|Courtside/.test(t.textContent)),
      feed: document.querySelectorAll(".feed li").length, gtime: document.querySelector(".glance .gtime")?.textContent || "",
    }));
    if (counts.big !== 1 || counts.pair < 1 || counts.parlay < 1 || counts.board < 1) fail(`${width}px Today counts ${JSON.stringify(counts)}`);
    if (counts.feed !== 2 || !/Updated .* ET/.test(counts.gtime)) fail(`${width}px updates feed ${counts.feed}, time line "${counts.gtime}"`);
    if (!counts.tagBook) fail(`${width}px tickets don't show their sportsbook`);
    if (counts.slate < 2 || counts.lean < 1 || !counts.payout || !counts.lastNight) fail(`${width}px slate/lean/payout/last night: ${JSON.stringify(counts)}`);
    if (counts.glance < 4) fail(`${width}px glance card has ${counts.glance} rows`);
    if (!/up to \$\d+|sit this one out/.test(counts.sizing)) fail(`${width}px Big Bet size guide missing: ${counts.sizing}`);
    await overflow("today");
    await page.screenshot({ path: path.join(process.env.SHOTS || "/tmp", `today-${width}.png`), fullPage: true });
    // Big Bet stake buttons are the bigger sizes, and placing saves the stake
    await page.locator(".ticket.k-big .stake", { hasText: "$50" }).click();
    await page.locator(".ticket.k-big .place").click();
    await page.waitForTimeout(100);
    const w = await page.evaluate(() => window.__writes);
    if (!w.some(([c, id, p]) => c === "parlays" && /-big$/.test(id) && p.placed && p.stake === 50)) fail(`${width}px placing Big Bet at $50 didn't save: ${JSON.stringify(w)}`);
    // Board row expands
    await page.locator(".board summary").first().click();
    // Game info link goes to the Games tab
    await page.locator(".gamelink").first().click();
    await page.waitForTimeout(100);
    if (await page.locator("#v-games").isHidden()) fail(`${width}px Game info didn't open Games tab`);
    const gc = await page.locator(".game").count();
    if (gc < 2) fail(`${width}px only ${gc} game cards`);
    await overflow("games");
    await page.screenshot({ path: path.join(process.env.SHOTS || "/tmp", `games-${width}.png`), fullPage: true });
    // Boosts tab shows the boosted ticket with its before/after odds and capped stakes
    await page.click("#tab-boosts");
    const bq = await page.evaluate(() => ({
      tickets: document.querySelectorAll("#boostsBox .ticket.k-boost").length,
      math: document.querySelector("#boostsBox .boost-math")?.textContent || "",
      stakes: [...document.querySelectorAll("#boostsBox .stake b")].map(b => b.textContent),
    }));
    if (bq.tickets < 1 || !/Without the boost/.test(bq.math)) fail(`${width}px Boosts tab: ${JSON.stringify(bq)}`);
    if (bq.stakes.some(s => Number(s.slice(1)) > 25)) fail(`${width}px boost stakes exceed the $25 cap: ${bq.stakes}`);
    await overflow("boosts");
    if (process.env.SHOTS) await page.screenshot({ path: path.join(process.env.SHOTS, `boosts-${width}.png`), fullPage: true });
    for (const tab of ["calendar", "profit", "history"]) {
      await page.click(`#tab-${tab}`);
      await page.waitForTimeout(50);
      await overflow(tab);
    }
    const kindRows = await page.locator("#byKind tr").count();
    if (kindRows !== 5) fail(`${width}px By bet type table has ${kindRows} rows`);
    await page.click("#tab-history");
    await page.locator("#filters button", { hasText: "Big Bet" }).click();
    const hrows = await page.locator("#hist li").count();
    if (hrows < 2) fail(`${width}px History Big Bet filter shows ${hrows} rows`);
    await page.locator("#hist li button").first().click();
    await overflow("history detail");
    if (errors.length) fail(`${width}px console errors: ${errors.join(" | ")}`);
    console.log(`${width}px ${scheme}: checked`);
    await ctx.close();
  }
  // Before today's picks post, Today must still show something: the slate and last night.
  {
    const EMPTY = JSON.parse(JSON.stringify(DATA));
    Object.keys(EMPTY.parlays).filter(id => id.startsWith(today)).forEach(id => delete EMPTY.parlays[id]);
    EMPTY.board = {};
    const ctx = await browser.newContext({ viewport: { width: 400, height: 900 } });
    const page = await ctx.newPage();
    const errors = [];
    page.on("pageerror", e => errors.push(e.message));
    await page.route("https://fonts.googleapis.com/**", r => r.fulfill({ body: "", contentType: "text/css" }));
    await page.addInitScript(mock, EMPTY);
    await page.route("https://app.test/", r => r.fulfill({ body: html, contentType: "text/html" }));
    await page.goto("https://app.test/");
    await page.waitForTimeout(300);
    const e = await page.evaluate(() => ({
      note: document.querySelector("#todayBox .note")?.textContent || "",
      slate: document.querySelectorAll("#s-slate li").length,
      panels: document.querySelectorAll("#todayBox .panel").length,
    }));
    if (!e.note || e.slate < 2 || e.panels < 2) fail(`empty morning view: ${JSON.stringify(e)}`);
    if (errors.length) fail(`empty morning view errors: ${errors.join(" | ")}`);
    if (process.env.SHOTS) await page.screenshot({ path: path.join(process.env.SHOTS, "empty-400.png"), fullPage: true });
    console.log("400px before picks: checked");
    await ctx.close();
  }
  await browser.close();
  console.log(failures ? `${failures} failures` : "page OK");
  process.exit(failures ? 1 : 0);
})();
