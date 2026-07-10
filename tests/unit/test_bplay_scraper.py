"""Unit tests for the Bplay PBA scraper.

Uses `httpx.MockTransport` so the parser is exercised against
synthetic SportNCO-shaped XML without hitting the real network. The
XML fixtures here are hand-trimmed mirrors of the live responses
captured during recon (see `scripts/recon/RECON_LOG.md`) — small
enough to read in one screen, real enough to catch parser drift.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from src.ingestion.scrapers.bplay import (
    BASE_URL,
    BplayPbaScraper,
)

# ---- Synthetic XML responses ----

# Trimmed Libertadores-style feed: one match (Mirassol vs Always Ready),
# four target markets (1X2, O/U at 2.5, DNB, Asian Handicap), one
# unknown market that should be skipped, one sub-unity-odds outcome
# that should be dropped, and a sample BTTS market via "Ambos" prefix.
_FEED_LIBERTADORES = b"""<?xml version="1.0"?>
<Data>
  <SportList>
    <Sport id="13" name="F&#xFA;tbol">
      <RegionList>
        <Region id="141" name="Am&#xE9;rica del Sur">
          <CompetitionList>
            <Competition id="36146" name="Copa Libertadores">
              <OutrightList>
                <Offer type_id="9991" type_name="Apuesta sobre el evento"
                       event="Copa Libertadores - 2026 - Ganador"
                       date="2026-11-28 20:00:00">
                  <Outcome name="Boca Juniors" odds="19.00"/>
                  <Outcome name="Palmeiras" odds="5.00"/>
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
                    <Offer type_id="1713500931" type_name="1-2">
                      <Outcome name="Always Ready" odds="1.16"/>
                      <Outcome name="Mirassol FC SP" odds="3.80"/>
                    </Offer>
                    <Offer type_id="1713500932" type_name="M&#xE1;s de / Menos de" number="2.5">
                      <Outcome name="M&#xE1;s" odds="1.77"/>
                      <Outcome name="Menos" odds="2.05"/>
                    </Offer>
                    <Offer type_id="1713500915" type_name="Handicap 1-2" number="-2.5">
                      <Outcome name="Always Ready" odds="4.70"/>
                      <Outcome name="Mirassol FC SP" odds="1.08"/>
                    </Offer>
                    <Offer type_id="1713500981" type_name="Ambos equipos anotan">
                      <Outcome name="Si" odds="1.85"/>
                      <Outcome name="No" odds="1.95"/>
                    </Offer>
                    <Offer type_id="1713500999" type_name="Resultado al medio tiempo">
                      <Outcome name="Mirassol FC SP" odds="6.00"/>
                      <Outcome name="Empate" odds="2.30"/>
                      <Outcome name="Always Ready" odds="2.10"/>
                    </Offer>
                    <Offer type_id="1713500937" type_name="M&#xE1;s de / Menos de" number="7.5">
                      <Outcome name="M&#xE1;s" odds="50.00"/>
                      <Outcome name="Menos" odds="1.00"/>
                    </Offer>
                  </OfferList>
                </Match>
              </MatchList>
            </Competition>
          </CompetitionList>
        </Region>
      </RegionList>
    </Sport>
  </SportList>
</Data>
"""

# Empty-with-Team feed: confirms that <Team> children on Match are
# preferred over offer-derived names when present.
_FEED_WITH_TEAMS = b"""<?xml version="1.0"?>
<Data>
  <SportList>
    <Sport id="13" name="F&#xFA;tbol">
      <RegionList>
        <Region id="1" name="Test">
          <CompetitionList>
            <Competition id="63057" name="Copa Mundial">
              <MatchList>
                <Match id="999999" date="2026-06-15 18:00:00">
                  <Team name="Argentina"/>
                  <Team name="Brasil"/>
                  <OfferList>
                    <Offer type_id="111" type_name="1-X-2">
                      <Outcome name="Argentina" odds="2.40"/>
                      <Outcome name="Empate" odds="3.20"/>
                      <Outcome name="Brasil" odds="2.80"/>
                    </Offer>
                  </OfferList>
                </Match>
              </MatchList>
            </Competition>
          </CompetitionList>
        </Region>
      </RegionList>
    </Sport>
  </SportList>
</Data>
"""

# A feed with no MatchList — just outrights. Real-world case for some
# competitions (Copa Mundial 2026 currently is mostly outrights).
_FEED_OUTRIGHTS_ONLY = b"""<?xml version="1.0"?>
<Data>
  <SportList>
    <Sport id="13" name="F&#xFA;tbol">
      <CompetitionList>
        <Competition id="6674" name="UEFA Champions League">
          <OutrightList>
            <Offer type_id="100" type_name="Apuesta sobre el evento" event="Winner">
              <Outcome name="Real Madrid" odds="3.50"/>
              <Outcome name="Manchester City" odds="4.00"/>
            </Offer>
          </OutrightList>
        </Competition>
      </CompetitionList>
    </Sport>
  </SportList>
</Data>
"""


Handler = Callable[[httpx.Request], httpx.Response]


def _make_client(handler: Handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _competition_routes_handler(
    routes: dict[int, tuple[int, bytes | None]],
) -> Handler:
    """Build a handler that serves XML for the listed competition IDs.

    `routes[id] = (status_code, body_or_None)`. Unmapped competition
    IDs return 404 (Bplay's signal for "no offers this cycle").
    """

    def _handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        # Parse the competition id out of /oddsfeeds/odds-competition<ID>.xml
        prefix = "/oddsfeeds/odds-competition"
        if not path.startswith(prefix) or not path.endswith(".xml"):
            return httpx.Response(404)
        cid_str = path[len(prefix) : -4]
        try:
            cid = int(cid_str)
        except ValueError:
            return httpx.Response(404)
        if cid not in routes:
            return httpx.Response(404)
        status, body = routes[cid]
        if body is None:
            return httpx.Response(status)
        return httpx.Response(status, content=body, headers={"content-type": "application/xml"})

    return _handler


# ---- Construction ----


class TestConstruction:
    async def test_default_platform_name_and_competitions(self) -> None:
        client = _make_client(_competition_routes_handler({}))
        s = BplayPbaScraper(http_client=client)
        assert s.platform_name == "bplay-pba"
        # Default target list — all five recon-confirmed marquee XML feeds.
        # (UCL, Libertadores, Sudamericana, Copa Mundial, Conference League).
        # Argentine domestic + Brasileirão + MLS flow through the WebSocket
        # subdomain, not this XML pattern.
        assert set(s._competitions.keys()) == {6674, 36146, 36148, 63057, 42958}
        await client.aclose()

    async def test_custom_competitions_override(self) -> None:
        client = _make_client(_competition_routes_handler({}))
        s = BplayPbaScraper(http_client=client, competitions={63057: "World Cup only"})
        assert s._competitions == {63057: "World Cup only"}
        await client.aclose()


# ---- 404 handling: empty competition ----


class TestEmptyCompetition:
    async def test_404_skips_competition_silently(self) -> None:
        """Bplay 404 = "no current offers for this competition", not an
        error. The scraper should log + continue, not raise."""
        # Only Libertadores returns data; the others 404 (default behaviour).
        client = _make_client(_competition_routes_handler({36146: (200, _FEED_LIBERTADORES)}))
        s = BplayPbaScraper(http_client=client)
        snaps = [snap async for snap in s.fetch_live_soccer()]
        # We get snapshots from the one served competition; 404s are
        # silent skips (no exception, no zero-output panic).
        assert snaps  # non-empty
        events = {snap.platform_event_id for snap in snaps}
        assert events == {"11428880"}
        await client.aclose()


# ---- HTTP / parse errors → BplayContractError ----


class TestContractErrors:
    async def test_non_404_4xx_logs_and_skips(self) -> None:
        """A 403 (Cloudflare-style block) or 500 is a contract error
        but is contained per-competition — the scraper logs + skips,
        consistent with Betsson's per-fixture skip pattern."""

        def handler(req: httpx.Request) -> httpx.Response:
            cid = req.url.path[len("/oddsfeeds/odds-competition") : -4]
            if cid == "36146":
                return httpx.Response(
                    200, content=_FEED_LIBERTADORES, headers={"content-type": "application/xml"}
                )
            return httpx.Response(503)

        client = _make_client(handler)
        s = BplayPbaScraper(http_client=client)
        snaps = [snap async for snap in s.fetch_live_soccer()]
        # Other competitions 503; Libertadores still produces.
        assert snaps
        assert {snap.platform_event_id for snap in snaps} == {"11428880"}
        await client.aclose()

    async def test_malformed_xml_raises_then_skips(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=b"<<<not xml>>>", headers={"content-type": "application/xml"}
            )

        client = _make_client(handler)
        s = BplayPbaScraper(http_client=client, competitions={6674: "UCL"})
        snaps = [snap async for snap in s.fetch_live_soccer()]
        # Parse error caught at the competition level → skipped, no
        # snapshots, no exception bubbles up.
        assert snaps == []
        await client.aclose()


# ---- Odds extraction ----


class TestOddsExtraction:
    async def test_emits_target_markets_only(self) -> None:
        """1-X-2, Más/Menos, DNB, AH, BTTS-prefix → emitted. Half-time
        result and other unknown markets → skipped."""
        client = _make_client(_competition_routes_handler({36146: (200, _FEED_LIBERTADORES)}))
        s = BplayPbaScraper(http_client=client)
        snaps = [snap async for snap in s.fetch_live_soccer()]
        market_names = {snap.raw_market_name for snap in snaps}
        # Expected markets
        assert "1-X-2" in market_names
        assert "1-2" in market_names
        assert "Más de / Menos de 2.5" in market_names
        assert "Handicap 1-2 -2.5" in market_names
        assert "Ambos equipos anotan" in market_names
        # The 7.5 over/under line is also a target market — only 1 of 2
        # outcomes valid (Menos has odds=1.00, dropped); we still emit Más.
        assert "Más de / Menos de 7.5" in market_names
        # Unknown market explicitly NOT emitted
        assert "Resultado al medio tiempo" not in market_names
        await client.aclose()

    async def test_1x2_outcomes_intact(self) -> None:
        client = _make_client(_competition_routes_handler({36146: (200, _FEED_LIBERTADORES)}))
        s = BplayPbaScraper(http_client=client)
        snaps = [snap async for snap in s.fetch_live_soccer()]
        mw3w = [snap for snap in snaps if snap.raw_market_name == "1-X-2"]
        assert len(mw3w) == 3
        outcomes = {snap.raw_outcome_name: snap.decimal_odds for snap in mw3w}
        assert outcomes == {
            "Mirassol FC SP": pytest.approx(5.30),
            "Empate": pytest.approx(4.00),
            "Always Ready": pytest.approx(1.55),
        }
        await client.aclose()

    async def test_ou_line_in_market_name_and_ids(self) -> None:
        client = _make_client(_competition_routes_handler({36146: (200, _FEED_LIBERTADORES)}))
        s = BplayPbaScraper(http_client=client)
        snaps = [snap async for snap in s.fetch_live_soccer()]
        ou_25 = [snap for snap in snaps if snap.raw_market_name == "Más de / Menos de 2.5"]
        assert len(ou_25) == 2
        for snap in ou_25:
            # Line value embedded in the platform_market_id
            assert snap.platform_market_id == "m11428880-1713500932-2.5"
        # outcome names preserved with accents
        outcomes = {snap.raw_outcome_name: snap.decimal_odds for snap in ou_25}
        assert outcomes == {"Más": pytest.approx(1.77), "Menos": pytest.approx(2.05)}
        await client.aclose()

    async def test_sub_unity_odds_dropped(self) -> None:
        """O/U 7.5 had Más=50.00 and Menos=1.00. Sub-unity odds are
        either suspended or nonsense — drop, don't emit."""
        client = _make_client(_competition_routes_handler({36146: (200, _FEED_LIBERTADORES)}))
        s = BplayPbaScraper(http_client=client)
        snaps = [snap async for snap in s.fetch_live_soccer()]
        ou_75 = [snap for snap in snaps if snap.raw_market_name == "Más de / Menos de 7.5"]
        # Only "Más" survived
        assert len(ou_75) == 1
        assert ou_75[0].raw_outcome_name == "Más"
        await client.aclose()

    async def test_outright_only_competition_emits_nothing(self) -> None:
        """OutrightList without MatchList → no per-match snapshots.
        Outright bets are real markets but they're tournament-winner
        style and don't fit the OddsQuote (event-keyed) shape. Don't
        emit them from this scraper; a dedicated outright pipeline
        can come later if needed."""
        client = _make_client(_competition_routes_handler({6674: (200, _FEED_OUTRIGHTS_ONLY)}))
        s = BplayPbaScraper(http_client=client, competitions={6674: "UCL"})
        snaps = [snap async for snap in s.fetch_live_soccer()]
        assert snaps == []
        await client.aclose()

    async def test_team_elements_preferred_for_event_name(self) -> None:
        client = _make_client(_competition_routes_handler({63057: (200, _FEED_WITH_TEAMS)}))
        s = BplayPbaScraper(http_client=client, competitions={63057: "World Cup"})
        snaps = [snap async for snap in s.fetch_live_soccer()]
        assert snaps
        # Team elements at Match level used directly.
        for snap in snaps:
            assert snap.raw_event_name == "Argentina vs Brasil"
        await client.aclose()

    async def test_fallback_event_name_from_1x2(self) -> None:
        """When <Team> elements are absent, derive home vs away from
        the 1-X-2 offer's outcomes (positions 0 and 2)."""
        client = _make_client(_competition_routes_handler({36146: (200, _FEED_LIBERTADORES)}))
        s = BplayPbaScraper(http_client=client)
        snaps = [snap async for snap in s.fetch_live_soccer()]
        assert snaps
        for snap in snaps:
            assert snap.raw_event_name == "Mirassol FC SP vs Always Ready"
        await client.aclose()

    async def test_snapshot_platform_ids(self) -> None:
        client = _make_client(_competition_routes_handler({36146: (200, _FEED_LIBERTADORES)}))
        s = BplayPbaScraper(http_client=client)
        snap = await anext(s.fetch_live_soccer())
        assert snap.platform == "bplay-pba"
        assert snap.platform_event_id == "11428880"
        # Market and outcome IDs encode the match+type+line+outcome
        assert snap.platform_market_id.startswith("m11428880-")
        assert snap.platform_outcome_id.startswith(snap.platform_market_id + "-")
        # Slug folds accents: "Más" → "mas"
        if snap.raw_outcome_name == "Más":
            assert snap.platform_outcome_id.endswith("-mas")
        assert snap.max_stake is None
        assert snap.timestamp > 0
        await client.aclose()


# ---- Request shape ----


class TestRequestShape:
    async def test_one_get_per_target_competition(self) -> None:
        seen: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req.url.path)
            return httpx.Response(404)

        client = _make_client(handler)
        s = BplayPbaScraper(http_client=client)
        async for _ in s.fetch_live_soccer():
            pass
        # One GET per competition in the default list
        assert sorted(seen) == sorted(
            f"/oddsfeeds/odds-competition{cid}.xml" for cid in (6674, 36146, 36148, 63057, 42958)
        )
        await client.aclose()

    async def test_base_url_unchanged(self) -> None:
        """Regression guard: BASE_URL is part of the recon-frozen
        contract. A future refactor that points the scraper at a
        different host would silently break production."""
        assert BASE_URL == "https://deportespba.bplay.bet.ar"
