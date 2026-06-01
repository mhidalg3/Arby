# Project Ledger

## 2026-06-01 — Credentials / login infra (manual-login profile + OS keychain)

**Context:** Stand up secure handling of sportsbook logins for logged-in
recon (bet-slip `max_stake`) and eventual bet execution — without secrets
ever touching the repo, env files, or chat.

**Two mechanisms:**
- **Manual login → persistent browser profile** (`recon.py --login`):
  opens the site headed, waits for a by-hand login (handles 2FA/captcha),
  and the session saves into the gitignored `recon/profile/<platform>/`.
  Stores NO credentials; records no HAR/request log (so the login POST is
  never captured). Best for logged-in recon.
- **OS keychain** (`src/credentials.py`): per-platform username/password in
  the macOS Keychain (service `arby`), set via
  `python -m src.credentials set <platform>`. `Credential.password` is
  excluded from `repr` so it can't leak into logs. For automated re-login
  / execution. `.env.example` documents both; no secrets in `.env`.

**State:** All four platform logins configured by the user and verified
retrievable (storage only — no functional login test, which would risk
2FA/lockout). Dep added: `keyring`. Tests: `test_credentials.py`
(monkeypatched keychain). Not yet wired into any automated login flow —
live odds recon needs none of this (the feeds are anonymous).
## 2026-06-01 — `betsson_ws` fetch_live_soccer discovery wired + live-validated

**Context:** `fetch_live_soccer` previously needed an injected event-id
source. Wired real discovery and validated it live.

**Approach:** the Diffusion fixture-phase feed has no sportId, but the HTTP
categories tree definitively marks soccer (the `futbol/…` slugs). So:
discover soccer event ids via the existing `BetssonScraper` fixture
discovery → subscribe to all their market topics over one Diffusion
connection → **only in-running events publish on the transient channel**,
so the live filter falls out for free (no per-event liveness probe needed).

**Bug found + fixed via the live run:** 217 soccer candidates × per-selector
frames overflowed the single-byte conversation id (>255). Confirmed live
that one `obg/gossip/subscribe` frame accepts MANY selectors, so now
selectors are sent in **batched frames** (`_SUBSCRIBE_BATCH=50`, markets-
only for the multi-event path) — ~5 frames, conv id stays tiny.

**Live result:** discovered 217 soccer events, surfaced **~15 currently-live
matches** with correct 1X2 (Austria–Túnez 4.6/1.85/2.88, Georgia–Rumanía,
Colombia–Costa Rica, Austria–Jordania, …). +2 unit tests
(`_discover_live_events` override / empty). 484 tests, ruff + mypy clean.

**State:** Betsson live in-play is now fully autonomous (discovery +
subscribe + decode), matching the other platforms. Remaining nicety: team
names come from the categories slug (event-level names not pulled from the
feed) — good enough for cross-platform matching.

---

## 2026-06-01 — `betsson_ws.py` LIVE-VALIDATED — all 4 platforms now do in-play

**Context:** Live test of `BetssonWsScraper` against an in-play match
(Austria vs Túnez friendly, half-time; event `f-5QvL3jntkEyj6aK02fCJhA`).

**Result — worked first try, no iteration needed.** The passive-capture-
derived connect handshake + subscribe + keepalive held against the live
Diffusion server. `fetch_event_odds` connected, subscribed, and decoded
the live MW3W 1X2 in one 25 s stream window: Austria 3.9 / draw 2.1 /
Túnez 2.7. Cross-checked vs Betano (4.2/2.27/2.62) and BetWarrior
(3.7/2.23/2.85) — Betsson's decoded odds land right in the middle and the
home/draw/away mapping is correct. So the decode is verified against
ground truth, not just "well-formed".

**State:** **All four platforms now ingest live in-play 1X2** (Betano,
BetWarrior, Bplay, Betsson). PR #2's "needs live validation" caveat is
resolved. Open follow-ups remain: wire `fetch_live_soccer`'s
`live_event_ids` to the HTTP fixture discovery, and enrich team names.
Best-of-book on this match ≈ 1.03 (no arb), as expected.

---

## 2026-06-01 — Betsson live WS scraper built (`betsson_ws.py`) — Diffusion subscriber

**Context:** Turn the decoded Diffusion protocol into a live in-play
scraper, so Betsson matches the other platforms.

**Built:**
- Extended `betsson_diffusion.py`: `decode_value_frame` now handles BOTH
  `0x04` (raw CBOR) and `0x84` (zlib CBOR) — small live updates arrive
  uncompressed, big snapshots compressed; the `0x80` bit = compression.
  Added the subscribe-frame **encoder** (`encode_subscribe_frame` +
  `markets_selector`/`events_selector`/`FIXTURE_PHASE_SELECTOR`) — a
  golden test confirms it reproduces the captured subscribe frames
  **byte-for-byte**. Exposed `outcome_of_selection` + `selection_decimal_price`.
- `src/ingestion/scrapers/betsson_ws.py` — `BetssonWsScraper(BaseScraper)`,
  `platform_name="betsson-pba"` (same book as the HTTP scraper).
  `fetch_event_odds(event_id)` (surgical) + `fetch_live_soccer()` (needs a
  `live_event_ids` source). Per-cycle: connect → subscribe markets/events
  → stream a bounded window → decode → 1X2 snapshots (mirrors `bplay_sse`
  to sidestep long-lived keepalive). Pure `market_value_to_snapshots`
  tested against the real MW3W fixture.
- Deps added: `cbor2`, `websockets`. Full suite **488 passed**, ruff +
  mypy clean. New golden fixtures under `tests/fixtures/`.

**State:** Protocol encode + decode are byte-verified against the real
capture. The **network layer (connect handshake + keepalive) is NOT yet
validated against a live server** — derived from a passive capture, so the
first live run may need iteration (e.g. server-ping handling). That's the
remaining step: a live test against an in-play Betsson match, like we did
for the other platforms.

**Open follow-ups:** (1) live-validate `betsson_ws` on a match (connect/
keepalive/discovery); (2) wire `fetch_live_soccer`'s `live_event_ids` to
the HTTP scraper's fixture discovery; (3) team names — the Diffusion feed
gives `ei`/selection ids, not names, so enrich from categories if needed.

**Uncommitted backlog:** (a) credentials/`--login` infra (+keyring), (b)
the Betsson decoder + WS scraper (+cbor2, +websockets). Bplay UA fix
already landed in PR #1.

---

## 2026-05-30 — Betsson live (Diffusion) feed DECODED: zlib + CBOR, 1X2 odds extractable

**Context:** Closed the last in-play gap. Betsson pushes live odds over a
Diffusion WebSocket; the captured frames were opaque binary. Decoded them
from the 3,404-frame capture (no new Betsson traffic spent).

**Protocol cracked:** server value frames are `0x84` + short header +
**zlib**; decompressed payload is **CBOR**; topics publish full values
(`PUBLISH_VALUES_ONLY=true`), so no delta application. Market messages
(`t==27`) carry `d={ei, mti, odds}` with the SAME market codes as the
prematch accordion (`MW3W`=1X2, etc.); `d.odds[selId].of["1"]` is the
decimal price, and 1X2 selection ids end in `-home`/`-draw`/`-away` (no
external outcome map needed). Verified: Nice vs St-Étienne 1X2 =
2.55/2.25/3.90.

**Built:** `src/ingestion/scrapers/betsson_diffusion.py` — pure decoder
(`decode_value_frame` → CBOR map; `market_1x2_odds` → `(event_id,
{home/draw/away: price})`). 6 unit tests incl. a golden test against a
REAL captured frame (`tests/fixtures/betsson_diffusion_mw3w.b64`). Added
`cbor2` dep. Full suite **482 passed**, ruff + mypy clean.

**State:** The hard reverse-engineering ("un-decode") is DONE and tested.
What remains for a working live scraper is the Diffusion WS **client**:
the connect handshake (`?ty=WB&v=28…` + server session-token frame), the
3 `obg/gossip/subscribe` frames (captured), and keepalive/reconnect —
which needs live iteration against a match. That's the `betsson_ws.py`
build (would slot beside `bplay_sse.py` as the realtime-subscriber
pattern).

**Uncommitted backlog now:** (a) credentials/`--login` infra (+keyring),
(b) this Betsson decoder (+cbor2). The Bplay UA fix already landed in PR #1.

---

## 2026-05-30 — In-play stress test (PSG vs Arsenal, UCL final): Bplay UA bug fixed; phantom-arb evidence

**Context:** Used the live UCL final (2nd half) to stress-test in-play
ingestion across all 4 platforms and fix what broke. 35-cycle / ~50-min
observation; time-series `recon/artifacts/live_test/ucl-20260530-171328/`.

**Fix — Bplay was returning ZERO (same UA class of bug as Betsson):**
Bplay's WAF now serves stubs to the default `python-httpx` UA — XML feeds
empty, `/en-vivo` a 269-byte shell, both **HTTP 200** (even more silent
than Betsson's 403). A browser UA → real 500KB SSR page + valid XML.
Added `BROWSER_USER_AGENT` to `bplay.py` (`_XML_HEADERS`), imported into
`bplay_sse.py` (`_discovery_headers`, `_sse_headers`). Bplay then ingested
the match live via SSE. 3 of 4 platforms have now hit the missing-UA bug
(Betano had it from the start) → centralizing one UA in `base.py` is a
strong follow-up. Bplay unit tests still pass (30).

**In-play scorecard (this match):**
- Betano 33/35, BetWarrior 32/35, Bplay 29/35 cycles — all 0 errors;
  absences are SSE/feed gaps + match end.
- **Betsson 0/35** — live = Diffusion WS, still un-decoded (the one
  remaining in-play gap; separate large task).
- Three books tracked a dramatic match (Arsenal leading → PSG equalized
  → headed to a draw) within a few % of each other.

**KEY FINDING — naive in-play margin = phantom arbs.** 10 of 33 cycles
showed margin < 1.0 (min **0.7132**), but **every one is a stale-quote
artifact, zero executable.** At c10 the 0.71 came from Bplay's SSE lagging
~90s through PSG's equalizer (still pricing Arsenal at 1.56 while Betano/
BetWarrior had repriced to ~even); best-of combined the stale Bplay home
(7.5) with a fresh away price. It vanished the moment Bplay caught up. The
other 8 (0.97–0.999) are books drifting at different latencies as Arsenal's
win-price ran out toward a draw. **This is the clearest evidence yet that
single-snapshot cross-book in-play margins are dangerous — the `src/risk/`
Tier-2 re-fetch + staleness/simultaneity gate is mandatory before acting,
especially in-play where feeds lag through goals.** Also noted: Bplay's SSE
per-cycle stream window makes its "latest" the laggiest of the three —
a per-platform staleness weight worth carrying into the risk layer.

**State:** In-play ingestion working on Betano + BetWarrior + Bplay;
Betsson live still pending the Diffusion decode. Uncommitted backlog to
land: (a) credentials/`--login` infra (recon `--login`, `src/credentials.py`,
keyring dep), (b) this Bplay UA fix. Logged-in recon is the next planned
step (accounts now exist).

---

## 2026-05-29 — Recon harness can now capture WebSocket frames (enables Betsson live-odds recon)

**Context:** Betsson's `accordion/v1` is prematch-only; in-play odds
arrive over a **WebSocket pub/sub channel** (OBG `?obg/sportsbook/
transient/events|markets/...` topics — already noted in RECON_LOG
2026-05-25 but never captured, because the recon harness only logged
HTTP requests + a HAR, and the HAR does not record WS frames). So we
literally could not recon Betsson live. This adds that capability.

**Change (`scripts/recon/recon.py`):**
- New `websocket_frames.jsonl` artifact: every WS frame (both
  directions) with `ts / ws_url / dir / payload`, via a
  `page.on("websocket", ...)` handler attached before navigation.
- Pure serializer `_ws_frame_row` — text stored verbatim, binary
  base64-encoded + flagged, oversized frames truncated (cap
  `MAX_WS_FRAME_CHARS = 200_000`) so artifacts stay bounded/readable.
  Defensive to Playwright passing the payload directly vs wrapped.
- Always-on (empty file when a site uses no WS; negligible cost).
- 5 unit tests (`test_recon_ws_capture.py`). Full suite **470 passed**,
  ruff + mypy clean.

**Executed same session** on a live match (Ligue 1 Nice vs
Saint-Étienne, event `f-rdwm7m-uK0yqVgGGcW5KIg`): captured **3,404 WS
frames**. Betsson live odds = **Diffusion pub/sub WebSocket**
(`wss://pba.betsson.bet.ar/diffusion`); subscribe to
`?obg/sportsbook/transient/markets/<eventId>/` + `events/<eventId>/`.
Frames are Diffusion binary deltas keyed by topic ID (decoding them →
odds is the next phase). Full notes: RECON_LOG 2026-05-29 betsson;
artifacts `recon/artifacts/betsson/20260529-194942/`.

**Harness bug fixed this session:** a late `request`/WS event firing
during `context.close()` wrote to the already-closed JSONL stream and
crashed teardown (exit 1, after artifacts were saved). Both loggers now
guard `if stream.closed: return`. Artifacts from the crashing run were
intact; the fix makes teardown clean.

**State:** Capability built + tested (470 + WS tests pass) AND validated
live. Going into PR #1 (extends its recon.py changes). The Betsson
prematch scraper is unaffected.

**Next:** decode the Diffusion binary delta format → live odds (via a
Diffusion client lib or reverse-engineering the captured frames), then
add a Betsson live WS subscriber scraper (cf. `bplay_sse.py`), keeping
`accordion/v1` for not-yet-live fixtures.

---

## 2026-05-29 — 90-min live observation RESULTS (Sudáfrica vs Nicaragua) + Betsson UA bug fixed

**Context:** Ran a 171-cycle / 90-min (30s interval) multi-platform
odds observation on the live friendly, to watch API/odds behavior and
validate the Betsson fix. Throwaway observer (`/tmp/live_observe.py`);
time-series at `recon/artifacts/live_test/20260529-160520/` (gitignored).
Betano polled via `fetch_live_soccer` (live), BetWarrior via the
friendlies competition, Betsson surgically via `fetch_event_quotes`.

**Reliability over 90 min:**
- **Betano: 171/171 cycles, 0 errors.** httpx + browser headers held
  against Cloudflare for the full run at 30s cadence. The scraper is
  solid.
- **BetWarrior: 160/171** (11 graceful absences, 0 errors — match
  dropped from the friendlies feed during suspensions / near full-time).
- **Betsson: 0/171 live** — prematch-only widget returns `{"data":{}}`
  in-play (handled as absent, not error). Live widget = open follow-up.

**Odds behavior:** heavy, continuous movement tracking the match. Betano
home 1.17→1.98 (68 changes), draw 6.0→1.95 (91), away ranged 14.5–25.0
(94) — classic "favorite never pulls away, draw shortens" arc. Both
books tracked each other (margins mostly 1.02–1.07). **Structural
finding: Betano priced the away longshot (Nicaragua) ~2× higher than
BetWarrior the whole match** (Betano 19.5–24 vs BetWarrior 10.5–12) —
the most persistent cross-book disagreement.

**Arb windows: 2 sub-1.0 margins, BOTH non-actionable — and instructive:**
- **c87, margin 0.9099 (apparent 9.9% arb): PHANTOM.** BetWarrior had
  been absent for 2 cycles, then reappeared with a stale/transitional
  quote (draw 7.0, away 21.0) wildly off its own next-cycle values
  (3.5/10.5). The "arb" evaporated by c88. Classic stale-quote false
  positive on re-appearance after suspension.
- **c164, margin 0.9946 (0.5%): borderline,** driven by Betano's
  persistently-high Nicaragua price + BetWarrior's home price. Too thin
  to survive latency/suspension/stake limits.
- **Takeaway:** live in-play throws phantom arbs from stale/suspended
  quotes; genuine thin edges get eaten by latency. This empirically
  justifies the `src/risk/` Tier-2 re-fetch-before-place + staleness
  gating. Do NOT act on a single-snapshot in-play margin.

**State:** Betano scraper validated end-to-end (90 min sustained).
Betsson odds ingestion repaired (UA bug — see entry below). Full suite
**465 passed**, mypy clean. Uncommitted: Betano scraper + Betsson UA fix
+ tests + LEDGER. Ready to fold into the PR.

**Follow-ups surfaced:** (1) Betsson live-odds widget recon; (2) Betano
`/upcomingcoupon/?sid=FOOT` for full pre-match coverage; (3) consider a
shared browser-UA default across all scrapers (WAFs tighten); (4) the
Betano/BetWarrior away-price gap — investigate whether it ever yields a
robust (non-phantom) edge.

---

## 2026-05-29 — LIVE TEST: Betano scraper works against the real API; cross-platform ingestion validated on Sudáfrica vs Nicaragua

**Context:** First live test of the Betano scraper (and a cross-platform
ingestion check) on the international friendly Sudáfrica vs Nicaragua
(kickoff 13:00 PBA / 16:00 UTC). User-authorized real-API calls for this
test.

**Headline result — the big unknown is RESOLVED:** a plain `httpx`
client with browser-like headers **clears Cloudflare on BOTH Betano
endpoints**. No browser fetch layer needed.
- Pre-match `/api/home/top-events-v2/` → JSON, 45–48 FOOT 1X2 snapshots;
  captured the match at 1.19 / 6.4 / 14.0 (home/draw/away).
- Live `/danae-webapi/api/live/overview/latest` → JSON, 237 live FOOT
  1X2 snapshots; same match once it went live, same 1.19 / 6.4 / 14.0.
The match transitioned out of `top-events-v2` into the live feed at
kickoff — expected prematch→live behavior.

**Cross-platform capture of the match (1X2 home/draw/away):**
- **Betano** 1.19 / 6.4 / 14.0 (both modes) ✅
- **BetWarrior** 1.18 / 6.75 / 15.0 ✅ (competition `international_friendly_matches`)
- **Betsson** — 0 snapshots under `futbol/internacionales/` ⚠️ (see below)
- **Bplay** — not covered (XML scraper does only 5 marquee tournaments;
  friendlies flow through the WebSocket channel it doesn't consume)
Best-of-book implied sum = 1/1.19 + 1/6.75 + 1/15.0 = **1.055 (5.5%
margin) → no arbitrage**, as expected. Pipeline validated regardless:
same match pulled from ≥2 books and compared.

**Findings / follow-ups:**
1. **Betano `top-events-v2` is featured-only** (~25 curated events), NOT
   comprehensive pre-match coverage. It happened to feature this
   friendly pre-kickoff. For full pre-match coverage we need the
   `/upcomingcoupon/?sid=FOOT` per-coupon endpoint (recon captured only
   the coupon skeleton; its data call shape is not yet frozen). The
   **live** mode IS comprehensive (237 events). → Treat live mode as the
   primary Betano feed; `top-events-v2` pre-match as partial until the
   upcoming-coupon endpoint is reconned + added.
2. **Betsson returned 0 — diagnosed as TWO issues (one fixed).** Not a
   slug gap: discovery found the match fine
   (`futbol/internacionales/amistosos-internacionales/sudafrica-nicaragua`,
   event `f-XofNqv4POkmG7kmfywi-nQ`, 35 friendlies discovered).
   (a) **UA 403 bug — FIXED.** The odds endpoint
   (`/api/sb/v1/widgets/accordion/v1`) WAF returns a 403 HTML block page
   to the default `python-httpx` UA; `categories/v2` tolerated it, which
   masked it (discovery worked, every odds call 403'd → zero snapshots).
   Added a browser `User-Agent` to the Betsson scraper headers + a
   regression test. Confirmed live: 403 → 200 with full odds JSON.
   **Implication: Betsson odds ingestion was fully broken before this.**
   (b) **Live-widget gap — follow-up.** `accordion/v1` is prematch-only;
   once the match went in-play it returns `{"data": {}}`. Betsson live
   odds are a different widget the scraper doesn't consume (analogous to
   Betano's separate prematch/live endpoints). Needs a Betsson live
   recon. So Betsson captured the match pre-kickoff but not in-play.
3. **`deportespba.bplay.bet.ar` is NOT a new backend** — our Bplay
   scraper already uses it as `BASE_URL` for the XML odds feeds. The
   "standard" `pba.bplay.bet.ar` is the SPA shell (what recon browsed).
   The user's planned deeper recon on `deportespba.*` should focus on the
   WebSocket (`ws-deportespba.bplay.bet.ar`) for domestic/friendly
   coverage the XML pattern doesn't serve.

**State:** Betano scraper validated end-to-end against the live API in
both modes. The httpx-vs-Cloudflare risk noted in the build entry is
cleared. Code unchanged by the test (no fixes needed). Ready to PR the
Betano scraper + the recon `--channel`/block_detect commit. Observation
scripts were throwaway (`/tmp`), not committed.

**Errors:** None in the Betano path. Betsson friendly-coverage gap and
the top-events-v2 partial-coverage limit are logged as follow-ups, not
regressions.

---

## 2026-05-29 — Betano ingestion scraper built (live + pre-match, one parser, mode-parameterized)

**Context:** With the recon contract frozen, built the 4th-platform
scraper `src/ingestion/scrapers/betano.py` so Betano joins Betsson +
BetWarrior + Bplay. Goal: scrape both pre-match and in-play 1X2 odds.

**Decision — one module, parameterized by `mode`, NOT one combined pass
and NOT two files.** Both Betano feeds (live `danae-webapi/api/live/
overview/latest`; pre-match `/api/home/top-events-v2/`) return the SAME
normalized `{events, markets, selections}` danae shape, so the parser
(`_parse_danae_soccer_1x2`) is shared. But in-play odds move every few
seconds while pre-match drifts over minutes, and the framework's
`poll_forever` drives one `fetch_live_soccer()` at one interval — a
single combined pass would force one cadence and couple failures. So
`BetanoScraper(mode="live"|"prematch")` selects endpoint + cadence
(live 4s, pre-match 45s); run two instances. (Contrast bplay's two files,
justified there by two transports REST vs SSE — here it's one transport.)

**Key choices:**
- **Both modes emit `platform="betano"`** (not `betano-live`/`-prematch`):
  same book, so two labels would let the arb engine see a false
  self-arbitrage between the feeds.
- **Canonical 1X2 = MRES / typeId 1 only.** The `MR12` "SuperCuotas"
  promo (typeId 2850) is excluded — enhanced-odds promos have different
  stake caps/terms, unsafe for clean arb. Selections `1/X/2` → home/
  draw/away (1,2 resolved to participant names; X → "Empate").
- Parser skips non-FOOT, virtuals (`isVirtual`), esports (url contains
  "esports"), and outright events (≠2 participants).
- Reuses `RateLimitGuard` (per-mode breaker `betano-live`/`betano-prematch`)
  and raises `BetanoContractError` on schema drift / non-JSON
  (Cloudflare challenge) / HTTP ≥400 — same dumb-scraper discipline as
  Betsson. `max_stake=None` (not in public feed).

**State:** Code + 11 unit tests (synthetic danae fixtures via
`httpx.MockTransport`). Full suite **464 passed**, ruff + mypy(strict)
clean. NOT yet committed — holding for the live test. Scraper is not yet
wired into any runner/verifier (callers instantiate scrapers directly,
e.g. `risk/refreshers.py` for Betsson); wire-in is follow-up.

**Open risk (UNVALIDATED):** Betano is Cloudflare + Kaizen protected and
the API was only ever confirmed via a real browser. Whether a plain
`httpx` client clears Cloudflare is unknown — the live test today is
exactly what proves/disproves it. Browser-like headers are set to help;
if it 403s / returns the HTML splash, the guard opens and surfaces a
`BetanoContractError` (do NOT hammer). Fallback if httpx is blocked:
drive the feed through the Playwright recon harness (browser context)
instead of raw httpx.

**Next:** live test against a match today → if httpx works, open the PR
(this scraper + the committed recon `--channel`/block_detect fix); if
blocked, pivot the fetch layer to a browser context. Then wire Betano
into the ingestion runner + canonicalization (needs the
`/api/static-content/assets/{teams,leagues,regions}` catalogs captured
in recon).

**Errors:** None — clean build.

---

## 2026-05-29 — Betano deep recon SUCCESS: pre-match endpoint captured + block-detector false-positive fixed (the real blocker)

**Context:** After correcting the morning record (see entry below), ran
a clean deep recon (>24h later, per user) to capture the one missing
piece — the pre-match odds endpoint.

**Root cause found (this changes the morning's story again):** The
harness was never getting past the homepage on Betano because of a
**false positive in `block_detect`**, not a real block. The HTML
pattern `/cdn-cgi/challenge-platform/` matched the Cloudflare
orchestration script that CF injects into *every* page it fronts — so
`_check_block` aborted on the homepage step before any navigation. This
is why every Betano recon (including this morning's "successes", which
were actually exit-3 aborts with `nav_steps: []` — their danae data was
just the homepage's own XHRs landing in the HAR before the abort)
stopped at the homepage. On a clean/warm profile Betano was NOT blocking
us at all.

**Fix:** Removed the `/cdn-cgi/challenge-platform/` regex from
`scripts/recon/block_detect.py` `_HTML_PATTERNS`. A genuine CF
interstitial is still caught by title (`"just a moment"`,
`"attention required"`) and body (`"enable javascript and cookies to
continue"`, `"verify you are human"`, `"ray id"`) signatures, so no real
coverage lost. Updated the test that encoded the bug
(`test_recon_block_detect.py`): the bare orchestration script now
asserts NOT-blocked; a real interstitial (title+body) still asserts
blocked. 9 tests pass; ruff + mypy clean.

**Recon runs (all `--channel chrome`, same warm profile):**
- `20260529-145858` — false-positive abort on homepage (confirmed the bug:
  370 reqs, 0 splash, data API responded, yet exit 3).
- `20260529-150241` — fixed detector; homepage→cookie-consent→click Fútbol
  OK. (SPA: Fútbol renders client-side without a route change; generic
  match selectors don't match Betano's DOM.)
- `20260529-150524` — warm deep-link to `/sport/futbol/proximos-partidos-hoy/`
  → SUCCESS. Title "Fútbol - Partidos de Hoy", 901 reqs, 0 splash.

**Endpoint map captured (the deliverable):**
- **Pre-match odds:** `GET /api/home/top-events-v2/` → normalized
  `{events, leagues, markets, selections}`. Selection carries
  `price` (decimal odds); market `type:"MR12"` = 1X2 match result; event
  has `participants[{teamId,name}]`, `leagueId`, `sportId:"FOOT"`,
  `startTime`, `url:/cuotas-de-partido/{slug}/{eventId}/`. Saved
  `prematch_top_events_v2.json`.
- **Upcoming-coupon nav:** `/api/home/upcoming-coupons` (coupon skeleton;
  events lazy-load per coupon via `/upcomingcoupon/?sid=FOOT`). Saved
  `prematch_upcoming_coupons.json`.
- **Live odds:** `danae-webapi/api/live/overview/latest` + `/{eventId}`;
  layout `danae-webapi/api/layout/live`. Danae query params:
  `queryOperatorId=19`, `queryLanguageId=8`, `queryPlatformType=1`.
- **Reference catalogs (for canonicalization):**
  `/api/static-content/assets/{teams (9.7MB), leagues, regions, players}`.
  Saved `static_leagues.json`, `static_regions.json`.
- **Per-event full market depth — NOT captured:** lives at
  `/cuotas-de-partido/{slug}/{eventId}/` (`totalMarketsAvailable` up to
  833). Only needed if we scrape beyond listing-level 1X2.

**Code change:** `--channel` flag added to `recon.py` (drive installed
Chrome via `launch_persistent_context(channel=...)`). Both bundled
Chromium and Chrome load Betano fine once the profile is clean.

**State:** Betano recon is DONE for a pre-match scraper build — the 1X2
feed (`/api/home/top-events-v2/`) and the canonicalization catalogs are
captured. Betano becomes the 4th platform alongside Betsson + BetWarrior
+ Bplay. **Next:** build `src/ingestion/scrapers/betano.py` against
`/api/home/top-events-v2/` for pre-match 1X2; decide whether live
(`danae-webapi/api/live/overview`) is in scope. Optional follow-up
recon: the per-event `/cuotas-de-partido/.../{eventId}/` endpoint for
full market depth.

**Errors:** (1) `block_detect` false positive — fixed (above). It had
silently capped every prior Betano recon at the homepage. (2) Generic
`MATCH_LINK_SELECTORS` don't match Betano's DOM (match-open click
failed) — not needed here since the warm deep-link worked; revisit only
if per-event recon is wanted.

---

## 2026-05-29 — Betano recon retry: UNBLOCKED by wiping the profile dir (block was profile-state-bound, NOT an IP watchlist)

**Context:** At 2026-05-28 21:00 ART (2026-05-29 00:32 UTC) we retried
the Betano recon. Goal: re-establish access and capture the live/
pre-match data endpoints. Trial mode — obtain recon info, don't
stress-test the stealth.

**What happened (corrected — supersedes the interrupted writeup):**
Three headed runs, same IP throughout:
- `20260529-003159` (00:32) — bundled Chromium-for-Testing
  (UA `Chrome/148`), **reusing the persistent profile dir** → BLOCKED:
  Kaizen splash + CF challenge, `nav_steps: []`, 33 requests, 20 splash
  assets, **0 data calls**. Harness `block_detect` caught it and aborted
  (exit 3) — failed safe, no hammering.
- `20260529-004152` (00:41) — **same bundled Chromium build, same UA
  `Chrome/148`, but with the profile dir WIPED** → SUCCESS: 361
  requests, **0 splash**, live data API loaded
  (`/danae-webapi/api/layout/live`, `/danae-webapi/api/live/overview/latest`).
- `20260529-004503` (00:45) — **real installed Google Chrome via the new
  `--channel chrome` flag** (UA `Chrome/147`), wiped profile → SUCCESS:
  identical 361 requests / 0 splash / 2 data calls.

User visually confirmed both successful runs: normal site rendered, no
splash, no detection popup.

**Decisive finding:** Run `004152` used the *identical Chromium build
and UA* as the blocked run `003159` on the *same IP* — the only changed
variable was wiping the profile dir, and it cleared the block within
~10 minutes. Therefore the Betano/Kaizen block was bound to
**persistent browser-profile state (cookies/localStorage in
`user_data_dir`)**, NOT a persistent IP/profile watchlist and NOT the
Chromium-for-Testing fingerprint. The earlier "24h is insufficient /
homepage permanently blocked / can't un-profile" conclusion was WRONG —
it was drawn solely from the dirty-profile run `003159`, and the prior
session was interrupted (API error on a `thinking`-block edit) before
the two subsequent SUCCESS runs were recorded. Memory corrected to match.

**Code change (uncommitted):** Added `--channel` arg to
`scripts/recon/recon.py` (passes through to
`launch_persistent_context(channel=...)`) so the harness can drive
installed Chrome instead of bundled Chromium-for-Testing, closing the
CfT fingerprint gap. Both Chromium and Chrome channels now load Betano
fine once the profile is clean.

**State:** Betano browser-recon path is **ALIVE** again — both data
feeds confirmed reachable. Caveats: `nav_steps` was empty (homepage-only;
the 2 `danae-webapi` calls are the homepage live feed), and no PNG
screenshots were captured (the harness only shoots screenshots on nav
steps) — so verification rests on request-level HAR evidence plus the
user's direct visual confirmation, not a saved screenshot. The pre-match
endpoint still has NOT been captured (needs a homepage→pre-match click
nav). 3-platform stack (Betsson + BetWarrior + Bplay) unaffected.

**Next:**
1. Commit the `--channel` flag.
2. Re-run Betano recon with a WIPED profile and a real nav step
   (homepage → pre-match-today click) to capture the pre-match endpoint.
   Keep one-session-per-day discipline.
3. Make profile-wipe-between-sessions the default for re-recon (it is
   what cleared the block); see updated `recon-bot-protection-cautions`.

**Errors:** (1) Tooling behaved correctly throughout — the harness
blocked-and-aborted on the dirty run as designed. (2) The prior session
hit an API 400 ("`thinking` blocks in the latest assistant message
cannot be modified") mid-LEDGER-edit and was interrupted, leaving the
wrong "STILL BLOCKED" entry committed to the working tree; this entry
replaces it. (3) Forensic note: distinguishing the runs required reading
the HAR user-agents, since `summary.json` does not record the channel —
consider logging the channel/profile-clean flag into `summary.json`.

---

## 2026-05-28 — DESIGN DISCUSSION: cross-jurisdiction (PBA × CABA) arbitrage — feasibility + recommended two-operator architecture

**Status: discussion only. Nothing implemented. No decision made.**
Captured here so the analysis isn't lost and a future session has the
full reasoning.

### The proposed idea

Run two arb bots with distinct scraper stacks — one for PBA, one for
CABA — that communicate. Each first finds arbitrage *within* its own
jurisdiction's markets (what we do today, scoped per province). Then,
as a second stage, each inspects the other's listed odds for the same
event and checks whether a complementary outcome across the two
jurisdictions forms a Dutch book. Concern raised: make the stage-2
cross-matching efficient (low-complexity sort, or a sliding-window
match).

### Findings (feasibility, layer by layer)

**1. Cross-jurisdiction DETECTION — feasible and cheap.**
- We already normalize events across platforms (canonicalizer +
  fixture_resolver + partition_validator) and detect Dutch books
  across any platform pair (arb_detector). "Jurisdiction" is just a
  **tag on each platform**, not a new system.
- The efficiency worry is misplaced. Concurrent live events are
  tens-to-low-hundreds (Betano's live overview returned 107). Matching
  across books at that scale is O(events × platforms) — trivial. The
  right primitive is a **hash join on a canonical event key**, not a
  sort or sliding window.
- The ideal join key already surfaced in the Betano recon:
  **`betradarMatchId`**. Betradar is the dominant data supplier; if
  multiple books expose its match IDs, cross-book event matching is an
  exact hash join regardless of jurisdiction — no fuzzy team-name
  matching. This is the single most useful lever for the whole idea.
- Price dispersion is likely *real*: PBA and CABA books are distinct
  corporate entities (or separate provincial arms) pricing
  independently, so cross-jurisdiction comparison should surface more
  gaps than within one province.

**2. Two separate communicating bots — unnecessary complexity *for
detection*.** Two bots syncing state over IPC means duplicated
canonicalization, state-consistency bugs, and a distributed-systems
problem the data volume doesn't justify. For pure detection, the
cleaner design is ONE jurisdiction-aware pipeline: ingest all books
tagged by jurisdiction, run the detector with a jurisdiction filter
(`same` → intra, `any` → cross, flagged execution-gated). (NOTE: this
changes once real *execution* enters — see the recommended
architecture below, where two nodes are required for legal/geo reasons,
not detection reasons.)

**3. Cross-jurisdiction EXECUTION — the real blocker.** This is the
same identity-coherence problem that ruled out proxy rotation, but
about *betting* identity rather than scraping identity:
- **Per-province licensing.** PBA and CABA are separate regulators.
  Legally betting on a PBA book requires being a registered PBA user;
  CABA likewise. One person legally holding both, betting both
  simultaneously, is legally murky.
- **Geo-enforcement.** Provincial books geo-fence. You cannot be
  physically in PBA and CABA at once, so a single operator cannot
  satisfy both books' real-time geo-checks for the two legs. Spoofing
  geo = fraud against the book = seized funds.
- **Capital fragmentation.** Balances are siloed per jurisdiction.
  Pre-funding both works, but a run of arbs drains one and grows the
  other, and cross-province rebalancing (withdraw → deposit) is slow,
  fee-laden, and KYC-gated. This quietly caps throughput.

### Pushback / bottom line

- **Solo operator:** cross-jurisdiction is a *dashboard*, not a P&L
  line. You can detect the arbs but can't legally/operationally collect
  them. Energy is better spent **saturating intra-PBA** book coverage
  (Betano + Betsson + BetWarrior + Bplay), which is executable today,
  and separately building **intra-CABA** (Codere is the natural seed —
  it's CABA-only) as its own executable market.
- **Two genuine operators** (one PBA-resident, one CABA-resident, each
  legally betting only in their own province, coordinating): the idea
  becomes real, and the bots-communicating layer is exactly the right
  coordination mechanism. This is the only clean path to actually
  collecting cross-jurisdiction arbs.
- Either way: build cross-jurisdiction as **detection-only first**.
  It's nearly free (jurisdiction tag + Betradar join) and the data
  tells you whether the two-operator logistics are worth it *before*
  committing.

### Recommended architecture — two-operator cross-jurisdiction

For the legitimate two-person model. Design goals: each operator only
ever places bets in their own province (real geo, no spoofing); each
jurisdiction stays fully functional alone (graceful degradation); the
cross-juris layer is additive, not load-bearing.

**Topology — two jurisdiction-local nodes + a thin coordinator:**

```
  PBA node (Operator A, in PBA)         CABA node (Operator B, in CABA)
  ├─ scrapers (PBA books only)          ├─ scrapers (CABA books only)
  ├─ canonicalizer  (shared keying)     ├─ canonicalizer  (shared keying)
  ├─ intra-PBA arb detector ─┐          ├─ intra-CABA arb detector ─┐
  ├─ risk + verifier         │          ├─ risk + verifier          │
  └─ digest publisher ───────┼──────────┴─ digest publisher ────────┤
                             ▼                                       ▼
                    shared coordination bus  (odds:digest:pba / :caba)
                             │
                    cross-juris coordinator
                             │
            paired-bet instructions → both operators' execution agents
            fills + settlement → shared reconciliation ledger
```

**1. Jurisdiction-local ingestion + intra-juris detection.** Each node
runs the *existing* pipeline scoped to its province, scraping only its
own books from inside that province (satisfies geo legitimately). Each
node finds and executes its own intra-jurisdiction arbs independently —
this is the bread-and-butter and must not depend on the other node
being up.

**2. Shared canonical event keying (the linchpin).** Both nodes must
agree on event identity so cross-juris comparison is an exact join.
Primary key: **`betradarMatchId`** where books expose it. Fallback:
the canonicalizer's fixture-resolution `fixture_id`, which both nodes
must compute identically (same team-normalization tables, same
resolver version — so that logic has to be a shared, versioned
library, not per-node copies). Without a stable shared key, cross-juris
matching degrades to fuzzy matching across two remote feeds — avoid.

**3. Compact odds digest, not the firehose.** Each node publishes only
the *current best quote per (canonical_event, market, outcome)* for its
jurisdiction — book, decimal odds, max_stake, timestamp. NOT the
~100k-snapshots/min raw stream. The digest is small (hundreds of
events × a few markets), so shipping it across the wire and joining is
cheap. Reuse the `odds:latest` hash concept, scoped per jurisdiction
and published to the shared bus.

**4. Cross-juris coordinator.** Joins the two digests on the canonical
key and runs the SAME Dutch-book math as the intra-juris detector,
except leg A is a PBA book and leg B a CABA book. Emits
`arb:cross_juris` opportunities tagged with both jurisdictions + both
books + both stake sizes. Can live on either node or a third
lightweight host. If the bus partitions, the coordinator goes blind but
both nodes keep doing intra-juris work — acceptable degradation.

**5. Two-sided execution coordination (the hard real-time part).** A
cross-juris arb needs Operator A to place leg A (PBA) and Operator B to
place leg B (CABA) near-simultaneously. The coordinator emits a paired
instruction to both execution agents with a shared deadline + an abort
token. Critical differences from intra-juris execution:
- **Two placement latencies + inter-node network latency** widen the
  drift window. The verifier's `pre_refresh_delay_sec` budget must be
  re-measured to include the full two-sided round-trip, and BOTH legs
  must pass a fresh re-verification before EITHER is placed.
- **Legging risk is higher and split across two parties.** If A fills
  and B's market suspends, the naked exposure sits on one operator's
  account — so the settlement model must pre-agree who eats a legged
  loss (e.g. shared P&L pool, not per-operator).
- Strong case for a **commit barrier**: neither leg places until both
  agents acknowledge "ready + still valid"; either can abort up to the
  barrier.

**6. Settlement & reconciliation ledger.** Two operators, two
bankrolls, asymmetric drain → a shared, append-only bet/fill log
recording who placed what, realized P&L per arb, and running inter-
operator balance. Periodic out-of-band rebalancing between the two
people. This is an accounting subsystem, not just code, and it needs a
trust model agreed up front.

**Build order if pursued:**
1. Add a `jurisdiction` tag to every platform + a shared, versioned
   canonical-keying library (with Betradar ID as primary key).
2. Detection-only cross-juris coordinator over two digests — measure
   real PBA×CABA dispersion for weeks. **Decision gate:** is the
   dispersion large and frequent enough to justify the two-operator
   logistics?
3. Only if the data justifies it: build the paired-execution commit
   barrier, the two-sided verifier, and the settlement ledger — and
   confirm the legal footing of two coordinated provincial operators.

**Open legal question that gates everything:** is a coordinated
two-operator, two-province betting operation actually permissible under
Argentine provincial gambling law? That's a question for a person, not
this codebase, and it must be answered before any real-money
cross-juris execution is built.

---

## 2026-05-28 — Production-scraper backoff + circuit breaker (ingestion-side counterpart)

**Context:** The ingestion-side fix for the 2026-05-27 Bplay block.
The recon harness got Tier-1 anti-detection earlier today; this is the
same discipline for the production scrapers that poll continuously.
The root cause of the Bplay block was a scraper with zero rate-limit
awareness: it logged each 429 (`competition_skipped`) and immediately
tried the next request at full cadence, and the SSE loop re-hit
`/en-vivo` ~1×/s through repeated failures until the platform
escalated 429 → hard 403.

**Shipped:**

| File | Change |
|---|---|
| `src/ingestion/rate_limit.py` (NEW) | `RateLimitGuard` circuit breaker + `RateLimitPolicy` + `CircuitOpenError`. Transport-agnostic: callers pass a zero-arg coroutine factory. Injected clock (`now_fn`) for testable state transitions. |
| `src/ingestion/scrapers/bplay.py` | XML scraper routes every competition GET through the guard; `CircuitOpenError` → `BplayContractError` so the cycle skips cleanly. Accepts an injected `guard`. |
| `src/ingestion/scrapers/bplay_sse.py` | Discovery GETs routed through the guard (breaks the loop on open). **Plus**: `IDLE_REDISCOVERY_BACKOFF_SEC=20` — when no live matches, wait before the next `/en-vivo` poll instead of re-hitting every `poll_interval_sec` (~1s). Both knobs injectable. |
| `src/ingestion/scrapers/betsson.py`, `betwarrior.py` | Same guard on their `_get_json` chokepoints. BetWarrior's two scrapers (list + depth) share one guard via the daemon. |
| `scripts/run_ingestion_daemon.py` | Builds ONE shared guard per host: Bplay XML+SSE share a `bplay-pba` guard, BetWarrior list+depth share a `betwarrior-pba` guard. A 403 on either source of a host immediately stops traffic from both. |
| `tests/unit/test_rate_limit.py` (NEW) | 15 tests: 403 opens immediately, 429 opens on threshold, open circuit fails fast WITHOUT a network call, success resets, Retry-After raises/caps the cooldown, half-open probe closes on success / re-opens longer on failure, transport errors count toward the breaker. |

**Circuit-breaker semantics:**

- **CLOSED** — normal. Non-block 4xx (e.g. 404) passes through and
  does NOT trip (it's a content signal, not a rate-limit).
- **403 (or any `immediate_open_status`)** — opens on the FIRST
  occurrence. A 403 is a block, not a transient limit; we stop at
  once. Cooldown floored at `block_cooldown_sec` (120s default).
- **429 / 503** — opens after `failure_threshold` (3) consecutive
  hits, OR immediately if a `Retry-After` header is present (we honor
  the server's explicit instruction).
- **OPEN** — `CircuitOpenError` raised WITHOUT calling the network for
  the cooldown. This is the core protection: even if a caller swallows
  the error (as the old Bplay loop did), zero requests go out.
- Cooldown grows exponentially per consecutive trip
  (`base * 2^(trips-1)`, capped at `max_cooldown_sec`=600s), and is
  raised to at least any `Retry-After` value.
- **HALF_OPEN** — after cooldown, one probe. Success → CLOSED + reset.
  Failure → re-OPEN with a longer cooldown.

**Why a shared per-host guard:** Bplay serves both the XML feed and
the SSE feed from `deportespba.bplay.bet.ar`. A block hits the whole
host. One shared circuit means a 403 seen by the XML scraper instantly
fails-fast the SSE scraper too (and vice versa) — they can't
independently keep hammering a host that's already blocking us.

**Design choices:**
- The guard does NOT block-sleep on 429 (would hurt shutdown latency);
  instead it realizes back-pressure as circuit-open-cooldown +
  fail-fast. Honoring `Retry-After` happens by setting the cooldown,
  not by sleeping in the request path.
- Transport errors (timeouts, connection refused/reset) count as
  failures — a host that drops our connections trips the breaker too.

**Validation:** 452 unit tests pass (was 437; +15 rate-limit). ruff +
mypy clean across `src tests scripts`. No live traffic — the verifier
daemon's Bplay refresher also picks up a default guard automatically,
so Tier-2 surgical refetch is protected too.

**Bugs found & fixed during the work:**
- `_parse_retry_after` let `email.utils.parsedate_to_datetime` raise on
  garbage input (modern Python raises rather than returning None) —
  wrapped in try/except → returns None.
- B023 (closure over loop variable) in the SSE discovery lambda →
  switched to `functools.partial` (binds eagerly, satisfies ruff + mypy
  where the default-arg trick had broken mypy).
- The new SSE idle backoff made a unit test sleep a real 20s → made the
  backoff an injectable constructor param, test passes `0.0`.

**State:** All production scrapers now fail safe under rate-limiting /
blocks. This closes the ingestion-side gap that the 2026-05-27 incident
exposed. Bplay + Betano cool-downs unchanged — all of today's work was
offline/local, no traffic to either.

**Next:** Bplay/Betano cool-downs clear ~22:00–23:30 UTC today; the
hardened scrapers + recon harness get their first live test then (and
on the 2026-05-30 UCL final). The latent `models.py` enum
`values_callable` mismatch (flagged earlier) still awaits the execution
agent work.

---

## 2026-05-28 — Recon Tier 1 anti-detection: homepage-first + human pacing + stealth + block-abort

**Context:** After two bot-blocks on 2026-05-27 (Bplay cumulative-
volume, Betano second-session deep-link), we decided which anti-
detection techniques to invest in. **Explicitly rejected proxies /
IP rotation / fingerprint fabrication** — for a funded-account
betting operation, the scraping IP and the betting IP must stay
coherent, and rotating IPs to defeat a deliberate block edges into
anti-circumvention risk that can get accounts (and balances) seized.
The correct strategy is "be a legitimate, consistent, polite client",
not "hide harder". Evidence backs this: Betsson + BetWarrior poll
continuously and have never been blocked; Bplay broke from a buggy
retry loop, not from lacking proxies.

**Tier 1 shipped (all in `scripts/recon/`):**

| File | Change |
|---|---|
| `stealth.py` (NEW) | Context init script patching automation tells: `navigator.webdriver`, `permissions.query` notification consistency, `navigator.languages`, `window.chrome` presence. Patches *tells only* — does NOT fabricate WebGL/fonts/screen, because a mismatched fake fingerprint is a stronger signal than the honest one. |
| `human.py` (NEW, then overhauled same day) | First cut: `warm_up` + `human_click`. **Overhauled into a stateful `HumanCursor`** after deciding the simpler version wasn't realistic enough — anti-bot systems (Cloudflare/DataDome) score *behavior*, not just fingerprints. `HumanCursor` adds: curved jittered mouse paths with mid-path bulge + per-step tremor (continuous position across the session, no teleports); imperfect scroll (variable deltas, overshoot up-corrections, reading pauses); right-skewed think-time/hesitation; off-center clicks (random point inside the element box); and `type_text` with variable per-key timing + occasional fat-finger-then-backspace (for future logged-in flows). Per-platform overrides via `PER_PLATFORM_PACING`. |
| `block_detect.py` (NEW) | Pure `block_reason_from(title, html)` + `detect_block(page)`. Recognizes Kaizen splash, Cloudflare challenge ("Just a moment", `/cdn-cgi/challenge-platform/`), "access restricted", "unusual activity". |
| `recon.py` | Rewired flow: **always enter via homepage + warm up**, then navigate (clicks preferred; deep `--url` only from a warm session). Block-check at every step → abort with exit 3. Navigation/network failure → clean abort exit 4 (was: raw traceback). Stealth applied to context before first nav. |
| `scripts/__init__.py` (NEW) | Empty — resolves mypy module-name ambiguity now that `recon.py` imports sibling recon modules. |
| `scripts/recon/README.md` | Documented the three anti-detection layers + exit-code table. |
| `tests/unit/test_recon_block_detect.py` (NEW) | 8 tests pinning block-page signatures (Kaizen splash, CF challenge, "access restricted", "unusual activity") and confirming normal sportsbook pages + benign "security" text don't false-positive. |

**Exit codes:** `0` success · `2` bad args · `3` block detected ·
`4` navigation/network failure.

**Validation:**
- 437 unit tests pass (was 429; +8 block-detect).
- ruff clean across `scripts`; mypy clean on all 6 recon modules.
- Offline mechanics check (data:/`set_content`, no network):
  confirmed `navigator.webdriver === false` after stealth,
  `warm_up` runs, `human_click` matches+clicks a real link,
  `detect_block` returns None on a normal page and a reason on a
  Kaizen-style block page.
- NOT validated against a live sportsbook — Bplay and Betano are
  both in cool-down, and we won't spend Betsson/BetWarrior IP
  reputation just to smoke-test a dev tool. First real exercise
  will be tomorrow's Betano retry.

**Rationale captured in memory:** `recon-bot-protection-cautions`
and `platform-cooldown-status` (project memory) hold the hard rules
and the current block status so future sessions don't relearn them.

**Lint fix (`src/storage/models.py:99`):** changed
`class OpportunityStatus(str, enum.Enum)` →
`class OpportunityStatus(enum.StrEnum)` (ruff UP042). Safe:
SQLAlchemy's `Enum` keys off member *names* for any Enum subclass,
so persistence is unchanged; and the enum has no usages outside
`models.py` yet (execution agent unbuilt), so the `str()`
representation change is inert. `str(status)` now yields
`"detected"` instead of `"OpportunityStatus.DETECTED"` — more
consistent with the lowercase PG type if anything ever uses it.

**Latent issue discovered, NOT fixed (flagged for the user):**
`init.sql` defines the `opportunity_status` PG enum with lowercase
labels (`'detected'`, …) matching the Python `.value`s, but
`models.py` maps the column with bare
`Enum(OpportunityStatus, name="opportunity_status")` — and
SQLAlchemy's default is to persist member *names* (`'DETECTED'`),
NOT values. So `init.sql` and `models.py` are out of sync on the
enum representation (AGENTS.md requires they stay in sync). Dormant
because nothing inserts `Opportunity` rows yet. The fix when the
execution agent is built: add
`values_callable=lambda e: [m.value for m in e]` to the
`mapped_column`. Left for a deliberate change with the execution
work, not bundled into a lint cleanup.

**State:** Recon harness is materially harder to detect and now
fails safe (aborts instead of hammering). Bplay + Betano cool-down
unchanged — no live traffic to either today. The Tier 1 work was
all offline/local; it did not touch any blocked platform.

**Next:** tomorrow, after verifying cool-downs, the new harness gets
its first live test on the Betano retry (homepage-first should avoid
the session-2 block). Production-scraper backoff/circuit-breaker
(task #90) is the remaining Tier-1-discipline item on the ingestion
side.

---

## 2026-05-27 — Betano AR recon: positive findings + bot-protection block on session 2

**Context:** Following the in-play 2-platform recon, we explored
adding a fourth scrape target to compensate for Bplay being
blocked. Two candidates: Codere (revisit) and Betano AR. Codere
remains CABA-only (jurisdiction mismatch with our PBA stack);
Betano was the candidate worth probing.

Ran two Playwright sessions against `www.betano.bet.ar`. **The
first session succeeded and gave us the API surface. The second
session was bot-blocked** — operator confirmed via screenshot
that the "Access to this page is restricted" splash was an
anti-automation block, not URL routing.

---

### Positive findings (firm)

**Jurisdiction:** Betano AR is **PBA-licensed** (operator
confirmed via footer in their browser). Pairs cleanly with
Betsson/BetWarrior/Bplay without jurisdiction-mismatch
constraints. No need to expand to CABA.

**Domain structure:** single canonical `www.betano.bet.ar`. No
provincial subdomains — region selection happens via in-app
state, not URL. The provincial DNS records (`pba.`, `caba.`,
`mendoza.`, etc.) all wildcard to Cloudflare; only `www` and
the apex actually serve content.

**API:** clean REST under `/danae-webapi/api/...` (codename
"danae"). Key endpoints captured:

| Endpoint | Size | Top-level keys |
|---|---|---|
| `/danae-webapi/api/live/overview/latest?includeVirtuals=true&queryLanguageId=8&queryOperatorId=19` | 335 KB | `sports`, `zones`, `leagues`, `events`, `markets`, `selections` |
| `/danae-webapi/api/layout/live?queryLanguageId=8&queryOperatorId=19&queryPlatformType=1` | 110 KB | `liveOverview`, `liveEvent` |
| `/api/home/top-events-v2/` | 103 KB | `data.topEventsV2` |
| `/api/static-content/assets/leagues` | 203 KB | leagues catalog |
| `/api/static-content/assets/teams` | 9.7 MB | full teams catalog |

**Auth model:** none for read endpoints. Just the `_cfuvid`
Cloudflare cookie set on any homepage hit. No JS-derived
headers, no CSRF tokens, no challenge cookies on the data path.
We can replicate with `httpx` after a homepage warm-up — exactly
like the Betsson scraper does.

**Data shape:** flat normalized JSON, the cleanest schema of any
platform we've seen. One `live/overview/latest` call returns
**107 live events, 384 markets, 1,046 selections** with every
ID cross-referenced. Compare:

- Betsson: per-event polling, nested `data.accordions.<CODE>.markets.selections`.
- BetWarrior (Kambi): per-event polling, separate `markets`/`outcomes` arrays per event.
- Bplay: XML feed per competition, plus SSE for live.
- **Betano: one call, all events, normalized.** Materially
  fewer HTTP calls per cycle than any of our existing scrapers.

**Cross-platform binding bonus:** every event carries
`betradarMatchId`. **If Bplay or BetWarrior also expose Betradar
IDs**, this is a universal join key — the canonicalizer would
no longer need fuzzy team-name matching for fixtures Betano
also carries. Worth verifying tomorrow against the Bplay/BetWarrior
recon artifacts.

**Engineering effort estimate:** ~Betsson-class, **possibly
simpler** because one endpoint covers everything. Live polling
at 10-30 s cadence on a single endpoint, vs Betsson's per-event
fan-out. Pre-match endpoint shape unknown (see roadblocks).

**Sample data saved:** `recon/artifacts/betano/20260527-231712/*.json`
— `danae_live_overview.json` is the canonical reference for the
schema. Use it as the test fixture when we write the scraper.

---

### Roadblocks (critical for tomorrow's session)

**1. Bot-protection confirmed active.** Session 2, attempted ~15
minutes after session 1 with a deep URL
(`/sport/futbol/proximos-partidos-hoy/`), returned a
**Kaizen Gaming custom block page**:

- Page title: `Betano Splash Screen`
- Iframe src: `https://landingpages.kaizengaming.com/betano-splash-screen-bz/index.html`
- Visible text: *"Access to this page is restricted due to
  security and compliance measures."*
- Visible sponsorships: Brasileirão Betano, Copa Betano do
  Brasil, Torneo Betano (all Brazil-tier branding — misleading,
  because the block was anti-automation not territorial).
- Zero `www.betano.bet.ar` XHR calls in the HAR (page never
  bootstrapped past the splash iframe).

**This is the block signature.** Detect it in future sessions
by checking page title for "Splash Screen" or the iframe src
matching `landingpages.kaizengaming.com/betano-splash-screen-*`.

**2. What triggered the block (best guess):**
- The deep-URL navigation pattern (`page.goto()` straight to
  `/sport/futbol/proximos-partidos-hoy/`) bypassed the normal
  user flow of homepage → click soccer link → click "today's
  matches". A real user always passes through the homepage first.
- The second session happening 15 minutes after the first, from
  the same profile and IP, with no warm-up.
- Possibly Playwright fingerprint signals (`navigator.webdriver`,
  font metrics, WebGL strings) that the first session got away
  with but the second session's pattern made suspicious.

**3. What we lost.** The two open follow-ups from the original
recon are now blocked until tomorrow:
- Pre-match endpoint discovery (was the goal of session 2).
- `queryOperatorId=19` vs `=1` distinction — one additional GET,
  but the block makes "one more request" a poor risk/reward.

**4. We're now in a TWO-platform cool-down.**
- Bplay since 2026-05-27 21:47 UTC (~24h needed).
- Betano since 2026-05-27 23:28 UTC (~24h needed).
- This is a real constraint on tomorrow's plan.

---

### Tomorrow's session — preconditions and order of operations

**Before any Betano or Bplay traffic, verify cool-down via
single careful probes:**

1. **Bplay HEAD probe:** `curl -sS -I https://pba.bplay.bet.ar/`
   with a real-browser UA. Status 200 = unblocked, 403 = still
   blocked, retry-after-24h.
2. **Betano HEAD probe:** `curl -sS -I https://www.betano.bet.ar/`
   similarly. Should always be 200 (apex never blocked).
   Real test is: load in a real browser and check no splash.

**For Betano retry, change the recon approach to avoid
re-tripping the block:**

1. **Always start from the homepage** with the existing isolated
   profile. Don't deep-link via `page.goto()`.
2. **Navigate in-page** to soccer via locator clicks. We have
   the homepage DOM from session 1 — captured selectors:
   - Soccer landing: link with `href="/sport/futbol/"`
   - Today's pre-match: link with `href="/sport/futbol/proximos-partidos-hoy/"`
   - Use `page.locator("a[href='/sport/futbol/']").first.click()`
     instead of `page.goto(...)`.
3. **One Playwright session per Betano per day.** Same rule we
   should have enforced today.
4. **Add splash-screen detection to recon.py.** Right now the
   harness happily logs "Done" on a block-page session. Add a
   check after each `_settle()`: if page title contains "Splash
   Screen" or the iframe src matches the Kaizen block-page URL,
   surface a clear ALERT and exit non-zero. This was the gap
   that let session 2 silently produce a "successful" but
   useless artifact.

**Once Betano is back, the two original follow-ups in order:**

1. Pre-match endpoint discovery — after the in-page navigation
   succeeds, the captured HAR should reveal
   `/danae-webapi/api/pre-match/overview/latest` (or similar).
2. `queryOperatorId` comparison — fire one additional GET from
   inside the working browser session via `page.evaluate(fetch...)`
   to keep all traffic on the same warm cookies. Compare event
   counts and `zones` lists between the two responses.

---

### Cross-cutting lessons

**1. Second-session bot detection is a real failure mode.** Two
platforms in one day, two different second-session blocks:

- Bplay: cumulative-volume block. We hit it because of 30+
  minutes of high-rate ingestion polling, plus the SSE retry
  loop with no backoff.
- Betano: pattern-based block. We hit it because session 2 had
  an "automation-shaped" navigation (deep `page.goto()` without
  homepage warm-up), within minutes of session 1, same profile.

These are different mechanisms but both burn a platform for
~24h. **The recon playbook's "one session per platform per
day" rule isn't a suggestion — it's load-bearing.**

**2. Always check the screenshot before declaring a recon
successful.** Today's session 2 wrote a "Done" exit code and
30 lines of `requests.jsonl` from a session that captured
*nothing useful*. A 5-second look at the PNG would have caught
it. Future recon analyses should always verify the screenshot
doesn't show a block page before processing the HAR.

**3. Custom block pages are a stronger anti-automation signal
than a 403.** Cloudflare 403s are noisy and often false-positive
on HEAD probes. A site-branded block page like Kaizen's "Access
restricted due to security and compliance measures" is
**deliberate** and indicates we're already on their watch list.
Different mitigation: not "wait for rate limit", but "we've been
profiled and need to look different next time".

**4. We need a "second session" warm-up protocol.** When
returning to a platform we recon'd recently, the right pattern
seems to be:

- Load homepage with random-feeling delay (5-15 s)
- Move mouse / scroll (mimic human attention) — Playwright can
  do this via `page.mouse.move()` and `page.mouse.wheel()`.
- Only then navigate deeper.

We don't have this in `recon.py` yet. Worth adding before the
next multi-session round against any platform.

---

### State

- Both Bplay and Betano are in cool-down through ~2026-05-28 22:00
  UTC. **No traffic to either from this IP until then.**
- Betsson + BetWarrior remain untouched and healthy. Our
  in-play measurement harness still works on the 2-platform
  pair if we need data tomorrow.
- UCL final smoke (2026-05-30 16:00 UTC) is still viable; the
  cool-down ends well before kickoff.
- Recon artifacts preserved at
  `recon/artifacts/betano/20260527-231712/` (good session) and
  `recon/artifacts/betano/20260527-232846/` (blocked session,
  useful as the block-page reference).

### Next session decisions to make

1. Verify Bplay cool-down expired (single HEAD probe).
2. Verify Betano cool-down expired (homepage load in a real
   browser, no splash).
3. Decide whether to do the Betano follow-ups (pre-match endpoint
   + operatorId comparison) before or after the UCL final smoke.
4. Decide whether to land the Bplay backoff/circuit-breaker fix
   (task #90) now, or after seeing whether Betano can replace
   Bplay's role as the third platform.
5. Decide whether to add splash-screen detection to `recon.py`
   (recommended — closes a real gap).

---

## 2026-05-27 — First in-play data: 2-platform Libertadores recon (Bplay disabled)

**Context:** After the aborted full-platform recon (Bplay blocked,
see entry below), we re-launched with `DISABLE_BPLAY=1` to collect
in-play data on Betsson + BetWarrior only. Goal: answer the
outstanding question — **does in-play actually produce more arb
opportunities, and if so, at what drift cost?**

**Code added:**

| File | Change |
|---|---|
| `scripts/run_ingestion_daemon.py` | `DISABLE_BPLAY` env var. When set, omits both Bplay scrapers (XML + SSE) and their HTTP clients from the producer set. |
| `scripts/run_dry_run_verifier.py` | Same `DISABLE_BPLAY` env var. Omits `BplayXMLQuoteRefresher` from `MultiPlatformRefresher.per_platform`. The dispatcher's design (graceful degradation for unknown platforms) means no other changes needed. |
| `scripts/recon_watchdog.py` | `EXCLUDE_PLATFORMS` env (comma-separated). Skips freshness alerts for the listed platforms. Used to silence Bplay-pba alerts during the cool-down. |
| `scripts/recon_watchdog.py` | `_check_raw_growth` rewritten to read the LATEST stream entry's millisecond-encoded ID timestamp rather than `xlen` delta. Necessary because the sink trims `odds:raw` to `maxlen=100k` and at the ceiling `xlen` stays roughly constant despite active writes. Same signal (sink stopped writing), correct in any throughput regime. |

**Run (60 min, 21:53 → 22:53 UTC, kickoff 22:00 UTC):**

| Stage | Volume |
|---|---|
| Snapshots ingested | 100,000+ (stream at maxlen ceiling) |
| Unique outcomes in `odds:latest` | 6,207 |
| Opportunities detected | 277 |
| Risk approvals | 277 |
| Verifications emitted | 277 |
| Pre-match (< 22:00 UTC) | 30 |
| **In-play (≥ 22:00 UTC)** | **247** |

**Verdict distribution — both buckets:**

| | Pre-match | In-play |
|---|---|---|
| STILL_VALID | 30 (100%) | **247 (100%)** |
| DRIFT_BELOW_ACCEPTANCE | 0 | 0 |
| MARKET_UNAVAILABLE | 0 | **0** |
| STALE_DATA | 0 | 0 |

**The headline number — arb rate by phase (2-platform pair):**

| | Duration | Arbs | Rate |
|---|---|---|---|
| Pre-match | 22 min | 30 | **1.4 arbs/min** |
| In-play | 53 min | 247 | **4.7 arbs/min** |

**In-play arb rate is ~3.3× pre-match on the same set of
platforms.** This is the answer to "is in-play disproportionately
more productive": yes, by 3-4×, even with a single live match
carrying most of the volume.

**Per-platform |odds drift|, in-play only:**

| Platform | Legs | \|median\| | \|p95\| | \|max\| | signed mean | stdev |
|---|---|---|---|---|---|---|
| betsson-pba | 305 | 0.000% | 0.000% | **0.000%** | +0.000% | 0.000% |
| betwarrior-pba | 349 | 0.000% | 0.000% | **6.061%** | -0.009% | 0.588% |

**This is the first time we've seen meaningfully different
per-platform drift profiles.** Betsson holds its in-play lines
rock-steady; BetWarrior reprices noticeably on its 1X2 markets
during the match. The signed mean is essentially zero, meaning
BetWarrior's drift is symmetric — sometimes our way, sometimes
against us — but the tail is real.

**Where the drift concentrates (per-fixture × market):**

| Fixture | Market | Platform | n | \|max\| |
|---|---|---|---|---|
| SC Internacional vs Grêmio Feminino | 1X2 | betwarrior | 104 | **6.06%** |
| same | btts | betwarrior | 1 | 0.81% |
| same | ou_goals\|1 | betwarrior | 9 | **2.00%** |
| Gimnasia de Jujuy vs Belgrano | 1X2 | betwarrior | 168 | 0.000% |
| Atlético GO vs Goiás GO | 1X2 | betwarrior | 34 | 0.000% |

Drift was concentrated entirely on the SC Internacional vs Grêmio
fixture — the others (including pre-match Argentine domestic)
stayed flat. The interpretation: drift correlates with
**actual in-play repricing**, not just the in-play *phase* —
only the fixture where the game state was actively changing
showed line movement.

**Why 100% STILL_VALID held despite 6% drift:**

Where BetWarrior odds moved, they generally moved *up* (longer
odds → larger payout), which IMPROVES the arb margin from our
side. Of the 247 in-play verifications, the largest
`margin_delta_pct` was only +1.96% — meaning even the worst
drift case still left ≥80% of the detection margin intact, well
above the 60% retention threshold. **The verifier policy is
behaving correctly**: it caught no false positives in this run.

**Other notable findings:**

1. **Time since detection: median 3.0s, p95 12.5s, max 17.9s.**
   Tighter than the 30-min full-platform smoke (median 14.4s)
   because Bplay's queue volume isn't in the pipeline. The 3s
   `pre_refresh_delay_sec` is now the dominant component.
2. **Tier-2 reach: 99.6%.** One Betsson leg fell back to Tier-1
   (likely a fixture endpoint timing out). BetWarrior 100%.
3. **MARKET_UNAVAILABLE: 0 in 247.** No suspensions captured.
   IDV vs Rosario Central and the Brazilian fixtures evidently
   didn't have goal/card events disrupting our markets during
   the 53-min window — or suspensions were too brief to land
   between scrape cycles.
4. **The target match (IDV vs Rosario Central) produced no
   cross-platform arbs.** Either the platforms named the
   fixture differently (fixture-resolver mismatch), or one book
   simply didn't carry it. Worth investigating, but not a
   blocker for the recon conclusion.
5. **Watchdog `xlen` false-positive fixed mid-run.** The
   stream-trimming behavior at maxlen made `xlen` an invalid
   stall signal; switched to reading the head entry's
   millisecond-ID timestamp. Now correctly silent at maxlen.

**State:**

- All daemons stopped cleanly via their auto-stop budgets.
- Watchdog `Monitor` task stopped via `TaskStop`.
- Bplay was untouched for the full 60-min run — no new traffic
  to the blocked endpoint. Cool-down continues uninterrupted.
- We now have the first empirical answer on **in-play
  viability**: ~3× more arbs, with measurable per-platform drift
  but a verifier policy that absorbs it correctly.

**Implications for the strategic question
("is in-play worth pursuing?"):**

The two-platform data is *encouraging*:

- **Pro:** 3.3× arb rate is large enough to materially change
  expected returns even if some arbs fail to fill.
- **Pro:** Betsson stays flat in-play — half the leg risk is
  near-zero drift.
- **Pro:** Where drift exists (BetWarrior), the verifier
  catches the worst tail moves at acceptance time.
- **Pro:** Zero MARKET_UNAVAILABLE in this window — but this
  window had no observed goals, so suspension risk is
  **unmeasured**.
- **Con:** BetWarrior 6% drift on a single leg is large enough
  to flip a marginal arb into a loss if it moves the wrong way
  and the executor lacks abort capability.
- **Con:** Without Bplay we're capped at 2-platform arbs only;
  the 3-way arbs we expected to find are still unmeasured.
- **Caveat:** This is one match-window's worth of data. The
  big unknown — suspension behavior during actual goals/cards
  — is still untested.

**Next:**

1. **Don't change anything before the UCL final.** The 2-platform
   harness is validated in-play. Bplay will hopefully be unblocked
   by 2026-05-29.
2. **Fix Bplay scraper backoff + circuit breaker** (task #90).
   Plumb 429/403 awareness so we don't trip the lockout again.
3. **UCL final smoke (2026-05-30 16:00 UTC)** should be run with
   Bplay re-enabled IF the block has expired and the backoff fix
   is landed. Otherwise stick to 2-platform.
4. **Add a "suspension probe" check.** In a future recon, watch
   for in-play matches where we observe at least one goal during
   our window, so we can characterize `MARKET_UNAVAILABLE`
   behavior under real disruption.
5. **Investigate why IDV vs Rosario Central didn't produce
   cross-platform arbs.** Likely fixture-resolver or naming
   mismatch — worth a recon pass to confirm cross-platform
   fixture binding works for international fixtures.

**Errors and resolutions:**

- `_check_per_platform_freshness` was using stream-tail sampling
  → switched to `odds:latest` HSCAN; this had been done before
  this run but the run validates the fix.
- `_check_raw_growth` was using `xlen` delta → switched to
  head-entry millisecond-timestamp during the run, hot-patched
  the watchdog without taking down the data pipeline.
- `DISABLE_BPLAY` env var was wired into both producers
  (ingestion) and the verifier without disturbing the
  `MultiPlatformRefresher` contract — required adding an
  explicit type annotation on the partial-platform dict so
  mypy stayed strict.

---

## 2026-05-27 — Aborted Libertadores in-play recon — Bplay blocked

**Context:** Planned in-play measurement against Ind. del Valle vs
Rosario Central (Copa Libertadores), kickoff 22:00 UTC. Goal: get
the first in-play data point and validate the harness during a
real live match before the May 30 UCL final. Aborted at 21:47 UTC,
**13 minutes before kickoff** — never reached the in-play phase.

**What happened:**

Bplay actively blocked our requests from the very first call.
Every Bplay endpoint returned 429 or 403:

| Endpoint | First response | Pattern |
|---|---|---|
| `/en-vivo` (SSE discovery) | 429 → 403 | Hammered every ~1s; quickly escalated to 403 |
| `/oddsfeeds/odds-competition6674.xml` (UCL) | 429 → 403 | Same |
| `/oddsfeeds/odds-competition36146.xml` (Libertadores) | 429 → 403 | Same |
| Other competition XMLs | Persistent 429 | Same |

The most-recent Bplay snapshot in `odds:latest` grew from 159s old
to 265s old before we cut the daemons — confirming Bplay was
producing zero new data while we kept hitting them.

**Proximate cause:** the Bplay scrapers have **no rate-limit-aware
backoff**. The SSE discovery loop re-requests `/en-vivo` on its
configured cadence (~1s) regardless of HTTP status; the XML
scraper logs `scraper.competition_skipped` on 429 and moves on,
but only to retry the same endpoint a few seconds later. Once
Bplay's bot-protection layer tripped (probably during the 30-min
delay-3s smoke earlier today), continued hammering escalated
429s into 403s.

**Root cause:** the day's cumulative traffic. Three runs today
(10-min, 30-min, this aborted 5-min) plus prior smoke history put
us past Bplay's tolerance threshold. Bplay's bot-protection
appears to be IP+UA based with no obvious unblock signal — we
just stopped hitting it.

**Salvaged data (pre-kickoff only, partial Bplay coverage):**

| Stage | Volume |
|---|---|
| Snapshots ingested | 69,430 |
| Verifications emitted | 59 |
| Pre-match (all in this run) | 59 |
| In-play | **0** (aborted before kickoff) |

| Verdict | Count | % |
|---|---|---|
| STILL_VALID | 57 | 96.6% |
| **STALE_DATA** | **2** | **3.4%** |

The 2 STALE_DATA verdicts are direct evidence of the Bplay
degradation — these were arbs whose Bplay leg's hash entry was
older than `max_freshness_age_sec=60s` because Bplay scraper had
stopped writing.

Tier-2 reach reflects the block clearly:

| Platform | Tier-2 % | Tier-1 % |
|---|---|---|
| betsson-pba | 100% | 0% |
| betwarrior-pba | 100% | 0% |
| **bplay-pba** | **42.9%** | **57.1%** |

For Bplay legs the verifier's Tier-2 surgical refetch hit the
same 403/429 wall, and degraded to the `odds:latest` hash —
which itself was stale. This is the dispatcher behaving
exactly as designed (graceful degradation), but the data
underneath was bad. Drift remained 0.000% across all platforms
because the hash held the same odds the detector saw.

**Watchdog false positive discovered & fixed:**

The first version of `scripts/recon_watchdog.py` flagged each
of the three platforms as "NO entries in last 5000 odds:raw"
during the run. Investigation showed this was the wrong
signal: BetWarrior bursts 14k+ entries per cycle (per-event
endpoint returns dozens of outcomes), pushing slower platforms
out of any 5000-entry tail window even when healthy.

**Fix:** `_check_per_platform_freshness` now reads
`odds:latest` via HSCAN and computes max timestamp per
platform. The hash has the most-recent snapshot per
`(platform, outcome_id)` regardless of stream burst patterns
— it's the right source of truth. After the fix, the watchdog
correctly identified the real problem ("most-recent snapshot
174s old") instead of the burst artifact.

**Lessons:**

1. **The Bplay scraper is a fragile dependency.** Its lack of
   429/403 awareness made a temporary rate-limit into a hard
   block we cannot recover from in-session. Before any further
   Bplay traffic, the scraper needs:
   - Honor `Retry-After` headers on 429.
   - Circuit breaker on N consecutive 403s — stop calling for
     a fixed cool-down period.
   - Slower base cadence for SSE re-discovery (`/en-vivo`
     should be polled every 30-60s, not every ~1s).
2. **No in-play data was captured.** The recon's primary
   purpose — characterizing in-play behavior — is unmet.
3. **The harness itself worked correctly.** Detector, risk
   daemon, verifier, sink, and the rest produced a clean
   shutdown and the partial dataset is well-formed. The
   verifier's STALE_DATA verdict fired in exactly the right
   conditions.
4. **The watchdog signal model needs revising.** Stream-tail
   sampling is wrong for bursty multi-platform data; hash-
   based freshness is the right primitive.

**State:**

- All daemons stopped, all background processes killed.
- Bplay is presumed blocked (no unblock test attempted, to
  avoid extending the lockout).
- `scripts/recon_watchdog.py` patched to use HSCAN on
  `odds:latest`; lint+mypy clean; not unit-tested (it's a
  script).
- UCL final smoke (2026-05-30 16:00 UTC) needs the Bplay
  scraper backoff/circuit-breaker fixed first. **At minimum 24h
  of zero Bplay traffic before retrying** to let the block
  expire.
- We have NO in-play data yet. The strategic question
  (is in-play worth pursuing?) remains unanswered empirically.

**Next:**

1. Add 429/403 backoff + circuit breaker to Bplay scrapers
   (XML + SSE).
2. Hold Bplay traffic to zero until at least 2026-05-29 22:00
   UTC (24h cool-down) — even unit tests that touch the live
   Bplay API.
3. Decide whether the UCL final on 2026-05-30 is the right
   retry vehicle or whether we wait for another high-liquidity
   live fixture after the Bplay block fully expires.
4. **Do not increase total daily Bplay request budget** when
   the scraper is fixed. The current cadence is at the
   tolerance edge; ratcheting back to safer limits is the
   sustainable path.

**Errors and resolutions:**

- Watchdog stream-tail sampling false positives → switched to
  HSCAN on `odds:latest` (see code change above).
- Ingestion daemon survived first `pkill` (still running as
  PID 47694+47699) → escalated to `kill -9` which terminated
  cleanly.
- `TaskStop` correctly halted the persistent `Monitor` watch
  on the watchdog log.

---

## 2026-05-27 — Artificial-delay drift characterization (30 min, delay=3s)

**Context:** Until now the verifier was tested with effectively
zero detection→verification latency (median 0.06s in the prior
10-min smoke). Real placement will introduce 1-5s of latency
(browser nav, form fill, click, confirm). The question: does that
gap materially shift the odds we'd actually trade against, and
does drift differ per platform?

**Mechanism:**
- Added `pre_refresh_delay_sec` to `VerificationPolicy`
  (`src/risk/verifier.py`). When > 0, the verifier sleeps that
  many seconds before calling `refresh_batch` — so
  `time_since_detection_sec` reflects the actual gap a real
  executor would face.
- Plumbed via `VERIFIER_DELAY_SEC` env var in
  `scripts/run_dry_run_verifier.py`.
- Added 2 unit tests verifying the delay path
  (`tests/unit/test_verifier.py::TestPreRefreshDelay`).
- New analyzer `scripts/analyze_drift.py`: decodes
  `arb:verification_results.drift_per_leg_json`, aggregates per-
  platform median/p95/max/stdev of `|odds_delta_pct|`, breaks
  down by `(platform, market_id)`, reports Tier-2 reach per
  platform.

**Run (30 minutes, full pipeline, VERIFIER_DELAY_SEC=3):**

| Stage | Volume |
|---|---|
| Snapshots ingested | 100,020 |
| Unique outcomes in `odds:latest` | 7,263 |
| Opportunities detected | 246 |
| Risk approvals | 246 |
| Verifications emitted | 246 |

| Verdict | Count | % |
|---|---|---|
| **STILL_VALID** | **246** | **100.0%** |
| DRIFT_BELOW_ACCEPTANCE | 0 | 0% |
| MARKET_UNAVAILABLE | 0 | 0% |
| STALE_DATA / NO_OUTCOME_ID | 0 | 0% |

Time-since-detection: median **14.4s**, mean 21.5s, p95 63.9s,
max 90.5s. The 3s synthetic delay stacks on top of natural queue
lag (detector → arb:opportunities → verifier consume), so the
verifier in this run was looking at windows much longer than 3s
on the tail — a stress-test that's *more* realistic than the
nominal 3s placement budget.

**Per-platform |odds drift|:**

| Platform | Legs | \|median\| | \|p95\| | \|max\| | signed mean |
|---|---|---|---|---|---|
| betsson-pba | 316 | 0.000% | 0.000% | 0.000% | +0.000% |
| betwarrior-pba | 274 | 0.000% | 0.000% | 0.000% | +0.000% |
| bplay-pba | 30 | 0.000% | 0.000% | 0.000% | +0.000% |

**Tier reach per platform:**

| Platform | Tier-2 | Tier-1 | Tier-0 |
|---|---|---|---|
| betsson-pba | 100.0% | 0.0% | 0.0% |
| betwarrior-pba | 100.0% | 0.0% | 0.0% |
| bplay-pba | 63.3% | 36.7% | 0.0% |

Fully Tier-2 verifications: 236/246 (95.9%) — the 10 mixed-tier
cases were Bplay-leg arbs where the XML refresher didn't cover
that competition and the verifier fell back to the `odds:latest`
hash (Tier-1). Hash hit-rate was 100% for those — XREVRANGE
fallback never fired.

**Key findings:**

1. **Pre-match Argentine PBA odds do not drift on multi-minute
   timescales.** Across 620 leg observations spanning 30 minutes
   and 8 distinct fixtures × 7 market types, every single fresh
   quote matched the detection quote to the cent. This is the
   strongest possible signal that the detection→placement risk
   surface for *pre-match* arbs is essentially zero for these
   books.
2. **No per-platform drift differentiation in this regime.** All
   three platforms showed identical 0.000% drift. If a real
   per-platform difference exists, it's below the precision of
   the published odds (typically 2 decimal places) or requires
   a different market state to observe (in-play, near kickoff,
   high-volume betting periods).
3. **Bplay Tier-2 coverage gap quantified.** 36.7% of Bplay legs
   degrade to Tier-1 — primarily Libertadores / international
   fixtures where the XML competition scan misses the event.
   The `odds:latest` hash absorbs the gap losslessly; this is a
   *feature*, not a regression, but it's worth knowing if we
   later care about end-to-end Tier-2 percentage.
4. **The verifier is over-engineered for the current target
   market.** A simpler `last-cached-price ≥ X% of detection`
   gate would catch the same cases at 1/100th the latency. The
   architecture investment pays off only when we extend to
   in-play markets, where drift is non-trivial.

**Caveats:**
- All captured arbs were pre-match. No live in-play matches
  during this window. **In-play drift is unmeasured** and will
  almost certainly look different.
- The 30-min sample is still small (246 verifications). Tail
  events (BTTS line moves, OU threshold shifts on goal scored)
  are not represented.
- We have no data on what happens when a real *bet placement*
  flow (form fill + submit) sits between detection and the next
  market poll on the same outcome. The synthetic delay
  approximates wall-clock but not the platform-side reaction to
  our query patterns.

**State:** Pre-execution drift safeguards are validated for the
pre-match Argentine soccer slice. The QuoteVerifier + dispatcher
+ `odds:latest` hash + `pre_refresh_delay_sec` knob form a
complete pre-execution risk layer. Decision-time gate
(`min_fresh_margin_pct=1%`, `min_retention=60%`) has yet to
reject a single arb under any tested condition — those
thresholds are correct but not yet load-bearing.

**Next:**
- The stated original goal — "logged-in stake-limit recon" — is
  the next authorization gate. **Awaiting explicit user
  confirmation** before any code that creates an authenticated
  session with a real bookmaker is added.
- Live in-play drift characterization is the second open
  measurement front. Requires identifying an upcoming live PBA
  fixture and running the same harness during the match.
- Periodic `odds:latest` hash cleanup is still deferred — fine
  for sessions ≤ 24h but will need attention for long-running
  production deployments.

**Errors:** None during this run. Daemons started, ran to their
auto-stop budgets, and exited cleanly. Stream sizes consistent
with prior smoke runs.

---

## 2026-05-26 — Verifier hardened: refresher tests + `odds:latest` hash + 10-min drift measurement

**Context:** Three follow-ups to the Tier-2 verifier slice:
1. Unit tests for the new per-platform refresher code (closed
   coverage gap on Tier-2 dispatch logic).
2. The deferred `odds:latest` Redis hash (Option B) — sink writes
   the latest snapshot per `(platform, outcome_id)` so the
   Tier-1 fallback path is O(1) HMGET instead of O(scan_count).
3. A 10-minute drift measurement to characterize behavior at
   scale and confirm the architecture is sound.

**Code shipped:**

| File | Change |
|---|---|
| `src/ingestion/redis_sink.py` | + `LATEST_HASH_NAME` constant; + `snapshot_to_latest_hash_field_and_value` helper; sink uses pipelined `XADD + HSET` per snapshot — one Redis round-trip, not two. |
| `src/risk/verifier.py` | `StreamCacheRefresher` now does **HMGET first, XREVRANGE scan only on miss**. The hash hit-rate determined empirically to be ~100% — scan fallback is the warmup/edge-case path. |
| `tests/unit/test_refreshers.py` (NEW) | 15 unit tests covering `BetssonQuoteRefresher`, `BetWarriorQuoteRefresher`, `BplayXMLQuoteRefresher`, and `MultiPlatformRefresher` dispatch with mocked scrapers + Tier-1 fallback. |
| `tests/unit/test_redis_sink.py` | Mock refactored to use the pipelined `pipe.xadd/pipe.hset` shape; + 2 new tests for hash write + per-snapshot pipeline execute count. |
| `tests/unit/test_verifier.py` | `_make_refresher` now mocks HMGET (default) or forces XREVRANGE fallback; new `TestHashFallback::test_hash_miss_falls_back_to_xrevrange_scan` covers the warmup case. |

**Test footprint:** 427/427 tests pass (up from 409). mypy strict
+ ruff clean across 50 source files.

**Operational shape of the hash:**

- Field key: `<platform>:<platform_outcome_id>` (e.g.
  `betsson-pba:s-m-f-evt-MW3W-home`).
- Field value: JSON-encoded snapshot field map — the same payload
  XADD writes to `odds:raw`.
- No per-field TTL (Redis hash limitation). Hash grows with the
  number of unique outcomes ever observed. In the 10-min smoke
  this reached 6,299 entries (~2 MB) — well below any practical
  limit; periodic cleanup is a future concern when running for
  weeks.
- Staleness handled by checking `timestamp` field at verifier
  time, not by Redis-side expiry — same logic the verifier
  already had for `STALE_DATA`.

**Why pipelined XADD + HSET (not two separate calls):** the sink
processes ~300 snapshots/sec. Two non-pipelined calls per snapshot
would be ~600 Redis ops/sec; pipelined is one round-trip per
snapshot — Redis sees both commands together and returns once.
Same correctness, half the syscall overhead.

**Live 10-minute drift measurement (full pipeline running):**

| Stage | Volume |
|---|---|
| Snapshots ingested | 93,595 |
| Unique outcomes tracked in `odds:latest` | **6,299** |
| Opportunities detected | 51 |
| Risk approvals | 51 |
| Verifications emitted | 51 |

Verdict distribution:

| Verdict | Count | % |
|---|---|---|
| STILL_VALID | 45 | **88%** |
| DRIFT_BELOW_ACCEPTANCE | 6 | 11% |
| MARKET_UNAVAILABLE | 0 | 0% |
| NO_OUTCOME_ID / STALE_DATA | 0 | 0% |

Operational metrics:

| | |
|---|---|
| Fully Tier-2 verifications | **51/51 (100%)** |
| Margin delta median | 0.00% |
| Per-leg odds drift median | 0.00% |
| Time detection → verification (median) | 0.06s |

**Key findings:**

1. **100% Tier-2 reach in this slice.** Every verification got
   surgical refetch — no Tier-1 fallbacks fired. The captured
   arbs all involve Betsson + BetWarrior on Brazilian fixtures
   whose per-event APIs respond reliably. Bplay-XML legs would
   degrade to Tier-1; we got none of those in this 10-minute
   window.
2. **0% drift is still the headline.** Verification runs 60ms
   after detection (median), and pre-match Brazilian fixture
   odds don't move on 60ms timescales. The verifier IS correctly
   fetching fresh platform data — the data is genuinely unchanged
   at this temporal resolution.
3. **The 11% DRIFT_BELOW_ACCEPTANCE rejections are POLICY
   filtering, not drift.** All 6 rejections have detected margin
   between 0.59% and 0.74% — under the verifier's 1.0% absolute
   floor (the detector accepts ≥0.5%). This is a real sanity gate:
   tiny arbs that don't survive execution overhead get filtered
   before they reach any future execution layer.
4. **The hash is doing real work.** 6,299 distinct outcomes
   tracked means HMGET hits the hash 100% of the time at this
   sample size. O(1) per leg instead of O(scan_count) — verifier
   completes a 3-leg verification in <100ms total Redis round-trips.

**Honest characterization of what's NOT yet measured:**

The 0% drift is **specific to pre-match Brazilian fixtures at
sub-second verification gaps**. NOT measured:

- **In-play / live drift.** Bplay SSE legs (Argentine domestic
  in-play) where odds change every few seconds. The 10-min
  window didn't catch live fixtures at high-activity moments.
- **Realistic detection-to-execution gap.** A real bet placement
  flow takes 1-5 seconds (HTTP round-trips, per-leg sequential or
  parallel placement). Our verifier runs immediately on emission
  — the 60ms gap is artificially fast.

To get a meaningful drift number for production:
- **In-play test:** wait for a live-active Bplay SSE moment and
  re-run; expect non-zero drift on those legs.
- **Artificial-delay mode:** verifier sleeps N seconds before
  refresh, simulating placement latency; lets us characterize
  pre-match drift at the realistic timescale without waiting for
  execution.

**Errors during build:**

- mypy caught a variable shadowing in `StreamCacheRefresher.refresh_batch`
  (`idx` reused across the HMGET phase and the XREVRANGE
  fallback phase with incompatible types `int` vs `int | None`).
  Fixed by renaming the second-phase variable to `target_idx`.
- redis-py's `hmget` stub returns `Awaitable[list] | list`
  (shared types with the sync client); one `type: ignore[misc]`
  on the await keeps mypy quiet without polluting the public
  API.
- Existing sink tests broke when the sink switched to pipelined
  `XADD + HSET`. Mock refactored to model the pipeline shape
  (`pipe.xadd` is sync and returns the pipeline; `pipe.execute`
  is async). All 17 sink tests pass on the new mock.

**Pipeline state — 5 daemons, 5 streams, full audit:**

```
[scrapers]         → odds:raw          → [detector]      → arb:opportunities
[sink]                                                          ↓
[sink writes HSET → odds:latest]                          [risk_daemon]
                                                                ↓
                                                          arb:risk_decisions
                                                                ↓
                                                          [verifier_daemon]
                                                                ↓
                                                          arb:verification_results
```

The hash + Tier-2 pair completes the verifier slice. Architecture
is feature-complete for measurement; what's left is empirical
characterization in conditions we haven't yet exercised.

---

## 2026-05-26 — Tier-2 surgical refetch shipped — verifier now genuinely fresh

**Context:** The Tier-1 verifier from the previous session was
architecturally complete but **read from the same `odds:raw`
stream the detector used** — so margin_delta was 0% by
construction and the 26% MARKET_UNAVAILABLE rate was largely a
scan-window artifact. The user chose to ship Tier-2 next so the
verifier could deliver real protection against drift.

**Tier-2 architecture (per-platform surgical refetch):**

Each refresher implements a `QuoteRefresher` Protocol and calls
the platform's read API directly for a single leg's event,
bypassing both the scraper's polling cadence and the
stream-cache. Per-platform implementations:

| Platform | API | Specificity | Latency |
|---|---|---|---|
| Betsson | `accordion/v1?eventId=...` | **Per-event** ✓ | ~200-500ms |
| BetWarrior list+depth | `betoffer/event/<id>.json` | **Per-event** ✓ | ~300-500ms |
| Bplay XML | `oddsfeeds/odds-competition<X>.xml` | Scan 5 competitions in parallel | ~100ms × 5 = ~150 KB |
| Bplay SSE | (push-only) | Degrades to Tier-1 fallback | — |

The `MultiPlatformRefresher` dispatcher routes each leg to its
per-platform refresher, runs them concurrently via
`asyncio.gather`, and falls back to Tier-1
(`StreamCacheRefresher`) when:
- The leg lacks `platform_event_id` or `platform_outcome_id`
- The per-platform refresher raises an exception
- The per-platform refresher returns `decimal_odds=None` (e.g.,
  Bplay XML scan didn't find a Brazilian fixture that came from
  Betsson+BetWarrior)

**Prereq refactor — `platform_event_id` on `OddsQuote`:** Same
pattern as the earlier `platform_outcome_id` addition. Optional
field (default `None`), populated by the canonicalizer from the
source snapshot, propagated through detector → risk_daemon
serialization. Needed because surgical refetch addresses the
platform's per-event endpoint by ID.

**Code shipped:**

| File | Change |
|---|---|
| `src/arbitrage/quotes.py` | + `platform_event_id: str \| None = None` |
| `src/semantic/canonicalizer.py` | Populates the new field |
| `src/semantic/arb_detector.py` | Threads it through `legs_json` |
| `src/risk/risk_daemon.py` | Reads it back in `opportunity_from_stream_fields` |
| `src/ingestion/scrapers/betsson.py` | + `fetch_event_quotes(event_id)` public method |
| `src/ingestion/scrapers/betwarrior.py` | + `fetch_event_quotes(event_id)` on the depth scraper, returning ALL v1 markets (1X2 + BTTS + OU) from one HTTP call; + module-level helper `_snapshots_for_kambi_match` for 1X2 extraction |
| `src/ingestion/scrapers/bplay.py` | + `fetch_competition_quotes(competition_id)` public method |
| `src/risk/refreshers.py` (NEW) | `BetssonQuoteRefresher`, `BetWarriorQuoteRefresher`, `BplayXMLQuoteRefresher`, `MultiPlatformRefresher` dispatcher |
| `src/risk/verifier.py` | `BatchRefresher` Protocol; verifier accepts either Tier-1 only or Multi-Tier |
| `scripts/run_dry_run_verifier.py` | Wires the dispatcher with per-platform refreshers + httpx clients |

**Test footprint:** 409/409 tests pass (no regression). mypy strict
+ ruff clean across 50 source files.

**Live 180-second smoke results — Tier-2 vs Tier-1 comparison:**

| Metric | Tier-1 only | **Tier-2 + Tier-1 fallback** |
|---|---|---|
| Snapshots ingested | 53,988 | 42,302 |
| Opportunities detected | 31 | 25 |
| Verifications emitted | 30 | 25 |
| `STILL_VALID` | 70% (21) | **96% (24)** |
| `MARKET_UNAVAILABLE` | 26% (8) | **0%** |
| `DRIFT_BELOW_ACCEPTANCE` | 3% (1) | 4% (1) |
| Fully Tier-2 verifications | 0/30 | **20/25 (80%)** |
| Tier-1 fallback | n/a | 5/25 (20%) |
| Margin delta median | 0% | 0% |

**The 0% MARKET_UNAVAILABLE rate is the headline win.** Previously
~26% of verifications said "leg not found" — those were Tier-1
scan-window artifacts (the last 5,000 stream entries weren't
enough lookback at ~300 snap/s). Tier-2 surgical refetch hits the
platform's live API directly and finds the leg whenever the
market still exists. The 0% rate is the real rate of in-flight
market vanishing within the ~80ms verification window.

**The 80% Tier-2 success rate is honest capability characterization:**

- **Betsson and BetWarrior legs reliably get Tier-2.** Both have
  per-event endpoints; one HTTP call, sub-second.
- **Bplay legs get Tier-2 only when the event is in our 5 target
  XML competitions** (UCL / Libertadores / Sudamericana / Mundial /
  Conference). Brazilian fixtures that came from Betsson+BetWarrior
  aren't in Bplay's XML — those legs fall back to Tier-1 cleanly,
  no error. Bplay SSE legs (Argentine domestic in-play) also
  degrade by design, since SSE is push-only.

**The 0% drift_delta deserves honest framing:**

verification happens 80ms after detection (median). Pre-match
platforms don't push odds updates on 80ms timescales — they
refresh on minute timescales. So **0% drift at this measurement
window doesn't mean "odds never drift"; it means "odds don't
drift in 80ms."** The Tier-2 refetch IS reaching the platform's
live API and parsing the response correctly; it just so happens
that pre-match Brazilian fixtures have stable odds over the
~80ms detection→verification gap.

The architecture's actual value surfaces in two scenarios not
exercised by this smoke:

1. **In-play / live odds** where seconds matter. When we run
   against the Bplay SSE-sourced live in-play legs, drift between
   detection and verification will appear.
2. **The detection→placement gap in production execution.** If
   the execution agent takes 1-5 seconds to place bets, Tier-2
   at placement-time will catch the drift accumulated during that
   gap.

**One subtle correctness point flagged during build:**

`MultiPlatformRefresher.refresh_batch` runs Tier-2 refreshers in
parallel via `asyncio.gather(return_exceptions=True)`. Exceptions
are caught and surface as Tier-1 fallback for that leg. Type
hint corrected to `Sequence[OddsQuote]` (not `list[OddsQuote]`)
so the verifier's `BatchRefresher` Protocol is satisfied
covariantly.

**State of the pipeline (5 stages):**

```
[scrapers] → odds:raw → [arb_detector] → arb:opportunities → [risk_daemon] → arb:risk_decisions
                                                ↓
                                       [verifier_daemon]
                                                ↓
                                       arb:verification_results
                                       (Tier-2 + Tier-1 fallback)
```

**Next steps available:**

1. **Option B (latest-snapshot Redis hash)** — the originally
   deferred optimization. Sink writes `(platform, outcome_id) →
   latest_snapshot` to a Redis hash. Tier-1 lookups become O(1)
   HGET instead of O(scan_count). With Tier-2 now eliminating
   MARKET_UNAVAILABLE artifacts, the hash is less urgent but still
   valuable for the Tier-1 fallback path (5/25 = 20% of legs in
   our smoke).
2. **In-play / live arb instrumentation** — re-run with explicit
   focus on SSE-sourced Bplay legs to actually observe drift on a
   timescale where it occurs.
3. **Per-platform refresher tests** — currently the refreshers
   themselves don't have unit tests; the `MultiPlatformRefresher`
   dispatcher logic is the riskier piece without test coverage.
   ~30 min of mock-HTTP tests would close this gap.
4. **Logged-in stake-limit recon** — the original deferred work
   for getting real per-platform stake limits. Crosses into
   authenticated bookmaker access; needs explicit operator
   authorization per `AGENTS.md`.

---

## 2026-05-26 — Pre-execution verifier (Tier-1) + dry-run daemon — measurement results

**Context:** User raised concerns about detection→execution latency
and odds drift. We agreed to ship two things in sequence:
(1) a `QuoteVerifier` that re-checks opportunity quotes before any
bet is placed, and (2) a dry-run daemon that runs the verifier
against the live pipeline without placing bets, to characterize
drift rates empirically.

**Architectural choice and a real correction to user's framing:**

The user proposed a "continuous communication channel between
ingestion and execution." I pointed out the existing `odds:raw`
stream IS that channel — the detector reads it for detection,
the verifier reads it for verification. No new pubsub needed.

**Code shipped this session:**

| File | Purpose |
|---|---|
| `src/arbitrage/quotes.py` | Added `platform_outcome_id: str \| None = None` to `OddsQuote` — needed by the verifier to match a leg back to a stream snapshot. Backwards-compatible default. |
| `src/semantic/canonicalizer.py` | Populates `platform_outcome_id` on emitted `OddsQuote` from the raw snapshot. |
| `src/semantic/arb_detector.py` | Adds `platform_outcome_id` to the `legs_json` in stream emissions. |
| `src/risk/risk_daemon.py` | Reads `platform_outcome_id` back from the JSON during deserialization. |
| `src/risk/verifier.py` (NEW) | `QuoteVerifier`, `StreamCacheRefresher` (Tier-1), `VerificationPolicy`, verdict enum, all result types. |
| `src/risk/verifier_daemon.py` (NEW) | `VerifierDaemon` async loop; serialization helpers. |
| `scripts/run_dry_run_verifier.py` (NEW) | Standalone process; env-var policy overrides. |
| `tests/unit/test_verifier.py` (NEW) | 10 unit tests covering all 5 verdict paths + batching + policy override. |

**Test footprint:** 409/409 tests pass (up from 399). mypy strict +
ruff clean across 49 source files (one pre-existing
`OpportunityStatus` ruff warning in `models.py` still pending —
not in current scope).

**Two-tier verification design (Tier 2 not yet implemented):**

- **Tier 1: stream-cache refresh.** Read the most recent matching
  snapshot per leg from `odds:raw`. Latency ~5-50ms. Freshness
  bounded by polling cadence (5-30s depending on platform).
  Architecturally agnostic — works for any platform whose
  scraper writes to `odds:raw`.
- **Tier 2: surgical refetch** (planned, not shipped). Call the
  platform's read API for THIS leg's specific event/market.
  Genuinely fresh (~300-800ms). Per-platform implementation.

**Acceptance policy** (configurable via env vars on the daemon):

```
STILL_VALID iff (fresh_margin ≥ min_fresh_margin_pct = 1.0%)
            AND (fresh_margin ≥ min_retention_fraction × detected = 60%)
            AND (oldest fresh quote ≤ max_freshness_age_sec = 60s)
```

Verdicts: `STILL_VALID`, `DRIFT_BELOW_ACCEPTANCE`,
`MARKET_UNAVAILABLE` (leg's quote not found), `STALE_DATA`
(quotes too old), `NO_OUTCOME_ID` (leg lacks
`platform_outcome_id`, can't Tier-1 verify).

**Live 180-second smoke (full pipeline: 5 scrapers + detector +
risk daemon + verifier daemon):**

| Metric | Value |
|---|---|
| Snapshots ingested | 53,988 |
| Opportunities detected | 31 |
| Risk daemon decisions | 31 |
| Verifications emitted | 30 (1 missed because verifier started tailing `$` after detector) |

Verdict distribution:

| Verdict | Count | % |
|---|---|---|
| STILL_VALID | 21 | 70% |
| MARKET_UNAVAILABLE | 8 | 26% |
| DRIFT_BELOW_ACCEPTANCE | 1 | 3% |

Drift metrics (across the 21+1 cases with fresh margin computed):

| | Value |
|---|---|
| Margin delta (detection − fresh) median | **0.00%** |
| Margin delta stdev | **0.00%** |
| Per-leg odds drift median | **0.00%** |
| Time detection→verification median | 0.08s |
| Tier-2 verifications | **0** (not yet implemented) |

**The headline honest finding: Tier-1 alone is insufficient as a
drift safeguard.**

The verifier reads from the same `odds:raw` stream the detector
reads. When verification runs 80ms after detection, both consumers
see the SAME snapshot — margin delta is 0% by construction. Tier 1
catches only:

- `MARKET_UNAVAILABLE`: leg's outcome no longer appears in the
  recent stream window
- `DRIFT_BELOW_ACCEPTANCE`: detector's min margin (0.5%) was
  looser than verifier's min (1.0%) — catches sub-1% arbs that
  the detector accepted

It does NOT catch within-poll-cadence odds drift, which was the
original concern. **Tier-2 surgical refetch is needed for that.**

**The 26% MARKET_UNAVAILABLE rate is partially a measurement artifact.**

The Tier-1 refresher scans the last 5,000 `odds:raw` entries. At
~300 snapshots/sec across all scrapers that's only ~17 seconds of
lookback. Many legs' latest matching snapshot aged out of that
window between detection and verification. Two fixes:

1. **Quick:** raise `VERIFIER_SCAN_COUNT` to 50,000 (~3 min lookback).
2. **Architectural:** maintain a Redis hash `(platform,
   platform_outcome_id) → latest_snapshot` updated by the sink.
   O(1) HGET instead of scan-back-N. Eliminates the scan-window
   artifact and is dramatically faster.

**Strategic assessment of where we stand:**

The verifier code itself is correct and tested. It's the
*measurement* that's degraded because Tier 1 can't catch real
drift and Tier 1's lookup is artifact-prone. The 70/26/3 verdict
mix is not yet meaningful drift data. To get meaningful data we
need both:

- **Option B (latest-snapshot hash)**: eliminates the MARKET_UNAVAILABLE
  scan-window artifact. Clarifies real vanishing-market signal.
- **Option A (Tier-2 surgical refetch)**: gets actual within-poll
  drift data. Requires per-platform implementation work
  (Betsson + BetWarrior most feasible; Bplay SSE degrades to Tier-1
  by necessity).

Recommended next move: **Option C (B then A)**. Latest-snapshot
hash first because it's small and clarifies the measurement. Then
Tier-2 surgical refetch on the platforms that support it.

**Errors during build:**

- The verifier's initial leg-matching algorithm was too clever
  (tried to re-canonicalize stream snapshots without fixture
  context). Rebuilt around adding `platform_outcome_id` to
  `OddsQuote` — direct `(platform, outcome_id)` match is far
  simpler. The refactor was clean: one field added with default
  None, populated at the canonicalizer, propagated through the
  serialization roundtrip.
- A test asserted `fresh_margin == detection_margin` using
  `realized_roi * 0.95` for the helper's `margin_pct` — wrong,
  since the actual detection margin equals `(1 - overround) * 100`.
  Fixed by computing the correct value from the legs' odds.

---

## 2026-05-26 — Deterministic stake sizing from capital + confidence

**Context:** User flagged that the v1 risk policy's 1000-ARS per-leg
limit and 2000-ARS total-stake gate were too small for serious arb
trading. Requested: stake sizes determined deterministically from
**total capital** and **arbitrage confidence metrics**.

**Architectural shift — stake sizing moves from risk DAEMON to the
DETECTOR.** The previous setup had the detector emit opportunities
with a fixed 1000-ARS budget and the risk daemon gate on a hard
2000-ARS total-stake cap (both arbitrary placeholders). The new
flow:

```
[detector] —[StakeSizer.compute_budget(quotes)]→ ArbitrageOpportunity (sized)
                                                         │
                                                         ▼
                                                  arb:opportunities
                                                         │
                                                         ▼
                                              [risk_daemon] — gates on
                                              per-leg feasibility +
                                              confidence + margin
                                                         │
                                                         ▼
                                                arb:risk_decisions
```

The detector calls an injected `budget_fn` per opportunity. In
production wiring, that's `StakeSizer.compute_budget`. The risk
daemon's arbitrary total-stake rule is gone — total budget is
bounded at sizing time by `total_capital × max_fraction_per_arb`.

**The deterministic formula:**

```python
def compute_budget(quotes):
    confidence = product(reliability[q.platform] for q in quotes)
    if confidence < min_confidence:
        return 0.0
    budget = total_capital × max_fraction_per_arb × confidence
    return budget if budget >= min_total_stake else 0.0
```

Defaults:
- `total_capital_ars = 1,000,000` (~$1k USD; operator-tunable via
  `RISK_TOTAL_CAPITAL_ARS`)
- `max_fraction_per_arb = 0.05` (Kelly-style ceiling, 5% of
  bankroll per opportunity)
- `min_confidence = 0.5` (below this → skip)
- `min_total_stake_ars = 500` (below this → skip; not worth
  placement overhead)

Per-platform reliability defaults moved to `src/risk/reliability.py`
as shared constants. Both `RiskPolicy` and `StakeSizingPolicy`
import the same table so they can't drift.

**Why no margin scaling**: for arbs (guaranteed positive ROI), the
"size by edge" Kelly intuition for uncertain bets doesn't apply.
A 0.5% arb and a 20% arb get the same risked capital; the *profit*
scales with margin (`budget × margin/100`), the *risk* doesn't.

**Sized-stake math (live smoke verified):**

| Platforms in legs | Confidence | Budget (capital 1M, frac 5%) |
|---|---|---|
| Bplay × BetWarrior (both 1.0) | 1.0 | **50,000 ARS** (capped at max_fraction) |
| Bplay × Betsson (1.0 × 0.8) | 0.8 | **40,000 ARS** |
| 3-leg Bplay × Bplay × Betsson | 0.8 | **40,000 ARS** |
| Betsson × BetWarrior (0.8 × 1.0) | 0.8 | **40,000 ARS** |
| Single-platform 3-leg Bplay | 1.0 | 50,000 (but risk evaluator rejects on distinct_platforms) |

**Risk policy changes:**

| Field | Before | After |
|---|---|---|
| `max_total_stake_ars` | 2000 (arbitrary cap) | **REMOVED** (sized at detector) |
| `default_max_stake_per_leg_ars` | 1000 (too low) | **50,000** (realistic prior for ARG bookmakers) |
| `min_margin_pct`, `max_margin_pct`, etc. | unchanged | unchanged |

The `stake_total` rule is gone from the evaluator cascade. The
`stake_per_leg` rule remains and uses the new 50k default for
legs whose `max_stake` is None (current state until logged-in
bookmaker stake-limit recon).

**Files shipped:**

| File | Purpose |
|---|---|
| `src/risk/reliability.py` (NEW) | Shared per-platform reliability constants |
| `src/risk/stake_sizing.py` (NEW) | `StakeSizingPolicy` + `StakeSizer.compute_budget` |
| `src/risk/policy.py` | Imports from `reliability`; removed `max_total_stake_ars`; raised `default_max_stake_per_leg_ars` to 50k |
| `src/risk/evaluator.py` | Removed `stake_total` rule (cascade 6 → 5 rules) |
| `src/semantic/arb_detector.py` | Accepts injected `budget_fn`; calls it per opportunity; skips emission when budget is 0 |
| `scripts/run_arb_detector.py` | Constructs `StakeSizingPolicy` from env (`RISK_TOTAL_CAPITAL_ARS`, `RISK_MAX_FRACTION_PER_ARB`, `RISK_MIN_TOTAL_STAKE_ARS`) and wires `StakeSizer.compute_budget` into the detector |
| `scripts/run_risk_daemon.py` | Removed `max_total_stake_ars` plumbing |
| `tests/unit/test_stake_sizing.py` (NEW) | 13 unit tests covering the formula across capital, confidence, leg counts |
| `tests/unit/test_arb_detector.py` | Added 2 tests for `budget_fn=0 → skip` and `budget_fn=N → total_stake≈N` |
| `tests/unit/test_risk_evaluator.py` | Removed total-stake-cap test, updated per-leg-cap test to use 50k |

**Test footprint:** 399/399 tests pass (up from 385 before, after
removing the obsolete `test_total_stake_above_budget_rejects` and
adding 13 stake-sizer + 2 budget-fn tests). mypy strict + ruff
clean across 46 source files.

**Live 90-second smoke (full pipeline: 5 scrapers + detector with
sizer + risk daemon):**

- Snapshots ingested: 24,286
- Opportunities emitted by detector: 30
- Risk verdicts: **30 APPROVED / 0 REJECTED**
- Per-opportunity total_stake: **40,000 ARS uniformly** (every
  emission involved Betsson → 0.8 confidence factor)
- Realized ROI range: 0.69% to 19.18%
- All emissions properly sized

Expected profit per arb (at 1M capital, 5% per-arb cap):

| Margin band | Stake (Betsson involved) | Profit per arb |
|---|---|---|
| 0.5-2% | 40,000 ARS | 200-800 ARS |
| 5-10% | 40,000 ARS | 2,000-4,000 ARS |
| 10-20% | 40,000 ARS | 4,000-8,000 ARS |

vs the previous 1000-ARS budget: a **40× scale-up in absolute
profit at the same risk percentage**.

**Errors:** None during the build. One mypy error after the policy
removal was caught immediately (the daemon script still referenced
`max_total_stake_ars`); fixed by removing the env-var plumbing
and updating the docstring.

**Tunable knobs (env vars for the detector daemon):**

| Env var | Default | Purpose |
|---|---|---|
| `RISK_TOTAL_CAPITAL_ARS` | 1,000,000 | Operator bankroll |
| `RISK_MAX_FRACTION_PER_ARB` | 0.05 | Per-arb Kelly cap |
| `RISK_MIN_TOTAL_STAKE_ARS` | 500 | Min stake worth placing |
| `DETECTOR_MIN_MARGIN_PCT` | 1.0 | Margin floor for detection |

`RISK_DEFAULT_MAX_STAKE_PER_LEG_ARS` on the risk daemon: 50,000
(per-leg bookmaker prior — update once logged-in recon captures
real limits).

**What this still does NOT solve (deferred):**

1. **Real bookmaker stake limits.** All snapshots currently have
   `max_stake=None`. Logged-in recon would replace the 50k default
   with actual per-platform per-market limits. That recon crosses
   into authenticated bookmaker access — explicit operator
   authorization required (per `AGENTS.md`).
2. **Stake-confidence calibration from outcomes.** Reliability
   factors (1.0 / 1.0 / 0.8) are operator priors. As actual
   bet-placement outcomes accumulate (wins honored vs limited vs
   voided), the table should be retuned with real data.
3. **Bankroll allocation across concurrent arbs.** The 5% per-arb
   cap doesn't prevent committing 30%+ of capital across many
   concurrent opportunities if many fire at once. A future
   portfolio layer would gate on total-deployed-capital across
   the in-flight set.

---

## 2026-05-26 — Bplay live-odds scraper shipped (SSE, not WebSocket as originally scoped)

**Context:** User requested a Bplay "WebSocket" scraper to cover
Argentine domestic Reserves matches that the existing Bplay XML
scraper can't reach. The recon for this session corrected a key
assumption from a prior recon log entry.

**Correction to a prior recon finding:** the earlier recon log
attributed Bplay's domestic odds flow to `wss://ws-deportespba.bplay.bet.ar/`.
That was wrong. The `ws-` subdomain is for **bet-slip operations
only** (`/bettingslip/save`, `/bettingslip/accept`, `/bettingslip/delete`,
etc.) — outgoing bet placement, not incoming odds. The actual
live-odds transport is **Server-Sent Events** on a different
subdomain `events-deportespba.bplay.bet.ar/live`, discovered by
greping the Nuxt JS bundle for `EventSource` (HTML5 SSE).

**Protocol decoded (full details in `scripts/recon/RECON_LOG.md`):**

- Endpoint: `https://events-deportespba.bplay.bet.ar/live?mode=v2&partner=1147&id=<A>|<B>...&main=&lang=ag&odds_format=dec`
- Three event types: `match` (metadata + team names), `odds`
  (the gold), `status` (mirrors match.status).
- Subscription is by `matchId` (a Bplay-internal ID different
  from the `eventLiveId` that appears in URL slugs).
- Live-match IDs discovered by greping `matchId:"<id>"` from
  the `/en-vivo` SSR HTML — no separate API call required.
- No authentication, no cookies. Plain HTTPS streaming GET with
  `Accept: text/event-stream` + `Origin`/`Referer` headers
  matching the SPA.
- Coverage: IN-PLAY matches only. Pre-match scheduled fixtures
  are NOT pushed — complements the XML scraper which covers
  pre-match tournament fixtures.

**Scope decision (refined from the user's original "WebSocket
scraper" framing):** ship as **`BplayPbaSSEScraper`**, sibling to
the existing XML `BplayPbaScraper`. Same `platform_name =
"bplay-pba"` on both — the canonicalizer treats their snapshots
uniformly via the per-(platform, platform_event_id) cache.

**Code shipped:**

| File | Purpose |
|---|---|
| `src/ingestion/scrapers/bplay_sse.py` | `BplayPbaSSEScraper` + pure parser functions |
| `tests/unit/test_bplay_sse_scraper.py` | 15 unit tests with synthetic SSE payloads |
| `scripts/run_ingestion_daemon.py` | Wired in as a 5th scraper |
| `src/semantic/market_resolver.py` | Extended Bplay 1X2 names (`"quien ganara el partido?"`) + OU pattern (`(?:mas de / menos de\|total de goles)`) |
| `scripts/recon/RECON_LOG.md` | Full session writeup |

**Test footprint:** 385/385 tests pass (up from 370), mypy strict
+ ruff clean across 44 source files. 15 new SSE-scraper tests
including a real-world-anchored sanity bound on OU lines (see
"surprises" below).

**Implementation choices:**

- **Streaming inside the BaseScraper interface.** `fetch_live_soccer`
  returns an `AsyncIterator[RawOddsSnapshot]` — the SSE scraper
  opens the HTTP stream, yields snapshots as `odds` events arrive
  for up to `stream_duration_sec` (default 55 s), then closes and
  returns. `poll_forever` waits the brief inter-cycle gap then
  restarts with fresh `/en-vivo` discovery.
- **Stateful event correlation.** The `match` event carries team
  names (`act1`/`act2`) but the `odds` event doesn't. The scraper
  maintains an in-memory `match_id → "Home vs Away"` map across
  events within a single SSE cycle so `raw_event_name` is
  populated on snapshots. If an `odds` event arrives before its
  `match` (rare race), `raw_event_name` is empty and the canonicalizer
  drops; the next odds event with a populated name picks it up.
- **Market filter.** v1 emits ONLY 1X2 (`¿Quién ganará el
  partido?`), BTTS (`Ambos equipos marcan`), and OU goals (`Total
  de Goles`). The 33 other SSE markets observed during recon
  (combos, half-time variants, team totals, "next goal", etc.)
  are silently skipped. Adding any later requires only resolver
  crosswalk entries.
- **Push-line + sanity bound for OU.** Integer goal totals
  (push lines) rejected at scraper time, same as the BetWarrior
  depth scraper. Lines above 12.0 goals also rejected — see
  the real-world surprise below.

**Live smoke (60-second run, all 5 scrapers including SSE):**

| Platform / market | Snapshots |
|---|---|
| betwarrior-pba (1X2 + BTTS + OU via list+depth) | 9,681 |
| bplay-pba (XML — `'1-X-2'`, `'Más de / Menos de X.X'`, ...) | 6,655 |
| bplay-pba **(SSE — `'¿Quién ganará el partido?'`, `'Total de Goles X.X'`)** | **~995 of the 6,655** |
| betsson-pba (per-fixture accordion) | 2,419 |
| Detected opportunities | 14 |

The SSE scraper contributed ~995 of Bplay's snapshots in 60 s.
Distribution: 825 SSE 1X2 (`'¿Quién ganará el partido?'`) +
10 SSE BTTS + 160 SSE OU. This is real live-odds coverage on
top of Bplay's existing pre-match XML feed.

**One real-world surprise to flag:**

Live smoke captured a soccer match `Sorocaba U21 vs Ad Centro
Olímpico U21` where Bplay's SSE pushed `qt: "Total de Goles"`
with outcome labels `"Más de 54.5"` / `"Más de 55.5"`. 54.5+
goals in a soccer match is impossible — either Bplay mislabels a
stat-prop market (combined shots? fouls?) under that `qt`, or it
is a feed error. **The scraper now bounds OU goal lines at 12.0**
to drop this noise without losing real coverage. Logged the
sanity-bound test (`test_implausibly_high_line_rejected`) so
future regressions surface.

**Pipeline state after this session:**

```
[scrapers] ──XADD──► odds:raw ──XREAD──► [arb_detector] ──XADD──► arb:opportunities ──XREAD──► [risk_daemon] ──XADD──► arb:risk_decisions
   │                                                                                                                       │
   ├─ BetssonScraper (PBA, accordion HTTP polling)                                                                  filter verdict=APPROVED
   ├─ BplayPbaScraper (XML feed, tournament competitions pre-match)                                                          │
   ├─ BplayPbaSSEScraper (SSE, ALL live in-play matches across sports/competitions)  ← NEW                                  ▼
   ├─ BetWarriorPbaScraper (Kambi list-view, 1X2 only)                                                              (future execution agent)
   └─ BetWarriorPbaDepthScraper (Kambi per-event, BTTS + OU)
```

Five scrapers, three platforms, single daemon. Adding a 6th
scraper is one more line in `_build_scrapers`.

**Errors:** Two minor ones during the build:
- Ruff F401 for `import asyncio` after I refactored away from
  internal async sleeps. Auto-fixed.
- Ruff I001 import-sort on the test file. Auto-fixed.
- The Bplay 55.5-goals issue caught at smoke time — a feature,
  not a bug (system surfaced the anomaly through real-world data).

**Operational note for future Bplay recon sessions:** the
persistent Playwright profile is now reliably fingerprinted
across fresh profile dirs. Plain `curl` continues to work on
the SPA shell (706 KB SSR HTML returns 200) and on every data
endpoint. Future Bplay work should default to curl + grep the
returned HTML and JS bundles; Playwright should be reserved for
cases where active SPA interaction is genuinely required.

---

## 2026-05-26 — Risk-layer v1: deterministic evaluator + audit daemon

**Context:** With the 30-minute continuous run producing 14 distinct
real arbs (and 1 single-platform false positive), it was time to
build the layer that gates between detection and execution. Per the
architectural mandate ("All financial decisions go through
`src/risk/`"), this is where every detected opportunity gets
filtered before any future execution agent sees it.

**Scoping decisions:**

- **Deterministic + stateless v1.** Six rules in a cascade — no
  LLM, no persistence tracking. Matches the partition_validator
  pattern: cheap deterministic decisions, escalation infrastructure
  (LLM, history) deferred until the data justifies it.
- **Emit ALL decisions, not just approvals.** Both APPROVED and
  REJECTED decisions go to `arb:risk_decisions`. Operator gets
  full audit visibility; execution agent filters on
  `verdict == "APPROVED"` downstream.
- **Defaults tuned to the 30-min capture.** `min_margin_pct = 0.5`
  matches the detector's threshold; `max_margin_pct = 25%` lets the
  Fluminense 19.18% outlier through (it persisted 27 minutes —
  proven real, not transient); `high_margin_warning_pct = 10%`
  flags the >10% band for manual verification without rejecting it.
- **Per-platform reliability scoring.** Bplay (SportNCO) and
  BetWarrior (Kambi) = 1.0 (sharp pricing, expected to honor
  winning bets). Betsson (OBG, recreational-skewed) = 0.8 pending
  logged-in stake-limit recon — they're the systematic loose-side
  on Argentine domestic and might post-hoc limit big winning bets.
- **Stake feasibility checks.** Per-leg cap defaults to 1000 ARS
  (the public API doesn't expose `max_stake` — that's a bet-slip
  field). Total stake cap 2000 ARS. Conservative — operator can
  widen as confidence accumulates.

**Six rules in cascade order:**

1. `margin_min` — `realized_roi_pct ≥ policy.min_margin_pct` (0.5%)
2. `margin_max` — `realized_roi_pct ≤ policy.max_margin_pct` (25%)
3. `distinct_platforms` — at least 2 distinct platforms across legs
4. `stake_total` — `total_stake ≤ policy.max_total_stake_ars`
5. `stake_per_leg` — every leg stake within its cap
6. `confidence` — product of per-platform reliability ≥ 0.5

First rule failure short-circuits; the decision records exactly
which rules ran. Auditable.

**Files shipped:**

| File | Purpose |
|---|---|
| `src/risk/policy.py` | `RiskPolicy` dataclass — thresholds + per-platform table |
| `src/risk/decision.py` | `RiskDecision` + `Verdict` enum |
| `src/risk/evaluator.py` | `RiskEvaluator` — pure rule cascade |
| `src/risk/risk_daemon.py` | Redis loop, serialization helpers |
| `scripts/run_risk_daemon.py` | Standalone process |
| `tests/unit/test_risk_evaluator.py` | 15 unit tests anchored on captured arbs |
| `tests/unit/test_risk_daemon.py` | 7 daemon + serialization tests |

**Test footprint:** 370/370 tests pass (up from 348), mypy strict
+ ruff clean across 41 source files.

**Live replay (captured 339 emissions from the 30-min run, replayed
through the risk daemon via `RISK_START_ID=0`):**

| Outcome | Raw count | Distinct (fixture, market) |
|---|---|---|
| APPROVED | 338 (99.7%) | 13 |
| REJECTED | 1 (0.3%) | 1 |

**The single rejection is exactly the BetWarrior-solo emission**
identified in the analysis — single-platform "arb" rule fired
exactly once across 339 emissions, zero false positives on the
distinct-platforms rule.

**Approved margin distribution (raw emissions):**

```
0.5-1%       17  ##
1-2%        106  ###########  (the modest-tradeable sweet spot)
2-3%          5  #
3-5%          0
5-10%       111  ###########  (high-confidence band)
10-15%       45  ##########
15-25%       54  ##########   (the Fluminense 19% cluster)
>25%          0               (ceiling rule didn't fire)
```

**High-margin warnings flagged: 99/338 (29%)** — these are
`>10%` margin approvals. The executor (future) should treat the
warning as "verify before placing" rather than auto-execute.

**The `arb:risk_decisions` stream is now the audit ledger.** Every
decision has: `verdict`, `reason`, `rules_evaluated`, `confidence`,
`high_margin_warning`, `fixture_id`, `market_id`,
`realized_roi_pct`, `platforms`, `evaluated_at`. Operator can
`XRANGE arb:risk_decisions - +` to grep policy behavior.

**Pipeline state after this session:**

```
[scrapers] → odds:raw → [arb_detector] → arb:opportunities → [risk_daemon] → arb:risk_decisions
                                                                                       │
                                                                            (filtered by verdict=APPROVED
                                                                              by future execution agent)
```

Three standalone daemons, each owns one stream, each can be
restarted independently. Risk-policy iteration is non-disruptive:
edit `RiskPolicy` defaults or pass env vars (`RISK_MIN_MARGIN_PCT`,
`RISK_MAX_MARGIN_PCT`, etc.) and restart only the risk daemon —
ingestion and detection keep running.

**Errors:** Two trivial ones during build:
- Ruff F401 on `import json` and `Verdict` in the daemon tests
  after an earlier draft was simplified. Auto-fixed.
- No other issues.

**Next-step options:**

1. **Logged-in recon to capture bookmaker stake limits.** The
   current `max_stake=None` for all snapshots forces conservative
   1000-ARS-per-leg defaults; real limits would let the risk layer
   approve larger stakes for high-confidence arbs. Requires
   creating accounts on each platform — explicit operator
   decision since it crosses into "calling real bookmakers" per
   `AGENTS.md`.
2. **Bplay WebSocket scraper for Argentine domestic.** Still
   valuable, now with a clearer ROI bound: Argentine Reserves
   matches produced 1 of 14 captured arbs at modest margin, so the
   incremental gain is meaningful but bounded.
3. **Execution-agent scoping.** The risk daemon writes
   APPROVED decisions; the next layer would attempt to actually
   place bets. This crosses architectural boundaries that need
   explicit operator authorization.
4. **Pasion or 4th platform recon** — adding a 3rd independent
   pricing engine on Argentine domestic + Brazilian fixtures
   would increase 3-platform arb detection (currently zero).

---

## 2026-05-26 — Recon-driven coverage expansion: **first live arb detections**

**Context:** User's intuition that "all three platforms should
contain more or less the same fixtures" was correct. The prior
"Betsson vs BetWarrior systematically diverge on pricing" finding
turned out to be an artifact of **our scrapers polling only a
narrow subset of each platform's actual offering**. Pre-WebSocket
recon revealed massive uncovered slugs on both Betsson and
BetWarrior.

**Recon findings (live curl probes against the platforms' own catalogs):**

- **BetWarrior** `group.json` exposes far more soccer than we polled:

| Slug | Events | Status |
|---|---|---|
| `brazil` | 426 | NOT POLLED → ADDED |
| `argentina` | 150 | already in scope |
| `world_cup_2026` | 139 | NOT POLLED → ADDED |
| `international_friendly_matches` | 101 | NOT POLLED → ADDED |
| `copa_libertadores` | 28 | already in scope |
| `copa_sudamericana` | 27 | already in scope |
| `champions_league`, `conference_league` | varies | UCL in scope; Conference League ADDED |

  `international_friendly_matches` sample: `Brasil - Panamá`,
  `Colombia - Costa Rica`, `Países Bajos - Argelia`, `Nigeria - Zimbabue`
  — exactly the fixtures Bplay's XML serves under Copa Mundial.

- **Betsson** `categories/v2` exposes a full top-level soccer tree:

| Slug | Status |
|---|---|
| `futbol/argentina/` | already in scope (was the ONLY one) |
| `futbol/copa-libertadores/` | NOT POLLED → ADDED |
| `futbol/copa-sudamericana/` | NOT POLLED → ADDED |
| `futbol/champions-league/` | NOT POLLED → ADDED |
| `futbol/conference-league/` | NOT POLLED → ADDED |
| `futbol/mundial/` | NOT POLLED → ADDED |
| `futbol/internacionales/` | NOT POLLED → ADDED |
| `futbol/brasil/` | NOT POLLED → ADDED |

  Confirmed fixture overlap with Bplay: `futbol/internacionales/
  amistosos-internacionales/brasil-panama`, `futbol/internacionales/
  amistosos-internacionales/nigeria-zimbabue`, `futbol/mundial/
  copa-del-mundo/mexico-sudafrica` etc. — these are the SAME
  fixtures Bplay's Copa Mundial XML serves.

**Changes shipped:**

- `src/ingestion/scrapers/betsson.py`:
  - `SOCCER_SLUG_PREFIX` → `DEFAULT_SOCCER_SLUG_PREFIXES` (tuple of 8 prefixes).
  - `_discover_argentine_soccer_fixtures` → `_discover_soccer_fixtures`.
  - Constructor now accepts `soccer_slug_prefixes` override for
    targeted scoping (used by tests).
- `src/ingestion/scrapers/betwarrior.py`:
  - Extended `TARGET_COMPETITIONS` from 4 slugs to 8 (added
    `brazil`, `world_cup_2026`, `international_friendly_matches`,
    `conference_league`).

**Test footprint:** 348/348 tests pass, mypy strict + ruff clean.
Existing Betsson + BetWarrior tests updated to match the new
attribute name and competition set.

**Live smoke (30s ingestion warmup + 30s detector, with depth
scraper enabled):**

- Snapshots ingested in 60s: **17,575**
- Detector saw: **7,662**
- Opportunities emitted: **18** ← **first time non-zero**

**Sample detected arbs:**

| Fixture | Market | Margin | Best legs |
|---|---|---|---|
| Fluminense vs Bolivar (Libertadores) | 1X2 | **19.2%** | bplay HOME-2nd, bplay+betsson |
| Fluminense vs Bolivar | OU 1.5 | 14.1% | bplay + betsson |
| Flamengo vs Coritiba (Brasil) | BTTS | 7.1% | betsson + betwarrior |
| Avai vs Criciuma (Brasil) | BTTS | 1.3% | betsson + betwarrior |
| Portuguesa RJ vs America RJ (Brasil) | OU 1.5 | 0.4% | betsson + betwarrior |

**Verification of the Fluminense 19.2% arb (the suspicious one):**

- Leg timestamp spread: **1.15 s** (co-temporal, well inside the
  30 s staleness window — not a stale-data artifact).
- Bplay quotes Fluminense at **1.22** (implies 82% win prob).
- Betsson quotes Fluminense at **1.75** (implies 57% win prob).
- That's a real **25-percentage-point bookmaker disagreement** on
  the same Brazilian-vs-Bolivian Libertadores match. Plausible:
  Bplay tightens Brazilian-favorite lines (their liquidity is
  Brazilian); Betsson prices it generically. Same dynamic that
  produces real arbs against Pinnacle vs softer books in
  international markets — except both sides happen to be
  available here.

**Caveats:**
- 19.2% is unusually large. Real or not, executing it would
  require near-instantaneous bet placement before either book
  rebalances. The current detector emits; the execution/risk
  layer (not yet built) decides what's actually tradeable.
- Some emissions duplicate the same `(fixture, market)` — that's
  the improvement-override throttle firing on margin progressions.
- BetWarrior didn't appear in the Libertadores arbs because its
  Libertadores fixtures don't fully overlap with Betsson's
  Libertadores set in this 30 s window. Brazilian fixtures DID
  bring BetWarrior into the cross-platform pairs (4 of 5 BTTS/OU
  emissions involve BetWarrior + Betsson).

**Strategic shift — WebSocket scraping priority lowered:**

The user's prior priority was Bplay WebSocket for Argentine
domestic Reserves coverage. That's still valuable but no longer
the highest-leverage move:

- Argentine Reserves matches are 2-platform-only (Betsson +
  BetWarrior) and have demonstrated tight pricing alignment
  (both books bias the same direction on lower-tier matches).
- Libertadores + Brazilian + international fixtures now have
  genuine 2-3 platform disagreement and are producing arbs RIGHT NOW.
- Coverage expansion in existing scrapers got us further than
  another transport-level addition would have.

**Next-step options for user decision:**

1. **Continuous run + verification.** Run the system for 1-4
   hours to characterize sustained arb rates, distinguish
   transient false-positives from real opportunities, and
   capture the BetWarrior Libertadores overlap when match
   schedules align.
2. **Risk-layer / execution scoping.** A detected 19% arb is
   only valuable if placeable. Now is the moment to design the
   risk layer (stake sizing, bookmaker reliability, execution
   timing).
3. **Continue with WebSocket scraping for Argentine Reserves.**
   Still adds value (the only remaining coverage gap) but no
   longer the gate to first-arb-detection.

**Errors:** None during implementation. The `categories/v2`
endpoint on Betsson has been served reliably across the recon
session — the same `brandid`/`marketcode`/`x-sb-type`/`x-sb-jurisdiction`
header set from the original recon still works.

---

## 2026-05-26 — BetWarrior depth scraper + Bplay coverage investigation

**Context:** Two parallel objectives this session:
1. **Investigation:** user observed that Bplay PBA's SPA shows
   Argentine domestic competitions (Primera Nacional, Copa Argentina,
   Reservas) and noted fixtures should overlap across all three
   platforms. Verify whether the current Bplay scraper is missing
   data accessible via the same XML endpoint pattern.
2. **Implementation:** ship BetWarrior depth scraper for BTTS + OU
   goals via per-event `betoffer/event/<id>.json` polling.

**Bplay coverage investigation (definitive):**

Probed the `/oddsfeeds/odds-competition<ID>.xml` pattern against
EVERY competition ID exposed in the SPA's main nav:

| Competition | XML pattern |
|-------------|-------------|
| 6674 UCL, 36146 Libertadores, 36148 Sudamericana, 63057 Copa Mundial, **42958 Conference League** | **200** |
| 81 Brasileirão, 5 MLS, 43411 Primera Nacional, 1493 Copa Argentina, 43414 Argentina-Reservas | 404 |

Also probed `/oddsfeeds/odds-categoria<ID>`, `/oddsfeeds/odds-region<ID>`,
`/oddsfeeds/event-<ID>`, `/oddsfeeds/odds-event<ID>`, `/api/...` and
~10 other patterns against domestic IDs and a known live Argentine
event ID. **All 404 except the canonical pattern.**

**Conclusion: Bplay's XML feed pattern serves tournament-style
competitions only. Argentine domestic + Brasileirão + MLS flow
exclusively through a different transport — the WebSocket
subdomain `ws-deportespba.bplay.bet.ar` (confirmed alive but
serves no HTTP endpoints).** Consuming Bplay's domestic coverage
requires a separate WebSocket scraper extension — not in this
session's scope.

**Quick win from the investigation:** added `42958 Conference
League` to `BplayPbaScraper.TARGET_COMPETITIONS`. We were missing
the XML feed for it; it's a 5 KB response and adds another
international competition where Bplay+BetWarrior coverage may
align on matchdays.

**BetWarrior depth scraper shipped:**

`BetWarriorPbaDepthScraper` — sibling class to `BetWarriorPbaScraper`
in `src/ingestion/scrapers/betwarrior.py`. Same `platform_name`,
different polling cadence:

- **List-view scraper:** `poll_interval = 5s`, hits
  `listView/football/<slug>/all/all/matches.json` per competition,
  emits 1X2 only.
- **Depth scraper:** `poll_interval = 30s`, discovers events via
  the same list-view, then hits `betoffer/event/<id>.json` for
  each event. Emits BTTS + OU goals only.

Per-event response is ~434 KB; ~100 active events per cycle =
~43 MB per depth cycle = sustained ~1.4 MB/s. Acceptable for
single-machine deployment.

**Kambi schema for the new markets (confirmed live):**

- **BTTS:** `criterion.label = "Ambos Equipos Marcarán"`,
  `betOfferType.englishName = "Yes/No"`; outcomes `"Sí"`/`"No"`,
  odds integer-scaled by 1000.
- **OU goals:** `criterion.label = "Total de goles"`,
  `betOfferType.englishName = "Over/Under"`; outcomes `"Más de"`/
  `"Menos de"`; **line lives in `outcome.line`, integer-scaled by
  1000** (500 → decimal 0.5, 2500 → 2.5, etc.). One betOffer per
  line. The depth scraper formats `raw_market_name` as
  `"Total de goles 2.5"` (Betsson-style) so the existing
  market-resolver regex pipeline works with one new platform entry.
- **Push-line guard:** Integer goal totals (`line % 1000 == 0`)
  are skipped because total goals = N refunds both sides; the
  {OVER, UNDER} partition isn't valid on those lines.

**Half-time BTTS guard:** Kambi exposes `"Ambos Equipos Marcarán
- 1.ª parte"` and `"Ambos Equipos Marcarán - 2.ª parte"` as
separate-but-similar betOffers. Resolver uses EXACT match (not
prefix) for BetWarrior BTTS so half-time variants don't leak in.

**market_resolver enhancement: NFKD-fold in the key normalizer.**
Required because Kambi's BTTS label has the accented `"Marcarán"`.
After folding, the crosswalk key is the accent-free
`"ambos equipos marcaran"`. The new normalizer is also a small
improvement for the other platforms — robustness against future
accent variants in market names.

**Test footprint:** 7 new depth-scraper unit tests + 1 updated
Bplay competition-set test. 348/348 tests pass (up from 341).
Coverage on the new depth scraper is ~88%. mypy strict + ruff
clean across all 38 source files.

**Live smoke (ingestion daemon + arb detector, both running for 60-70s):**

- Snapshots ingested: **13,918** (up from ~3,166 before depth = 4.4×)
- Detector saw: **10,943** snapshots
- Opportunities emitted: **0**

Per-platform contribution to the new canonical surface:

| | 1X2 quotes | BTTS quotes | OU quotes |
|---|---|---|---|
| betsson-pba | 252 | 200 | 590 |
| bplay-pba | 1,386 | 0 (XML feed lacks BTTS in current comps) | 3,036 |
| betwarrior-pba | 3,333 | **324 (new)** | **1,746 (new)** |

**Cross-platform candidate market expansion (before → after depth):**

| Market | Cross-platform markets |
|--------|------------------------|
| 1X2 | 18 → **21** |
| BTTS | 0 → **16** |
| OU goals | 0 → **56** |
| **Total** | **18 → 93** |

**Structural finding from the data — the 0-emission result is
NOT an architectural problem.** With time-coherent overround
analysis on the 93 new cross-platform candidates:

| Market | Co-temporal cross-platform | Closest margin | Best legs come from |
|--------|---------------------------|----------------|----------------------|
| 1X2 | 13 | -1.11% | All Betsson |
| BTTS | 11 | -4.45% | **All Betsson** |
| OU goals | 42 | -3.34% | **All Betsson** |

**Every closest-to-arb candidate across all three markets has all
legs from Betsson.** The depth scraper IS contributing — BetWarrior
emits BTTS + OU quotes that share canonical fixtures with Betsson.
But Betsson's odds are systematically HIGHER than BetWarrior's on
every cell, so cross-platform best-per-cell selection effectively
reduces to single-platform Betsson, and the effective overround
equals Betsson's standalone vig (~1.04–1.07).

The structural cause: **Betsson (OBG, recreational-skewed) prices
looser than BetWarrior (Kambi, sharp-skewed) on Argentine domestic
matches. Both books agree directionally on which team is the
favorite — Betsson just leaves more vig on the table. Cross-platform
best-per-cell rewards the looser book; you only get a real arb when
two books DISAGREE about the favorite, not when one is uniformly
looser.**

**Next moves in priority order:**

1. **Bplay WebSocket scraper for Argentine domestic.** Bplay's
   pricing model is independent of both Betsson and BetWarrior
   (different platform backend, different bookmaker operator).
   Adding a third pricing engine on the same Reserves matches is
   the cleanest path to surfacing real divergence. ~3–5 days work
   (WebSocket protocol recon + persistent connection management +
   reconnection logic).
2. **Add a 4th PBA-licensed platform if any are accessible.**
   Earlier sweep noted Pasion as PBA-eligible but not yet recon'd.
3. **Continuous run.** Even with the current Betsson-BetWarrior
   directional alignment, transient odds movements (in-play repricing,
   suspension+reopen cycles) can briefly produce a real arb. 24-48h
   continuous run would surface those if they happen. Useful before
   committing to #1 to confirm that the structural finding holds
   under broader sampling.

**Errors:**
- One mypy error: `yield from` is not allowed inside an async
  generator. Fixed by inlining the iteration
  (`for x in sync_gen(): yield x`). Documented in the depth
  scraper alongside the sync generators it consumes from.
- Bplay's Playwright recon got "Request Rejected" partway through
  this session (the persistent profile got fingerprinted after
  several recon runs). Plain curl HTTP still works fine — only
  the SPA shell is gated. Documented in the recon log so future
  sessions don't waste time on Playwright-based Bplay recon
  unless using a fresh profile.

---

## 2026-05-26 — Semantic layer widened to BTTS + OU goals (2-platform scope)

**Context:** With v1 (1X2 only) shipping cleanly but yielding 0 cross-
platform arb candidates due to fixture-coverage divergence between
Bplay (World Cup qualifiers + Libertadores) and BetWarrior (Argentine
domestic), widening the canonical layer to BTTS + OU goals was the
agreed next move. Goal: increase the canonical-market surface area
on the fixtures we already have, even before adding BetWarrior depth.

**Scope decisions:**
- **BTTS + OU on Betsson and Bplay only.** Both already emit these
  markets in the existing scrapers — no scraper changes required.
  BetWarrior would need per-event polling (the 434KB-per-event
  endpoint) which is a separate scope.
- **Reject Betsson integer OU lines (push lines).** Betsson emits
  both half-lines (X.5) AND integer goal totals (1, 2, 3, 4). On
  an integer line, total goals = N refunds both sides, so {OVER,
  UNDER} doesn't form a clean partition. The arb math (implied
  probabilities sum to 1) is invalid on push lines and would emit
  false arbs. The market_resolver filters integer lines explicitly.
- **partition_validator still bypassed.** BTTS {YES, NO} and OU
  {OVER, UNDER}-at-same-line are structurally exhaustive
  partitions, same as 1X2. partition_validator becomes load-bearing
  on Asian Handicap (where line semantics require the validator)
  not yet shipped.
- **Defensive Bplay BTTS handling.** Bplay's BTTS market label
  wasn't observed in the 30s smoke window (its current marquee
  comps don't expose BTTS). The market_resolver uses a defensive
  `ambos` prefix match for Bplay, mirroring what the Bplay scraper
  itself does at extraction time. Will be tightened to exact match
  once a live Bplay BTTS sample appears.

**Changes:**
- `src/semantic/canonical.py`: added `BTTS` + `OU_GOALS` enum
  members, `CELL_YES/NO/OVER/UNDER` constants, populated
  `EXPECTED_CELLS` for both new markets.
- `src/semantic/market_resolver.py`: BTTS exact/prefix crosswalk +
  OU regex extraction + half-line validator. Push lines rejected
  with a code comment explaining why.
- `src/semantic/outcome_resolver.py`: added `_resolve_btts` (Si/Sí
  /Yes/No) and `_resolve_ou_goals` (first-token prefix match on
  Más/Menos/Over/Under — handles both Bplay's bare label and
  Betsson's line-embedded label uniformly).
- Test updates: 84 new resolver tests + 4 new canonicalizer
  integration tests + adjusted prior tests that asserted
  "not-in-v1" against markets now in scope.

**Test footprint:** 341/341 tests pass (up from 293), mypy strict
+ ruff clean.

**A subtle architectural finding the tests exposed:**

- Betsson BTTS and OU outcomes (`Si`/`No`/`Más`/`Menos`) can't
  anchor a fixture on their own because they're not team names.
  Realistic snapshot order: Betsson's `accordion/v1` endpoint
  returns 1X2 + BTTS + OU markets in one HTTP, and 1X2 arrives
  first in the response. The 1X2 outcomes (team names) anchor
  `(betsson-pba, platform_event_id)` to the canonical fixture
  via team-name fuzzy match; then BTTS + OU on the same event
  ride the cache fast path. The fixture_resolver's existing
  `(platform, platform_event_id) → fixture_id` index handles
  this transparently — no resolver change needed, but documented
  as a precondition in a new test
  (`test_betsson_btts_without_1x2_anchor_drops`).

**Live smoke against 20k pre-existing snapshots in `odds:raw`:**

| Market   | Distinct markets | Full cell coverage | ≥2-platform | ≥3-platform |
|----------|------------------|--------------------|-------------|-------------|
| 1X2      | 138              | 138                | 18          | 0           |
| BTTS     | 18               | 18                 | **0**       | 0           |
| OU goals | 200              | 166                | **0**       | 0           |

The semantic widening 3x'd the canonical-market surface area (from
138 to 322 fully-covered markets), but cross-platform overlap on
BTTS + OU is still 0 — same root cause as 1X2: Bplay covers
international fixtures, Betsson covers Argentine domestic, the
fixture sets don't intersect.

**End-to-end live smoke (ingestion + detector concurrently, 30s):**
- 3933 snapshots through the detector
- 0 opportunities emitted (correct given coverage state)
- Architecture verified hot — no regressions

**State and the next leverage move:**

The architecture now supports BTTS + OU end-to-end. The bottleneck
for actual cross-platform arb detection has narrowed to a single
clear target: **BetWarrior depth scraping**. BetWarrior covers the
same ~18 Argentine domestic fixtures as Betsson but only emits 1X2
today. Extending it to per-event polling (the
`betoffer/event/<id>.json` endpoint, ~434 KB per event, ~470 markets)
unlocks roughly **180 new 2-platform candidate markets per cycle**
(18 fixtures × ~10 markets each: 1 BTTS + ~9 OU half-lines).

**Errors:** None during implementation. One ruff `RET504` on an
unrelated team_normalize change in a prior session (already fixed).
Pre-existing `OpportunityStatus(str, enum.Enum)` ruff warning in
`src/storage/models.py` still in place — flagged again for a
future cleanup, still not in scope.

---

## 2026-05-26 — Semantic layer v1 shipped: cross-platform canonicalization + arb detector

**Context:** With three PBA scrapers ingesting into `odds:raw`, the
gap to "actual arbitrage" was the translation layer. This session
designed and shipped that layer end-to-end: raw snapshots from three
distinct white-label backends (OBG / SportNCO / Kambi) →
platform-agnostic `CanonicalQuote`s → grouped by canonical
`(fixture, market)` → `dutch_book.detect_arbitrage` → emit to
`arb:opportunities`. Runs as a separate process from the ingestion
daemon.

**Scoping decisions (user-confirmed at kickoff):**
- **v1 = 1X2 only.** The single market all three current scrapers
  emit. Resolver crosswalks stay tight; BTTS / OU / DNB / AH wait
  for the semantic plumbing to prove itself first.
- **Deterministic-first, LLM-fallback architecture.** Mirrors the
  partition_validator pattern: cheap deterministic path resolves
  the common case; ambiguity escalates to an injected
  `LLMFixtureMatcher`. Ships with `NoopLLMFixtureMatcher` (always
  returns None) so v1 is fully deterministic. Real LLM
  implementation hooked in later when ambiguity rate justifies it.
- **In-process cache only.** No Postgres canonical_fixtures
  table. Restart re-warms from the snapshot stream in ~1 min.

**Modules shipped in `src/semantic/`:**

| Module | LOC | Coverage |
|---|---|---|
| `canonical.py` | 75 | 100% |
| `team_normalize.py` | 50 | 90% |
| `market_resolver.py` | 40 | 100% |
| `outcome_resolver.py` | 75 | 84% |
| `fixture_resolver.py` | 180 | 90% |
| `canonicalizer.py` | 60 | 96% |
| `arb_detector.py` | 220 | 92% |

Plus `scripts/run_arb_detector.py` (daemon) and helper
`stream_fields_to_snapshot` co-located with the existing
`snapshot_to_stream_fields` in `redis_sink.py`.

**Test footprint:** 86 new unit tests, 293/293 total project tests
pass (up from 196 before semantic layer). 90% project coverage.
mypy strict + ruff clean.

**Three asymmetries I'd have gotten wrong from docstrings alone —
caught only by inspecting the real Redis stream:**

1. Betsson 1X2 label capitalization is *inconsistent across
   snapshots* (`Ganador del partido` AND `Ganador del Partido`
   both appear in the same 30s ingestion run). Market resolver
   lowercases.
2. Betsson `raw_event_name` is a flat slug like
   `"gimnasia jujuy belgrano"` — no separator, can't parse
   home/away. Resolved by using Betsson's outcome labels (which
   ARE team names) to anchor against existing canonical fixtures
   created by anchor platforms.
3. BetWarrior 1X2 outcome labels are position strings
   `"1"`/`"X"`/`"2"` (not team names). Resolver branches on
   `platform in {betwarrior-pba}` for position-label decoding.

**One design point I had to revisit mid-build:**

- **Throttle with improvement override.** First pass: throttle
  prevented re-emitting the same opp within N seconds. Test
  revealed this also suppressed *strictly-better* emissions when
  a slower platform's improving quotes arrived after a first
  detection. Fix: throttle suppresses only opportunities with
  realized_roi ≤ last emission's realized_roi. Strict
  improvements bypass the throttle. Three emissions in the
  smoke test (7.7% → 8.33% → 10% as each platform contributed)
  confirm this is the right behavior.

**One v1 correctness fix I made:**

- **Fixture resolver: fuzzy match, not exact.** Initial draft
  used exact normalized-string match on home/away pairs, which
  would have silently split canonical fixtures whenever two
  platforms used different canonical team names (e.g. Bplay
  `"Mirassol FC SP"` vs BetWarrior `"Mirassol-SP"`). The whole
  point of the layer is dedup, so I promoted to fuzzy match
  with `team_similarity ≥ 0.85` — same threshold as
  outcome_resolver, tested to correctly DEDUP cross-platform
  variants AND correctly REJECT Reserves-vs-first-team
  conflations (Belgrano vs Belgrano Reserves stays separate).

**Live smoke (60s detector run against live ingestion):**

- Snapshots seen:                **9,869**
- Market-resolved (1X2 only):    **9,136** (45% of total; the rest
                                  are BTTS/OU/AH/DNB markets v1
                                  doesn't process — correct drop)
- Fixture-resolved:              **8,548** (94% of market-resolved)
- Outcome-resolved:              **8,485** (93% of market-resolved)
- Canonical fixtures registered:  ~150
- Full HOME+DRAW+AWAY coverage:  **138 fixtures**
- Of which 3-platform:           **0**
- Of which 2-platform:           **18** (all Betsson+BetWarrior
                                  on Argentine domestic Reserves)
- Opportunities emitted:         **0**

**The 0-emission result is correct, not a bug.** Diagnostic showed
the platforms cover **non-overlapping fixture sets** right now:

- **Bplay's 1X2 feed** is dominated by World Cup qualifiers
  (`Francia vs Senegal`, `Alemania vs Curazao`, `México vs
  Sudáfrica`) and a thin slice of Libertadores.
- **BetWarrior's 1X2 feed** covers Argentine domestic Reserves +
  Primera Nacional + a *different* slice of Libertadores.
- **Betsson** covers Argentine domestic (overlaps with BetWarrior).
- **Bplay ∩ BetWarrior = 0** active fixtures during the smoke
  window, confirmed by fuzzy team-pair match across all 41 Bplay
  events vs all 97 BetWarrior events.

The 18 Betsson∩BetWarrior fixtures DID get processed end-to-end
and their odds were checked against the 1% margin threshold — no
opportunity exceeded the threshold. That's also expected: a
single-direction 2-platform arb on the same backend pricing
characteristics typically lives within the bookmaker vig.

**State:** The data pipeline is complete and correct. The
bottleneck for actual arb detection is now **coverage overlap**,
not engineering. To surface a real opportunity we need at least
one of:
1. **Run for longer.** Bplay's Libertadores rotation and
   BetWarrior's match schedule are not the same right now;
   matchday alignment will produce overlap.
2. **Widen the market scope.** BTTS / O/U / DNB have higher
   pricing divergence across backends than 1X2 — the same
   semantic layer plus per-event Kambi fetch unlocks them.
3. **Add a fourth platform.** Each backend adds new pricing,
   raising the probability of any one cell being mispriced.

**Errors:**
- One `Argument "raw_market_name" ... incompatible type` mypy
  error in outcome_resolver — fix was hoisting the
  `criterion.get("label")` chain into a local so the `isinstance`
  narrowed. Same pattern as the BetWarrior scraper fix in the
  prior session.
- One ruff `RET504` on `team_normalize` for unnecessary
  intermediate assignment — fixed by inlining the return.
- One test originally assumed `call_count == 1` for the
  detector's full-coverage emission. After the
  improvement-override throttle change, the correct expectation
  is multiple emissions (one per strict improvement) and the
  last one carries the peak margin. Test updated.
- Pre-existing ruff warning on `OpportunityStatus(str, enum.Enum)`
  in `src/storage/models.py` — still not in scope.

**Next steps in priority order:**
1. **Run the detector continuously for 24-48h.** That's the real
   test — does any 3-platform window of overlap produce an actual
   arb? Even a single detection validates the whole pipeline.
2. **Widen v1 to BTTS + OU.** Same semantic layer + 2 new resolver
   crosswalks + extend BetWarrior to per-event polling (a few
   hundred LOC). Expected to substantially increase arb candidate
   surface.
3. **Risk layer scoping.** When an arb is detected, who decides
   whether to actually place the bet? That's the next missing
   architectural slice.

---

## 2026-05-26 — BetWarrior PBA scraper shipped + wired into daemon (3-way PBA ingestion)

**Context:** Built and shipped the MVP BetWarrior PBA scraper
immediately after the recon session, per user direction. Wired into
`run_ingestion_daemon.py` alongside Betsson and Bplay. All three PBA
platforms now feed the single `odds:raw` Redis stream from one
daemon process.

**Decisions:**
- **MVP scope: list view, 1X2 only.** `BetWarriorPbaScraper` polls
  `listView/football/<slug>/all/all/matches.json` per target
  competition. Each poll returns all matches in that competition
  with their primary 1X2 betoffer attached. Deeper markets (AH,
  O/U, BTTS) live in the per-event endpoint (~434 KB per event,
  ~470 markets each) and are intentionally NOT consumed yet —
  they're a one-method addition when the semantic layer is ready
  to canonicalize across platform-specific market labels.
- **Default target competitions narrow on purpose.** Four slugs:
  `argentina`, `copa_libertadores`, `copa_sudamericana`,
  `champions_league`. Picked for overlap with existing scrapers'
  coverage plus the Argentine-domestic gap that Bplay's XML can't
  see. Easy to widen via constructor arg.
- **Same per-competition isolation pattern as Bplay.** One bad
  competition logs + skips, doesn't kill the cycle. Schema break
  recurs across all four and surfaces in logs cleanly.
- **`Origin` + `Referer` headers explicit per the recon contract.**
  Kambi's offering-api doesn't actually enforce them in current
  testing — but mirroring the SPA's request shape is the cheapest
  insurance against a future Kambi-side header check.
- **Kambi odds scale handled inline.** `decimal_odds = odds_raw / 1000.0`
  with a single named `KAMBI_ODDS_SCALE` constant. The unit test
  `test_kambi_odds_scaled_by_1000` is the regression guard.
- **Defensive `betOfferType.englishName == "Match"` filter.** The
  list-view endpoint should only carry Match offers, but the
  filter makes the scraper robust against Kambi widening the
  payload later — emits clean 1X2 only until a deliberate scope
  widening here.
- **One extra httpx client + one extra line in `_build_scrapers`**
  was the entire daemon change. The aggregator/sink machinery the
  earlier session built handles N scrapers without modification.

**State:** Three PBA platforms ingesting from one daemon. Verified
end-to-end:

| Platform        | Snapshots in 30s | Architecture                           |
|-----------------|------------------|----------------------------------------|
| bplay-pba       | 2,700            | XML, per-competition (~540/call)       |
| betwarrior-pba  | 1,485            | JSON (Kambi), per-competition list view |
| betsson-pba     | 479              | JSON (OBG), per-fixture                |
| **Total**       | **4,664 — 0 dropped** | shared queue → 1 Redis stream      |

BetWarrior's per-cycle output (~297 snapshots × 5 cycles in 30s) is
mid-range: not as dense as Bplay's competition-XML payloads, but
substantially denser than Betsson's per-fixture polling. The
~297-snapshot smoke run covered 99 fixtures × 3 outcomes — first
fixtures returned were Argentine-domestic Reserves matches
(Almirante Brown Reserves vs CA San Miguel Reserves, Defensa y
Justicia Reserves vs Independiente Reserves) — closing the Bplay
coverage gap exactly as the recon predicted.

**Errors:**
- One mypy `[arg-type]` on the `raw_market_name` ternary
  (`Any | str | None` vs expected `str`). mypy couldn't narrow
  `criterion.get("label")` across two separate `.get()` calls in
  the same expression. Fix: hoist `criterion_label = criterion.get(...)
  if isinstance(criterion, dict) else None` into a local, then narrow
  on the local. Cleaner anyway.
- Pre-existing ruff warning on `src/storage/models.py`'s
  `OpportunityStatus(str, enum.Enum)` (should be `enum.StrEnum`).
  Not touched per surgical-changes guideline; flagged here so it's
  picked up in a future cleanup pass.

**Test footprint:** 16 new tests in `tests/unit/test_betwarrior_scraper.py`,
196 total tests pass (up from 180). Coverage on `betwarrior.py`:
87%. Full real-API smoke: `scripts/smoke_betwarrior.py`.

**Next steps (in priority order):**
1. **Semantic layer.** Three platforms in, the input-fleet question
   is settled enough to design canonical fixture-matching and
   canonical market-code matching. Three distinct platform
   representations to reconcile: Betsson's `f-<base64ish>` event
   IDs + Spanish friendly names; Bplay's numeric match IDs +
   accent-folded names; Kambi's numeric IDs + clean
   `HomeName - AwayName` strings.
2. **BetWarrior depth (later).** Per-event `betoffer/event/<id>.json`
   for AH/O/U/BTTS once the semantic layer can canonicalize across
   the three platforms' Spanish market labels.
3. **Domestic fixture density observation.** Watch Bplay vs
   BetWarrior on Argentine domestic over a few days — Bplay's XML
   doesn't see domestic; if BetWarrior consistently has 100+
   domestic fixtures while Bplay+Betsson share a smaller domestic
   intersection, that's the highest-EV 2-way arb surface.

---

## 2026-05-26 — BetWarrior PBA recon: third platform mapped, Kambi backend

**Context:** First-pass recon of `pba.betwarrior.bet.ar` —
the third confirmed PBA-licensed sportsbook after Betsson and
Bplay. Goal was a viability assessment, not code; identify the
backend, the odds API, and any anti-bot wall before deciding
whether to build a scraper next.

**Decisions:**
- **Backend identified: Kambi** (Swedish white-label, brand ID
  `bwargbap`). All odds traffic to
  `eu.offering-api.kambicdn.com/offering/v2018/bwargbap/...`. The
  on-prem SPA is a Shapegames-orchestrated wrapper around the
  Kambi client; data layer is pure Kambi.
- **Plain httpx works.** Cloudflare gates the SPA shell, not the
  Kambi data API. No challenge, no auth, no special headers — just
  `Origin` + `Referer` and the standard `lang/market/client_id/
  channel_id/ncid` query string the SPA sends. Verified by direct
  curl against six endpoints; all returned `application/json`.
- **JSON, not XML.** Kambi schema is stable across all tenants
  globally — `betOfferType.englishName` is a fixed enum (`Match`,
  `Over/Under`, `Asian Handicap`, `3-Way Handicap`, etc.) and
  `criterion.label` carries the Spanish-localized market name. No
  parser work required beyond filtering and reshaping into our
  `RawOddsSnapshot`.
- **Two-tier polling pattern available.** List-view
  (`listView/football/<slug>/all/all/matches.json`) is cheap
  (~31 KB per competition) and returns 1X2 across all events.
  Per-event (`betoffer/event/<id>.json`) is ~434 KB and carries
  ~470 markets — full depth at the cost of per-event polling.
  MVP scraper would be list-view-only; depth comes later.
- **MVP scope deferred to a future session.** This is recon, not
  implementation. No code shipped beyond adding `betwarrior` to
  `scripts/recon/recon.py`'s `DEFAULT_URLS` dict.

**State:** Three PBA platforms now mapped, all on distinct
white-label tech (OBG / SportNCO / Kambi). Two have shipped
scrapers ingesting into Redis; BetWarrior is the next obvious
addition because (a) it requires no anti-bot work, (b) it closes
the Bplay gap on Argentine domestic football (159 events in the
`argentina` slug vs Bplay's XML 404), and (c) all three engines
on the same fixtures is the substrate the arb layer was designed
for. The semantic layer (canonical fixture + canonical market
matching) is still the blocker downstream, but adding BetWarrior
first widens the input fleet without changing that blocker's
shape.

**Errors:** None during recon. One environment hiccup mid-session
— heredoc-style for-loops in Bash were emitting "command not
found" inside the zsh subshell from this harness; worked around
by writing the probe as a `/tmp/bw_probe.sh` file and invoking
it via `bash`. Doesn't reflect anything about the project; logged
here so future sessions don't waste time on the same path.

**Two odds-API gotchas worth flagging in any future scraper:**
1. Kambi odds are **integer-scaled by 1000**. `odds: 1290` is
   decimal 1.29. Easy gotcha.
2. List-view returns ONLY the primary 1X2 betoffer per event.
   Deeper markets (AH, O/U, BTTS) need the per-event call.

Full recon writeup with endpoint table, JSON schema, scraper
sketch, and coverage-diff table is in
`scripts/recon/RECON_LOG.md` (entry dated 2026-05-26 20:31 UTC).

---

## 2026-05-26 — Multi-scraper ingestion daemon: Betsson + Bplay → one Redis stream

**Context:** Wired the new Bplay PBA scraper into the existing
ingestion daemon. Both platforms now feed the single `odds:raw`
stream from one process. User explicitly de-prioritized the
semantic layer until the platform fleet is wider — get the data
flowing first.

**Decisions:**
- **Generalize, don't duplicate.** Renamed
  `scripts/run_betsson_daemon.py` → `scripts/run_ingestion_daemon.py`.
  Added `BplayPbaScraper` alongside `BetssonScraper` in the
  pipeline. Future scrapers add one line in `_build_scrapers`. No
  new abstraction layer — the existing `BaseScraper.poll_forever`
  shape already handles it.
- **Per-platform httpx clients** (one per scraper) — same shape but
  separate connection pools per host. Mildly wasteful but it
  isolates platform-specific header overrides cleanly and the
  per-host pool ergonomics are the right default.
- **Single shared `asyncio.Queue` + single sink.** Both scrapers
  put into the same queue; one `RedisSnapshotSink` drains. Each
  `RawOddsSnapshot` carries `platform` so downstream can filter.
- **Producer aggregator for the sink's shutdown gate.** The sink
  needs a single "all producers done" Task for its drain
  condition. Wrapped `gather(*producers)` in a single
  `aggregator` task; passed that to `sink.run(..., producer_task=
  aggregator)`. No change to the sink API.
- **Same shutdown semantics.** Any single watched task exiting
  before stop is signaled trips stop (logged as
  `daemon.task_exited_unexpectedly`). All-OR-none — one crashed
  producer takes the rest down cleanly so operator visibility is
  unambiguous.
- **Env vars renamed**: `BETSSON_RUN_SECONDS` →
  `INGESTION_RUN_SECONDS`, `BETSSON_QUEUE_MAXSIZE` →
  `INGESTION_QUEUE_MAXSIZE`. The Betsson-named env vars were the
  only externally-visible breaking change.

**Real run (30s smoke against live API + live Redis):**
- 3,166 snapshots written, **0 dropped**
- **2,700 from bplay-pba**, **466 from betsson-pba**
- Bplay's higher rate is structural: one XML feed per competition
  yields ~540 snapshots per fetch, whereas Betsson polls per
  fixture. Bplay cycles ~5× as often in the same wall-clock
  window. Not a problem for arbitrage — the consumer side filters
  by per-snapshot timestamps.
- Producers stopped within 100ms of each other at T+30s. Sink
  drained for ~700ms after. `daemon.stopped` fired last.
  Shutdown order is correct.

**State:**
- Two production scrapers ingesting into one Redis stream.
- Daemon is now generic — adding a third platform (BetWarrior or
  the WebSocket-based Bplay domestic-league extension) requires
  one extra line in `_build_scrapers` and a new httpx client.
- 180 unit tests still pass (no scraper logic changed; daemon
  scripts are not unit-tested).

**Honest observation worth flagging:** Bplay snapshots dominate
the stream 5:1 over Betsson because of per-competition vs
per-fixture call patterns. This is fine for now — `OddsQuote`s
will be paired by canonical fixture ID once the semantic layer
lands, and the higher Bplay refresh rate is actually a feature
(fresher cross-platform spreads). But the imbalance does mean
the Betsson scraper is the rate-limiting factor on cycle time.
If we ever need symmetric refresh, options include sharding
Betsson fixtures across multiple workers or raising its
`poll_interval_sec`. Filed as a future tune.

## 2026-05-26 — Bplay PBA scraper shipped + smoke passes

**Context:** Translated the Bplay PBA recon findings into a working
`BplayPbaScraper`. Marquee tournament coverage (UCL, Libertadores,
Sudamericana, World Cup) — the use case the user explicitly asked
for. Second concrete scraper in the project; first that's
non-Betsson.

**Decisions:**
- **Inherit from `BaseScraper`**, same as `BetssonScraper`. Reuses
  `poll_forever()` + the Redis sink — Bplay snapshots ingest through
  the same daemon pipeline with zero changes there.
- **Stdlib `xml.etree.ElementTree` parser, no new dep.** The XML
  schema is small and well-formed; no need for lxml's extra weight.
- **One HTTP per competition per poll**, not per fixture. The XML
  files are pre-rendered and small (~32 KB for Libertadores with
  9 matches and 174 offers). More bandwidth-efficient than
  Betsson's per-fixture pattern.
- **404 = "no offers this competition" = silent skip.** Bplay
  doesn't pre-render XML for competitions between rounds. Treating
  404 as an error would spam logs and falsely back the framework
  off; treating it as a normal "skip this cycle" is the right
  semantics.
- **Per-competition `BplayContractError` caught and logged.** A
  single bad competition skips that competition only; a platform-
  wide schema break recurs across all four and is alertable from
  log volume.
- **Outright bets (tournament winner) NOT emitted.** They live
  under `<OutrightList>` not `<MatchList>` and don't fit the
  event-keyed `OddsQuote` shape. Test `test_outright_only_competition_emits_nothing`
  locks this in. A dedicated outrights pipeline can be added later.
- **No domestic coverage.** Argentine leagues use a WebSocket path
  we deliberately don't consume here — see recon notes. Adding
  that is a separate, bigger piece of work (~3-5 days). The user
  explicitly de-prioritized.

**Tests (15 new, total 180):**
- Construction: defaults, custom competitions
- Empty-competition: 404 silently skipped, other competitions still
  produce
- Contract errors: non-404 HTTP errors + malformed XML, both
  per-competition (logged, skip, don't kill cycle)
- Odds extraction: target markets only (1-X-2, Más/Menos, 1-2,
  Handicap 1-2, BTTS-prefix); unknown markets skipped; sub-unity
  odds dropped; line value in market name and IDs; outright-only
  emits nothing; `<Team>` preferred over offer-derived names for
  the event label
- Platform IDs: `bplay-pba`, market_id encodes match+type+line,
  outcome_id slugs the outcome name (accent-folded)
- Request shape: one GET per competition, BASE_URL unchanged

**Smoke result (real API, all marquee competitions):**
- 540 snapshots from 41 fixtures and 267 markets
- Paris SG vs Arsenal (UCL) — first sample, odds 2.22 / 3.5 / 3.25
- Market distribution healthy: 123 1-X-2, 78 over/under at 2.5,
  multiple O/U lines from 0.5 to 5.5, plenty of Asian Handicap
- No `Ambos` / BTTS market appeared in this sample — could mean
  Bplay doesn't offer BTTS on these specific competitions, or the
  label differs from our prefix. Not blocking; the BTTS prefix
  matcher stays in for when it appears.

**State:** two production-ready PBA scrapers (`BetssonScraper` and
`BplayPbaScraper`). The first cross-platform arb pair is now
technically feasible — Betsson PBA × Bplay PBA on UCL /
Libertadores / Sudamericana / World Cup matches.

**Next natural slice:** the missing piece between scrapers and
arbitrage is the **semantic layer** — canonical resolution that
maps `betsson-pba`'s `MW3W` market for "Boca Juniors vs River
Plate" to the SAME canonical market identifier as `bplay-pba`'s
`1-X-2` for the same fixture. The partition validator (already
built) is one ingredient; the cross-platform matcher / market-code
mapper is the rest. Without it the dutch_book detector can't pair
Betsson and Bplay snapshots even though we have both flowing
through Redis.

## 2026-05-26 — Bplay follow-up: XML feeds are tournament-scoped, not per-competition

**Context:** The first Bplay recon flagged 404s on Argentine domestic
competition IDs as a concern. User confirmed Apertura final (River vs
Belgrano) just passed and asked for a deeper look. This session
answers the question and clarifies the data architecture.

**Findings:**
- **The `/oddsfeeds/odds-competition<ID>.xml` pattern is a curated
  subset, not a per-competition feed.** Only marquee tournaments get
  pre-rendered XML: UCL (6674), Libertadores (36146), Sudamericana
  (36148), World Cup (63057). Argentine domestic competitions
  (1493, 43411, 43414) all 404 by design, not by being off-season.
- **The real navigation API is `POST ws-deportespba.bplay.bet.ar/
  component/datatree`** with `{context: {url_key, lang,
  timezone, ...}}`. Returns a recursive component tree with full
  event metadata for any page URL — including Argentine domestic
  fixtures (35 match_id refs under `/categoria/84-argentina`,
  confirmed live: Club Almirante Brown vs CA San Miguel etc.).
- **`EventList` and `EventLiveMarketList` carry event metadata +
  market filter categories but NOT odds.** Odds for non-XML
  competitions almost certainly flow via WebSocket on the same
  `ws-deportespba` host (Playwright HAR doesn't capture WS frames
  by default, so we never saw them).

**Architectural summary:**
- **XML feeds**: pre-rendered, anonymous httpx, covers marquee
  tournaments including World Cup.
- **JSON datatree**: anonymous httpx, covers all navigation +
  event metadata.
- **WebSocket (presumed)**: covers live odds for all events;
  not yet captured.

**Implications for the scraper plan:**
- **World Cup arb (the user's stated long-term target): viable now**
  with plain httpx + XML parser. No WebSocket work required.
- **Marquee continental tournaments (Libertadores, Sudamericana,
  UCL): viable now** by the same path. Useful for opportunistic arb
  during domestic between-season gaps.
- **Argentine domestic league arb: would require WebSocket recon
  + parser.** Event-metadata path already mapped; odds channel
  remains. User has de-prioritized this — fine to defer.

**Recommendation:** ship the XML-feed Bplay scraper next. Covers
World Cup + UCL + Libertadores + Sudamericana. Bets that can be
arb'd against Betsson PBA on those competitions. ~1 day of work.
The WebSocket-based domestic-league extension can come later if /
when we want to widen coverage.

## 2026-05-26 — Bplay PBA recon: viable, SportNCO XML, Betsson-class

**Context:** Fourth platform recon. Bplay was flagged PBA-licensed
during the earlier multi-platform sweep; this session confirms full
viability for our PBA-IP, lightweight-scraper plan.

**Findings:**
- **Backend: SportNCO** (French sportsbook white-label) — confirmed
  via `sportx-static.sportnco.com/flag_*.png` references throughout
  the SPA. Conventions should transfer to other SportNCO operators.
- **Frontend: Nuxt.js SSR.** Competition catalog is pre-rendered
  into the HTML — discoverable via a single GET on
  `deportespba.bplay.bet.ar/`.
- **Cloudflare-fronted shell, NOT data feeds.** Same pattern as
  Betsson and Codere: bot protection sits on the marketing HTML,
  the XML odds feeds answer plain httpx.
- **Odds API: per-competition XML feeds** at
  `deportespba.bplay.bet.ar/oddsfeeds/odds-competition<ID>.xml`.
  Schema is clean and parseable with stdlib `xml.etree.ElementTree`.
  Each feed carries both `<OutrightList>` (tournament winner) and
  `<MatchList>` with full per-match markets. **9 matches in 32KB
  for Copa Libertadores** — more bandwidth-efficient than Betsson's
  one-HTTP-per-fixture model (one HTTP per *competition* gets all
  matches in that competition at once).
- **Market codes already decoded** without further recon:
  - `1-X-2` ≡ Betsson `MW3W` (1X2 — Home / Empate / Away)
  - `Más de / Menos de` + `number="N.5"` ≡ Betsson `MTG2W` (O/U)
  - `1-2` ≡ Draw No Bet
  - `Handicap 1-2` + `number="±N"` ≡ Asian Handicap
- **BTTS not seen in sampled match** (Copa Libertadores Mirassol
  vs Always Ready) but likely under `type_name="Ambos equipos
  anotan"` for other matches. Needs confirmation.
- **Argentine competition IDs (from Nuxt SSR)**: 1493 Copa
  Argentina, 43411 Primera Nacional, 36146 Copa Libertadores,
  36148 Copa Sudamericana, 6674 UEFA Champions League. Domestic
  leagues (1493, 43411) **returned 404** on the XML pattern at
  recon time — could be temporary (between-rounds) or different
  ID mapping. Requires a re-probe during an active fixture
  window to confirm coverage overlap with Betsson.

**Engineering cost: ~1-2 days to a working `BplayPbaScraper`.**
Same shape as `BetssonScraper`: `BaseScraper` subclass, fixture
cache (competition list rather than fixture list), per-competition
XML poll with `xml.etree.ElementTree`, emit `RawOddsSnapshot`s.
Mechanical XML parser swap, no protocol-level work.

**Real concern surfaced: Argentine domestic coverage.** Without
overlap on Argentine domestic fixtures Bplay can't pair with
Betsson PBA for cross-platform arb on what we actually care about.
**Action item: short follow-up recon when Argentine leagues are
serving fixtures** to confirm 1493 / 43411 do produce content.

**State:** Three PBA-viable platforms now mapped, all
lightweight-scraper-tier:
1. Betsson PBA — scraper + daemon shipped, in production
2. Bplay PBA — recon complete, ready for scraper work
3. BetWarrior PBA — flagged IPLYC-licensed, recon pending

Three platforms → three pairwise arb combinations in PBA. Enough
to start cross-platform work once we have two scrapers running.

## 2026-05-26 — Codere AR recon: viable, Betsson-class, CABA-only

**Context:** Recon on `codere.bet.ar` — third platform after Betsson
(production) and bet365 (shelved).

**Findings:**
- **Architecture:** Codere AR runs on **Playtech BIT Sportsbook (PBS)**,
  white-labeled. `codere.vie.pbs-master.com/dig-codere-com/...` reveals
  the underlying platform. Front-end is an Ionic-based SPA at
  `m.caba.codere.bet.ar/Deportes/#/HomePage`. The marketing root
  `www.codere.bet.ar` is Akamai-protected static shell only.
- **API surface:** clean JSON REST. Four key endpoints identified and
  probed direct-httpx successfully:
  - `/NavigationService/LeftMenu/GetMenuLeft` — sports tree
  - `/NavigationService/Game/SportsGameTypes` — market taxonomy
  - `/NavigationService/Home/GetHomeInfo?...` — live events + odds
  - `/SportsMisc/api/Home/GetFeatures?region=33` — region config
  All return JSON with a plain User-Agent — no Akamai gating on the
  data path despite Akamai sensors running during SPA bootstrap.
- **Geographic footprint: CABA only.** `m.pba.codere.bet.ar` /
  `m.cba.codere.bet.ar` / other-province subdomains all fail DNS.
  Codere operates only the Capital Federal license.

**Engineering effort: ~1-2 days to a working `CodereCabaScraper`.**
Same shape as `BetssonScraper`: BaseScraper subclass, fixture cache,
per-event odds polling, JSON parsing, `OddsQuote` emission via
`RawOddsSnapshot`. Mechanical parser difference (response is wrapped
as `{Game: {Results: [...]}}` rather than Betsson's
`{data: {accordions: {<CODE>: {markets, selections}}}}`).

**Real constraint surfaced:** **CABA vs PBA jurisdiction mismatch.**
Our existing Betsson scraper is PBA. CABA Codere + PBA Betsson is
not a coherent arbitrage pair — different regulators, separate
accounts, separate jurisdictions. To actually arb with Codere we
need either:
1. Recon Betsson CABA (`caba.betsson.bet.ar`) so both scrapers run
   on the same province. CABA's `x-sb-jurisdiction` header value is
   unconfirmed from the PBA recon; needs its own session.
2. Recon a PBA-licensed competitor (Bplay / BetWarrior / Pasion).

**Side benefit:** the PBS backend is widely used. Recon-derived
endpoint conventions should transfer to other PBS-backed operators
(Codere ES, Codere MX, Sportium, several Eastern European brands).
One protocol parser, many scrapers.

**State:** no Codere scraper code written yet. Detailed findings in
`scripts/recon/RECON_LOG.md` — soccer-specific market codes and the
Argentine Primera División filter are the remaining unknowns to
clear before writing `codere_caba.py`.

**Decision needed:** which way next?
1. Recon Betsson CABA → unlock cross-platform arb in CABA, write
   both CABA scrapers.
2. Recon a PBA Betsson competitor (Bplay / BetWarrior / Pasion) →
   keep PBA province focus, fast time-to-first-cross-platform-arb.
3. Write the Codere CABA scraper now, defer cross-platform pairing.

## 2026-05-26 — Bet365 AR recon: viable but ~5× the engineering of Betsson

**Context:** First recon of `bet365.bet.ar` (the AR-licensed
subsidiary). Per the user's mandate: gather evidence on viability,
document either way.

**Tooling change:** renamed `scripts/recon/betsson_recon.py` →
`scripts/recon/recon.py` and parameterized it by `--platform`. Each
platform now has its own artifact dir + browser profile. Existing
Betsson profile migrated. The script is now generic; per-platform
default URLs live in a small dict.

**Findings (full detail in `scripts/recon/RECON_LOG.md`):**
- AR-licensed, accessible from AR IP (footer carries `ba-province`
  and `ba-regulatorv2` markings). Not geo-blocked.
- Hash-routed SPA at `https://www.bet365.bet.ar/#/HO/`. Nav lives in
  the URL fragment; PD strings like `#AC#B1#C1#D1002#G40#` translate
  letter-by-letter into hash routes.
- **The routing manifest** at `/websiteroutingdatacontentapi/routingdata`
  maps URL patterns to data endpoints. Identified the soccer
  upcoming-matches endpoint:
  `/matchmarketscontentapi/soccerupcomingmatches` with required
  query params `tzo`, `csidex`.
- **Content APIs return a custom pipe-delimited text protocol**, not
  JSON. Format: `|`-separated records, `KEY=VALUE;` fields. Order of
  magnitude harder to parse than Betsson's JSON but tractable.
- `/Api/1/Blob` is the JS bundle delivery endpoint, not a data API.
  Irrelevant to the scraper.
- **Cloudflare bot challenge fronts the data APIs.** Direct httpx
  calls get a "Just a moment..." interstitial (HTTP 403); Playwright
  clears the challenge transparently and gets real responses. This
  is the operational wall.

**Cost vs Betsson:**
- Betsson: httpx + 3 headers, ~50MB RAM, polling forever, ~1 day.
- Bet365: long-running Playwright session per scraper, ~400MB RAM,
  periodic `cf_clearance` refresh, custom protocol parser. ~3-5 days
  for a first working scraper.

**Decision pending:** waiting on user direction. Two reasonable paths:
1. Deprioritize bet365 and recon Codere AR / Bplay / BetWarrior first
   (likely Betsson-class lightweight) for faster cross-platform
   arbitrage coverage.
2. Press on with bet365 — long-running Playwright scraper, in-context
   XHRs, pipe-delimited text protocol parser, scheduled profile
   refresh.

**Why this is logged even though no scraper code was written yet:**
the recon answered a real question (is bet365 AR scrapeable?) with
real evidence (Cloudflare, custom protocol, no Akamai, no aggressive
fingerprinting). Either future direction will lean on this
information, so capturing it here is what makes the recon valuable
even if we ship no bet365 code.

## 2026-05-26 — Sink shutdown fix: producer-task gate (no trailing drops)

**Context:** First production run of the ingestion daemon surfaced a
real shutdown bug — sink exited while the producer was still mid-cycle,
silently dropping the last batch of snapshots. Reading the structured
logs caught it; fixing it took five lines.

**The bug:**
- `RedisSnapshotSink.run` exited on `stop_event AND queue.empty()`.
- `BaseScraper.poll_forever` only checks `stop_event` between polling
  cycles (the inner `async for snapshot in fetch_live_soccer()` runs
  to completion).
- At SIGTERM time the producer is mid-cycle; the queue goes
  transiently empty between puts; sink sees stop+empty and exits.
- Producer continues until its current cycle finishes (~8s observed
  on the first prod run); subsequent snapshots land in a queue with
  no consumer and die at process exit.

**The fix:**
- New optional `producer_task: asyncio.Task | None = None` kwarg on
  `RedisSnapshotSink.run`.
- Exit condition becomes `stop AND queue empty AND (producer is None
  OR producer.done())`. Default None preserves the old semantics for
  callers that don't have a producer-task handle (tests, ad-hoc
  scripts).
- Daemon wires the producer task through to the sink.

**Verified empirically.** Cleared the stream, re-ran the daemon for
30 seconds. New order is now `scraper.stopped → sink.stopped` (170ms
apart), `dropped=0`, `XLEN odds:raw` matches what the sink reported
(461 entries). Previous run captured 323; the extra 138 in this run
are the trailing-cycle snapshots that used to die silently.

**Tests:**
- New `test_keeps_draining_while_producer_task_alive` simulates a
  slow producer with stop already set; sink must wait for the
  producer task to finish before exiting. This is the regression
  guard for the exact bug we just hit.
- New `test_exits_when_producer_done_and_queue_empty` covers the
  other end: once producer is fully done and queue is drained, the
  sink should exit promptly rather than hang.
- Total suite: 165 (163 prior + 2 new). 98% coverage on
  `redis_sink.py` (one line — a debug log emitted only every 100th
  write — uncovered).

**Why this didn't show up in unit tests:** the mock-redis tests were
deliberately set up to put items first, then call `stop.set()`, then
run the sink. The producer-is-still-running shape never appeared
because there was no async producer in the test — just a queue
pre-loaded by the test body. The bug needed an actual concurrent
producer to manifest. The new tests use a real `asyncio.Task` as the
fake producer, which is exactly the shape that triggers the race in
production.

## 2026-05-26 — Ingestion daemon: scraper → asyncio.Queue → Redis stream

**Context:** Wired `BaseScraper.poll_forever` into a long-running
daemon per `docs/architecture.md`'s "Scrapers → Redis queue →
Normalizer" pipeline. Phase 1 ingestion now has its first end-to-end
runnable component.

**Decisions:**
- **Sink as a separate concern.** `src/ingestion/redis_sink.py` owns
  the queue→Redis path; the scraper doesn't know Redis exists. This
  keeps the scraper testable without infra and lets us add more sinks
  later (Postgres direct-write, file-based audit dump) without
  touching the scraper.
- **Redis Streams over Lists / Pub-Sub.** Streams give us ordered
  durability + multi-consumer reads + replay for audit / debugging.
  Single stream `odds:raw` (platform is a field) — per-platform
  streams would add operator overhead without a current benefit.
- **`maxlen=100_000, approximate=True`** on `XADD`. Approximate
  trimming is dramatically faster than exact MAXLEN and keeps memory
  bounded. 100K entries × ~300B ≈ 30MB — well below the 256MB
  docker-compose Redis cap.
- **Best-effort writes.** A failed XADD logs and drops the snapshot
  rather than re-queueing. Re-queue would cause unbounded retry
  growth on sustained Redis failure; back-pressure via the bounded
  in-process queue is the correct circuit-breaker. The producer
  stalls if Redis is down for long, which is exactly what we want
  (better than silently consuming memory).
- **Bounded in-process queue** (`maxsize=10_000`, env-overridable).
  With ~30-100 snapshots per 5s poll, this gives ~10-30 polling
  cycles of slack before back-pressure. Realistic operational margin.
- **Graceful shutdown.** SIGINT / SIGTERM set a shared `stop_event`.
  Producer exits at its next poll boundary; consumer drains the
  remaining queue items, then exits. No data loss on Ctrl-C.
- **`BETSSON_RUN_SECONDS` for time-boxed smoke runs.** Unset = run
  forever until signal. Set to e.g. 30 to validate the daemon for
  half a minute and have it exit on its own.
- **Failure observability.** If either producer or consumer exits
  early (uncaught), the daemon sets stop_event, drains the other
  task, and logs `daemon.task_exited_unexpectedly` with the failing
  task's name. No silent zombies.

**Tests:** 14 new unit tests for `redis_sink.py` (Redis client mocked
at the `xadd` boundary — no real infra needed). Cover the serializer
(all fields become strings, decimal odds round-trip, absent
max_stake serializes as empty string), the loop (drain full queue,
exit when stop+empty, idle polling, continue after write failure,
custom stream name, maxlen passthrough). Total suite now 163.

**State:** Everything compiles, types check, all tests pass. The
daemon hasn't been run against real Redis + real Betsson yet — the
local Docker daemon was down at build time. The launch instructions
are:
    docker compose up -d redis
    uv run python scripts/run_betsson_daemon.py
    # or for a time-boxed smoke:
    BETSSON_RUN_SECONDS=30 uv run python scripts/run_betsson_daemon.py
    # to verify Redis got the snapshots:
    docker exec -it arby-redis redis-cli XLEN odds:raw
    docker exec -it arby-redis redis-cli XRANGE odds:raw - + COUNT 3

**Errors:**
- mypy stub-narrowness on `xadd` (dict is invariant; redis-py's stub
  demands a union-typed key/value that `dict[str, str]` doesn't
  satisfy even though the values are runtime-valid) and on `ping()`
  (typed `Awaitable[bool] | bool` to share a stub with the sync
  client). Both papered with `# type: ignore` + inline comments —
  the workaround is documented so future readers don't have to
  re-derive it.
- Initial `# type: ignore` was on the wrong line (the `await`
  expression itself rather than the offending argument). Fixed.

## 2026-05-25 — Betsson smoke test (real API): live odds flowing

**Context:** First end-to-end run of `BetssonScraper` against the real
Betsson PBA API via `scripts/smoke_betsson.py`. Surfaced one major
contract gap our synthetic tests couldn't catch — required-headers —
and a real-world fixture edge case we'd built for defensively. Net
result: scraper produces live odds.

**Decisions / findings:**
- **Required-headers contract discovered by deletion test.** First
  smoke attempt 400'd with `E_VALIDATION_INVALIDHEADER`; second
  attempt with the platform-derived headers 500'd with `E_UNHANDLED`.
  Bisected against the live API to find the actually-required minimal
  set:
  - `brandid: 238cb63a-3dcc-4fdf-b241-23a12cb71aa7` — HTTP 400 if missing
  - `marketcode: ag` — HTTP 400 if missing
  - `x-sb-type: b2b` — HTTP 500 (`E_UNHANDLED`) if missing — server
    dispatches by this header and crashes on absence
  Plus `x-sb-jurisdiction: Iplyc` for semantic correctness (so the
  response is scoped to PBA's offering, not a default). Every other
  `x-sb-*` / `x-obg-*` header we saw the browser send is unnecessary
  and deliberately omitted — fewer surfaces for a future OBG infra
  change to break us.
- **Reduced subdomain support to PBA only.** CABA / CBA need their
  own recon to discover their jurisdiction-header values (different
  Argentine provincial regulators). Constructor now raises with a
  clear message if either is passed. `SUBDOMAIN_JURISDICTION` is the
  single source of truth — when CABA/CBA recon happens, add them to
  the dict and they're supported.
- **`x-sb-type: b2b` discovery is the kind of trap recon misses.** It
  was in the browser HAR but didn't *look* required — until we tried
  without it and got an opaque 500. Documented inline in `betsson.py`
  so the next scraper author / platform recon knows to bisect headers
  with the deletion test when they hit similar `E_UNHANDLED` errors.

**State / smoke results:**
- 38 Argentine soccer fixtures discovered (multiple competitions:
  Copa Argentina, Primera Nacional, etc.).
- 30 `RawOddsSnapshot`s emitted (script limit hit) across 3 fixtures
  and 14 markets — 1X2, BTTS, and O/U at lines 0.5 / 1.5 / 3.5 / 4.5
  all parsed correctly.
- Real odds match the recon-frozen values (Gimnasia Jujuy 3.95 /
  Empate 3.05 / Belgrano 2.02 ✓).
- One fixture had no open markets and was skipped with a structured
  warning log — exactly the "single bad fixture, not platform-wide
  schema break" branch we built defensively. Triggered correctly in
  production.

**Tests:** 17 unit tests still pass. New test
`test_required_obg_headers_sent_on_every_request` locks in the
required-headers contract so a future refactor that drops them fails
at PR time, not in production against real Betsson.

**Errors / lessons (filed for next platform):**
- Synthetic mock tests can't catch required-headers contracts. The
  smoke script is the first line that does. Every new platform
  scraper should ship with both the unit-test suite AND a smoke
  script that hits the real API once, so this class of contract gap
  surfaces before the scraper is wired into production polling.
- When a sportsbook API returns an opaque 4xx/5xx that doesn't
  obviously point at a missing field, the bisection-of-headers
  deletion test is the right diagnostic (run the full browser-HAR
  header set; confirm 200; then remove headers one at a time and
  watch for status flips). Memorialized inline in `betsson.py`
  comments and in `RECON_LOG.md`.

## 2026-05-25 — First Betsson scraper draft (`betsson.py`)

**Context:** Translated the recon findings into a working
`BetssonScraper` for the three Argentine provincial subdomains. The
scraper is the first concrete implementation of the existing
`BaseScraper` abstract; built with the explicit goal that subsequent
sportsbook scrapers (Codere, Bplay, etc.) follow the same shape.

**Decisions:**
- **Small additive change to `RawOddsSnapshot`:** added
  `platform_market_id` and `platform_outcome_id`. Without them
  downstream code can only address a selection by string-matching on
  user-facing labels, which is fragile across locale changes and
  unworkable for eventual bet placement. By analogy with the
  pre-existing `platform_event_id`, this is the obvious shape and
  generalizes to every platform.
- **One scraper class, subdomain-parameterized:** `BetssonScraper(http_client, subdomain="pba")`
  with `subdomain in {pba, caba, cba}`. All three Argentine
  provincial sites run identical OBG backends; one class for all three
  with `platform_name = "betsson-<subdomain>"` is the right grain.
- **Fixture discovery cached (5-min TTL).** The categories tree is
  ~2.8MB. Re-fetching every 5s poll would be both wasteful and
  WAF-suspicious. Fixtures don't churn that fast in real schedules.
  Per-fixture odds calls (the accordion endpoint, ~kilobytes) run
  every poll.
- **One typed exception, `BetssonContractError`,** for every shape
  mismatch (missing key, wrong type, HTTP error, non-JSON body). This
  is the alertable "the platform changed something" error — easy to
  grep for and the right place to attach paging in production. Per
  the architecture doc, scrapers don't try to reason about contract
  drift at runtime; they fail loud.
- **Defensive type guards in the parser.** Every list/dict access
  checks `isinstance` and skips on mismatch (vs. raising), with the
  one exception of top-level missing keys which raise hard. Single
  bad fixtures (e.g. event finished mid-poll, race conditions) skip
  with a warning log; a platform-wide schema break still raises
  loudly because it recurs across every fixture.
- **Stake limits intentionally `None`.** `max_stake` /
  `min_stake` / `stake_increment` aren't in the public response;
  they only appear on the logged-in bet slip. Downstream code falls
  back to platform-wide policy defaults until a logged-in recon pass
  captures them.
- **Test strategy:** `httpx.MockTransport` with hand-trimmed
  synthetic responses mirroring the real OBG shapes captured during
  recon session `20260525-210248`. No real network in tests; parser
  is exercised against realistic-but-small fixtures that fit in one
  screen and document the contract simultaneously.

**State:** 149 unit tests pass total (133 prior + 16 new). Coverage on
`betsson.py` is 88% — uncovered lines are defensive type guards and
lower-level HTTP error paths. Ruff + mypy strict clean across
`src/ingestion/`. The scraper hasn't been run against the real Betsson
API yet (that requires `poll_forever` + an orchestrator, which is the
next slice).

**Generalization for future platforms:**
- Every new platform adds one file under `src/ingestion/scrapers/`,
  subclassing `BaseScraper` and implementing `fetch_live_soccer()`.
- The shared `RawOddsSnapshot` + `BaseScraper.poll_forever()` give
  the framework for free (HTTP-client management, polling loop,
  backoff, structured logging).
- The pattern this scraper establishes — typed `<Platform>ContractError`,
  fixture cache + per-event odds call, defensive type guards, slug-
  based fixture filtering, raw human-readable labels in `raw_*`,
  platform stable IDs in `platform_*_id` — is what subsequent
  scrapers should mirror. No shared base helpers extracted yet; we
  do that after the second concrete scraper exists and we can see
  what's actually common.

**Errors:** Pre-existing `# type: ignore[unreachable]` in
`base.py`'s abstract `fetch_live_soccer` stub became unused once the
concrete subclass landed (mypy could resolve the type). Removed the
ignore and clarified the comment about why the unreachable `yield` is
present (it's what tells the type checker the method is an async
generator, not a coroutine).

## 2026-05-25 — Betsson match drill-down: 1X2 / BTTS / O/U codes confirmed

**Context:** Fourth Betsson recon session — drilled into a specific
match URL (Gimnasia Jujuy vs Belgrano, Copa Argentina) to pin down the
exact market codes and selection encoding for the three target market
types.

**Decisions / findings:**
- **The accordion widget is THE odds endpoint:**
  `GET /api/sb/v1/widgets/accordion/v1?eventId=<f-…>&marketTemplateIds=<csv>`
  Returns markets + selections in one round trip; accepts a
  comma-separated list of market codes so all three target markets fit
  in a single call.
- **Market codes confirmed for soccer:**
  - `MW3W` — Match Winner 3-Way (this IS the 1X2 we were hunting).
    Selections carry `selectionTemplateId` of `HOME` / `DRAW` / `AWAY`.
  - `BTTS` — Both Teams To Score, with `BTTS1H` / `BTTS2H` for first /
    second half. Selections: `YES` / `NO`.
  - `MTG2W` — Match Total Goals (O/U) with separate market per line.
    Selections: `OVER` / `UNDER`. Line lives in `lineValue` /
    `lineValueRaw`.
  - `DC` — Double Chance with `HOMEORDRAW` / `HOMEORAWAY` /
    `DRAWORAWAY` (labels `"1X"`, `"12"`, `"X2"`).
- **JSON shape stable:** `{data: {accordions: {<CODE>: {markets:[],
  selections:[]}}}, referenceId}`. Selections carry `odds` as a number,
  status `Open`/`Suspended`, and `marketSelectionPriceFormats.1` as a
  decimal string. Format ID 1 = decimal odds.
- **Stake limits not in public response.** `max_stake` / `min_stake` /
  `stake_increment` are not in the accordion or event endpoints — they
  almost certainly appear only on the bet-slip flow behind login. The
  scraper can run without them initially; the risk layer can fall back
  to platform-wide defaults from the operator's terms page until we do
  a logged-in recon pass.
- **Live updates inferred not captured.** Main `event/v2` response
  carries a `topics` array (`?obg/sportsbook/transient/events/.../...`)
  that confirms a pub/sub subscription model under the SPA. Playwright
  HAR doesn't capture WebSocket frames; live-channel reverse engineering
  is a later optimization. For now 3-10 s polling on the accordion
  endpoint is the architecture-doc-aligned path.

**State:** Enough surface mapped to draft a minimum-viable Betsson PBA
scraper that hits the categories tree once (for fixture discovery) then
polls the accordion endpoint per fixture for MW3W / BTTS / MTG2W. The
scraper would emit `OddsQuote` records with `platform="betsson-pba"`,
`market_id` derived from `marketTemplateId` (plus line for O/U), and
`outcome` from `selectionTemplateId`. Cross-platform canonicalization
is the semantic layer's job.

**Errors:** The recon script's navigation loop always runs `home →
cookie → soccer → click match`. When invoked with `--url` pointing at a
specific match URL it loaded the match (good) then clicked "Fútbol"
which navigated away (mildly bad). Match-page API calls fired before
the click-away so artifacts were intact, but the script should grow a
`--single-url` mode that captures one URL with no further navigation.
Filed in `RECON_LOG.md` as next-pass improvement.

## 2026-05-25 — First Betsson recon (PBA jurisdiction, OBG sportsbook API)

**Context:** First recon pass on a real bookmaker. Built minimal
Playwright-based tooling (`scripts/recon/betsson_recon.py`), ran three
sessions, captured HAR + DOM + screenshots, and decoded enough of the
Betsson PBA API surface to draft a first scraper.

**Decisions:**
- **Tooling:** single Playwright script using a persistent browser
  profile (`recon/profile/`), full HAR capture, live JSONL request log
  (survives crashes), per-step screenshot + DOM dump. Headful default;
  `--headless` flag for unattended runs; `--url` flag for arbitrary
  entry point. Read-only; no logins, no bet-slip clicks. Artifacts
  land in `recon/artifacts/betsson/<utc-timestamp>/`. Both
  `recon/artifacts/` and `recon/profile/` added to `.gitignore` —
  large, local, regenerable.
- **Notes layout:** `scripts/recon/RECON_LOG.md` — newest-first
  hand-written summaries of each session. Raw artifacts are the
  source of truth; the log distills what's worth carrying forward.

**State / findings:**
- Betsson Argentina is **provincially split**: marketing root at
  `www.betsson.com.ar` only routes users to one of three provincial
  sites — `pba.betsson.bet.ar`, `caba.betsson.bet.ar`,
  `cba.betsson.bet.ar`. Each has its own license. Cross-provincial
  arbitrage from one account is almost certainly illegal; the
  scraper needs at least one instance per province we want to cover.
- The sportsbook URL on PBA is `/apuestas-deportivas`. The API
  namespace is `/api/sb/v1` and `/api/sb/v2` — observed backend is
  the **OBG** platform (channel strings carry `?obg/sportsbook/transient/...`).
- Key API endpoints identified (anonymous access works,
  `isLoggedIn=false&jurisdiction=IPLYC` query parameters):
  - `widgets/categories/v2` — full sport/country/league/match tree
  - `competitions/liveEvents` — in-play event IDs
  - `widgets/event-market/v1?marketids=…` — odds (the core endpoint
    for the scraper)
  - `content/groups/mappings` — likely the market-code dictionary
- Event IDs are `f-<hash>`. Market IDs are `m-<eventId>-<MARKETCODE>[-<line>]`.
  Decoded so far: `MWOU-N.5` = match O/U total goals; `1HTG-N.5` = 1H
  total goals; `1HTC-N.5` = 1H total corners; `FRSTGOALSB-0.5` = first
  goal scorer. **The 1X2 market code is not yet confirmed** — need a
  drill-down on a specific match.
- Anti-bot in front: AWS WAF, Contentsquare and Optimizely
  fingerprinting, plus a custom Group-IB-like fraud endpoint
  (`/cdn/fraud/api/fl`). Scraping with a long-running clean profile
  worked fine for this read-only recon; production scraper will need
  to monitor for 403s and rotate / back off.

**Next slice (your call):**
1. Recon drill-down on one specific match to confirm 1X2 market code
   and full selection-encoding for the three target markets (1X2,
   BTTS, O/U).
2. Quick comparison of CABA / CBA — same backend? Same odds?
3. Start drafting `src/ingestion/scrapers/betsson.py` from what we
   already have (categories + event-market endpoints are sufficient
   for a minimum viable scraper that pulls O/U for known
   competitions).

**Errors:**
- First two recon passes were dead ends:
  (a) `www.betsson.com.ar` returned only a 15KB static jurisdiction
  selector — no sportsbook content at all. Discovered the provincial
  subdomain split this way.
  (b) Best-effort `a:has-text('Fútbol')` selector matched a
  live-casino card game called "Football Studio" before it matched
  the sports nav, sending us to `/casino-en-vivo/.../futbol-studio`.
  Lesson logged: prefer direct URL navigation once a canonical path
  is known.
- Both failures produced useful artifacts (the splash page revealed
  the provincial split; the casino misroute prompted us to inspect
  the menu API directly instead of trusting text selectors).

## 2026-05-21 — LLM partition validator with strict tool use + prompt caching

**Context:** Phase 2 second piece — the LLM fallback for partition cases the
rule-based pre-filter defers as UNKNOWN. Lives at
`src/semantic/llm_validator.py`. Tests at `tests/unit/test_llm_validator.py`.

**Decisions:**
- **Model:** `claude-opus-4-7` (per the `claude-api` skill default; not
  overridden by user). Overridable via constructor for testing/Haiku tier.
- **Structured output:** strict tool use with `tool_choice` forcing
  `report_partition_verdict`. Schema has all four properties in `required`
  and `additionalProperties: false` (strict-mode invariants enforced by a
  dedicated test class). The model cannot respond with free-form text,
  which guarantees a parseable shape and removes a class of failure mode.
- **Adaptive thinking:** `{type: "adaptive"}` per skill default —
  partition validation is a reasoning task (scope, push, AH overlap), not
  shallow classification.
- **Effort:** `"high"` — precision matters more than latency on the cases
  that survive pre-filtering.
- **Prompt caching:** `cache_control: {type: "ephemeral"}` on the system
  prompt. Opus 4.7's cache minimum is 4,096 tokens — first draft of the
  prompt landed at ~2,600 tokens and would have silently failed to cache.
  Expanded with a "Borderline cases" section adding worked examples (clean
  sheet, wrapper negation, anytime-scorer no-includes-0-0, range
  partitions, half-with-most-goals tie case, language synonyms). Now at
  ~4,300 estimated tokens — comfortably above the floor and meaningfully
  more accurate as a classifier. The two purposes (cacheability and
  quality) line up naturally; padding for its own sake was avoided.
- **Async:** `AsyncAnthropic` per project's "async for I/O" convention.
- **Typing:** used the SDK's `ToolParam`, `ThinkingConfigAdaptiveParam`,
  `OutputConfigParam`, `TextBlockParam`, `ToolChoiceToolParam`,
  `MessageParam` instead of `dict[str, Any]` so mypy strict mode passes
  cleanly. The SDK's TypedDict-union signatures need explicit type hints
  on the dict literals to narrow — without them, every keyword arg shows
  up as `call-overload` mismatch. Avoided `# type: ignore`.
- **Logging:** structlog with bound `desc_a`/`desc_b`/`context` plus the
  verdict, confidence, trap pattern, and full token usage including
  `cache_read_input_tokens` (so we can verify caching is actually firing
  in production).

**Tests:** 14 unit tests across three classes:
- `TestRequestShape`: confirms model, adaptive thinking, effort, system
  caching, strict tool forcing, user message contents.
- `TestSchemaInvariants`: locks in strict-mode constraints (every property
  in `required`, no `additionalProperties`, verdict enum in sync with
  `Verdict` Python enum). Catches schema drift before it ships.
- `TestResponseParsing`: valid/invalid extraction, defensive raise on
  missing tool_use block (the "refusal" stop reason case), parser picks
  the right tool_use if multiple are present.

**State:** 127 unit tests pass total (113 prior + 14 new). 100% line
coverage on `llm_validator.py`. Ruff + mypy strict clean. The validator
runs against the real API given an `AsyncAnthropic` instance; no real
calls in CI. Next piece: a composition entry point (`partition_filter` →
`llm_validator`) plus a fixture-driven integration test that actually
measures precision/recall on the labeled set against the real API.

**Errors:** Two issues during implementation surfaced cleanly via the
checks:
(1) First-draft system prompt was ~2,600 tokens, below Opus 4.7's
4,096-token cache floor. Caching would have silently failed
(`cache_read_input_tokens` stuck at 0). Fixed by expanding with genuine
content (the "Borderline cases" section).
(2) mypy strict mode rejected dict-literal kwargs on `messages.create`
because the SDK's TypedDict-union parameter types don't infer from
generic `dict[str, str]`. Fixed by typing each param via the SDK's
exported TypedDicts (`ThinkingConfigAdaptiveParam`, etc.).

## 2026-05-21 — Labeled partition validator fixture (100 cases)

**Context:** Built the ground-truth labeled set the Phase 2 partition
validator will be evaluated against. Lives at `tests/fixtures/partitions.yaml`
and contains 100 hand-curated bet pairs with `valid`/`invalid` labels and
edge-case category tags.

**Decisions:**
- Format: YAML list, one entry per case with `id`, `desc_a`, `desc_b`,
  `match_context`, `expected`, `reasoning`, `edge_case_category`. Matches
  the shape the `partition-test-case-generator` skill defines.
- Added `pyyaml` and `types-pyyaml` to dev deps for loading and type
  checking. Justification: format is dictated by the skill and the
  fixture is consumed by tests; a JSON or TOML rewrite would lose
  readability with no benefit.
- Distribution: 58 valid / 42 invalid across 39 categories. Skewed valid
  because many real bet markets are clean binaries (BTTS, half-line O/U,
  clean sheet, anytime scorer); the invalids cover the trap shapes
  exhaustively.
- Critical traps explicitly represented and enforced by
  `test_critical_traps_are_covered`: win/loss-without-draw in both
  knockout and non-knockout contexts, integer over/under push, AH
  overlap / push / quarter-line, double-chance overlap, goalscorer
  overlap, scope mismatch (HT vs FT, leg vs tie).
- Reasoning field is mandatory and non-empty — when a future labeling
  decision is questioned we need the rationale next to the label.

**State:** 85 unit tests pass (76 arbitrage + 9 fixture-integrity checks).
Ruff + mypy strict clean across the new module. Fixture is ready to feed
the partition validator once it exists. Next slice is the validator itself
in `src/semantic/`.

**Errors:** First attempt to build this fixture was cut off mid-stream by
an upstream API error before any file was written; recovered by checking
disk state explicitly, then writing the whole fixture in a single pass.
The lesson: when a turn ends mid-task, verify on disk before claiming the
work is done.

## 2026-05-21 — Edge case test pass + NaN/Infinity hardening

**Context:** Comprehensive edge case sweep across `dutch_book.py` and
`stake_allocator.py`. Test count went from 46 to 76. Found and fixed two
real bugs in the process.

**Decisions:**
- Reject non-finite inputs (`NaN`, `±inf`) at the `detect_arbitrage`
  boundary. Without these guards, `NaN` odds silently propagated through
  the math and produced an `ArbitrageOpportunity` whose fields were all
  `NaN`; `inf` odds collapsed to `1/inf = 0` in the overround and then
  generated `NaN` payouts via `0 * inf`. Both are upstream-corruption cases
  that should fail loudly.
- Reject non-positive `max_stake` and negative `min_stake` (previously
  unvalidated). Negative `max_stake` was particularly nasty — it produced a
  negative scale factor and yielded negative stakes downstream.
- Kept validation in `detect_arbitrage` rather than moving it to
  `OddsQuote.__post_init__`. Considered the refactor; rejected on the
  grounds that the existing pattern is already consistent and moving it
  would force test restructuring across two files for no behavioral gain.

**State:** 76 unit tests; 100% line coverage on all three arbitrage modules;
ruff + mypy strict clean. New test classes cover: numerical boundaries
(overround just under/over 1.0), extreme budgets (1.0, 1e9), extreme odds
(symmetric 100.0), NaN/Inf inputs (raise), per-quote field validation
(negative max_stake, etc.), margin-exactly-at-threshold, ArbitrageOpportunity
hash/equality and leg-order preservation, input mutation safety, tuple
input, same-platform multi-leg, cap-exactly-at-optimum, all-legs-capped,
min_stake boundary (zero / exact / just-above), increment-fits-exactly,
5-way arb.

**Errors:** Initial run failed on five tests:
(1-4) four existing validation tests used `match="must be > 1.0"` /
`match="must be positive"` regexes that no longer hit the longer "finite
value" messages — relaxed both patterns;
(5) `test_legs_preserve_input_order` picked odds 2.10/2.05/2.20 whose
overround is ~1.42 (not an arb at all) — replaced with the known-good
2.60/3.50/3.20 trio.

## 2026-05-21 — Extract stake allocator; reject "max capital utilization"

**Context:** Split allocation out of `dutch_book.py` into
`src/arbitrage/stake_allocator.py`, and made an explicit, documented decision
to reject the "more capital-efficient" variant that was floated.

**Decisions:**
- New module `src/arbitrage/quotes.py` owns `OddsQuote`. `dutch_book.py`
  re-exports it (`__all__`) so existing callers/tests keep working. This
  breaks the would-be circular import between the detector and the allocator
  without resorting to a TYPE_CHECKING dance.
- `stake_allocator.allocate_maxmin(quotes, budget)` is the only allocation
  strategy. It performs equal-payout → proportional cap scale-down → floor to
  `stake_increment` → `min_stake` gate. Returns None when placement is
  infeasible; raises nothing (precondition checks live in the detector).
- `dutch_book.detect_arbitrage` is now thin: validation, overround/margin
  gate, delegate to allocator, profit and realized-ROI accounting, package
  `ArbitrageOpportunity`.
- **Rejected:** an "asymmetric / max capital utilization" allocator that
  clamps capped legs and pushes uncapped legs further while keeping every
  outcome profitable. Worked through the math and it does not actually
  improve anything we care about — proportional scale-down already maximizes
  worst-case profit; any allocation that uses more capital does so by
  lowering the worst-case profit (and, at the limit, by making one outcome
  break-even, i.e. no longer a strict arb). Documented this rationale at the
  top of `stake_allocator.py` so the question doesn't get re-litigated.

**State:** 46 unit tests, 100% line coverage across `dutch_book.py`,
`stake_allocator.py`, and `quotes.py`. Ruff + mypy strict clean. Next slice
remains GARCH-adaptive thresholds inside `src/arbitrage/`, or the semantic
partition validator.

**Errors:** None during this refactor. Working through the math for the
"capital-efficient" variant took most of the time but produced the design
note rather than code.

## 2026-05-21 — N-way dutch_book detector, breaking API change

**Context:** Fleshed out `src/arbitrage/dutch_book.py` from the 2-way scaffold
into the production-shaped N-way detector that will sit under the partition
validator and the risk layer.

**Decisions:**
- `detect_arbitrage` now takes `Sequence[OddsQuote]` of length ≥ 2 instead of
  two positional args. Required for 3-way 1X2 soccer markets (the dominant
  Argentine shape) and trivially extends to 4+ for asian-handicap edge cases.
- `OddsQuote` gained `min_stake` and `stake_increment` (both optional). The
  detector rounds each leg's stake DOWN to its increment so we never blow past
  `max_stake`, and rejects the arb if any post-rounding stake falls below
  `min_stake` — the bet is unplaceable; no proportional rescue preserves the
  hedge.
- `ArbitrageOpportunity` now carries `legs`/`stakes` tuples (in place of
  `leg_a`/`leg_b`/`stake_a`/`stake_b`) plus two new fields:
  - `realized_roi_pct`: actual return on deployed capital after caps and
    rounding. `min_margin_pct` is re-checked against this, not just the
    theoretical margin, because coarse rounding can flip a viable arb
    negative.
  - `capital_utilization`: `total_stake / budget`. Drops below 1 when a
    liquidity cap binds (the only reason proportional scaling fires).
- Liquidity scaling now finds the single tightest binding cap and scales all
  legs once, rather than iterating per leg. Same result, simpler invariant.
- `guaranteed_profit` is computed from `min(payouts) - total_stake` so it is
  correct after rounding breaks the equal-payout property.

**State:** Phase 1 arbitrage core is done. 30 unit tests, 100% line coverage
on `dutch_book.py`. Ruff + mypy strict clean. Nothing else in `src/arbitrage/`
yet; the GARCH-adaptive threshold work the architecture doc mentions is the
natural next slice. The semantic layer (partition validator) and the storage
models that feed `OddsQuote` are still empty.

**Errors:** Initial test expectations had two arithmetic mistakes:
(1) miscomputed 3-way 1X2 margin (1.72% actual vs 2.2% asserted); (2) picked
symmetric odds where the optimal stake landed on a clean multiple of every
increment, so the "rounding erodes ROI" tests showed no rounding loss.
Both fixed by recomputing or by switching to asymmetric odds where rounding
actually bites.
