# Platform recon playbook

How we discover the scrapeable API surface of a new sportsbook.
Read this before reconning a new platform; update it when the
procedure changes.

## When to run a recon

You're reconning when you:

- Are evaluating a new sportsbook as a candidate scrape target
  (jurisdiction, API style, bot-protection profile).
- Need to confirm an existing platform still works the same way
  after a long gap.
- Are debugging why an existing scraper isn't getting data — the
  recon harness's HAR trace gives you a clean comparison
  against what the browser sees.

You're **not** reconning when you're tweaking a working scraper.
Recon is for *first-pass discovery*. After we have the endpoint
shape, code lives in `src/ingestion/scrapers/`.

## The tools

| File | Purpose |
|---|---|
| `scripts/recon/recon.py` | Playwright session: **enters via homepage, warms up (dwell/mouse/scroll), then navigates**; dismisses cookies; drills into soccer + a match (or a deep `--url` from a warm session); records HAR + screenshots + DOM. Per-platform isolated browser profile. Aborts non-zero if a block/challenge page is detected. |
| `scripts/recon/human.py` | Human-navigation helpers: `HumanPacing` config, `warm_up` (dwell + mouse move + scroll), `human_click` (hover + pause + click). The highest-leverage anti-detection technique — behave like a patient visitor. |
| `scripts/recon/stealth.py` | Patches automation tells (`navigator.webdriver`, permissions/languages consistency) via a context init script. Patches *tells*, never fabricates hardware — consistency over fabrication. |
| `scripts/recon/block_detect.py` | Recognizes block/challenge pages (Kaizen splash, Cloudflare challenge, "access restricted") by title/iframe/body signatures. `recon.py` aborts on a hit instead of logging a false "Done". |
| `scripts/recon/analyze_har.py` | Post-recon: parses the captured HAR. Lists JSON/XHR hosts, surfaces API-looking paths, flags odds-shaped responses, optionally dumps responses to `<slug>.json` next to the HAR. |
| `scripts/recon/recon.py:DEFAULT_URLS` | The platform-to-landing-URL map. Add new platforms here. |
| `recon/artifacts/<platform>/<UTC-timestamp>/` | Per-session artifacts: `network.har`, `requests.jsonl`, `summary.json`, `NN_<step>.png/.html`. |
| `recon/profile/<platform>/` | Persistent browser profile per platform — keeps cookies/fingerprints isolated across sites. |
| `scripts/recon/RECON_LOG.md` | The dated, append-only narrative log of every recon session and what we concluded. **Write here at the end of every recon pass.** |

### Anti-detection built into the harness (Tier 1)

As of 2026-05-28 the harness has three anti-detection layers, all
in the "behave like a real, patient visitor" category — no proxies,
no IP rotation, no fingerprint fabrication (see the LEDGER rationale
on why those are the *wrong* tools for a funded-account operation):

1. **Homepage-first navigation.** `recon.py` always enters via the
   platform homepage and warms up before going deeper, even when you
   pass a deep `--url`. Cold deep-links are what tripped Betano.
2. **Human behavior simulation** (`human.py:HumanCursor`). Anti-bot
   systems score *behavior*, flagging unrealistic perfection. The
   cursor is imperfect on purpose:
   - **Curved, jittered mouse paths** with mid-path bulge + per-step
     tremor — never straight lines or teleports. Position is
     continuous across the whole session (one `HumanCursor`).
   - **Imperfect scrolling** — variable deltas, occasional
     up-corrections (overshoot), and the odd long reading pause.
   - **Right-skewed think-time** before actions — usually quick,
     occasionally a long hesitation.
   - **Off-center clicks** — land at a random point inside the
     element box, never dead-center.
   - **Typo-capable typing** (`type_text`) — variable per-key timing
     with occasional fat-finger-then-backspace. For future logged-in
     flows; read-only recon never types.

   Tune any of it per platform via `human.py:PER_PLATFORM_PACING`.
3. **Stealth init script + block detection.** `navigator.webdriver`
   and friends are patched; block pages abort the run with exit
   code 3 so we never hammer a site that's already said no. A
   navigation/network failure aborts with exit code 4.

**Exit codes:** `0` success · `2` bad args · `3` block page detected
· `4` navigation/network failure.

## Why playwright + chromium privy and not curl/httpx for first-pass

Modern sportsbooks bot-protect with some combination of:

- **Cloudflare WAF** that scores request fingerprints (TLS, JA3, header order, JS evaluation).
- **In-page JS challenges** that set cookies the API expects.
- **Per-IP rate scoring** that escalates from rate-limit (429) to outright block (403) when abused. **We've already burned an IP on Bplay this way today** — see the 2026-05-27 LEDGER entry on that.

A real browser session sidesteps all three for the *read* path
because we're being a normal visitor. We do recon in the browser,
then once we know the endpoints and the headers the browser sends,
we can usually translate to direct `httpx` for the production
scraper — but only after seeing what the wire looks like.

The chromium-privy isolated-profile design also means cookies and
fingerprints don't bleed across platforms. A recon on Betano
shouldn't make us look like the same automated profile that
recon'd Bplay an hour earlier.

## The procedure (5 steps)

### 1. Add the platform URL

Edit `scripts/recon/recon.py` and add an entry to `DEFAULT_URLS`:

```python
DEFAULT_URLS: dict[str, str] = {
    ...
    "newplatform": "https://www.newplatform.bet.ar/",
}
```

Use the canonical user-facing URL (the one your browser ends up on
after typing the bare domain). If apex redirects to `www.`, use
`www.`. If the site segments by province via subdomain, use the PBA
subdomain unless you've decided otherwise.

### 2. Run the recon

```bash
uv run python scripts/recon/recon.py --platform newplatform
```

The harness will:

1. Launch a non-headless chromium with the platform's persistent profile.
2. Load the landing page.
3. Dismiss the cookie banner (OneTrust, custom, etc.).
4. Click into the soccer section (best-effort; selector list is generic).
5. Open the first match (best-effort).
6. Idle 5 s to capture late XHR.
7. Write everything to `recon/artifacts/newplatform/<timestamp>/`.

If steps 4-5 fail (selectors don't match), you still get the
homepage XHR traffic. Most platforms expose their API on the
homepage's "live events" / "featured" widgets — that's where Betano
revealed `/danae-webapi/api/live/overview/latest`.

You can pass `--url <override>` for an ad-hoc drill (e.g. to grab
a specific match-detail page once you know its URL pattern).

### 3. Analyze the HAR

```bash
uv run python scripts/recon/analyze_har.py \
    recon/artifacts/newplatform/<timestamp>/network.har \
    --min-bytes 5000
```

Look at:

- **Top hosts.** A single dominant first-party host (your target's
  CDN) is the good case. Many third-party hosts means tracking/
  marketing noise; that's normal but you can ignore it.
- **`/api/`-prefix paths.** Real API endpoints usually live under
  `/api/`, `/v1/`, `/webapi/`, `/sportsbook/`, etc.
- **JSON responses ≥ 5,000 bytes.** Anything smaller is config /
  feature flags / translations. The big payloads are the data.
- **`<-- ODDS-SHAPED?` hint.** The analyzer flags JSON whose top-
  level keys include any of `events`, `markets`, `selections`,
  `outcomes`, `fixtures`, `betoffers`, `data`. That's your
  first-pass shortlist.

Pass `--dump` to write each qualifying JSON response to a
`<slug>.json` file next to the HAR. Then `jq`/`grep`/cat them
without re-parsing the HAR each time:

```bash
uv run python scripts/recon/analyze_har.py <har> --dump --min-bytes 5000
```

### 4. Inspect a real odds response

Once you've identified the odds-shaped endpoint, look at one
real event in the response. Confirm:

- **Event identifier** — is it stable across calls? Is it a
  platform-internal ID, or a universal one (Betradar match ID,
  similar)?
- **Market structure** — flat list per event, or nested under
  market-group → market? What identifies a market (numeric ID,
  slug, name string)?
- **Outcome structure** — what's the price field called (`odds`,
  `price`, `value`)? What's the outcome identifier (`name`,
  `label`)?
- **Status fields** — `isLive`, `isSuspended`, `status:
  "OPEN"/"SUSPENDED"`. Critical for the verifier — suspensions
  are the in-play risk surface.
- **Cross-platform binding hints** — `betradarMatchId`, `radarId`,
  `srMatchId`, ISO timestamps, team-name strings. Useful for the
  canonicalizer.

### 5. Document what you found

Append a dated entry to `scripts/recon/RECON_LOG.md` covering:

- **Goal** — what you were trying to learn.
- **Artifacts** — the artifact directory path.
- **Findings** — domain shape, API surface, auth requirements,
  jurisdiction footprint, bot-protection observations.
- **Engineering effort estimate** — "~Betsson-class, ~1-2 days"
  is a useful unit.
- **Real constraints** — jurisdiction mismatch, login required,
  geo-blocking, anything that affects whether we can actually
  use this.
- **Decision needed** — what the operator should decide next.

For platforms we then ship a scraper for, also write a LEDGER
entry under the same date.

## Safety considerations

These are not optional. We've already burned a platform IP today.

### Cumulative IP reputation

Cloudflare and similar WAFs **score by IP across all sites they
protect**. If you recon 5 sportsbooks in an hour from one IP,
your IP looks like a tool, not a user. Stagger recon sessions
across days when possible. **Don't run recon and the live
ingestion daemon against the same platform in the same hour** —
the ingestion daemon's volume is what trips the protection,
recon then arrives looking like the same actor.

### One recon per platform per session is enough

The harness captures everything in a single ~30-second pass.
There is no value in re-running the same platform multiple times
on the same day; you just rack up requests for nothing.

### Don't escalate to scripted GETs prematurely

If a HEAD probe returns 403, **stop**. Don't loop with retries
or vary the headers — that pattern is exactly what bot-protection
is built to catch. Load the site in a real browser first to
confirm whether the 403 is the WAF or a real "doesn't exist"
signal, then decide.

### Bot-protection signals to watch for

In the rendered page or response headers, treat as red flags:

- "Unusual activity from your device or network" challenges
  with your IP echoed back.
- `cf-mitigated: challenge` or `cf-mitigated: block` response headers.
- Datadome / Akamai bot-manager fingerprinting scripts in the
  page source.
- A `403` that returns a Cloudflare-branded HTML body (not just
  status 403 with empty body) — that's an active block, not
  just a missing route.

When any of these appear, slow down and rethink. Don't bypass.

## What this playbook does NOT cover

- **Logged-in recon.** Account creation, deposit limits, stake
  limits, and the bet-placement endpoint all require an
  authenticated session and explicit operator approval per
  `AGENTS.md`. That's a separate procedure, not this one.
- **Production scraper structure.** Once recon is done, scrapers
  go in `src/ingestion/scrapers/<platform>.py` following the
  `BaseScraper` pattern. See an existing scraper (Betsson is the
  cleanest reference) for the shape.
- **In-play SSE / WebSocket discovery.** Some platforms (e.g.
  Bplay) use SSE for live odds. The HAR will capture the initial
  SSE handshake; further frames need either a longer Playwright
  session with a network log, or a dedicated WS sniffer. Cross
  that bridge when it appears.

## Quick reference

```bash
# Add platform → recon.py:DEFAULT_URLS, then:
uv run python scripts/recon/recon.py --platform <name>
uv run python scripts/recon/analyze_har.py \
    recon/artifacts/<name>/<ts>/network.har --dump --min-bytes 5000

# Then write the entry in scripts/recon/RECON_LOG.md.
```
