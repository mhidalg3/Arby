"""Unit tests for the Bplay PBA SSE scraper.

Tests mock httpx at the transport boundary using MockTransport and
feed back synthetic SSE-formatted bytes. The synthetic payloads
mirror the actual shapes captured during recon on 2026-05-26 (see
`scripts/recon/RECON_LOG.md` — bplay SSE protocol decoded).
"""

from __future__ import annotations

import json

import httpx
import pytest

from src.ingestion.scrapers.bplay_sse import (
    BplayPbaSSEScraper,
    _extract_ou_line,
    _snapshots_from_odds_payload,
)

# ---- Synthetic SSE bodies (matching the recon-captured shapes) ----

# /en-vivo SSR HTML with two live match IDs embedded.
_EN_VIVO_HTML: bytes = (
    b"<!doctype html>"
    b'<html><body><div data-live-id="13925270">match-1</div>'
    b'<div data-live-id="13925271">match-2</div>'
    # The SPA SSR also embeds `matchId:"..."` patterns; the scraper
    # greps for those, not the data-live-id.
    b'<script>window.__NUXT__ = {events: [{matchId:"13925270",x:1}, {matchId:"13925271",y:2}]}</script>'
    b"</body></html>"
)


def _odds_event_1x2() -> dict:
    """One `event: odds` payload for a 1X2 market (matches recon shape)."""
    return {
        "match_id": "13925270",
        "odds": [
            {
                "qt": "¿Quién ganará el partido?",
                "qlid": 2133000,
                "nbl": 3,
                "bets": [
                    {
                        "od": 1234,
                        "pid": "abc",
                        "ha": "xyz",
                        "tch": {
                            "c1": {
                                "cid": "SNC_ACTOR_HOME",
                                "ct": 1.58,
                                "ct_dsp": "1.58",
                                "act": "Real Cundinamarca",
                                "td": "",
                            },
                            "c2": {
                                "cid": "SNC_ACTOR_DRAW",
                                "ct": 3.25,
                                "ct_dsp": "3.25",
                                "act": "Empate",
                                "td": "",
                            },
                            "c3": {
                                "cid": "SNC_ACTOR_AWAY",
                                "ct": 6.20,
                                "ct_dsp": "6.20",
                                "act": "Rionegro Águilas",
                                "td": "",
                            },
                        },
                    }
                ],
            }
        ],
    }


def _odds_event_btts() -> dict:
    return {
        "match_id": "13925270",
        "odds": [
            {
                "qt": "Ambos equipos marcan",
                "qlid": 2133023,
                "bets": [
                    {
                        "tch": {
                            "c1": {"cid": "39", "ct": 1.65, "act": "Sí"},
                            "c2": {"cid": "40", "ct": 2.11, "act": "No"},
                        }
                    }
                ],
            }
        ],
    }


def _odds_event_ou(line: float = 2.5) -> dict:
    return {
        "match_id": "13925270",
        "odds": [
            {
                "qt": "Total de Goles",
                "qlid": 2133446,
                "bets": [
                    {
                        "tch": {
                            "c1": {"cid": "30", "ct": 2.35, "act": f"Más de {line}"},
                            "c2": {"cid": "31", "ct": 1.55, "act": f"Menos de {line}"},
                        }
                    }
                ],
            }
        ],
    }


def _match_event() -> dict:
    """`event: match` payload — provides team names for the match_id."""
    return {
        "match_id": "13925270",
        "evt": "Fútbol - Copa Colombia",
        "lb": "Real Cundinamarca / Rionegro Águilas",
        "act1": "Real Cundinamarca",
        "act2": "Rionegro Águilas",
        "tp": 1779830224747,
        "st": "1T",
        "sc": "0:0",
    }


def _build_sse_body(events: list[tuple[str, dict]]) -> bytes:
    """Format a list of (event_type, payload) into raw SSE bytes."""
    parts = [":ok", "retry: 2000"]
    for event_type, payload in events:
        parts.append(f"event: {event_type}")
        parts.append(f"data: {json.dumps(payload, ensure_ascii=False)}")
        parts.append("")  # blank line dispatches the event
    return ("\n".join(parts) + "\n").encode("utf-8")


def _routes_handler(
    en_vivo_body: bytes = _EN_VIVO_HTML,
    sse_body: bytes = b":ok\nretry: 2000\n\n",
):
    """Build a MockTransport handler that returns the en-vivo HTML for
    the live-page URL and the synthetic SSE body for the SSE URL."""

    def _handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "deportespba.bplay.bet.ar":
            return httpx.Response(
                200,
                content=en_vivo_body,
                headers={"content-type": "text/html; charset=utf-8"},
            )
        if req.url.host == "events-deportespba.bplay.bet.ar":
            return httpx.Response(
                200,
                content=sse_body,
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(404)

    return _handler


def _make_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---- Pure parsing tests ----


class TestSnapshotsFromOddsPayload:
    def test_1x2_yields_three_snapshots(self) -> None:
        snaps = _snapshots_from_odds_payload(
            _odds_event_1x2(),
            platform_name="bplay-pba",
            raw_event_name="Real Cundinamarca vs Rionegro Águilas",
        )
        assert len(snaps) == 3
        labels = {s.raw_outcome_name: s.decimal_odds for s in snaps}
        assert labels == {
            "Real Cundinamarca": pytest.approx(1.58),
            "Empate": pytest.approx(3.25),
            "Rionegro Águilas": pytest.approx(6.20),
        }
        for s in snaps:
            assert s.platform == "bplay-pba"
            assert s.platform_event_id == "13925270"
            assert s.raw_market_name == "¿Quién ganará el partido?"
            assert s.raw_event_name == "Real Cundinamarca vs Rionegro Águilas"

    def test_btts_yields_two_snapshots(self) -> None:
        snaps = _snapshots_from_odds_payload(
            _odds_event_btts(),
            platform_name="bplay-pba",
        )
        assert len(snaps) == 2
        labels = {s.raw_outcome_name: s.decimal_odds for s in snaps}
        assert labels == {"Sí": pytest.approx(1.65), "No": pytest.approx(2.11)}
        for s in snaps:
            assert s.raw_market_name == "Ambos equipos marcan"

    def test_ou_format_includes_line(self) -> None:
        """OU `raw_market_name` is formatted as `"Total de Goles 2.5"`
        so the existing market_resolver regex finds the line."""
        snaps = _snapshots_from_odds_payload(
            _odds_event_ou(line=2.5),
            platform_name="bplay-pba",
        )
        assert len(snaps) == 2
        for s in snaps:
            assert s.raw_market_name == "Total de Goles 2.5"
        labels = {s.raw_outcome_name: s.decimal_odds for s in snaps}
        # Outcome `act` still carries the line, but that's fine —
        # outcome_resolver matches on the first token ("mas" / "menos").
        assert labels == {"Más de 2.5": pytest.approx(2.35), "Menos de 2.5": pytest.approx(1.55)}

    def test_ou_integer_line_dropped(self) -> None:
        """Push lines (integer goal totals) — scraper rejects them at
        source, so downstream never sees an invalid OU partition."""
        payload = _odds_event_ou(line=2.5)
        # Mutate to integer line
        for outcome in payload["odds"][0]["bets"][0]["tch"].values():
            outcome["act"] = outcome["act"].replace("2.5", "2")
        snaps = _snapshots_from_odds_payload(payload, platform_name="bplay-pba")
        assert snaps == []

    def test_sub_unity_odds_dropped(self) -> None:
        """Outcomes with odds ≤ 1.0 are either suspended or nonsense."""
        payload = _odds_event_1x2()
        # Set HOME to 1.0
        payload["odds"][0]["bets"][0]["tch"]["c1"]["ct"] = 1.0
        snaps = _snapshots_from_odds_payload(payload, platform_name="bplay-pba")
        # Only DRAW + AWAY survive
        assert len(snaps) == 2
        labels = {s.raw_outcome_name for s in snaps}
        assert labels == {"Empate", "Rionegro Águilas"}

    def test_non_v1_market_skipped(self) -> None:
        payload = {
            "match_id": "13925270",
            "odds": [
                {
                    "qt": "Doble oportunidad",  # Double Chance — not v1
                    "qlid": 2133007,
                    "bets": [{"tch": {"c1": {"cid": "7", "ct": 1.5, "act": "X o Y"}}}],
                }
            ],
        }
        snaps = _snapshots_from_odds_payload(payload, platform_name="bplay-pba")
        assert snaps == []

    def test_missing_match_id_returns_empty(self) -> None:
        assert _snapshots_from_odds_payload({}, platform_name="bplay-pba") == []
        assert (
            _snapshots_from_odds_payload(
                {"odds": [_odds_event_1x2()["odds"][0]]}, platform_name="bplay-pba"
            )
            == []
        )


class TestExtractOuLine:
    def test_extracts_half_line(self) -> None:
        bets = _odds_event_ou(line=2.5)["odds"][0]["bets"]
        assert _extract_ou_line(bets) == 2.5

    def test_rejects_integer_line(self) -> None:
        bets = _odds_event_ou(line=2.0)["odds"][0]["bets"]
        # _odds_event_ou(2.0) puts the literal "2.0" in the act; the
        # regex captures "2.0" but the half-line filter must reject it.
        # Replace with the cleaner integer form to be unambiguous.
        for outcome in bets[0]["tch"].values():
            outcome["act"] = outcome["act"].replace("2.0", "2")
        assert _extract_ou_line(bets) is None

    def test_returns_none_on_no_line(self) -> None:
        bets = [{"tch": {"c1": {"cid": "X", "ct": 1.5, "act": "no number here"}}}]
        assert _extract_ou_line(bets) is None

    def test_implausibly_high_line_rejected(self) -> None:
        """Real-world data showed Bplay pushing nonsensical 54.5/55.5
        OU lines on a U21 soccer match (likely a mislabeled stat-prop
        market). Sanity-bound at 12.0 goals max."""
        bets = [
            {
                "tch": {
                    "c1": {"cid": "30", "ct": 1.95, "act": "Más de 55.5"},
                    "c2": {"cid": "31", "ct": 1.75, "act": "Menos de 55.5"},
                }
            }
        ]
        assert _extract_ou_line(bets) is None


# ---- End-to-end scraper integration tests ----


class TestFetchLiveSoccerEnd2End:
    async def test_full_flow_yields_v1_snapshots(self) -> None:
        sse_body = _build_sse_body(
            [
                ("match", _match_event()),  # name first
                ("odds", _odds_event_1x2()),
                ("odds", _odds_event_btts()),
                ("odds", _odds_event_ou(2.5)),
            ]
        )
        client = _make_client(_routes_handler(sse_body=sse_body))
        s = BplayPbaSSEScraper(http_client=client, stream_duration_sec=2.0)
        snaps = [snap async for snap in s.fetch_live_soccer()]
        # 3 (1X2) + 2 (BTTS) + 2 (OU) = 7
        assert len(snaps) == 7
        markets = {snap.raw_market_name for snap in snaps}
        assert "¿Quién ganará el partido?" in markets
        assert "Ambos equipos marcan" in markets
        assert "Total de Goles 2.5" in markets
        # All snapshots carry the team-name event label from the
        # preceding `match` event
        for snap in snaps:
            assert snap.raw_event_name == "Real Cundinamarca vs Rionegro Águilas"
        await client.aclose()

    async def test_no_discovered_matches_yields_nothing(self) -> None:
        empty_html = b"<html><body>no matches today</body></html>"
        client = _make_client(_routes_handler(en_vivo_body=empty_html))
        # idle_rediscovery_backoff_sec=0 so the no-match path doesn't
        # sleep the production default (20s) during the test.
        s = BplayPbaSSEScraper(
            http_client=client,
            stream_duration_sec=1.0,
            idle_rediscovery_backoff_sec=0.0,
        )
        snaps = [snap async for snap in s.fetch_live_soccer()]
        assert snaps == []
        await client.aclose()

    async def test_odds_before_match_yields_empty_event_name(self) -> None:
        """If an odds event arrives before the corresponding match
        event (rare race condition), the snapshot has an empty
        raw_event_name. canonicalizer's fixture_resolver will drop
        it; the next odds event will succeed once the match event
        arrives and populates the map."""
        sse_body = _build_sse_body(
            [
                ("odds", _odds_event_1x2()),  # no match event before it
            ]
        )
        client = _make_client(_routes_handler(sse_body=sse_body))
        s = BplayPbaSSEScraper(http_client=client, stream_duration_sec=2.0)
        snaps = [snap async for snap in s.fetch_live_soccer()]
        # Snapshots emitted but with empty raw_event_name
        assert len(snaps) == 3
        for snap in snaps:
            assert snap.raw_event_name == ""
        await client.aclose()

    async def test_subscription_url_contains_pipe_separated_ids(self) -> None:
        seen_urls: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.host == "deportespba.bplay.bet.ar":
                return httpx.Response(
                    200,
                    content=_EN_VIVO_HTML,
                    headers={"content-type": "text/html"},
                )
            if req.url.host == "events-deportespba.bplay.bet.ar":
                seen_urls.append(str(req.url))
                return httpx.Response(
                    200,
                    content=_build_sse_body([]),
                    headers={"content-type": "text/event-stream"},
                )
            return httpx.Response(404)

        client = _make_client(handler)
        s = BplayPbaSSEScraper(http_client=client, stream_duration_sec=1.0)
        async for _ in s.fetch_live_soccer():
            pass
        assert len(seen_urls) == 1
        url = seen_urls[0]
        # IDs from the en-vivo HTML are 13925270 and 13925271 (sorted).
        # URL-encoded pipe is %7C.
        assert "id=13925270%7C13925271" in url
        assert "mode=v2" in url
        assert "partner=1147" in url
        assert "lang=ag" in url
        assert "odds_format=dec" in url
        await client.aclose()
