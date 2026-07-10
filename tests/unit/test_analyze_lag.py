"""Unit tests for the lag-structure analysis — synthetic data with known answers.

Two platforms, scripted moves with a known 30s lag and known poll gaps.
The tests verify the analysis recovers leader_share, lag quantiles, and
arb-window durations from this controlled input.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.analyze_lag_structure import (
    GARCH_SCALE,
    arb_windows,
    attribute_leaders,
    build_artifact,
    discrepancy_variance,
    extract_moves_per_outcome,
    fit_garch_per_market_type,
    implied_prob,
    market_type_from_code,
)


def _make_move_df(
    leader: str = "betano",
    laggard: str = "betsson-pba",
    lag_sec: float = 30.0,
    poll_sec: float = 120.0,
    n_moves: int = 60,
    start: float = 1_000_000.0,
    fixture_key: str = "boca|river|2026-07-10",
    market_type: str = "1x2",
    cell: str = "HOME",
) -> pd.DataFrame:
    """Build a synthetic moves DataFrame with a known leader→laggard lag.

    Each ``poll_sec``, both platforms observe. The leader moves first; the
    laggard follows ``lag_sec`` later. Change rows carry prev_observed_at
    and is_change=True.
    """
    rows: list[dict] = []
    leader_odds = 2.0
    laggard_odds = 2.0

    for i in range(n_moves):
        t = start + i * poll_sec
        # Leader moves at t
        leader_odds += 0.01 * (1 if i % 2 == 0 else -1)
        rows.append(
            {
                "time": pd.Timestamp(t, unit="s", tz="UTC"),
                "platform": leader,
                "platform_outcome_id": f"{leader}-o1",
                "decimal_odds": round(leader_odds, 4),
                "market_code": market_type,
                "line": None,
                "cell": cell,
                "fixture_key": fixture_key,
                "kickoff_utc": pd.Timestamp(1_779_742_576, unit="s", tz="UTC"),
                "transport": "poll",
                "is_change": True,
                "prev_observed_at": pd.Timestamp(t - poll_sec, unit="s", tz="UTC")
                if i > 0
                else None,
                "recorder_session_id": "test-session",
                "session_fixture_id": "fx-test",
            }
        )
        # Laggard follows at t + lag_sec
        laggard_odds = leader_odds  # copy the move
        rows.append(
            {
                "time": pd.Timestamp(t + lag_sec, unit="s", tz="UTC"),
                "platform": laggard,
                "platform_outcome_id": f"{laggard}-o1",
                "decimal_odds": round(laggard_odds, 4),
                "market_code": market_type,
                "line": None,
                "cell": cell,
                "fixture_key": fixture_key,
                "kickoff_utc": pd.Timestamp(1_779_742_576, unit="s", tz="UTC"),
                "transport": "poll",
                "is_change": True,
                "prev_observed_at": pd.Timestamp(t + lag_sec - poll_sec, unit="s", tz="UTC")
                if i > 0
                else None,
                "recorder_session_id": "test-session",
                "session_fixture_id": "fx-test",
            }
        )

    df = pd.DataFrame(rows)
    df["time_epoch"] = df["time"].apply(lambda ts: int(ts.timestamp()))
    mask = df["prev_observed_at"].notna()
    df.loc[mask, "prev_epoch"] = df.loc[mask, "prev_observed_at"].apply(
        lambda ts: int(ts.timestamp())
    )
    df["market_type"] = market_type
    df["join_key"] = fixture_key
    return df.sort_values("time_epoch").reset_index(drop=True)


class TestMarketTypeFromCode:
    def test_plain_code(self) -> None:
        assert market_type_from_code("1x2", None) == "1x2"

    def test_line_market(self) -> None:
        assert market_type_from_code("ou", 2.5) == "ou|2.5"

    def test_nan_line_treated_as_unlined(self) -> None:
        # pandas reads NULL from the nullable odds_snapshots.line col as NaN, not None.
        assert market_type_from_code("1x2", float("nan")) == "1x2"

    def test_none_code(self) -> None:
        assert market_type_from_code(None, None) == ""


class TestImpliedProb:
    def test_basic(self) -> None:
        assert implied_prob(2.0) == 0.5

    def test_below_unity_returns_zero(self) -> None:
        assert implied_prob(0.5) == 0.0


class TestExtractMovesPerOutcome:
    def test_extracts_change_points_with_d_prob(self) -> None:
        df = _make_move_df(n_moves=5)
        moves = extract_moves_per_outcome(df)
        # First move per outcome has no d_prob (diff is NaN) — dropped.
        # Two platforms × 4 remaining moves = 8 change points.
        assert len(moves) > 0
        assert "d_prob" in moves.columns
        assert "censor_lo" in moves.columns
        assert "censor_hi" in moves.columns


class TestAttributeLeaders:
    def test_recovers_known_leader_and_lag(self) -> None:
        """Two platforms, 30s lag, leader='betano' → leader_share≈1.0, lag≈30s."""
        df = _make_move_df(leader="betano", laggard="betsson-pba", lag_sec=30.0, n_moves=60)
        moves = extract_moves_per_outcome(df)
        results = attribute_leaders(moves)

        assert "1x2" in results
        data = results["1x2"]
        assert data["n_matched_moves"] > 0

        # Betano leads every pair
        assert data["leader_share"]["betano"] > 0.8
        assert data["leader_share"]["betsson-pba"] < 0.2

        # Lag midpoint should be near 30s (within poll-gap tolerance)
        assert 20.0 <= data["lag_p50_s"] <= 40.0

        # Censoring bounds: optimistic ≤ midpoint ≤ pessimistic
        lo, hi = data["lag_bounds_p90_s"]
        assert lo <= data["lag_p90_s"] <= hi


class TestDiscrepancyVariance:
    def test_returns_nonzero_spread(self) -> None:
        df = _make_move_df(n_moves=30)
        results = discrepancy_variance(df)
        assert "1x2" in results
        assert results["1x2"] > 0.0


class TestArbWindows:
    def test_detects_overround_dip(self) -> None:
        """Construct a fixture where one platform is persistently off → overround < 1."""
        rows: list[dict] = []
        fixture_key = "teamA|teamB|2026-07-10"
        for t_step in range(20):
            t = 1_000_000 + t_step * 60
            # Platform A: HOME=1.8, AWAY=2.0 → overround = 1/1.8 + 1/2.0 = 1.056
            rows.append(_row("betano", "HOME", 1.8, t, fixture_key, "1x2"))
            rows.append(_row("betano", "AWAY", 2.0, t, fixture_key, "1x2"))
            # Platform B: HOME=2.2, AWAY=3.0 → best across both:
            # HOME best=1.8, AWAY best=2.0 → overround=1.056 (no arb)
            # But at step 5-10, platform B mispriced → HOME=2.5, AWAY=3.5
            # Best HOME=1.8(A), best AWAY=3.0(B)→ no. Let's make B HOME=2.5 AWAY=2.1
            # Then best HOME=1.8(A), best AWAY=2.0(A) → still 1.056
            # For arb: need best odds sum < 1. Make B have HOME=2.5, AWAY=2.2
            # best HOME=2.5(B), best AWAY=2.2(B) → 1/2.5+1/2.2 = 0.4+0.4545=0.8545 < 1 → arb!
            if 5 <= t_step <= 10:
                rows.append(_row("betsson-pba", "HOME", 2.5, t, fixture_key, "1x2"))
                rows.append(_row("betsson-pba", "AWAY", 2.2, t, fixture_key, "1x2"))
            else:
                rows.append(_row("betsson-pba", "HOME", 1.9, t, fixture_key, "1x2"))
                rows.append(_row("betsson-pba", "AWAY", 1.9, t, fixture_key, "1x2"))

        df = pd.DataFrame(rows)
        df["time_epoch"] = df["time"].apply(lambda ts: int(ts.timestamp()))
        df["market_type"] = "1x2"
        df["join_key"] = fixture_key

        results = arb_windows(df)
        assert "1x2" in results
        # The mispriced window (steps 5-10, 6 grid points × 60s = 360s) is an arb
        assert results["1x2"]["count"] >= 1
        assert results["1x2"]["p50_s"] > 0.0


class TestBuildArtifact:
    def test_staleness_rank_omits_sparse_types(self) -> None:
        """Market types with < MIN_MATCHED_MOVES get no staleness_rank entry."""
        leaders = {
            "1x2": {
                "leader_share": {"betano": 0.9, "betwarrior-pba": 0.0, "betsson-pba": 0.1},
                "lag_p50_s": 30.0,
                "lag_p90_s": 60.0,
                "lag_bounds_p90_s": [20.0, 70.0],
                "n_matched_moves": 100,
            }
        }
        spreads = {"1x2": 0.001}
        arb_wins = {"1x2": {"count": 5, "p50_s": 60.0, "p90_s": 120.0}}

        artifact = build_artifact(leaders, spreads, arb_wins, window_days=7)

        assert "1x2" in artifact["staleness_rank"]
        # Laggard (betsson-pba, 0.1 leader share) → staleness = 1 - 0.1 = 0.9
        assert artifact["staleness_rank"]["1x2"]["betsson-pba"] == 0.9
        assert "1x2" in artifact["burst_eligible_market_types"]

    def test_staleness_rank_omits_below_threshold(self) -> None:
        leaders = {
            "1x2": {
                "leader_share": {"betano": 0.6, "betwarrior-pba": 0.3, "betsson-pba": 0.1},
                "lag_p50_s": 10.0,
                "lag_p90_s": 30.0,
                "lag_bounds_p90_s": [5.0, 40.0],
                "n_matched_moves": 10,  # below MIN_MATCHED_MOVES
            }
        }
        artifact = build_artifact(leaders, {}, {}, window_days=7)
        assert "1x2" not in artifact["staleness_rank"]
        assert "1x2" not in artifact["burst_eligible_market_types"]

    def test_artifact_schema_keys(self) -> None:
        artifact = build_artifact({}, {}, {}, window_days=7)
        for key in (
            "generated_at",
            "window_days",
            "hot_loop_platforms",
            "per_market_type",
            "staleness_rank",
            "burst_eligible_market_types",
            "garch_per_market_type",
        ):
            assert key in artifact

    def test_garch_section_carried_verbatim(self) -> None:
        garch = {
            "1x2": {
                "mu": 0.0,
                "omega": 1.0,
                "alpha": 0.1,
                "beta": 0.8,
                "scale": 1000.0,
                "n_series": 5,
            }
        }
        artifact = build_artifact({}, {}, {}, window_days=7, garch=garch)
        assert artifact["garch_per_market_type"] == garch

    def test_garch_section_defaults_empty(self) -> None:
        artifact = build_artifact({}, {}, {}, window_days=7)
        assert artifact["garch_per_market_type"] == {}


def _row(platform: str, cell: str, odds: float, t: float, fk: str, code: str) -> dict:
    return {
        "time": pd.Timestamp(t, unit="s", tz="UTC"),
        "platform": platform,
        "platform_outcome_id": f"{platform}-{cell}",
        "decimal_odds": odds,
        "market_code": code,
        "line": None,
        "cell": cell,
        "fixture_key": fk,
        "kickoff_utc": pd.Timestamp(1_779_742_576, unit="s", tz="UTC"),
        "transport": "poll",
        "is_change": True,
        "prev_observed_at": pd.Timestamp(t - 60, unit="s", tz="UTC"),
        "recorder_session_id": "test",
        "session_fixture_id": "fx-test",
    }


# ----- B3b: GARCH(1,1) per-market-type fit -----


def _make_garch_spread_frame(
    n_fixtures: int = 3,
    n_grid: int = 120,
    market_type: str = "1x2",
    cell: str = "HOME",
    seed: int = 42,
) -> pd.DataFrame:
    """Build a synthetic raw frame whose per-(fixture, cell) spread follows a
    stationary GARCH(1,1) (omega=0.5, alpha=0.3, beta=0.5), so arch's MLE recovers a
    stationary fit. Each fixture has ``n_grid`` 60s-spaced grid points with betano +
    betsson-pba both present; the spread = prob_betano − prob_betsson."""
    rng = np.random.default_rng(seed)
    rows: list[dict] = []
    omega, alpha, beta = 0.5, 0.3, 0.5
    for fi in range(n_fixtures):
        fk = f"team{fi}|team{fi + 1}|2026-07-10"
        sigma2 = 1.0
        eps_prev = 0.0
        for gi in range(n_grid):
            t = 1_000_000.0 + gi * 60.0
            sigma2 = omega + alpha * eps_prev**2 + beta * sigma2
            eps = np.sqrt(sigma2) * rng.standard_normal()
            spread = eps / GARCH_SCALE  # unscaled; spread.values * GARCH_SCALE == eps
            prob_b = 0.4
            prob_a = max(0.35, min(0.45, 0.4 + spread))
            rows.append(_row("betano", cell, round(1.0 / prob_a, 4), t, fk, market_type))
            rows.append(_row("betsson-pba", cell, round(1.0 / prob_b, 4), t, fk, market_type))
            eps_prev = eps
    df = pd.DataFrame(rows)
    df["time_epoch"] = df["time"].apply(lambda ts: int(ts.timestamp()))
    df["market_type"] = market_type
    df["join_key"] = df["fixture_key"]
    return df


class TestFitGarchPerMarketType:
    def test_three_series_yields_stationary_fit(self) -> None:
        """Three distinct fixtures (≥120 grid points each) → ``"1x2"`` entry with all
        six fields, ``n_series == 3``, stationary (alpha+beta < 0.999), scale 1000.
        Structural assertions only — arch MLE on synthetic data isn't exact."""
        df = _make_garch_spread_frame(n_fixtures=3, n_grid=120)
        result = fit_garch_per_market_type(df)
        assert "1x2" in result
        entry = result["1x2"]
        for key in ("mu", "omega", "alpha", "beta", "scale", "n_series"):
            assert key in entry
        assert entry["n_series"] == 3
        assert entry["alpha"] + entry["beta"] < 0.999
        assert entry["scale"] == GARCH_SCALE

    def test_two_series_omitted_below_min_fits(self) -> None:
        """Only two qualifying series (< MIN_GARCH_FITS=3) → market type omitted."""
        df = _make_garch_spread_frame(n_fixtures=2, n_grid=120)
        assert fit_garch_per_market_type(df) == {}

    def test_short_series_omitted(self) -> None:
        """Series shorter than MIN_GARCH_SERIES_LEN (50 grid points) → no qualifying
        series → market type omitted."""
        df = _make_garch_spread_frame(n_fixtures=3, n_grid=20)
        assert fit_garch_per_market_type(df) == {}
