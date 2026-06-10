"""Unit tests for FixtureResolver.

Exercises:
  - Anchor parsing for BetWarrior (" - ") and Bplay (" vs ")
  - Cross-platform fixture dedup (BetWarrior + Bplay → 1 canonical fixture)
  - Betsson outcome-label anchoring (links to existing canonical fixture)
  - Cache fast path: repeat snapshots from same platform event ID
  - Observation-window: same teams beyond window → new canonical fixture
  - Ambiguous match escalates to LLM matcher; matcher's None drops snapshot
  - Unparseable event names return None
"""

from __future__ import annotations

from src.ingestion.scrapers.base import RawOddsSnapshot
from src.semantic.canonical import CanonicalFixture
from src.semantic.fixture_resolver import (
    OBSERVATION_WINDOW_SEC,
    FixtureResolver,
    LLMFixtureMatcher,
)


def _snap(
    platform: str,
    platform_event_id: str,
    raw_event_name: str,
    raw_outcome_name: str = "1",
    timestamp: float = 1748287200.0,
) -> RawOddsSnapshot:
    return RawOddsSnapshot(
        platform=platform,
        platform_event_id=platform_event_id,
        platform_market_id=f"mkt-{platform_event_id}",
        platform_outcome_id=f"out-{platform_event_id}-1",
        raw_event_name=raw_event_name,
        raw_market_name="ignored-by-fixture-resolver",
        raw_outcome_name=raw_outcome_name,
        decimal_odds=2.0,
        max_stake=None,
        timestamp=timestamp,
    )


# ---- Anchor parsing ----


class TestAnchorResolution:
    async def test_betwarrior_event_creates_fixture(self) -> None:
        r = FixtureResolver()
        snap = _snap("betwarrior-pba", "1027027525", "LDU Quito - Always Ready")
        fixture = await r.resolve(snap)
        assert fixture is not None
        assert fixture.home_team == "ldu quito"
        assert fixture.away_team == "always ready"
        assert fixture.kickoff_utc is None
        assert fixture.competition_slug is None

    async def test_bplay_event_creates_fixture(self) -> None:
        r = FixtureResolver()
        snap = _snap("bplay-pba", "11428880", "Paris SG vs Arsenal")
        fixture = await r.resolve(snap)
        assert fixture is not None
        assert fixture.home_team == "paris sg"
        assert fixture.away_team == "arsenal"

    async def test_unparseable_event_name_returns_none(self) -> None:
        r = FixtureResolver()
        # Neither " - " nor " vs " in this name
        snap = _snap("betwarrior-pba", "123", "FlatNoSeparator")
        assert await r.resolve(snap) is None
        # Multiple separators (ambiguous parse) also returns None.
        snap2 = _snap("betwarrior-pba", "124", "A - B - C")
        assert await r.resolve(snap2) is None


# ---- Cache fast path ----


class TestCacheFastPath:
    async def test_repeat_snapshot_returns_same_fixture(self) -> None:
        r = FixtureResolver()
        snap1 = _snap("betwarrior-pba", "evt-1", "Boca Juniors - River Plate")
        snap2 = _snap(
            "betwarrior-pba", "evt-1", "Boca Juniors - River Plate", timestamp=1748287205.0
        )
        f1 = await r.resolve(snap1)
        f2 = await r.resolve(snap2)
        assert f1 is not None
        assert f2 is not None
        assert f1.fixture_id == f2.fixture_id


# ---- Cross-platform dedup ----


class TestCrossPlatformDedup:
    async def test_betwarrior_then_bplay_same_teams_links_same_fixture(self) -> None:
        r = FixtureResolver()
        bw = _snap("betwarrior-pba", "k-1", "LDU Quito - Always Ready", timestamp=1000.0)
        bp = _snap(
            "bplay-pba", "b-1", "LDU Quito vs Always Ready", timestamp=1003.0
        )
        f1 = await r.resolve(bw)
        f2 = await r.resolve(bp)
        assert f1 is not None
        assert f2 is not None
        assert f1.fixture_id == f2.fixture_id

    async def test_bplay_then_betwarrior_with_kambi_variant_dedups(self) -> None:
        """Real cross-platform case: Bplay names 'Mirassol FC SP' (SportNCO
        canonicalization) and BetWarrior 'Mirassol-SP' (Kambi
        canonicalization) for the same team. The fixture resolver uses
        fuzzy team-name match (same threshold as outcome_resolver) so
        the two snapshots resolve to ONE canonical fixture.

        Regression guard for the v1 dedup bug fix: previously the
        resolver used exact normalized-string match here, which would
        have silently split the canonical fixture and prevented
        cross-platform arb detection on this team."""
        r = FixtureResolver()
        bp = _snap("bplay-pba", "b-1", "Mirassol FC SP vs Always Ready", timestamp=1000.0)
        bw = _snap(
            "betwarrior-pba", "k-1", "Mirassol-SP - Always Ready", timestamp=1003.0
        )
        f1 = await r.resolve(bp)
        f2 = await r.resolve(bw)
        assert f1 is not None
        assert f2 is not None
        assert f1.fixture_id == f2.fixture_id

    async def test_reserves_does_not_link_to_first_team(self) -> None:
        """The dedup threshold's discriminator test. `Belgrano` and
        `Belgrano Reserves` are different teams; the fuzzy match
        must NOT conflate them."""
        r = FixtureResolver()
        first = _snap(
            "betwarrior-pba", "k-1", "Belgrano - Quilmes", timestamp=1000.0
        )
        reserves = _snap(
            "bplay-pba", "b-1", "Belgrano Reserves vs Quilmes Reserves",
            timestamp=1003.0,
        )
        f1 = await r.resolve(first)
        f2 = await r.resolve(reserves)
        assert f1 is not None
        assert f2 is not None
        assert f1.fixture_id != f2.fixture_id


# ---- Observation window ----


class TestObservationWindow:
    async def test_same_teams_within_window_link(self) -> None:
        r = FixtureResolver()
        s1 = _snap("betwarrior-pba", "k-1", "Boca - River", timestamp=1000.0)
        s2 = _snap(
            "bplay-pba", "b-1", "Boca vs River", timestamp=1000.0 + OBSERVATION_WINDOW_SEC - 1
        )
        f1 = await r.resolve(s1)
        f2 = await r.resolve(s2)
        assert f1 is not None
        assert f2 is not None
        assert f1.fixture_id == f2.fixture_id

    async def test_same_teams_beyond_window_creates_new_fixture(self) -> None:
        r = FixtureResolver()
        s1 = _snap("betwarrior-pba", "k-1", "Boca - River", timestamp=1000.0)
        # Same teams playing two days later (e.g. league + Copa Argentina).
        s2 = _snap(
            "bplay-pba", "b-99", "Boca vs River", timestamp=1000.0 + OBSERVATION_WINDOW_SEC + 60
        )
        f1 = await r.resolve(s1)
        f2 = await r.resolve(s2)
        assert f1 is not None
        assert f2 is not None
        assert f1.fixture_id != f2.fixture_id


# ---- Betsson outcome-label anchoring ----


class TestBetssonAnchoring:
    async def test_betsson_without_existing_anchor_returns_none(self) -> None:
        r = FixtureResolver()
        snap = _snap("betsson-pba", "f-abc", "gimnasia jujuy belgrano", "Belgrano")
        # No anchor → drop. The scraper will retry next cycle; by then
        # BetWarrior/Bplay should have registered the fixture.
        assert await r.resolve(snap) is None

    async def test_betsson_links_to_existing_anchor_by_outcome_team(self) -> None:
        r = FixtureResolver()
        # Anchor first via BetWarrior.
        await r.resolve(_snap("betwarrior-pba", "k-1", "Gimnasia Jujuy - Belgrano", timestamp=1000.0))
        # Now Betsson snapshot with outcome label = "Belgrano" (the away team).
        bet = _snap("betsson-pba", "f-bet", "gimnasia jujuy belgrano", "Belgrano", timestamp=1002.0)
        f = await r.resolve(bet)
        assert f is not None
        assert f.home_team == "gimnasia jujuy"
        assert f.away_team == "belgrano"

    async def test_betsson_empate_outcome_links_via_slug(self) -> None:
        """The slug carries BOTH teams, so a draw ("Empate") snapshot resolves
        the fixture without needing the outcome label — the draw cell completes
        the partition in the same cycle instead of waiting for a non-draw snap."""
        r = FixtureResolver()
        await r.resolve(_snap("betwarrior-pba", "k-1", "Gimnasia Jujuy - Belgrano", timestamp=1000.0))
        bet = _snap("betsson-pba", "f-bet", "gimnasia jujuy belgrano", "Empate", timestamp=1002.0)
        f = await r.resolve(bet)
        assert f is not None
        assert f.home_team == "gimnasia jujuy" and f.away_team == "belgrano"

    async def test_betsson_sharing_one_team_does_not_mislink(self) -> None:
        """Regression for the live 72% phantom arb: a Betsson event that shares
        only ONE team with a registered fixture must NOT link to it. The slug
        ("boca defensa") covers only 'boca' of Boca-River — 'defensa' ≠ 'river'
        — so requiring both teams drops it rather than cross-attributing odds."""
        r = FixtureResolver()
        await r.resolve(_snap("betano", "k-1", "Boca vs River", timestamp=1000.0))
        other = _snap("betsson-pba", "f-x", "boca defensa", "Boca", timestamp=1002.0)
        assert await r.resolve(other) is None

    async def test_betsson_cached_after_first_link(self) -> None:
        """After Betsson is linked once via outcome anchoring, repeat
        snapshots (even with Empate as outcome) take the cache fast path."""
        r = FixtureResolver()
        await r.resolve(_snap("betwarrior-pba", "k-1", "Boca - River", timestamp=1000.0))
        # First link via non-draw outcome
        link_snap = _snap("betsson-pba", "f-bet", "boca river", "Boca", timestamp=1002.0)
        f1 = await r.resolve(link_snap)
        assert f1 is not None
        # Subsequent Empate snapshot from same Betsson event hits cache.
        draw_snap = _snap("betsson-pba", "f-bet", "boca river", "Empate", timestamp=1003.0)
        f2 = await r.resolve(draw_snap)
        assert f2 is not None
        assert f1.fixture_id == f2.fixture_id


# ---- LLM fallback (default Noop + injectable) ----


class _RecordingMatcher:
    """Test double that records calls and returns the configured response."""

    def __init__(self, response_fixture_id: str | None = None) -> None:
        self.response = response_fixture_id
        self.calls: list[tuple[RawOddsSnapshot, list[CanonicalFixture]]] = []

    async def choose(
        self,
        snapshot: RawOddsSnapshot,
        candidates: list[CanonicalFixture],
    ) -> str | None:
        self.calls.append((snapshot, list(candidates)))
        return self.response


class TestLLMFallback:
    async def test_protocol_matches_recording_matcher(self) -> None:
        # Structural typing sanity: RecordingMatcher fits the Protocol.
        m: LLMFixtureMatcher = _RecordingMatcher()
        assert m is not None

    async def test_noop_matcher_drops_ambiguous(self) -> None:
        """Default Noop matcher returns None → ambiguous fixtures drop."""
        r = FixtureResolver()
        # Register two distinct fixtures that both match the same
        # (home, away) pair within the window.
        # We force ambiguity by directly inserting two fixtures with the
        # same teams.
        await r.resolve(_snap("betwarrior-pba", "k-1", "A - B", timestamp=1000.0))
        # Same teams, different platform_event_id, manually create a
        # second fixture by mutating internals to simulate a race.
        existing = next(iter(r._fixtures.values()))
        duplicate = CanonicalFixture(
            fixture_id="fx-dup",
            home_team=existing.home_team,
            away_team=existing.away_team,
        )
        r._fixtures["fx-dup"] = duplicate
        r._last_seen["fx-dup"] = 1000.0

        # Now a fresh snapshot with the same teams resolves ambiguously.
        snap = _snap("bplay-pba", "b-1", "A vs B", timestamp=1001.0)
        result = await r.resolve(snap)
        assert result is None

    async def test_llm_matcher_chooses_specific_candidate(self) -> None:
        r = FixtureResolver(llm_matcher=_RecordingMatcher(response_fixture_id="fx-pick"))
        await r.resolve(_snap("betwarrior-pba", "k-1", "A - B", timestamp=1000.0))
        existing = next(iter(r._fixtures.values()))
        chosen = CanonicalFixture(
            fixture_id="fx-pick",
            home_team=existing.home_team,
            away_team=existing.away_team,
        )
        r._fixtures["fx-pick"] = chosen
        r._last_seen["fx-pick"] = 1000.0

        snap = _snap("bplay-pba", "b-1", "A vs B", timestamp=1001.0)
        result = await r.resolve(snap)
        assert result is not None
        assert result.fixture_id == "fx-pick"


class TestBetanoAnchor:
    """Betano is an anchor (its raw_event_name is '{home} vs {away}'); Betsson
    (non-anchor) links to a Betano-registered fixture by outcome-team label."""

    async def test_betano_event_creates_fixture(self) -> None:
        r = FixtureResolver()
        fixture = await r.resolve(_snap("betano", "86490800", "Panama vs Dominicana"))
        assert fixture is not None
        assert fixture.home_team == "panama" and fixture.away_team == "dominicana"

    async def test_betano_anchor_then_betsson_links_same_fixture(self) -> None:
        r = FixtureResolver()
        ban = await r.resolve(
            _snap("betano", "86490800", "Gimnasia Jujuy vs Belgrano", timestamp=1000.0)
        )
        bet = await r.resolve(
            _snap("betsson-pba", "f-bet", "gimnasia jujuy belgrano", "Belgrano", timestamp=1002.0)
        )
        assert ban is not None and bet is not None
        assert ban.fixture_id == bet.fixture_id  # Betsson leg aligns to the Betano fixture
