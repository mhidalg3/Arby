"""Resolve a RawOddsSnapshot to a CanonicalFixture.

The fixture resolver is the stateful component of the semantic
layer. It maintains an in-process registry of canonical fixtures
(`Dict[fixture_id, CanonicalFixture]`) and a fast-path index
(`Dict[(platform, platform_event_id), fixture_id]`) so repeat
snapshots from the same platform event resolve in O(1) without
re-parsing.

Three platforms, three strategies:

- **BetWarrior** (`raw_event_name = "Home - Away"`) — split on
  `" - "`, normalize, register or link.
- **Bplay** (`raw_event_name = "Home vs Away"`) — split on
  `" vs "`, normalize, register or link.
- **Betsson** — `raw_event_name` is a flat lowercase slug (e.g.
  `"gimnasia jujuy belgrano"`) that does NOT preserve home/away
  split. We CANNOT create a canonical fixture from a Betsson
  snapshot alone. The strategy: anchor against existing canonical
  fixtures (created by Bplay or BetWarrior) using the outcome
  label, which IS a team name on Betsson. If the outcome label
  fuzzy-matches one of an existing fixture's teams within the
  observation window, link the Betsson event to it. Otherwise
  defer — the next snapshot from an anchor platform will register
  the fixture, and a later Betsson cycle will pick it up.

Dedup key: `(home_team_normalized, away_team_normalized)`, with a
recent-observation-window filter. Two anchor-platform snapshots
within `OBSERVATION_WINDOW_SEC` that normalize to the same
home/away pair link to the same canonical fixture. Beyond the
window, a fresh fixture is created — same teams playing again
later is a separate match.

Ambiguity (multiple existing fixtures match the same parsed
home/away) escalates to the injected `LLMFixtureMatcher`. The
default `NoopLLMFixtureMatcher` always returns None, so v1 drops
ambiguous snapshots. A real LLM matcher can be plugged in when
the ambiguity rate justifies the cost.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Final, Protocol

from src.ingestion.scrapers.base import RawOddsSnapshot
from src.semantic.canonical import CanonicalFixture
from src.semantic.team_normalize import normalize_team_name, team_similarity

# Two anchor-platform snapshots within this many seconds of each
# other are candidates for the same canonical fixture. Beyond it,
# we assume the team-pair is playing a separate match.
#
# 10 minutes is comfortably wider than the 5s poll interval but
# narrow enough to never conflate a Friday Boca-River with a Tuesday
# Boca-River. Same-day rematches (a friendly + a league fixture
# kicking off the same evening) are vanishingly rare; if they
# happen, the LLM fallback (or a future kickoff_utc field) is the
# escape hatch.
OBSERVATION_WINDOW_SEC: Final[float] = 600.0

# Threshold for matching a Betsson outcome label (team name) against
# an existing canonical fixture's home or away. Same as
# outcome_resolver's STRICT_MATCH_THRESHOLD — keep them in sync if
# either is tuned.
ANCHOR_MATCH_THRESHOLD: Final[float] = 0.85

# Per-platform separator for parsing raw_event_name into (home, away).
_ANCHOR_SEPARATORS: Final[dict[str, str]] = {
    "betwarrior-pba": " - ",
    "bplay-pba": " vs ",
}


class LLMFixtureMatcher(Protocol):
    """Async hook for LLM-based ambiguity resolution.

    Invoked only when the deterministic pass returns >1 candidate
    fixture for a snapshot. Receives the snapshot and the candidate
    fixtures; returns the chosen fixture_id (or None to drop).

    Real implementations live outside this module — same pattern as
    `partition_validator.PartitionValidator` injecting `LLMValidator`.
    """

    async def choose(
        self,
        snapshot: RawOddsSnapshot,
        candidates: list[CanonicalFixture],
    ) -> str | None: ...


class NoopLLMFixtureMatcher:
    """Default LLMFixtureMatcher that always returns None.

    Lets v1 ship deterministically without an LLM dependency.
    Ambiguous cases get dropped (visible as `resolver.ambiguous`
    log entries) until a real matcher is wired in.
    """

    async def choose(
        self,
        snapshot: RawOddsSnapshot,
        candidates: list[CanonicalFixture],
    ) -> str | None:
        return None


@dataclass
class FixtureResolver:
    """Stateful fixture registry. NOT thread-safe; one resolver per task.

    Construct once at startup, share across all snapshots in a single
    asyncio task. Restart-resilient is out of scope for v1 — the
    registry is rebuilt from a few cycles of fresh snapshots after a
    restart.
    """

    llm_matcher: LLMFixtureMatcher = field(default_factory=NoopLLMFixtureMatcher)
    # Fast path: (platform, platform_event_id) → fixture_id
    _by_platform_key: dict[tuple[str, str], str] = field(default_factory=dict)
    # Registry: fixture_id → CanonicalFixture
    _fixtures: dict[str, CanonicalFixture] = field(default_factory=dict)
    # fixture_id → most-recent observation timestamp (used by the
    # observation-window filter and lazy GC).
    _last_seen: dict[str, float] = field(default_factory=dict)

    async def resolve(self, snapshot: RawOddsSnapshot) -> CanonicalFixture | None:
        """Return the canonical fixture for `snapshot`, registering a
        new one if needed. Returns None when unresolvable (e.g. Betsson
        snapshot with no existing anchor, or ambiguous match)."""
        # Fast path: same platform event already linked.
        key = (snapshot.platform, snapshot.platform_event_id)
        if key in self._by_platform_key:
            fid = self._by_platform_key[key]
            self._last_seen[fid] = snapshot.timestamp
            return self._fixtures[fid]

        if snapshot.platform in _ANCHOR_SEPARATORS:
            return await self._resolve_anchor(snapshot)
        # Non-anchor platform (Betsson currently): can only link to
        # existing canonical fixtures via outcome-label match.
        return await self._resolve_non_anchor(snapshot)

    async def _resolve_anchor(
        self, snapshot: RawOddsSnapshot
    ) -> CanonicalFixture | None:
        parts = self._split_event_name(snapshot)
        if parts is None:
            return None
        home_n = normalize_team_name(parts[0])
        away_n = normalize_team_name(parts[1])
        if not home_n or not away_n:
            return None

        candidates = self._find_candidates_by_team_match(
            home_n, away_n, snapshot.timestamp
        )
        if len(candidates) == 1:
            return self._link(snapshot, candidates[0].fixture_id)
        if len(candidates) > 1:
            chosen_id = await self.llm_matcher.choose(snapshot, candidates)
            if chosen_id is not None and chosen_id in self._fixtures:
                return self._link(snapshot, chosen_id)
            return None

        # No match — create a new canonical fixture.
        return self._register_new(snapshot, home_n, away_n)

    async def _resolve_non_anchor(
        self, snapshot: RawOddsSnapshot
    ) -> CanonicalFixture | None:
        """Betsson and any other platform without a parsable home/away
        in raw_event_name. Use the outcome label as a team hint."""
        label = snapshot.raw_outcome_name.strip()
        if not label or label.lower() == "empate":
            # Empate alone doesn't disambiguate; wait for a non-draw
            # snapshot from the same Betsson event in a future cycle.
            return None
        label_n = normalize_team_name(label)
        if not label_n:
            return None

        candidates = self._find_candidates_by_team_anchor(label_n, snapshot.timestamp)
        if len(candidates) == 1:
            return self._link(snapshot, candidates[0].fixture_id)
        if len(candidates) > 1:
            chosen_id = await self.llm_matcher.choose(snapshot, candidates)
            if chosen_id is not None and chosen_id in self._fixtures:
                return self._link(snapshot, chosen_id)
            return None
        # No anchor yet — drop. Next anchor-platform cycle will register.
        return None

    # ---- internals ----

    def _split_event_name(
        self, snapshot: RawOddsSnapshot
    ) -> tuple[str, str] | None:
        sep = _ANCHOR_SEPARATORS.get(snapshot.platform)
        if sep is None:
            return None
        parts = snapshot.raw_event_name.split(sep)
        if len(parts) != 2:
            # Either the separator doesn't appear (e.g. outright-style
            # event name) or appears multiple times (rare, but
            # `"Atlético-MG - Argentinos Jrs - Reserves"` is theoretical).
            # Treat as unparseable — caller drops.
            return None
        home, away = parts[0].strip(), parts[1].strip()
        if not home or not away:
            return None
        return home, away

    def _find_candidates_by_team_match(
        self, home_n: str, away_n: str, observed_at: float
    ) -> list[CanonicalFixture]:
        """Find canonical fixtures whose home AND away teams both
        clear ANCHOR_MATCH_THRESHOLD similarity against the parsed
        pair. Fuzzy match (not exact) is required because two
        platforms can canonicalize the same team differently
        (Bplay 'Mirassol FC SP' vs BetWarrior 'Mirassol-SP' →
        normalized 'mirassol fc sp' vs 'mirassol sp', sim ≈0.94).
        The threshold is the same as the outcome-resolver's, by
        design — `Belgrano` vs `Belgrano Reserves` stays at ≈0.79
        and correctly does NOT link."""
        return [
            fx
            for fid, fx in self._fixtures.items()
            if team_similarity(home_n, fx.home_team) >= ANCHOR_MATCH_THRESHOLD
            and team_similarity(away_n, fx.away_team) >= ANCHOR_MATCH_THRESHOLD
            and self._within_window(fid, observed_at)
        ]

    def _find_candidates_by_team_anchor(
        self, label_n: str, observed_at: float
    ) -> list[CanonicalFixture]:
        return [
            fx
            for fid, fx in self._fixtures.items()
            if self._within_window(fid, observed_at)
            and (
                team_similarity(label_n, fx.home_team) >= ANCHOR_MATCH_THRESHOLD
                or team_similarity(label_n, fx.away_team) >= ANCHOR_MATCH_THRESHOLD
            )
        ]

    def _within_window(self, fixture_id: str, observed_at: float) -> bool:
        last = self._last_seen.get(fixture_id)
        if last is None:
            return False
        return abs(last - observed_at) <= OBSERVATION_WINDOW_SEC

    def _link(
        self, snapshot: RawOddsSnapshot, fixture_id: str
    ) -> CanonicalFixture:
        self._by_platform_key[(snapshot.platform, snapshot.platform_event_id)] = (
            fixture_id
        )
        self._last_seen[fixture_id] = snapshot.timestamp
        return self._fixtures[fixture_id]

    def _register_new(
        self, snapshot: RawOddsSnapshot, home_n: str, away_n: str
    ) -> CanonicalFixture:
        fixture_id = f"fx-{uuid.uuid4().hex[:12]}"
        fixture = CanonicalFixture(
            fixture_id=fixture_id,
            home_team=home_n,
            away_team=away_n,
        )
        self._fixtures[fixture_id] = fixture
        self._by_platform_key[(snapshot.platform, snapshot.platform_event_id)] = (
            fixture_id
        )
        self._last_seen[fixture_id] = snapshot.timestamp
        return fixture
