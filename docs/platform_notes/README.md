# Platform reconnaissance notes

One file per platform, named `<platform>.md`. Each file documents what we learned during the Phase 3 reconnaissance pass and serves as the spec for that platform's scraper.

## Template

When starting on a new platform, copy this template:

```markdown
# <Platform Name>

## URLs
- Live odds page: https://...
- Login page: https://...
- API base (if discovered): https://...

## Authentication
- Mechanism: cookie | JWT | session token | none
- Renewal cadence: ...
- Stealth requirements: ...

## API endpoints (if available)
- `GET /events/live/soccer` — returns ...
- `GET /events/{id}/markets` — returns ...

## Page structure (if HTML scraping required)
- Live events list selector: ...
- Event detail selector: ...
- Markets/outcomes selector: ...
- Update mechanism: WebSocket | polling | SSE

## Rate limits
- Observed: N requests per second before throttling
- Strategy: ...

## Anti-bot measures
- Cloudflare: yes | no
- DataDome: yes | no
- JS challenges: ...
- What works: pure httpx | httpx with headers | Playwright headless | Playwright headful with stealth

## Soccer market types available
- 1X2 (match result)
- Both teams to score
- Total goals over/under
- Correct score
- First scorer
- ...

## Quirks and gotchas
- ...
```
