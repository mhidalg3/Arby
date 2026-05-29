"""Tests for the recon block-page detector's pure matching logic.

The detector exists because a Betano recon on 2026-05-27 walked into
a Kaizen block page but the harness logged success anyway. These
tests pin the signatures so a future refactor can't silently stop
recognizing a known block page.
"""

from __future__ import annotations

from scripts.recon.block_detect import block_reason_from


class TestKnownBlockPages:
    def test_kaizen_betano_splash(self) -> None:
        """The exact block we hit: Kaizen splash iframe + title."""
        title = "Betano Splash Screen"
        html = (
            '<html><head><title>Betano Splash Screen</title></head>'
            '<body><iframe src="https://landingpages.kaizengaming.com/'
            'betano-splash-screen-bz/index.html"></iframe></body></html>'
        )
        reason = block_reason_from(title, html)
        assert reason is not None
        assert "splash screen" in reason.lower()

    def test_betano_body_text_signature(self) -> None:
        """Even if the title were generic, the body text catches it."""
        title = "Betano"
        html = (
            "<html><body><h1>Access to this page is restricted due to "
            "security and compliance measures.</h1></body></html>"
        )
        reason = block_reason_from(title, html)
        assert reason is not None
        assert "restricted" in reason.lower()

    def test_cloudflare_challenge_title(self) -> None:
        reason = block_reason_from("Just a moment...", "<html></html>")
        assert reason is not None

    def test_real_managed_challenge_caught_by_body(self) -> None:
        """A genuine CF managed-challenge interstitial — caught by its
        title + body text, not by the always-present orchestration script."""
        html = (
            '<html><head><title>Just a moment...</title></head><body>'
            "Enable JavaScript and cookies to continue."
            '<script src="/cdn-cgi/challenge-platform/h/b/orchestrate">'
            "</script></body></html>"
        )
        reason = block_reason_from("Just a moment...", html)
        assert reason is not None

    def test_unusual_activity_challenge(self) -> None:
        """The registration-flow warning Betano showed (named our IP)."""
        html = (
            "<div>We detected unusual activity from your device or "
            "network.</div>"
        )
        reason = block_reason_from("Verify", html)
        assert reason is not None


class TestRealPages:
    def test_normal_sportsbook_home_not_flagged(self) -> None:
        title = "Apuestas deportivas online | Betano"
        html = (
            '<html><head><title>Apuestas deportivas online | Betano'
            '</title></head><body><div class="sportsbook">'
            '<a href="/sport/futbol/">Fútbol</a></div></body></html>'
        )
        assert block_reason_from(title, html) is None

    def test_empty_inputs_not_flagged(self) -> None:
        assert block_reason_from("", "") is None

    def test_cf_orchestration_script_alone_not_flagged(self) -> None:
        """Cloudflare injects /cdn-cgi/challenge-platform/ into every page
        it fronts; its mere presence is NOT a block. Flagging it aborted
        every Betano recon on the homepage (2026-05-29)."""
        title = "Apuestas deportivas online | Betano"
        html = (
            '<html><head><title>Apuestas deportivas online | Betano</title>'
            '</head><body><div class="sportsbook">'
            '<a href="/sport/futbol/">Fútbol</a></div>'
            '<script src="/cdn-cgi/challenge-platform/h/b/orchestrate">'
            "</script></body></html>"
        )
        assert block_reason_from(title, html) is None

    def test_odds_content_not_flagged(self) -> None:
        """A page that happens to contain the word 'security' in an
        unrelated context shouldn't trip — we match specific phrases,
        not bare keywords."""
        html = "<div>Security deposit bonus terms apply.</div>"
        assert block_reason_from("Promociones", html) is None
