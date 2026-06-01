"""Block-page detection for recon sessions.

On 2026-05-27, a Betano recon session navigated into a Kaizen Gaming
block page ("Access to this page is restricted due to security and
compliance measures") but the harness still logged a successful
"Done" and wrote a `requests.jsonl` from a session that captured
nothing useful. A 5-second look at the screenshot would have caught
it. This module makes that check automatic.

The matching logic is pure (`block_reason_from`) so it's unit-
testable against captured block-page HTML; `detect_block` wraps it
around a live Playwright page.

Signatures are intentionally broad — a false "this is a block page"
costs us one aborted recon, while a false "this is fine" wastes a
whole session and (worse) keeps us hammering a site that's already
told us to stop.
"""

from __future__ import annotations

import contextlib
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page

# Substrings checked against the page <title> (case-insensitive).
_TITLE_SIGNATURES = (
    "splash screen",
    "access denied",
    "attention required",  # Cloudflare
    "just a moment",  # Cloudflare challenge interstitial
    "blocked",
)

# Substrings checked against the page body text (case-insensitive).
_BODY_SIGNATURES = (
    "access to this page is restricted",
    "security and compliance measures",
    "unusual activity from your device",
    "verify you are human",
    "checking your browser before",
    "enable javascript and cookies to continue",
    "ray id",  # Cloudflare block/challenge footer
    "we detected unusual activity",
)

# Regexes checked against the raw HTML (e.g. block-page iframes).
# NOTE: we deliberately do NOT match the bare `/cdn-cgi/challenge-platform/`
# script reference. Cloudflare injects that orchestration script into the
# HTML of *every* page it fronts, not just challenge interstitials — so
# matching it flagged healthy Betano homepages and aborted every recon
# before it could navigate (2026-05-29). A genuine CF interstitial is
# caught instead by its title ("just a moment", "attention required") and
# body text ("enable javascript and cookies to continue", "verify you are
# human", "ray id"), which a normal page does not carry.
_HTML_PATTERNS = (
    re.compile(r"landingpages\.kaizengaming\.com/[^\"']*splash-screen", re.I),
    re.compile(r"challenges\.cloudflare\.com", re.I),
)


def block_reason_from(title: str, html: str) -> str | None:
    """Pure detection: given a page title and raw HTML, return a short
    reason string if this looks like a block/challenge page, else None.

    Kept pure (no Playwright) so it can be unit-tested against saved
    block-page captures."""
    title_l = (title or "").lower()
    for sig in _TITLE_SIGNATURES:
        if sig in title_l:
            return f"title matches block signature: {sig!r}"

    html_l = (html or "").lower()
    for sig in _BODY_SIGNATURES:
        if sig in html_l:
            return f"body matches block signature: {sig!r}"

    for pat in _HTML_PATTERNS:
        if pat.search(html or ""):
            return f"html matches block pattern: {pat.pattern!r}"

    return None


async def detect_block(page: Page) -> str | None:
    """Check a live page for block/challenge signatures. Returns a
    reason string if blocked, else None. Never raises — on any error
    reading the page, returns None (fail-open: don't abort a recon
    just because we couldn't read the DOM)."""
    title = ""
    html = ""
    with contextlib.suppress(Exception):
        title = await page.title()
    with contextlib.suppress(Exception):
        html = await page.content()
    return block_reason_from(title, html)
