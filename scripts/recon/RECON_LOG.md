# Recon Log

Hand-curated notes from recon sessions. Each entry distills what was
learned from the raw artifacts in `recon/artifacts/<platform>/<session>/`
so future scraper work doesn't require re-reading every HAR.

Add a new entry per session at the top. Keep each entry short; link to
the raw artifacts for detail.

---

### 2026-06-02 — ALL platforms — bet PLACEMENT contracts (real placements captured)

One tiny real bet placed per platform (login-first `--interactive`; HARs
sanitized). All four place over **HTTP** (Betsson's Diffusion WS is odds-only).
Confirmation parsers built + tested in `src/execution/placers.py`.

| Platform | Place endpoint | Request shape (key fields) | Confirmation (success markers) | State needed |
|---|---|---|---|---|
| **betano** | `POST /api/betslip/v3/place` | `{betslip:{hash, slipData, legs:[{eventId, tag, …, amount}]}}` | `data.accepted==true`, `data.receipts[0].{betId,totalAmount,totalOdds}` | **hash** from prior `getbetslip`/`updatebets` (slip must be built) |
| **bplay** | `POST ws-deportespba.bplay.bet.ar/bettingslip` | `{context:{…}, data:{data:{betslip:{stake:{<outcomeId>:n}, accept:true}}, csrf_token}}` | `return=="OK"` + `message.type=="success"` ("Apuesta colocada"); `player.balance` updated | **csrf_token** + slip built (togglebet/update) |
| **betwarrior** | `POST .../coupon.json` (Kambi) | `{couponRows:[{index,odds(×100),outcomeId,type:"SIMPLE"}], bets:[{couponRowIndexes,stake(×1000)}], requestId:<uuid>, channel:"WEB"}` | `status=="SUCCESS"`, `couponRef`, `coupon.bets[0].betRef` | **Authorization Bearer** (Kambi session); ~stateless POST |
| **betsson** | `POST /api/sb/v2/coupons` (OBG) | `{bets:[{stake, oddsFormat:1, currencyCode:"ARS", betSelections:[{marketSelectionId, odds}]}], acceptOddsChanges:true}` | `couponStatus.couponStatusPollingResult=="Success"` + empty `couponPlacementErrors`, `couponId` | OBG headers (brandid/marketcode/x-sb-*) + session; ~stateless POST |

**Notes:** Betsson + BetWarrior are ~stateless single POSTs (post the selection
refs + stake directly). Betano + Bplay are stateful — the place request carries
a `hash`/`csrf_token` from a prior slip-build call, so their LegPlacer must run
the slip sequence first. Units differ: Kambi odds ×100 / stake ×1000; Betano +
Betsson use decimal odds + ARS stake (the parsers normalise). Artifacts:
`recon/artifacts/{betano/20260602-211506, bplay/20260602-210151,
betwarrior/20260602-210806, betsson/20260602-213017}/`.

---

### 2026-05-30 — betsson — Diffusion live feed DECODED (zlib + CBOR; 1X2 odds extracted)

**Goal:** decode the Betsson live WebSocket frames captured on 2026-05-29
so we can build an in-play scraper like the other platforms.

**Protocol cracked** (from the 3,404 captured frames,
`recon/artifacts/betsson/20260529-194942/websocket_frames.jsonl`):
- Two server frame types dominate: `0x00` = topic **specifications**
  (path + properties: `_CREATOR`, `PUBLISH_VALUES_ONLY true`, `REMOVAL
  "when no updates for 10m"`), `0x84` = topic **values**.
- A `0x84` value frame is: `84` + a short header + a **zlib** stream
  (`78 01 …`). Decompress → **CBOR**. Because `PUBLISH_VALUES_ONLY=true`,
  every frame is a FULL value — no binary-delta application needed.
- CBOR shapes: `t==32` = fixture/event messages; **`t==27` = market
  messages**: `{id, t:27, d:{ei, mti, odds}}`.
- `d.mti` is the market code — **same codes as the prematch accordion**
  (`MW3W`=1X2, `MTG2W`, `BTTS`, `DC`, …). `d.odds` maps selection id →
  `{"of":{"1":"<decimal>","2":"<american>"}, "sof":{<segmentUUID>:…}}`.
  **`of["1"]` is the decimal price**; `sof` is per-customer-segment
  pricing (ignore for arb).
- **The 1X2 selection ids end in `-home`/`-draw`/`-away`** (identical to
  the accordion), so outcomes need no external mapping. Verified live:
  Nice vs St-Étienne MW3W = home 2.55 / draw 2.25 / away 3.90.

**Built:** `src/ingestion/scrapers/betsson_diffusion.py` — pure decoder
(`decode_value_frame`, `market_1x2_odds`), tested against a real captured
frame (`tests/fixtures/betsson_diffusion_mw3w.b64`). Deps: `cbor2`.

**Open / next:** the live WS **subscriber** (the Diffusion connect
handshake + the `obg/gossip/subscribe` frames + keepalive/reconnect)
feeding this decoder. The 3 subscribe frames are captured; the connect
handshake (URL `?ty=WB&v=28&…` + the server's session-token first frame)
and keepalive still need live iteration to nail. That's the remaining
build for `betsson_ws.py`.

---

### 2026-05-29 — betsson — LIVE (in-play) odds transport: **Diffusion WebSocket pub/sub** (captured)

**Goal:** find Betsson's in-play odds source. The HTTP `accordion/v1`
widget is prematch-only — it returns `{"data":{}}` once a match goes
live (confirmed during the 2026-05-29 cross-platform test). Live odds
were known to be pushed (the prematch `event/v2` response carries a
`topics` array of `?obg/sportsbook/transient/...` channels) but never
captured, because the recon harness only logged HTTP + a HAR and the HAR
does not record WebSocket frames.

**Method:** added WebSocket frame capture to `scripts/recon/recon.py`
(`websocket_frames.jsonl`), then ran a headed recon deep-linked to a
**live** match — Ligue 1 Nice vs Saint-Étienne, event
`f-rdwm7m-uK0yqVgGGcW5KIg`. Artifacts:
`recon/artifacts/betsson/20260529-194942/`.

**Findings:**
- **Transport = Diffusion** (Push Technology's pub/sub platform), at
  `wss://pba.betsson.bet.ar/diffusion?ty=WB&v=28&...`. Captured 3,404
  frames in the dwell window (3,401 recv / 3 sent).
- **Subscription protocol** (the 3 sent frames — `obg/gossip/subscribe`):
  to receive an event's live odds, subscribe to these Diffusion topics:
  - `?obg/sportsbook/transient/markets/<eventId>/` — market/odds updates
  - `?obg/sportsbook/transient/events/<eventId>/` — event data
  - `?obg/sportsbook/transient/events/.*/fixture/phase` — match clock/phase
- **Frame format:** Diffusion binary wire protocol. A topic-registration
  frame (partly ASCII-readable) carries the topic path + metadata; then
  odds arrive as **binary deltas keyed by a numeric topic ID** (opaque
  without implementing the Diffusion delta/CBOR decoding + topic-ID map).
  6 of 3,404 frames were plain text (control/handshake).

**Open / next:**
- Decode the Diffusion delta format → live odds values. Options: a
  Diffusion JS/Python client library, or reverse-engineer the binary
  deltas from the captured frames. This is the substantial follow-up.
- Then add a Betsson live scraper (likely a WS subscriber, cf.
  `bplay_sse.py` for the realtime-feed pattern), keeping the existing
  prematch `accordion/v1` scraper for not-yet-live fixtures.
- Harness fix made this session: loggers now guard against a closed
  stream (a late event during `context.close()` was crashing teardown).

---

### 2026-05-26 — bplay — domestic LIVE odds transport: **SSE, not WebSocket** (protocol decoded)

**Goal:** the prior recon ("ws-deportespba.bplay.bet.ar exists →
WebSocket scraper needed for Argentine domestic") turned out to
be partially wrong. The user requested a WebSocket scraper; this
session decoded the actual transport.

**Key correction to the prior recon log:** `ws-deportespba.bplay.bet.ar`
is the **bet-slip operations** subdomain (`/bettingslip/save`,
`/bettingslip/accept`, `/bettingslip/delete`, etc. — all OUTGOING
bet placement, not incoming odds streams). The previous recon
misread this as the odds-stream endpoint.

**Actual transport for live in-play odds: Server-Sent Events (SSE)**
on a different subdomain `events-deportespba.bplay.bet.ar/live`.
Discovered via grep of the Nuxt JS bundle (`20485eb.js`) for
`EventSource` — three handlers registered:
`addEventListener("match")`, `addEventListener("odds")`,
`addEventListener("status")`.

**Endpoint:**

```
https://events-deportespba.bplay.bet.ar/live
  ?mode=v2
  &partner=1147           # Bplay's SportNCO partner ID
  &id=<A>|<B>|<C>|...     # pipe-separated matchIds (subscription set)
  &main=                  # empty (or per-match value when on match-detail page)
  &lang=ag                # Argentine Spanish
  &odds_format=dec        # decimal odds
```

Headers required: `Accept: text/event-stream`, `Origin:
https://deportespba.bplay.bet.ar`, `Referer:
https://deportespba.bplay.bet.ar/...`. No cookies, no auth tokens.

**Subscription ID discovery:**
- IDs are NOT competition IDs (like 43411 Primera Nacional) and
  NOT the eventLiveId from URL slugs (11526457). The right ID is
  `matchId` — a separate inner ID embedded in the page's SSR
  data as `matchId:"<id>"`.
- The `/en-vivo` page embeds matchIds for ALL currently live
  matches (e.g. 13925270, 13925271 — observed 2 soccer matches
  live during the recon). Competition pages (e.g.
  `/competicion/43414-argentina-reservas`) embed matchIds only
  for that competition's currently-live matches.

**Three event types observed in 25s capture:**

| Type | Frequency | Purpose |
|---|---|---|
| `match` | 16 | Match metadata (teams, score, status, time, cards) |
| `odds` | 16 | Odds updates (the gold) |
| `status` | 16 | Status changes (mostly mirrors `match`'s status field) |

**`odds` event payload shape** (abbreviated field names):

```json
{
  "match_id": "13925270",
  "odds": [
    {
      "qt": "¿Quién ganará el partido?",  // market label (Spanish)
      "qlid": 2133000,                      // canonical market type ID
      "nbl": 3,                              // num outcomes (= bet legs)
      "bets": [
        {
          "od": ..., "pid": "...", "ha": "...",
          "tch": {
            "c1": {"cid": "1", "ct": 1.85, "ct_dsp": "1.85", "act": "Real Cundinamarca", "td": "", "id": "<base64>"},
            "c2": {"cid": "X", "ct": 3.40, ...},
            "c3": {"cid": "2", "ct": 4.20, ...}
          }
        }
      ]
    }, ...
  ]
}
```

Fields decoded:
- `qt` — market label in Spanish
- `qlid` — stable market type ID (1X2 = 2133000, BTTS = 2133023, etc.)
- `tch.c<N>` — outcome cells, each with:
  - `cid` — outcome code ("1" / "X" / "2", "Sí" / "No", "Más" / "Menos")
  - `ct` — decimal odds (numeric)
  - `act` — outcome label (Spanish team name or descriptive text)
  - `id` — base64-encoded ID for bet placement

**Market catalog observed (36 distinct `qt` values in 25s):**

| `qt` (Spanish label) | Type | qlid |
|---|---|---|
| **`¿Quién ganará el partido?`** | **1X2 (v1 scope)** | 2133000 |
| **`Ambos equipos marcan`** | **BTTS (v1 scope)** | 2133023 |
| **`Total de Goles`** | **OU goals (v1 scope, line in outcome `act` like "Más de 2.5")** | 2133446 |
| `Doble oportunidad` | Double Chance | 2133007 |
| `Ganador (empate anula apuesta)` | Draw No Bet | 2133008 |
| `Hándicaps` / `Hándicaps (2 opciones)` | Asian Handicap | 2133011 / 2133013 |
| `Resultado Correcto` | Correct Score | 2133034 |
| ...plus 28 other markets (combos, half-time, team-totals, etc.) | — | — |

Note: SSE's OU has line in the outcome `act` (`"Más de 2.5"`),
NOT in the market `qt` (which is plain `"Total de Goles"`). Same
shape as Betsson HTTP. The XML feed (different transport) embeds
the line in the market name — so two different formats per
Bplay scraper.

**Other findings:**

- **No authentication.** Plain curl with the recon-correct headers
  works. Anti-bot only gates the SPA shell, same as the XML feed.
- **Anti-bot status on the SPA:** the persistent Playwright profile
  remains fingerprinted (returned "Request Rejected" even on a
  fresh profile dir). Fresh profile attempts in this session also
  blocked. Verdict: Playwright-based SPA recon on Bplay is no
  longer reliable; future Bplay recon should use plain curl +
  grep-the-bundle, which works fine.
- **Coverage scope:** SSE only pushes for matches currently LIVE
  (in-play). Pre-match scheduled fixtures are NOT pushed. This
  complements the XML feed's pre-match-only coverage of marquee
  tournament competitions.

**Implementation plan implied:**

1. **Discovery:** periodically (every ~60s) GET `/en-vivo` HTML,
   grep `matchId:"X"` to get current live-match list. Also probe
   target-competition pages for completeness.
2. **Subscription:** open SSE with `id=<A>|<B>|...&main=` for the
   discovered set. Reconnect when the set changes.
3. **Parse:** stream lines, accumulate event blocks separated by
   blank lines. On `event: odds`, extract markets matching v1
   `qt` filter, emit `RawOddsSnapshot` per outcome.
4. **Platform-name re-use:** snapshots tagged `platform=bplay-pba`
   like the XML scraper. The canonicalizer treats them uniformly
   via the existing `(platform, platform_event_id)` cache. The
   matchId space doesn't collide with XML's `<Match id="...">`
   space, so deduplication via team-name fuzzy match in
   `fixture_resolver` handles any overlap.

**Artifacts:**
- `recon/artifacts/bplay/sse-181807/sse_capture.txt` — 25 s,
  608 KB capture of two live matches (Deportes Pereira /
  Jaguares de Córdoba, Real Cundinamarca / Rionegro Águilas —
  Copa Colombia live matches at the time of recon).

---

### 2026-05-26 22:30 UTC — bplay — Argentine domestic coverage re-investigation (definitive)

**Goal:** verify whether Bplay PBA's `/oddsfeeds/odds-competition<ID>.xml`
endpoint pattern can be coaxed into serving Argentine domestic
fixtures (Primera Nacional, Copa Argentina, Reservas). The
prior recon log noted these IDs returned 404 but the SPA does
render those competition pages — investigate whether we missed
an endpoint pattern.

**Method:** plain curl, no Playwright (the persistent profile got
fingerprinted partway through the session and started returning
"Request Rejected" on every navigation). The XML and JSON data
endpoints are NOT gated by the SPA WAF and answer cleanly.

**Findings:**

1. **Complete XML endpoint coverage map across every competition
   ID exposed in the SPA's main nav:**

| ID | Competition | XML pattern |
|----|-------------|-------------|
| 6674 | UEFA Champions League | **200 (5 KB)** |
| 36146 | Copa Libertadores | **200 (32 KB)** |
| 36148 | Copa Sudamericana | **200 (30 KB)** |
| 63057 | Copa Mundial 2026 | **200 (64 KB)** |
| **42958** | **UEFA Conference League** | **200 (5 KB)** ← MISSED |
| 81 | Brasileirão | 404 |
| 5 | Major League Soccer | 404 |
| 206 | NBA | (basketball — not soccer) |
| **43411** | **Primera Nacional (AR)** | **404** |
| **1493** | **Copa Argentina (AR)** | **404** |
| **43414** | **Argentina-Reservas (AR)** | **404** |

2. **Alternative endpoint patterns probed against domestic IDs:**
   - `/oddsfeeds/odds-categoria<ID>.xml`
   - `/oddsfeeds/odds-region<ID>.xml`
   - `/oddsfeeds/competition-<ID>.xml`
   - `/oddsfeeds/odds-<ID>.xml`
   - `/api/odds/competition/<ID>.xml`
   - `/api/competition/<ID>.json`
   - `/oddsfeeds/odds-event<ID>.xml` (against a known live Argentine event)
   - `/api/event/<ID>`, `/api/match/<ID>`
   - `/oddsfeeds/{categories,sports,feeds}.json`

   **All 404. No alternative HTTP endpoint serves domestic odds.**

3. **WebSocket subdomain `ws-deportespba.bplay.bet.ar` is alive**
   (HTTP probe returns 404 on `/` with Cloudflare headers — the
   server exists but rejects HTTP requests by design). This is
   consistent with the prior recon note: domestic leagues flow
   via WebSocket.

4. **The marketing copy in the SPA explicitly confirms the
   architectural split:** "podrás elegir entre todas las ligas
   del ascenso, la Liga Profesional de Fútbol o Copa Argentina.
   También podés apostar en las ligas europeas: Premier League,
   LaLiga, Serie A, Bundesliga o Ligue 1." So Bplay PBA's
   commercial offering DOES include Argentine domestic + European
   top-5 — they're just served via WebSocket, not the XML feed.

**Operational implication:**

- Quick win: add `42958 UEFA Conference League` to the scraper's
  `TARGET_COMPETITIONS` (one-line change shipped this session).
- Larger work: Argentine domestic coverage requires a WebSocket
  scraper. Architecture sketch — connect to
  `wss://ws-deportespba.bplay.bet.ar/`, subscribe to a domestic
  category channel (protocol unknown — would need a second recon
  pass with the Playwright profile refreshed), translate push
  frames into `RawOddsSnapshot`s with the same SportNCO field
  conventions the XML feed uses. ~3–5 days work.

**Bandwidth/cost-benefit of WebSocket path:** WebSocket is push-
based (lower polling overhead) but requires persistent connection
management + reconnect logic + initial-state replay handling.
For now, deferred — the BetWarrior depth scraper (this session)
adds enough cross-platform coverage on the existing fixtures to
keep the architecture exercised.

**Artifacts:** none new (the Playwright session got blocked); the
endpoint probe data lives in this recon entry directly.

---

### 2026-05-26 22:50 UTC — betwarrior — depth endpoint schema for BTTS + OU goals

**Goal:** confirm Kambi's `betoffer/event/<id>.json` schema for
BTTS and OU markets in es_AR so the depth scraper's resolver
crosswalks match real strings.

**Method:** curl with `Origin`+`Referer` headers, no auth needed
(same access pattern as the list-view endpoint). Probed across
four live events in different competitions (argentina,
copa_libertadores, copa_sudamericana, champions_league).

**Confirmed schema — BTTS:**

```json
{
  "id": 9001,
  "criterion": {
    "label": "Ambos Equipos Marcarán",
    "englishLabel": "Both Teams To Score"
  },
  "betOfferType": {"englishName": "Yes/No", "name": "Si/No"},
  "outcomes": [
    {"id": ..., "label": "Sí", "englishLabel": "Yes",
     "type": "OT_YES", "odds": 2040, "status": "OPEN"},
    {"id": ..., "label": "No", "englishLabel": "No",
     "type": "OT_NO", "odds": 1680, "status": "OPEN"}
  ]
}
```

Note: present-tense `"Marcarán"` (future) is the canonical Kambi
form, differing from Betsson's `"Ambos equipos anotan"` (present).
Resolver matches the NFKD-folded form `"ambos equipos marcaran"`.

Kambi also exposes `"Ambos Equipos Marcarán - 1.ª parte"` (first-
half BTTS) and various `"Victoria de X y ambos equipos marcan"`
combo bets. The depth scraper requires EXACT match on
`criterion.label`, so half-time and combo variants are dropped.

**Confirmed schema — OU goals:**

```json
{
  "id": 9002,
  "criterion": {
    "label": "Total de goles",
    "englishLabel": "Total Goals"
  },
  "betOfferType": {"englishName": "Over/Under", "name": "Más/Menos de"},
  "outcomes": [
    {"id": ..., "label": "Más de", "englishLabel": "Over",
     "type": "OT_OVER", "line": 2500, "odds": 1900, "status": "OPEN"},
    {"id": ..., "label": "Menos de", "englishLabel": "Under",
     "type": "OT_UNDER", "line": 2500, "odds": 1950, "status": "OPEN"}
  ]
}
```

**Critical Kambi convention: `outcome.line` is integer-scaled by
1000** (same scale as odds). `line=2500` means decimal 2.5. The
line lives in the OUTCOME, not in `criterion.label` — different
from Betsson (`"Total de goles 2.5"`) and Bplay (`"Más de /
Menos de 2.5"`) where the line is in the market name.

For symmetry across platforms, the depth scraper constructs
`raw_market_name = "Total de goles 2.5"` (Betsson-style) so the
existing `_OU_PATTERNS_BY_PLATFORM` regex pipeline works — one
new platform regex entry rather than a new code path.

**One betOffer per OU line.** Lines 0.5, 1.5, 2.5, 3.5, 4.5, 5.5
all observed as separate betOffers. Integer lines (1.0, 2.0,
3.0, 4.0) — the push lines — NOT observed on these events but
the depth scraper filters them defensively anyway.

**Number of betoffers per event:** ~470 (matches the prior
estimate). After filtering to BTTS + OU half-lines only, the
typical event yields ~12 snapshots (1 BTTS market × 2 outcomes +
~5 OU half-lines × 2 outcomes).

---

### 2026-05-26 20:31 UTC — betwarrior PBA — viability: HIGH (Kambi backend, plain httpx, full domestic coverage)

**Goal:** evaluate `pba.betwarrior.bet.ar` as the third PBA scraper
after Betsson and Bplay. BetWarrior was flagged PBA-licensed in the
earlier multi-platform sweep; this session confirms the license,
identifies the backend, and maps the odds API surface.

**Session:** `recon/artifacts/betwarrior/20260526-173113/`

**Backend identified: Kambi.** Confirmed via three independent signals:
fingerprints in the Next.js SSR HTML (`Kambi`, `KAMBI`,
`fetchApiPlatform`/`fetchApiVersion` config keys), the recon's
`requests.jsonl` showing 38 requests to `eu.offering-api.kambicdn.com`,
and the Kambi brand-ID `bwargbap` (BetWarrior Argentina BA Province)
appearing in every offering URL.

**Architecture:**

- **Sportsbook subdomain:** `pba.betwarrior.bet.ar` — Express app
  (`x-powered-by: Express`) behind CloudFront (`via: ...cloudfront.net`),
  itself behind Cloudflare. Redirects `/` → `/es-ar`, then
  `/es-ar/sports/home` for the sportsbook landing. ~436 KB SSR shell.
- **Marketing/casino root:** `betwarrior.bet.ar` — Next.js. Cloudflare +
  AWS ALB cookies. Irrelevant to scraping; bookmarked for context.
- **Legacy alias:** `www.betwarrior.com.ar` → 301 → `betwarrior.bet.ar`.
- **Two CMS layers stacked on top of Kambi:**
  - `workload.shapegamescloud.com` (Shapegames, Danish iGaming platform
    orchestration) — branding, layouts, bonuses, content pages. Irrelevant
    to odds.
  - `eu.offering-api.kambicdn.com` — **the actual odds backend.**
- **Cloudflare gates the SPA shell, NOT the data feeds.** Same pattern
  as Betsson PBA, Codere AR, Bplay PBA. Direct httpx hits the Kambi
  endpoints returning HTTP 200 JSON.
- **PBA jurisdiction wiring confirmed:** SSR HTML contains
  `IPLyC`, `Provincia de Buenos Aires`, `jurisdiccion`. Title on the
  sportsbook section: "BetWarrior Province".

**Kambi odds API surface (Brand: `bwargbap`):**

All under `https://eu.offering-api.kambicdn.com/offering/v2018/bwargbap/`,
all return `application/json`, plain httpx works:

| Endpoint                                                 | Bytes (probed) | What it returns                                            |
|----------------------------------------------------------|----------------|------------------------------------------------------------|
| `category/combined_layout,list_view/sport/FOOTBALL.json` | 3.7 KB         | Sport tree (categories + counts)                           |
| `group.json` *(with params)*                             | 7.5 KB         | Full path tree; lists every competition under each sport   |
| `group/highlight.json`                                   | 7.5 KB         | Featured competitions                                      |
| `listView/football/<slug>/all/all/matches.json`          | 31 KB          | All matches in competition + their PRIMARY 1X2 betoffer    |
| `betoffer/event/<event_id>.json`                         | **434 KB**     | All ~470 markets for one event (full depth)                |
| `event/live/open.json`                                   | 216 KB         | All live events index                                      |
| `coupon/<coupon_id>.json`, `prepackcoupon/event/...`     | varies         | Pre-built combined bets (irrelevant for arb MVP)           |
| `oddsLadder`                                             | 11 KB          | Kambi canonical decimal-odds ladder (reference data)       |
| `betoffer/outcome.json`                                  | (POST?)        | Push-style refresh of named outcome IDs (not yet probed)   |

**Required request shape (verified):**
- Method: `GET`
- Headers: `Origin: https://pba.betwarrior.bet.ar`, `Referer:
  https://pba.betwarrior.bet.ar/` (User-Agent recommended as good citizen).
  **No Kambi-specific tokens, no jurisdiction header, no auth.**
- Query params: `?lang=es_AR&market=AR&client_id=2&channel_id=1&ncid=<rand>`
  - `lang` is REQUIRED for `listView/.../matches.json` (bare returns 400).
  - Other endpoints work bare too, but the params are cheap and the
    SPA always sends them — keep them in the scraper request shape.

**JSON schema for matches.json (the scraper's main loop endpoint):**

```json
{
  "events": [
    {
      "event": {
        "id": 1027027525,
        "name": "LDU Quito - Always Ready",
        "homeName": "LDU Quito",
        "awayName": "Always Ready",
        "start": "2026-05-26T22:00:00Z",
        "state": "NOT_STARTED",
        "group": "Copa Libertadores",
        "path": [
          {"termKey": "football", "name": "Fútbol"},
          {"termKey": "copa_libertadores", "name": "Copa Libertadores"}
        ],
        "sport": "FOOTBALL",
        "tags": ["OFFERED_LIVE", "BET_BUILDER", "MATCH"]
      },
      "betOffers": [
        {
          "id": 2649147284,
          "criterion": {"label": "Resultado Final"},
          "betOfferType": {"englishName": "Match"},
          "outcomes": [
            {"id": ..., "label": "1", "odds": 1290, "status": "OPEN"},
            {"id": ..., "label": "X", "odds": 5600, "status": "OPEN"},
            {"id": ..., "label": "2", "odds": 9000, "status": "OPEN"}
          ]
        }
      ]
    }
  ]
}
```

**Three things to know about Kambi odds:**

1. **Integer-scaled by 1000.** `odds: 1290` ⇒ decimal 1.29. Easy
   gotcha. Divide by 1000 on emit. Confirmed against a strong-favorite
   matchup (LDU Quito 1.29 / X 5.6 / Always Ready 9.0 — plausible).
2. **`status: "OPEN"`** filters live offers — suspended outcomes carry
   a different status. Scraper must skip non-OPEN.
3. **List-view returns ONLY the "Match" (1X2) betoffer per event.**
   For deeper markets (Asian Handicap, Over/Under goals, BTTS) the
   scraper must call `betoffer/event/<id>.json` per event — that
   endpoint returned ~470 betoffers for one Libertadores event (player
   props from Opta, AH ladder, O/U at many lines, corners, cards).

**Competition slug inventory (from `group.json`, `eventCount`):**

| `termKey`                         | `name`                       | events |
|-----------------------------------|------------------------------|--------|
| `argentina`                       | Argentina                    | **159** |
| `international_friendly_matches`  | Amistosos Internacionales    | 102    |
| `esports_football`                | Esports Fútbol               | 70     |
| `spain`                           | España                       | 29     |
| `copa_libertadores`               | Copa Libertadores            | 28     |
| `copa_sudamericana`               | Copa Sudamericana            | 27     |
| `england`                         | Inglaterra                   | 26     |
| `italy`                           | Italia                       | 14     |
| `france`                          | Francia                      | 8      |
| `germany`                         | Alemania                     | 6      |
| `champions_league`                | Champions League             | 5      |
| `conference_league`               | Conference League            | 2      |

`copa_mundial` / World Cup didn't appear in `group.json` this session —
either named differently here or no live offering yet. Not blocking.

**Coverage diff vs current scrapers:**

| Competition          | Betsson PBA | Bplay PBA       | BetWarrior PBA  |
|----------------------|-------------|-----------------|-----------------|
| UCL                  | yes         | yes             | yes             |
| Copa Libertadores    | yes         | yes             | yes (28 events) |
| Copa Sudamericana    | yes         | yes             | yes (27 events) |
| Argentine domestic   | yes         | **XML 404s**    | **yes (159)**   |
| Big-5 European       | yes         | not via XML     | yes (~83 total) |
| Copa Mundial         | yes         | yes (outrights) | not seen        |

**This is the value-add of BetWarrior:** it directly covers Argentine
domestic football via the same lightweight JSON endpoint as
everything else — closing the Bplay gap and giving us a real
two-platform arb surface (Betsson ⨯ BetWarrior) on the largest event
pool we have available. For UCL/Libertadores/Sudamericana it makes a
three-way arb surface (Betsson ⨯ Bplay ⨯ BetWarrior).

**Cost-benefit comparison:**

|                       | Betsson PBA                | Bplay PBA              | BetWarrior PBA              |
|-----------------------|----------------------------|------------------------|-----------------------------|
| Anti-bot wall         | 3 required headers         | none (XML feed open)   | none (open JSON API)        |
| Transport             | JSON                       | XML                    | JSON                        |
| Polling pattern       | per-fixture                | per-competition        | per-competition (list view) |
| Domestic coverage     | yes                        | **NO (XML 404)**       | yes (159 events)            |
| Market depth in MVP   | 1X2 + O/U + DNB + AH + BTTS| 1X2 + O/U + DNB + AH + BTTS | 1X2 (list view) — deeper requires per-event call |
| Parser work needed    | None                       | None                   | None (JSON + Kambi schema)  |
| Operational cost      | 1 process, low RAM         | 1 process, low RAM     | 1 process, low RAM          |
| Engineering effort    | ~1 day (done)              | ~1 day (done)          | **~1 day for MVP (list view) + ~1 day for per-event depth** |

**Scraper sketch (`src/ingestion/scrapers/betwarrior.py`):**

```python
class BetWarriorPbaScraper(BaseScraper):
    poll_interval_sec = 5.0
    backoff_seconds_on_error = 30.0
    platform_name = "betwarrior-pba"

    BASE = "https://eu.offering-api.kambicdn.com/offering/v2018/bwargbap"
    PARAMS = {"lang": "es_AR", "market": "AR", "client_id": 2, "channel_id": 1}

    # MVP target set — start narrow, expand once 1X2 arb is wired.
    TARGET_COMPETITIONS: dict[str, str] = {
        "argentina": "Argentina (domestic)",
        "copa_libertadores": "Copa Libertadores",
        "copa_sudamericana": "Copa Sudamericana",
        "champions_league": "Champions League",
    }

    async def fetch_live_soccer(self):
        for slug, label in self.TARGET_COMPETITIONS.items():
            data = await self._get_json(
                f"{self.BASE}/listView/football/{slug}/all/all/matches.json",
                params=self.PARAMS,
            )
            observed_at = time.time()
            for block in data.get("events", []):
                ev = block["event"]
                for offer in block.get("betOffers", []):
                    if offer["betOfferType"]["englishName"] != "Match":
                        continue  # list view only carries 1X2 anyway
                    market_id = str(offer["id"])
                    for outcome in offer["outcomes"]:
                        if outcome.get("status") != "OPEN":
                            continue
                        yield RawOddsSnapshot(
                            platform=self.platform_name,
                            platform_event_id=str(ev["id"]),
                            platform_market_id=market_id,
                            platform_outcome_id=str(outcome["id"]),
                            raw_event_name=ev["name"],
                            raw_market_name=offer["criterion"]["label"],
                            raw_outcome_name=outcome["label"],
                            decimal_odds=outcome["odds"] / 1000.0,
                            max_stake=None,
                            timestamp=observed_at,
                        )
```

Two-tier depth (later): for each event discovered in the list view,
schedule a follow-up `betoffer/event/{id}.json` poll filtering by
`betOfferType.englishName in {"Over/Under", "Asian Handicap",
"3-Way Handicap", ...}`. Cost is ~434 KB per event per cycle — would
need to throttle (e.g., poll only events kicking off within N hours).

**Viability verdict: HIGH.** Kambi's offering API is one of the most
predictable surfaces in sports betting — public, JSON, no auth, no
anti-bot wall on the data layer, stable schema across all Kambi
tenants. MVP scraper is the lightest of the three (no header-bisection
needed, no XML parser to write). The only real engineering decision is
how deep to go: list-view-only ships in a day and gives us 1X2 arb
across ~250 events daily; per-event depth doubles the effort but
unlocks AH/O/U/BTTS markets at full ladder resolution.

**Recommended next step:** ship the MVP (list-view 1X2 only),
wire into the daemon, validate against live odds for 24 hours,
then revisit per-event depth once the semantic-layer fixture-matching
work is in flight.

**Open questions for follow-up:**
- Confirm `copa_mundial` slug (World Cup) — re-probe `group.json`
  closer to tournament window.
- Sample `betoffer/event/<id>.json` for an Argentine-domestic fixture
  (currently no domestic match was open at recon time) to confirm
  market labels match the Spanish strings used in Bplay XML and
  Betsson JSON — relevant for the semantic-layer canonical-market
  matching.
- Probe `betoffer/outcome.json` shape — likely the push-refresh
  endpoint Kambi uses for delta updates. Could replace per-event
  full-payload polling later for bandwidth savings.

**Cross-platform implication:** three confirmed PBA backends now,
all on distinct white-label tech stacks:
- Betsson PBA → OBG (Betsson's proprietary B2B)
- Bplay PBA → SportNCO (French white-label)
- BetWarrior PBA → Kambi (Swedish/UK white-label)

Three independent pricing engines on the same fixtures = the
mathematical condition for sustained Dutch-book opportunities. With
all three ingesting we have the cross-platform substrate the
arbitrage layer was designed for. The remaining work is the semantic
layer (canonical fixture + canonical market matching across the
three feeds' string representations).

---

### 2026-05-26 20:15 UTC — bplay — domestic coverage follow-up (XML scoping decoded)

**Goal:** explain why Argentine-domestic competition IDs (1493 Copa
Argentina, 43411 Primera Nacional, 43414 Argentina-Reservas) all 404
on the XML feed pattern, despite domestic football being active.
Anchor: user noted River vs Belgrano Apertura final just passed.

**Sessions:**
- Argentina category page recon: `recon/artifacts/bplay/20260526-170123`
- Plus six follow-up `httpx` probes documented inline below.

**What we learned:**

1. **The XML feeds at `/oddsfeeds/odds-competition<ID>.xml` are a
   CURATED SUBSET, not a per-competition feed of all offerings.**
   Probed all 25 competitions exposed in the Nuxt SSR; only four
   returned XML:

   | ID    | Competition         | Feed   | Why it has one         |
   |-------|---------------------|--------|------------------------|
   | 6674  | UEFA Champions Lge  | 5 KB   | Marquee tournament     |
   | 36146 | Copa Libertadores   | 32 KB  | Marquee tournament     |
   | 36148 | Copa Sudamericana   | 30 KB  | Marquee tournament     |
   | 63057 | Copa Mundial 2026   | 64 KB  | Marquee tournament + outrights |

   Everything else (Primera Nacional 43411, Copa Argentina 1493,
   Reservas 43414, Copa de Brasil, Copa Colombia, Liga Pro, etc.)
   returns 404. The pattern is "tournament-style competitions
   with outrights" — domestic ongoing leagues don't get the
   pre-rendered XML treatment.

2. **The real navigation API is `POST /component/datatree`** on
   `ws-deportespba.bplay.bet.ar` (despite the "ws" prefix, this is
   HTTP POST, not a WebSocket). Request:
   ```json
   {"context": {
     "url_key": "/categoria/84-argentina",
     "lang": "ag",
     "device": "web_vuejs_desktop",
     "timezone": "America/Buenos_Aires",
     "version": "1.0.1", "url_params": {}, "clientIp": "..."
   }}
   ```
   Returns 175–260KB JSON: a recursive component tree (`Page` →
   `Layout` → ... → `EventList` / `EventLiveMarketList`) with TTL
   per component and inlined data.

3. **`EventList.data.events[]` carries match metadata, NOT odds.**
   Sample for an active Argentine fixture:
   ```json
   {
     "id": 11528713,
     "label": "Club Almirante Brown / CA San Miguel",
     "sport":       {"id": 13, "label": "Fútbol"},
     "category":    {"id": 84, "label": "Argentina"},
     "competition": {"id": 43414, "label": "Argentina - Reservas"},
     "ts_start": 1779811200,
     "state": "live",
     "match_id": "13930775",
     "event_prematch_id": 11528445,
     "actors": [
       {"id": 819650, "type": "home", "label": "Club Almirante Brown"},
       {"id": 582517, "type": "away", "label": "CA San Miguel"}
     ]
   }
   ```
   Multiple IDs per event: `id` (current), `match_id`,
   `event_prematch_id`, `event_live_id` — represent
   different lifecycle phases of the same fixture.

4. **`EventLiveMarketList` carries the event detail + filter
   categories but NOT the odds.** The filters (`1310 Destacados`,
   `1330 Goles`, `1355 Córners`, etc.) define which market
   groups the UI shows, but the actual market lines / offers /
   prices are not in the JSON response. The data must flow via a
   separate channel.

5. **WebSocket is the most likely channel** for the missing odds.
   The `scoreBoard.bridge_url` field (`ws-deportespba.bplay.bet.ar/
   app/lmt/<event_id>`) and BetGenius integration hints point at
   WS. Playwright HAR doesn't capture WS frames by default — that's
   why we never saw them. A targeted recon with
   `page.on("websocket")` callbacks would surface the frames.

**Architectural summary (revised):**

```
Bplay PBA architecture
├── deportespba.bplay.bet.ar (Nuxt SSR shell, Cloudflare-fronted)
│   ├── /oddsfeeds/odds-competition<ID>.xml ← pre-rendered XML
│   │       (only for marquee tournaments: Libertadores,
│   │        Sudamericana, UCL, World Cup. Has full offers
│   │        + outrights. Anonymous httpx works.)
│   └── /<any-page-url> ← initial Nuxt SSR HTML
├── ws-deportespba.bplay.bet.ar
│   ├── /component/datatree POST ← page composition + event
│   │       metadata (NOT odds). Anonymous httpx works.
│   ├── /app/lmt/<event_id> ← BetGenius LMT bridge (live tracker)
│   └── (presumed) wss://...     ← real-time odds feed
│           Not yet captured.
└── events-deportespba.bplay.bet.ar
    └── /live?... ← param-gated, returns 400 without right combo;
            visible reference in EventList.live_event_stream_url
```

**Use-case fit for our project:**

- **World Cup arb (user's stated long-term target): VIABLE NOW.**
  Competition 63057 has the XML feed pre-rendered. Plain httpx,
  no WebSocket needed.
- **Marquee club tournaments (Libertadores, Sudamericana, UCL):
  VIABLE NOW** via the same XML pattern. Useful for opportunistic
  arb during between-season gaps.
- **Argentine domestic league arb: would require WebSocket recon
  + parser.** The event-metadata path is already mapped via
  datatree; only the odds channel is left to crack. The user has
  explicitly de-prioritized this, so we don't need to do it now.

**Engineering implication for the Bplay scraper:**

Two distinct code paths:
1. **For XML-fed competitions** (curated tournament list): poll
   `/oddsfeeds/odds-competition<ID>.xml` per competition; parse
   with `xml.etree.ElementTree`; emit `RawOddsSnapshot`s. Same
   pattern as Betsson's per-fixture polling but per-competition
   instead. Effort: ~1 day.
2. **For domestic / non-XML competitions** (future, optional):
   subscribe to the WebSocket on `ws-deportespba`; parse SportNCO
   real-time protocol; emit snapshots as updates arrive. Effort:
   ~3-5 days, requires a WebSocket-aware recon pass first.

**Recommendation for next step:** ship the XML-feed-based scraper
covering the marquee tournaments (including World Cup). That gives
us Betsson PBA × Bplay PBA arbitrage on World Cup fixtures and the
major continental tournaments — meets the user's stated goal
without doing the harder WebSocket work first. The domestic-league
gap can be closed later if/when we want to widen coverage.

**Artifacts:** `recon/artifacts/bplay/20260526-170123/` (Argentina
category page).

---

### 2026-05-26 19:48 UTC — bplay — viability: high (Betsson-class, XML)

**Goal:** assess `pba.bplay.bet.ar` as a scraping target. Bplay was
flagged PBA-licensed in the multi-platform sweep earlier today; this
session confirms.

**Sessions:** `recon/artifacts/bplay/20260526-164300`

**Architecture decoded:**
- **Platform backend: SportNCO** (French sportsbook white-label) —
  confirmed via image URLs `sportx-static.sportnco.com/flag_*.png`
  embedded throughout the SPA. Bplay is one of many SportNCO
  operators; the recon-derived endpoint conventions should transfer
  to other SportNCO-backed brands.
- **Frontend: Nuxt.js SSR.** `deportespba.bplay.bet.ar/` returns a
  738KB SSR'd HTML page with all initial state inlined via
  `window.__NUXT__ = (function(...) {...})(...)` IIFE. Competition
  IDs and slugs are pre-rendered into the markup.
- **Cloudflare** in front of the SPA (`/cdn-cgi/rum?` calls visible),
  but — same pattern as Betsson and Codere — Cloudflare gates only
  the HTML shell, NOT the data feeds. Direct httpx returns 200 +
  XML on the odds endpoints.
- **Three distinct subdomains** for different concerns:
  - `pba.bplay.bet.ar` — marketing / casino root (Cloudflare shell)
  - `deportespba.bplay.bet.ar` — sportsbook SPA + XML odds feeds
  - `ws-deportespba.bplay.bet.ar` — WebSocket for live odds
- **`caba.bplay.bet.ar` ALSO resolved during recon** (11 incidental
  requests). Bplay has multi-province coverage despite my earlier
  probe missing the CABA subdomain. Worth confirming PBA = CABA
  odds parity in a separate session.

**Odds API surface: per-competition XML feeds.**

URL pattern: `deportespba.bplay.bet.ar/oddsfeeds/odds-competition<ID>.xml`

Confirmed competition IDs (from Nuxt SSR embed):

| ID    | Competition          | Feed status |
|-------|----------------------|-------------|
| 1493  | Copa Argentina       | 404 — no current offer |
| 43411 | Primera Nacional     | 404 — no current offer |
| 36146 | Copa Libertadores    | 200 — 32 KB, 9 matches |
| 36148 | Copa Sudamericana    | 200 — 30 KB |
| 63057 | Copa Mundial 2026    | 200 — 64 KB (outrights) |
| 6674  | UEFA Champions Lge   | (not probed) |
| 38537, 45732, 47972, 47979, 49159, etc. | (other competitions in SSR) | varies |

**Argentine domestic leagues (1493, 43411) currently return 404 on
the XML pattern.** Could be between-rounds / off-season / different
feed for the moment, OR they use a different competition-ID mapping
than the SSR embed exposes. Worth a re-probe when domestic football
is active — Betsson PBA was serving Argentine domestic content this
afternoon, so the leagues are live somewhere.

**XML schema** (clean, easy to parse with `xml.etree.ElementTree`):

```xml
<Data>
  <SportList>
    <Sport id="13" name="Fútbol">
      <RegionList>
        <Region id="141" name="América del Sur">
          <CompetitionList>
            <Competition id="36146" name="Copa Libertadores">
              <OutrightList>
                <Offer type_id="..." type_name="Apuesta sobre el evento"
                       event="Copa Libertadores - 2026 - Ganador"
                       date="2026-11-28 20:00:00">
                  <Outcome name="Boca Juniors" odds="19.00"/>
                  ...
                </Offer>
              </OutrightList>
              <MatchList>
                <Match id="11428880" date="2026-05-19 21:00:00">
                  <OfferList>
                    <Offer type_id="1713500919" type_name="1-X-2">
                      <Outcome name="Mirassol FC SP" odds="5.30"/>
                      <Outcome name="Empate" odds="4.00"/>
                      <Outcome name="Always Ready" odds="1.55"/>
                    </Offer>
                    <Offer type_id="..." type_name="1-2">  <!-- DNB -->
                    <Offer type_id="..." type_name="Más de / Menos de" number="2.5">
                      <Outcome name="Más" odds="1.77"/>
                      <Outcome name="Menos" odds="2.05"/>
                    </Offer>
                    <Offer type_id="..." type_name="Handicap 1-2" number="-2.5">
                    ...
                  </OfferList>
                </Match>
                ...more Match elements...
              </MatchList>
            </Competition>
          </CompetitionList>
        </Region>
      </RegionList>
    </Sport>
  </SportList>
</Data>
```

**All three Betsson-target markets present with same semantics:**
- `1-X-2` ≡ Betsson `MW3W` — selections by team name + "Empate"
- `Más de / Menos de` + `number="N.5"` ≡ Betsson `MTG2W-N.5` —
  selections labeled "Más" / "Menos"
- `1-2` ≡ Draw No Bet (DNB) — Betsson doesn't have this code
  explicitly but it's covered by `MW2W`
- `Handicap 1-2` + `number="±N"` ≡ Asian Handicap
- **BTTS** ("Ambos equipos marcan") not visible in the Libertadores
  match-level offers I sampled, but is offered by most platforms;
  confirm with a more active competition's feed.

**Per-competition feeds are EFFICIENT.** 9 matches in 32KB. One
HTTP per competition per poll gets ALL match-level odds for that
competition — more efficient than Betsson's one-HTTP-per-fixture
accordion pattern.

**Cost-benefit comparison vs Betsson PBA:**

|                       | Betsson PBA                       | Bplay PBA                          |
|---                    |---                                |---                                 |
| Backend platform      | OBG                               | SportNCO                           |
| Frontend              | (no SSR)                          | Nuxt.js SSR                        |
| Anti-bot              | 3 required headers                | Cloudflare on shell only           |
| Data format           | JSON                              | XML (well-formed)                  |
| Polling unit          | 1 HTTP per fixture                | 1 HTTP per competition (~9 matches/req) |
| Bet close timing      | per-market `deadline` field       | per-Match `date` attribute         |
| Stake-limit data      | Not in public API                 | Not visible in feed                |
| Engineering effort    | ~1 day (done)                     | ~1-2 days expected                 |

**Engineering effort estimate: ~1-2 days to a working
`BplayPbaScraper`.** Same pattern: BaseScraper subclass, fixture
cache (competition list rather than fixture list), per-competition
XML fetch with `xml.etree.ElementTree`, emit `RawOddsSnapshot`s.
Mechanical XML parser vs Betsson's JSON parser.

**Argentine soccer coverage caveat.** The Argentine domestic
leagues (Copa Argentina 1493, Primera Nacional 43411) returned 404
during this recon. Could be temporary (off-season, between rounds,
or feed update lag) or could indicate Bplay uses different
competition IDs than what the Nuxt SSR exposes. **A repeat
recon during an active Argentine fixture window** (when Betsson is
showing fixtures, like we saw earlier today) is needed to confirm
Bplay covers the same Argentine matches Betsson does. Without
overlap, the cross-platform arb pair value drops.

**Open / next:**
- Confirm BTTS market code in Bplay's XML (probably another
  `type_name` we haven't seen yet — likely "Ambos equipos
  anotan").
- Test Argentine domestic feeds during an active fixture window.
- Optional: investigate the `ws-deportespba` WebSocket for live
  in-play data (not needed for pre-match arb).

**Bottom line: Bplay PBA is viable, lightweight, and Betsson-class
engineering. Two-ish days to a working scraper. The pending
question is whether their Argentine domestic coverage overlaps
Betsson PBA's during real fixture windows — answerable with a
short follow-up recon when leagues are active.**

**Artifacts:** `recon/artifacts/bplay/20260526-164300/`

---

### 2026-05-26 19:25 UTC — codere — viability: high (Betsson-class)

**Goal:** assess `codere.bet.ar` as a scraping target.

**Sessions:**
- `recon/artifacts/codere/20260526-162040` — marketing root
- `recon/artifacts/codere/20260526-162535` — actual sportsbook UI
  at `m.caba.codere.bet.ar/Deportes/`

**Architecture decoded:**
- **`www.codere.bet.ar`** — marketing/landing site only. Akamai-protected
  (`/akam/13/*`, obfuscated sensor paths like `/qiRuX_tA8IS1.../`).
  Returns a 3.8KB JS shell that hydrates client-side. Mostly static.
- **`m.caba.codere.bet.ar/Deportes/`** — the actual sportsbook, an
  Ionic-based SPA. Title: "Apuestas en Vivo | Codere CABA". 193
  internal requests on first load — the real workload lives here.
- **Backend platform: Playtech BIT Sportsbook (PBS)**.
  `codere.vie.pbs-master.com/dig-codere-com/betslipservice/api/v3/settings`
  is the giveaway — `pbs-master.com` is Playtech's white-label
  sportsbook hosting domain ("vie" = Vienna). Codere is one of many
  operators on this stack; the recon-derived endpoint conventions
  should transfer to other PBS-backed brands.
- Auxiliary services on Azure:
  - `sportsconfiguration.azurewebsites.net/api/Display/GetAllCached`
  - `coderesbgonlinegeoip.azurewebsites.net/ips/currentRequestCountryIsoCode`
- Marketing/landing backend on Heroku
  (`inicio-master-f0bfea23cf49.herokuapp.com`) — promotions,
  banners, featured events. Direct httpx 403s with `{"message":
  "Error don't come back!"}` so we ignore it.

**Geographic footprint: CABA only.** DNS lookups for
`m.pba.codere.bet.ar` / `m.cba.codere.bet.ar` / `m.mendoza` /
`m.santafe` all fail (`nodename nor servname provided`). Codere
operates the Capital Federal license only.

**This is a significant constraint for cross-platform arbitrage:**
our existing Betsson scraper is PBA. CABA Codere + PBA Betsson are
different jurisdictions — different regulators, different markets,
arbitrage between them is not legally coherent. To use Codere for
arb we need either Betsson CABA (recon pending — jurisdiction
header for CABA is unknown) or another PBA-licensed sportsbook.

**JSON REST API surface — all anonymous, all plain httpx:**

| Endpoint | Returns |
|---|---|
| `/NavigationService/LeftMenu/GetMenuLeft` | Sports tree: `[{Name, NodeId, SportHandle, Priority}]` |
| `/NavigationService/Game/SportsGameTypes` | Market-type taxonomy: `{soccer: [97, 1, 18, ...], basketball: [184, 259, 393], ...}` |
| `/NavigationService/Home/GetHomeInfo?...` | Live events + odds: `{marquee, betBuilder, homeLiveEvents, highlightsEvents}` |
| `/SportsMisc/api/Home/GetFeatures?region=33` | Feature flags / per-region config (region=33 = CABA) |

**Critical test passed:** direct httpx GET on each of these endpoints
with just a User-Agent + standard Accept headers returns full JSON,
no Akamai interstitial, no auth challenge. **Akamai sensor scripts
run on the SPA bootstrap path but the REST APIs themselves are
unprotected.** Same pattern as Betsson — anti-bot decoration on the
HTML page, clean JSON on the data path.

**Sample event schema** (from `GetHomeInfo` marquee):
```jsonc
{
  "SportHandle": "tennis",
  "NodeId": "13221308769",
  "ParentNodeId": "3124885578",
  "IsLive": true,
  "LeagueName": "Roland Garros - Masculino (FR)",
  "ParticipantHome": "Alexander Bublik",
  "ParticipantAway": "Jan-Lennard Struff",
  "Game": {
    "Results": [
      { "Odd": 1.25, "GameTypeId": 97, "Name": "Alexander Bublik",
        "EventId": "13221308769", "GameId": "13221308793",
        "LeagueId": "3124885578", "Locked": false, ... },
      { ... away selection ... }
    ]
  }
}
```

- `GameTypeId 97` is the "match winner" market across sports — for
  soccer it should be 1X2 (3 results) instead of 2.
- Odds as native floats (no string-parse needed, unlike Betsson's
  dual format).
- `NodeId` = event ID; `LeagueId` = competition ID; `LocationId` =
  country/region ID.

**Engineering effort estimate vs Betsson PBA:**
- Same: REST endpoint shape, JSON parsing, fixture cache pattern,
  per-event odds polling, headers contract bisection if needed.
- Different: response wrapping (`{Game: {Results: [...]}}` vs
  Betsson's `{data: {accordions: {<CODE>: {markets, selections}}}}`).
  Mechanical parser change, no protocol-level work.
- ~1-2 days to a working `CodereCabaScraper` once we commit.

**Open questions / next steps:**
- Drill into a single soccer match to confirm soccer market codes
  (analogous to Betsson's MW3W / BTTS / MTG2W). The
  `gameTypesHomeLiveEvents=159;259;18;393;...` query param hints at
  market IDs; need to map each numeric ID to a market type.
- Argentine Primera División event IDs / league ID — needed to scope
  the scraper to AR domestic soccer.
- Whether the same JSON surface serves *all* PBS-backed operators
  (Codere ES, Codere MX, etc.) — would mean one parser, many
  scrapers.
- **Betsson CABA recon** to unlock actual cross-platform arbitrage
  in CABA province.

**Bottom line: Codere CABA is viable and lightweight, ~Betsson
PBA-equivalent engineering cost. Geographic mismatch with our
existing Betsson scraper means we either recon Betsson CABA next or
pick a PBA competitor (Bplay / BetWarrior / Pasion) before any arb
can actually run cross-platform.**

**Artifacts:** `recon/artifacts/codere/20260526-162040/` (marketing
site), `recon/artifacts/codere/20260526-162535/` (sportsbook).

---

### 2026-05-26 19:11 UTC — bet365 (AR) — first-pass viability assessment

**Goal:** evaluate whether `bet365.bet.ar` (the Argentine-licensed
subsidiary) is a viable scraping target — same kind of recon as
Betsson PBA but on a much more anti-bot-hostile platform.

**Sessions:** `recon/artifacts/bet365/20260526-160759` (home),
`20260526-161156` (attempted soccer drill-down).

**Tooling change:** renamed `scripts/recon/betsson_recon.py` to
`scripts/recon/recon.py` and parameterized it by `--platform`. Each
platform now gets its own artifact directory
(`recon/artifacts/<platform>/`) and its own persistent browser profile
(`recon/profile/<platform>/`) so fingerprints/cookies don't bleed
across sites — relevant to anti-bot systems that flag profiles with
suspicious cross-domain history. Existing Betsson profile dir
migrated to `recon/profile/betsson/`.

**What we learned:**

1. **AR-licensed, accessible from AR.** Title "bet365 - Apuestas
   deportivas en la red", footer carries `ba-province.svg` +
   `ba-regulatorv2.svg` markings. Not a global geo-block.

2. **Hash-routed SPA.** Landing URL rewrites to
   `https://www.bet365.bet.ar/#/HO/`. Navigation lives entirely in
   the URL fragment — `#/AC/B1/C1/D1002/G40/` for "all-competitions,
   sport=1=soccer, ...". The PD fields we saw in the menu response
   (e.g. `#AC#B1#C1#D1002#E131418680#G40#`) translate to these hash
   routes letter-by-letter.

3. **The routing manifest is the key.**
   `GET /websiteroutingdatacontentapi/routingdata?v=<cachebuster>` →
   maps URL hash patterns to data endpoints. Sample entries:
   - `B:1~D:1002~Q:1~F^:3,12,24,48,72,2001,2002 → /matchmarketscontentapi/soccerupcomingmatches`
   - `B:1,3,12,16,17,18~D:5,8,19~K^:12 → /matchbettingcontentapi/coupon`
   - `B:1~D:1002~Q:1 → /matchmarketscontentapi/upcomingmatches`
   Required query params for each are encoded in `q:` (e.g.
   `tzo,csidex` — timezone offset, session checksum).

4. **Custom pipe-delimited text protocol for content APIs.**
   `/leftnavcontentapi/allsportsmenu` returns:
   ```
   F|CS;IT=LN-HL1;SY=Sports;NA=Deportes;|CL;ID=-5;...;|EV;ID=1;IT=LN-PO00;NA=Copa Libertadores;PD=#AC#B1#C1#D1002#E131418680#G40#;...
   ```
   - `|` separates records
   - `KEY=VALUE;` for fields
   - Record type as first token (`F`, `CS`, `CL`, `EV`)
   - `B:1` confirms sport ID 1 = soccer
   - This is the format we'd need to write a parser for. Order of
     magnitude harder than Betsson's JSON but tractable (~few-hundred
     lines for a clean parser + tests).

5. **`/Api/1/Blob` is NOT a data API.** It's the JS bundle delivery
   endpoint. Query format `?<lid>,<app>,<module>/<ver>/|...` asks the
   server to assemble a specific bundle of React libraries and app
   modules. Responses are minified JavaScript. Irrelevant to the
   scraper.

6. **Cloudflare bot challenge fronts the data APIs.** Direct httpx
   call to `/matchmarketscontentapi/soccerupcomingmatches` returns
   HTTP 403 with a "Just a moment..." Cloudflare interstitial. The
   Playwright session passes the challenge transparently (real
   Chromium can execute the JS challenge and obtain `cf_clearance`)
   — that's why the recon successfully fetched the routing manifest
   and the menu. **This is the operational wall.**

**Cost-benefit comparison vs Betsson:**

|                       | Betsson PBA                       | Bet365 AR                            |
|---                    |---                                |---                                   |
| Anti-bot wall         | 3 required headers                | Cloudflare JS challenge              |
| Scraper transport     | httpx (lightweight)               | Playwright session (heavy, stateful) |
| Data format           | JSON                              | Pipe-delimited text protocol         |
| Polling cadence       | Per-fixture HTTP every 5s         | Per-fixture browser-context fetch    |
| Operational cost      | 1 process, ~50MB RAM              | 1 Chromium per scraper, ~400MB RAM   |
| Cookie/WAF refresh    | N/A                               | Required (~30min-2hr `cf_clearance`) |
| Parser work to add    | None                              | Pipe-delimited text → records        |
| Engineering effort    | ~1 day (done)                     | ~3-5 days for first working scraper  |

**Viability verdict:** **technically viable, operationally
significantly heavier than Betsson.** The Cloudflare challenge is the
real wall and it's not insurmountable — Playwright clears it
automatically. But moving from httpx-based scraping to long-running
Playwright sessions changes the deployment shape: each scraper needs
a persistent Chromium process, periodic challenge re-clearance, and
either an httpx-cookie bridge or in-page `page.evaluate(fetch(...))`
calls. Plus a parser for the pipe-delimited text protocol.

**Recommendation:**
- If the goal is shipping arbitrage soon, deprioritize bet365.
  Recon Codere AR / Bplay / BetWarrior first — they're AR-licensed
  competitors of Betsson and likely follow similar lightweight
  architecture.
- If the goal includes bet365 long-term (their odds are good and
  arbitrage spreads against bet365 are historically wider): the
  architecture sketch is — long-running Playwright scraper, expose
  `page.context.request` as the HTTP shim, write a `bet365_protocol.py`
  decoder for the pipe-delimited format, schedule a profile-refresh
  cycle every ~30 minutes to renew `cf_clearance`.

**Anti-bot footnote:** the AR version is materially LESS aggressive
than international bet365 (no Akamai Bot Manager, no mouse-trajectory
fingerprinting visible, no JS-challenge that breaks bare Playwright).
Just plain Cloudflare's default bot detection — same tier as many
mid-grade SaaS sites.

**Artifacts:**
- `recon/artifacts/bet365/20260526-160759/` — initial home capture
- `recon/artifacts/bet365/20260526-161156/` — hash-route navigation
  attempt (no soccer XHRs fired; SPA didn't route on hash-only load)

---

### 2026-05-25 22:34 UTC — betsson — smoke run (no Playwright session)

**Goal:** validate `BetssonScraper` against the live API; surface
contract gaps the synthetic test fixtures can't catch.

**How:** `scripts/smoke_betsson.py` — one polling cycle, HTTP only
(no Playwright), no logins.

**Required-headers contract** (newly discovered, NOT visible from
recon HAR alone — needed deletion-test bisection against live API):
- `brandid: 238cb63a-3dcc-4fdf-b241-23a12cb71aa7` — HTTP 400 /
  `E_VALIDATION_INVALIDHEADER` if absent.
- `marketcode: ag` — HTTP 400 / `E_VALIDATION_INVALIDHEADER` if absent.
- `x-sb-type: b2b` — HTTP 500 / `E_UNHANDLED` if absent. The server's
  request handler dispatches on this header value; missing it
  triggers an unhandled exception in the dispatch path. This was the
  surprise — it doesn't *look* required, but every OBG endpoint blows
  up without it.
- Plus `x-sb-jurisdiction: Iplyc` (PBA) for semantic correctness:
  scopes the response to the province's offering rather than a
  default.

Every other `x-sb-*` / `x-obg-*` header the browser sends is
optional; the deletion test confirms removing any one (other than the
three above) still yields a 200.

**Diagnostic recipe for future platforms** (next-scraper playbook):
1. Implement the scraper from recon-derived synthetic responses.
2. Hit the live API. If 4xx/5xx:
3. Build a `FULL` header dict from the browser HAR (everything you
   saw the browser send).
4. Confirm `FULL` returns 200.
5. Remove headers one at a time from `FULL`; the ones that flip the
   status code back to 4xx/5xx when removed are required.
6. Bake those into the scraper's `_platform_headers`; document
   inline why each is there.

**Smoke results (PBA, 2026-05-25 22:34 UTC):**
- 38 Argentine soccer fixtures discovered.
- 30 snapshots before script limit, across 3 fixtures and 14 markets.
- One fixture had no open markets — scraper logged warning and
  continued. Defensive design verified.

**Open / next:**
- Recon CABA and CBA to discover their `x-sb-jurisdiction` values
  (real Argentine regulators are LOTBA for CABA and LCBA for
  Córdoba; the exact header string the OBG backend accepts is
  unconfirmed).
- Logged-in recon pass to capture `max_stake` from the bet slip.

---

### 2026-05-25 21:02 UTC — betsson — 20260525-210248

**Goal:** drill into a specific match URL, confirm the 1X2 market code,
nail down selection encoding for the three target markets (1X2, BTTS,
totals).

**URL drilled:** Gimnasia (Jujuy) vs Belgrano, Copa Argentina —
`https://pba.betsson.bet.ar/apuestas-deportivas/futbol/argentina/copa-argentina/gimnasia-jujuy-belgrano`
(kick-off 2026-05-30T18:00:00Z).
Note: site appends `?eventId=f-...` after the slug; both forms resolve.

**The per-match API surface (53 requests touched this event ID):**

The match SPA fires one main `event/v2` call plus one
`widgets/accordion/v1` call per market group. The accordion endpoint
accepts either `groupableId=<group code>` or
`marketTemplateIds=<csv of market codes>` and returns markets +
selections in one shot. **This is THE odds endpoint for the scraper.**

Confirmed market codes for soccer (via `marketTemplateId`):

| Code        | Market (Spanish label)          | Layout    | Selections |
|-------------|---------------------------------|-----------|------------|
| `MW3W`      | "Ganador del partido" (1X2)     | 3-col     | `HOME` / `DRAW` / `AWAY` |
| `BTTS`      | "Ambos equipos anotan"          | 2-col     | `YES` / `NO` |
| `BTTS1H`    | BTTS first half                 | 2-col     | `YES` / `NO` |
| `BTTS2H`    | BTTS second half                | 2-col     | `YES` / `NO` |
| `DC`        | "Doble oportunidad"             | 3-col     | `HOMEORDRAW` / `HOMEORAWAY` / `DRAWORAWAY` (labels "1X" / "12" / "X2") |
| `MTG2W`     | Match total goals (O/U)         | line-based | `OVER` / `UNDER` per line |
| `1HTG`      | 1st half total goals            | line-based | `OVER` / `UNDER` |
| `T2HGOU`    | 2nd half total goals            | line-based | `OVER` / `UNDER` |
| `MW2W`      | Match winner 2-way (DNB)        | 2-col     | likely `HOME` / `AWAY` |
| `M3WHCP`    | 3-way handicap                  | 3-col     | TBD |
| `MW3W2UPEP` | 3-way variant (probably 1X2 + over period) | — | TBD |

Also observed in groupableIds (less critical, mostly novelty markets):
`MTG2W25`, `ATCS`, `AGSCRSB`, `AWEH`, `FRSTGOALSB`, `FTCSR`.

**JSON shape (digest of `groupableId=MW3W` response):**

```jsonc
{
  "data": {
    "accordions": {
      "MW3W": {
        "markets": [{
          "eventId": "f-1BTUpOr2SEi33O_h-WHKyg",
          "marketTemplateId": "MW3W",
          "id": "m-f-1BTUpOr2SEi33O_h-WHKyg-MW3W",
          "marketFriendlyName": "Ganador del partido",
          "label": "Ganador del partido",
          "status": "Open",
          "deadline": "2026-05-30T18:00:00Z",
          "lineValue": "",        // populated for O/U / handicap markets
          "lineValueRaw": 0.0,
          "columnLayout": 3,
          "isCashoutAvailable": true,
          // ... metadata
        }],
        "selections": [
          {
            "marketId": "m-...-MW3W",
            "id": "s-m-...-MW3W-home",
            "selectionTemplateId": "HOME",
            "label": "Gimnasia Jujuy",
            "alternateLabel": "Gimnasia Jujuy",
            "participantLabel": "Gimnasia Jujuy",
            "participantId": "919",
            "odds": 3.95,
            "marketSelectionPriceFormats": {"1": "3.95"},
            "status": "Open",
            "sortOrder": 1,
            "isHomeTeam": true
          },
          { "selectionTemplateId": "DRAW",  "label": "Empate",   "odds": 3.05, ... },
          { "selectionTemplateId": "AWAY",  "label": "Belgrano", "odds": 2.02, ... }
        ]
      }
    }
  },
  "referenceId": "..."
}
```

**Key fields for scraper:**

| What we want                | JSON path                                                  |
|-----------------------------|-----------------------------------------------------------|
| Market type (canonical)     | `data.accordions.<code>.markets[].marketTemplateId`       |
| Market ID (full)            | `markets[].id`                                            |
| Line (for O/U, handicap)    | `markets[].lineValue` (string) / `lineValueRaw` (number)  |
| Market status               | `markets[].status` — `"Open"`, presumably `"Suspended"`/`"Closed"` |
| Bet close time              | `markets[].deadline` (ISO 8601 UTC)                        |
| Selection type (canonical)  | `selections[].selectionTemplateId` (HOME/DRAW/AWAY/YES/NO/OVER/UNDER/HOMEORDRAW/...) |
| Selection ID                | `selections[].id`                                          |
| Decimal odds                | `selections[].odds` (float)                                |
| Selection status            | `selections[].status`                                      |

**Odds format:** decimal (e.g., 3.95), stored as both a `number`
(`odds`) and as a stringified entry under
`marketSelectionPriceFormats["1"]`. Format ID `1` is decimal. No
fractional or American variant requested in our session.

**Stake limits (max/min/increment) are NOT in this response.** They are
almost certainly only exposed when the user adds a selection to the bet
slip (logged-in flow). Need a separate logged-in recon to capture
those, OR we fall back to platform-wide policy defaults from the
sportsbook's terms-and-conditions page.

**One-call coverage for the three target markets:**

```
GET /api/sb/v1/widgets/accordion/v1
    ?eventId=f-<event id>
    &marketTemplateIds=MW3W,BTTS,MTG2W
```

Single round trip returns 1X2 + BTTS + match total goals O/U for a
fixture. That's enough to feed a scraper today.

**Live updates:** the main `event/v2` response carries a `topics` array
with channel strings like `?obg/sportsbook/transient/events/<eventId>/...`
and `.../markets/<marketId>/...`. These look like a pub/sub subscription
manifest — confirms the SPA does receive live odds via WebSocket / SSE,
which Playwright HAR doesn't capture. For our use case the polling
cadence (every 3-10s per `docs/architecture.md`) on the HTTP endpoint
is probably sufficient; live-channel reverse-engineering is a future
optimization.

**Open / next:**
- Confirm Argentina league code (`117`) vs. cup code (`5292`).
- Capture a logged-in session to find `max_stake` on the bet slip.
- Compare CABA / CBA — almost certainly same OBG backend, possibly
  same exact responses with a different `jurisdiction` parameter.
- Draft `src/ingestion/scrapers/betsson.py` from this surface.

**Script bug observed:** the recon script always runs the full
`home → cookie → soccer → match` sequence. When given a match URL
directly, it loads the match (good), then immediately clicks "Fútbol"
and navigates away (bad). The match-page API calls did fire before the
click-away, so artifacts are intact, but the script should grow a
"single URL, no nav" mode for drill-down recons. Filed as next-pass
improvement.

**Artifacts:** `recon/artifacts/betsson/20260525-210248/`

---

### 2026-05-25 20:56 UTC — betsson — 20260525-205613

**Goal:** reach the actual sportsbook UI on PBA and identify odds API.

**URLs visited:**
- `https://pba.betsson.bet.ar/apuestas-deportivas/`  (sportsbook home)
- Auto-navigated to `https://pba.betsson.bet.ar/apuestas-deportivas/futbol?tab=liveAndUpcoming`
  after the script clicked the "Fútbol" link.

**Markets observed on page:** soccer competitions listed include Copa
Libertadores, Copa Sudamericana, UEFA Conference League, Copa Argentina,
and various European leagues. Live events (in-play) and upcoming.

**Key findings (substantial):**

1. **Sportsbook backend looks like the OBG (Online Betting Group)
   platform.** Topic / channel strings in API responses use the prefix
   `?obg/sportsbook/transient/markets/...` and `?obg/sportsbook/transient/events/...`,
   suggesting a pub/sub realtime fabric underneath the HTTP API. This is
   the same backend many regulated EU operators use (Betsson Group is
   the parent).

2. **The sportsbook API namespace is `/api/sb/v1/` and `/api/sb/v2/`.**
   Key endpoints we observed actually getting called:
   - `GET /api/sb/v1/widgets/categories/v2` — full sports tree
     (2.8MB on first hit). Indexed by URL slug; nodes carry
     `[categoryId, countryId, competitionId, fixtureId]` arrays.
     Sport category id `"1"` = Fútbol. Country id `"117"` = Argentina.
     Competition id `"5292"` = Copa Argentina, `"275"` = Copa
     Libertadores, `"691"` = Copa Sudamericana.
   - `GET /api/sb/v1/competitions/liveEvents` — live-event IDs grouped
     by competition with popularity metrics.
   - `GET /api/sb/v1/widgets/event-market/v1?marketids=<csv>` — **the
     odds endpoint.** Pass a comma-separated list of market IDs,
     returns market + selection + odds payload. The widget is the
     server-side view used by the SPA to render odds tiles.
   - `GET /api/sb/v1/widgets/most-popular-competitions/v1` — leagues.
   - `GET /api/sb/v1/widgets/most-popular-categories/v1` — sports.
   - `GET /api/sb/v1/content/groups/mappings` — likely market-name
     dictionary; should be inspected to decode market type codes.

3. **Event ID format:** `f-<base64ish hash>` (e.g.
   `f-9FZrznZUD0iQGqI943tqzw`). The `f-` prefix likely stands for
   "fixture".

4. **Market ID encoding is dense and informative.** A market ID is
   `m-<eventId>-<marketCode>[-<line>]`. Examples seen for soccer:
   - `MWOU-2.5`, `MWOU-3.5` — almost certainly **match winner /
     over-under total goals** at lines 2.5, 3.5.
   - `1HTG-1.5`, `HTG-3.5` — **1st half / total half goals** O/U
     lines.
   - `1HTC-5.5` — 1st half total corners O/U 5.5.
   - `MGT-1` — likely match goals total (unsure of "1" meaning).
   - `FRSTGOALSB-0.5` — first goal scorer (special bet) with
     line 0.5.
   - `ATCS`, `AGSCRSB`, `AWEH`, `FTCSR` — anytime team to score,
     anytime goal scorer special bet, asian handicap-ish, and full
     time correct score range (all guesses based on abbreviation
     shape).
   - **1X2 / match result code is not yet confirmed.** Needs a
     drill-down on a specific match to capture the home/draw/away
     market explicitly. Likely a 3-selection market with a known
     code (`3WAY`, `MR`, `1X2`, or similar) we just haven't seen
     yet.

5. **URL slug structure for matches:** the categories tree's
   `indexBySlug` shows paths like
   `futbol/argentina/copa-argentina/gimnasia-jujuy-belgrano`. A direct
   match URL is probably
   `/apuestas-deportivas/futbol/argentina/copa-argentina/<home>-<away>`.

6. **Jurisdiction parameter:** `jurisdiction=IPLYC` appears in some
   content endpoints. IPLYC = Instituto Provincial de Lotería y Casinos,
   the PBA regulator. CABA and CBA likely use different jurisdiction
   codes (their respective regulators) and may serve different odds.

7. **Anti-bot surface:**
   - **AWS WAF in front** (`*.edge.sdk.awswaf.com` host visible).
   - **Browser fingerprinting** via Contentsquare (`c.contentsquare.net`)
     and Optimizely (`logx.optimizely.com`).
   - **Custom fraud endpoint** `pba.betsson.bet.ar/cdn/fraud/api/fl`
     POSTs a `cfidsgib-w-bab-betssonarba` cookie value. Looks like
     Group-IB device-fingerprinting kit. This is the one most likely
     to cause issues for a headless scraper.
   - Cookie consent must be accepted before the SPA hydrates the
     navigation; persistent profile suffices.

8. **Anonymous access works.** Several content endpoints carry an
   explicit `isLoggedIn=false&jurisdiction=IPLYC` query param. We did
   not log in, and the markets and competitions API still returned data.

**Open questions / next steps:**
- Drill into one specific match URL (e.g. one of the Copa Libertadores
  fixtures captured) to confirm: 1X2 market code, BTTS market code,
  exact selection encoding (home/draw/away semantics), price format
  (decimal vs fractional).
- Fetch `/api/sb/v1/content/groups/mappings` standalone to get the
  market-code dictionary in one shot (may decode MGT/MWOU/etc.
  without guessing).
- Compare CABA / CBA: same backend? Same odds? Same jurisdiction
  parameter? If shared, one scraper suffices.
- Quantify polling cadence — for live events the SPA likely uses
  websockets or SSE on the `?obg/sportsbook/transient/...` channels.
  HAR doesn't capture those; need a separate websocket-aware capture
  pass.
- Decide whether to scrape via the JSON API (preferred — clean,
  stable, public) or via DOM (only if APIs gate behind auth, which so
  far they don't).

**Artifacts:** `recon/artifacts/betsson/20260525-205613/` (and the
prior two sessions at `20260525-204314`, `20260525-205338`).

---

### 2026-05-25 20:53 UTC — betsson — 20260525-205338

**Goal:** find the actual sportsbook on the PBA subdomain.

**URLs visited:**
- `https://pba.betsson.bet.ar/` (200, 335KB HTML, 537 requests).
- Auto-clicked "Fútbol" → landed on `/casino-en-vivo/.../futbol-studio`
  (which is a live-casino card game, **not** the sportsbook). Dead end
  via this nav path.

**Key findings:**
- PBA serves a real, hydrated SPA. The initial HTML has zero
  meaningful nav hrefs — everything is JS-rendered.
- Cookie consent: `button:has-text('Aceptar todas')` works.
- All real navigation comes from `/api/v2/content/documentgroups/desktop-common-menus`
  (64KB JSON) and `desktop-mega-menu` (20KB JSON). These document
  groups define the SPA's nav structure with canonical URLs:
  - `/apuestas-deportivas` — sportsbook (this is the URL we needed)
  - `/apuestas-deportivas/en-directo` — in-play
  - `/casino`, `/live-casino`, `/jackpots`, `/promotions`, ...

- Lesson: text-based selector matching (`a:has-text('Fútbol')`) on a
  multi-vertical brand can hit the wrong vertical. Better to navigate
  by URL once known.

**Artifacts:** `recon/artifacts/betsson/20260525-205338/`

---

### 2026-05-25 20:43 UTC — betsson — 20260525-204314

**Goal:** first-pass surface mapping of Betsson Argentina from
`www.betsson.com.ar`.

**URLs visited:**
- `https://www.betsson.com.ar/` (200, landed here, no further nav)

**Markets observed on page:** none — the landing page is a static
jurisdiction selector (15KB HTML, 23 requests total, no XHRs).

**Key findings:**
- **Betsson Argentina is split into three provincial subdomains**
  because Argentine sports betting is regulated at the provincial
  level. The marketing root just routes users to one of:
  - `https://pba.betsson.bet.ar`   — Provincia de Buenos Aires
  - `https://caba.betsson.bet.ar`  — Capital Federal / CABA
  - `https://cba.betsson.bet.ar`   — Córdoba
- The provincial site is where the actual sportsbook lives. The
  `www.betsson.com.ar` page is entirely static — no odds, no events,
  no API calls.
- Architectural implication: scrapers must target the per-province
  subdomain. Odds and markets may differ between provinces (separate
  licenses, possibly separate pricing engines). Cross-provincial
  arbitrage is likely illegal from a single account (different
  regulators) — only cross-platform arbs *within* a province are
  viable.

**Open questions / next steps:**
- Recon `pba.betsson.bet.ar` (PBA — largest provincial market).
- Confirm whether CABA and CBA serve the same prices as PBA. If so,
  one scraper suffices. If not, separate per-province.
- Find the soccer / fútbol section under the provincial subdomain and
  identify the actual odds-bearing endpoints.

**Artifacts:** `recon/artifacts/betsson/20260525-204314/`

---

## Template

### YYYY-MM-DD HH:MM UTC — <platform> — <session_id>

**Goal:** what we set out to learn.

**URLs visited:**
- (final URL after redirects)
- (each navigation step)

**Markets observed on page:** 1X2, BTTS, O/U, ...

**Key findings:**
- Is the markup server-rendered or client-side hydrated?
- Are odds in the initial HTML, or loaded via XHR?
- Identified API endpoint(s): `METHOD https://...` — what they return.
- Auth: anonymous OK? requires cookie? requires logged-in token?
- Anti-bot / anti-VPN signals (Cloudflare challenge, captcha, etc.)?

**Open questions / next steps:**
- ...

**Artifacts:** `recon/artifacts/<platform>/<session_id>/`
