"""Bootstrap analysis of the cross-platform update-lag structure.

Reads the ``odds_snapshots`` hypertable and produces:
  - B1: Move extraction (change points with censoring intervals)
  - B2: Leader/lag attribution per market_type (with censoring bounds)
  - B3: Discrepancy variance D(M) per market_type
  - B4: Empirical arb-window durations
  - B5: Placement-half audit (reads opportunities + placements; runs today)
  - B6: Machine artifact ``data/lag_model.json`` (``--write-artifact``)

Usage:
  uv run python scripts/analyze_lag_structure.py [--days N] [--write-artifact] [--garch] [--execution-audit]

No Hurst analysis (AGENTS.md decided-against). GARCH(1,1) is the one endorsed
model (architecture.md:33,105-107); it's an optional estimator behind ``--garch``.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import structlog

from src.arbitrage.garch import PAIR_ORDER
from src.config import get_settings

log = structlog.get_logger(__name__)

HOT_LOOP_PLATFORMS = ["betano", "betwarrior-pba", "betsson-pba"]
MATCH_WINDOW_SEC = 600.0
MIN_MATCHED_MOVES = 50
MIN_LAG_P90_SEC = 60.0
ARB_MARGIN_FLOOR = 0.002  # 0.2% — skip float dust in overround < 1
# GARCH(1,1) per-market-type fit thresholds. Scale keeps arch's MLE numerically
# stable (spread x scale); stored in the artifact so the live side unscales.
GARCH_SCALE = 1000.0
MIN_GARCH_SERIES_LEN = 50  # grid points per (fixture, cell) series
MIN_GARCH_FITS = 3  # accepted per-series fits required per market type


def market_type_from_code(code: str | None, line: float | None) -> str:
    """Derive the market_type key from canonical columns.

    Matches the ``canonical_market_id`` suffix grammar: ``h2h_3way`` / ``btts``
    / ``ou|2.5``. ``code`` here is the raw StrEnum value (``1x2``, ``btts``,
    ``ou``).
    """
    if code is None:
        return ""
    if line is not None and line == line:  # NaN (from nullable PG line col) == bare code
        return f"{code}|{line:g}"
    return code


def load_snapshots(days: int, db_url: str) -> pd.DataFrame:
    """Load change+heartbeat rows from the hypertable for the last ``days`` days."""
    since = datetime.now(UTC) - timedelta(days=days)
    sql = """
        SELECT time, platform, platform_outcome_id, decimal_odds,
               market_code, line, cell, fixture_key, kickoff_utc,
               transport, is_change, prev_observed_at,
               recorder_session_id, session_fixture_id
        FROM odds_snapshots
        WHERE time >= %(since)s
        ORDER BY time
    """
    frames = list(pd.read_sql(sql, db_url, params={"since": since}, chunksize=100_000))
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df["time_epoch"] = df["time"].astype("int64") // 10**9
    if "prev_observed_at" in df.columns:
        mask = df["prev_observed_at"].notna()
        df.loc[mask, "prev_epoch"] = df.loc[mask, "prev_observed_at"].astype("int64") // 10**9
    else:
        df["prev_epoch"] = np.nan
    df["market_type"] = df.apply(
        lambda r: market_type_from_code(r.get("market_code"), r.get("line")), axis=1
    )

    # Unified cross-platform join key: fixture_key when kickoff is known,
    # else (recorder_session_id, session_fixture_id) for the session-scoped
    # fallback (the canonicalizer's in-process links are consistent within one
    # recording session). Rows with neither are dropped by the analysis.
    def _join_key(r: pd.Series) -> str | None:
        fk = r.get("fixture_key")
        if pd.notna(fk) and fk:
            return str(fk)
        sid = r.get("session_fixture_id")
        rsid = r.get("recorder_session_id", "")
        if pd.notna(sid) and sid:
            return f"session:{rsid}:{sid}"
        return None

    df["join_key"] = df.apply(_join_key, axis=1)
    return df


def implied_prob(odds: float) -> float:
    return 1.0 / odds if odds > 1.0 else 0.0


# ----- B1: Move extraction -----


def extract_moves_per_outcome(df: pd.DataFrame) -> pd.DataFrame:
    """Extract per-outcome change points with correct Δimplied-prob.

    Groups by (platform, platform_outcome_id), computes the implied-prob diff
    between consecutive change rows, and attaches the censoring interval.
    """
    if df.empty:
        return pd.DataFrame()
    changes = df[df["is_change"] & df["prev_epoch"].notna()].copy()
    if changes.empty:
        return pd.DataFrame()
    changes = changes.sort_values(["platform", "platform_outcome_id", "time_epoch"])
    changes["prob"] = changes["decimal_odds"].apply(implied_prob)
    changes["d_prob"] = changes.groupby(["platform", "platform_outcome_id"])["prob"].diff()
    changes = changes.dropna(subset=["d_prob"])
    changes["censor_lo"] = changes["prev_epoch"]
    changes["censor_hi"] = changes["time_epoch"]
    return changes


# ----- B2: Leader/lag attribution -----


def attribute_leaders(moves: pd.DataFrame) -> dict[str, dict]:
    """Per market_type: leader_share, lag quantiles, n_matched_moves.

    Matches moves across platforms on the same canonical outcome (fixture_key +
    market_type + cell), same direction (sign of d_prob), within MATCH_WINDOW_SEC.
    """
    results: dict[str, dict] = {}
    if moves.empty:
        return results

    for mt, mt_group in moves.groupby("market_type"):
        if not mt:
            continue
        # Only hot-loop platforms
        mt_group = mt_group[mt_group["platform"].isin(HOT_LOOP_PLATFORMS)]
        if mt_group.empty:
            continue

        # Group by canonical outcome (join_key + cell)
        lags_mid: list[float] = []
        lags_lo: list[float] = []
        lags_hi: list[float] = []
        leader_counts: dict[str, int] = {p: 0 for p in HOT_LOOP_PLATFORMS}
        total_pairs = 0

        for (fk, _cell), outcome_group in mt_group.groupby(["join_key", "cell"]):
            if fk is None:
                continue
            # Per-platform moves, sorted by time
            moves_by_platform: dict[str, pd.DataFrame] = {}
            for platform, pg in outcome_group.groupby("platform"):
                pg = pg.sort_values("time_epoch")
                moves_by_platform[platform] = pg

            # Pairwise matching between platforms (nearest-in-time, same direction)
            platforms = list(moves_by_platform.keys())
            used: set[tuple[str, int]] = set()  # (platform, row_idx)
            for i in range(len(platforms)):
                for j in range(i + 1, len(platforms)):
                    pa, pb = platforms[i], platforms[j]
                    ma, mb = moves_by_platform[pa], moves_by_platform[pb]
                    for ia, ra in ma.iterrows():
                        if (pa, ia) in used:
                            continue
                        # Find nearest unused same-direction move in pb
                        best_jb = None
                        best_dt = float("inf")
                        for jb, rb in mb.iterrows():
                            if (pb, jb) in used:
                                continue
                            if np.sign(ra["d_prob"]) != np.sign(rb["d_prob"]):
                                continue
                            dt = abs(ra["time_epoch"] - rb["time_epoch"])
                            if dt < best_dt:
                                best_dt = dt
                                best_jb = jb
                        if best_jb is not None and best_dt <= MATCH_WINDOW_SEC:
                            rb = mb.loc[best_jb]
                            used.add((pa, ia))
                            used.add((pb, best_jb))
                            total_pairs += 1
                            # Determine leader/laggard, then compute directional bounds
                            t_ra, t_rb = ra["time_epoch"], rb["time_epoch"]
                            prev_ra, prev_rb = ra["censor_lo"], rb["censor_lo"]
                            if t_ra <= t_rb:
                                leader_counts[pa] = leader_counts.get(pa, 0) + 1
                                t_lead, t_follow = t_ra, t_rb
                                prev_lead, prev_follow = prev_ra, prev_rb
                            else:
                                leader_counts[pb] = leader_counts.get(pb, 0) + 1
                                t_lead, t_follow = t_rb, t_ra
                                prev_lead, prev_follow = prev_rb, prev_ra
                            lag_mid = t_follow - t_lead
                            # Pessimistic: leader could have moved at prev_lead → longest lag
                            lag_pess = (
                                t_follow - prev_lead
                                if pd.notna(prev_lead) and prev_lead
                                else lag_mid
                            )
                            # Optimistic: laggard could have moved at prev_follow → shortest lag
                            lag_opt = (
                                max(0.0, prev_follow - t_lead)
                                if pd.notna(prev_follow) and prev_follow
                                else lag_mid
                            )
                            lags_mid.append(lag_mid)
                            lags_lo.append(lag_opt)
                            lags_hi.append(lag_pess)

        if total_pairs == 0:
            continue

        leader_share = {p: leader_counts.get(p, 0) / total_pairs for p in HOT_LOOP_PLATFORMS}
        lag_mid_arr = np.array(lags_mid) if lags_mid else np.array([0.0])
        lag_lo_arr = np.array(lags_lo) if lags_lo else np.array([0.0])
        lag_hi_arr = np.array(lags_hi) if lags_hi else np.array([0.0])

        results[mt] = {
            "leader_share": leader_share,
            "lag_p50_s": float(np.percentile(lag_mid_arr, 50)),
            "lag_p90_s": float(np.percentile(lag_mid_arr, 90)),
            "lag_bounds_p90_s": [
                float(np.percentile(lag_lo_arr, 90)),
                float(np.percentile(lag_hi_arr, 90)),
            ],
            "n_matched_moves": total_pairs,
        }

    return results


# ----- B3: Discrepancy variance D(M) -----


def discrepancy_variance(df: pd.DataFrame) -> dict[str, float]:
    """Per market_type: std of the time-aligned cross-platform implied-prob spread."""
    results: dict[str, float] = {}
    if df.empty:
        return results

    for mt, mt_group in df.groupby("market_type"):
        if not mt:
            continue
        mt_group = mt_group[mt_group["platform"].isin(HOT_LOOP_PLATFORMS)]
        if mt_group["platform"].nunique() < 2:
            continue

        spreads: list[float] = []
        for (fk, _cell), og in mt_group.groupby(["join_key", "cell"]):
            if fk is None:
                continue
            # Time-align on 60s grid, forward-fill ≤120s
            og = og.sort_values("time_epoch").copy()
            og["prob"] = og["decimal_odds"].apply(implied_prob)
            og["grid"] = (og["time_epoch"] // 60) * 60
            # Pivot: grid × platform → prob (last value per grid cell)
            pivoted = og.drop_duplicates(["grid", "platform"], keep="last")
            pivot = pivoted.pivot(index="grid", columns="platform", values="prob")
            pivot = pivot.ffill(limit=2)
            # Pairwise spreads
            platforms = [p for p in HOT_LOOP_PLATFORMS if p in pivot.columns]
            for i in range(len(platforms)):
                for j in range(i + 1, len(platforms)):
                    pa, pb = platforms[i], platforms[j]
                    spread = (pivot[pa] - pivot[pb]).dropna()
                    if len(spread) > 5:
                        spreads.extend(spread.tolist())

        if spreads:
            results[mt] = float(np.std(spreads))

    return results


# ----- B3b: GARCH(1,1) per-market-type fit -----


def fit_garch_per_market_type(df: pd.DataFrame) -> dict[str, dict]:
    """Per market_type: median GARCH(1,1) params across per-(join_key, cell) spread
    series. Series pair rule = ``garch.PAIR_ORDER`` (first two present), identical to
    the live extractor. Fits with omega<=0, alpha<0, beta<0 or alpha+beta>=0.999
    (non-stationary) are discarded; per-series fit errors are caught and skipped.
    Market types with fewer than ``MIN_GARCH_FITS`` accepted fits are omitted (too
    thin to trust).

    Series construction mirrors ``discrepancy_variance`` exactly (60s grid, last
    value per grid cell, ffill limit 2) but with the PAIR_ORDER first-two rule
    instead of all-pairwise — so fitted units and the live extractor's units can
    never drift."""
    from arch import arch_model  # local import — heavy, only under --garch

    results: dict[str, dict] = {}
    if df.empty:
        return results

    for mt, mt_group in df.groupby("market_type"):
        if not mt:
            continue
        mt_group = mt_group[mt_group["platform"].isin(PAIR_ORDER)]
        if mt_group["platform"].nunique() < 2:
            continue

        accepted_mu: list[float] = []
        accepted_omega: list[float] = []
        accepted_alpha: list[float] = []
        accepted_beta: list[float] = []

        for (fk, _cell), og in mt_group.groupby(["join_key", "cell"]):
            if fk is None:
                continue  # orphan rows (no fixture) would pool into bogus series
            og = og.sort_values("time_epoch").copy()
            og["prob"] = og["decimal_odds"].apply(implied_prob)
            og["grid"] = (og["time_epoch"] // 60) * 60
            pivoted = og.drop_duplicates(["grid", "platform"], keep="last")
            pivot = pivoted.pivot(index="grid", columns="platform", values="prob")
            pivot = pivot.ffill(limit=2)
            # PAIR_ORDER first-two-present rule (shared with the live extractor).
            present = [p for p in PAIR_ORDER if p in pivot.columns]
            if len(present) < 2:
                continue
            spread = (pivot[present[0]] - pivot[present[1]]).dropna()
            if len(spread) < MIN_GARCH_SERIES_LEN:
                continue
            try:
                am = arch_model(
                    spread.values * GARCH_SCALE,
                    vol="Garch",
                    p=1,
                    q=1,
                    mean="Constant",
                    rescale=False,
                )
                res = am.fit(disp="off")
                mu = float(res.params["mu"])
                omega = float(res.params["omega"])
                alpha = float(res.params["alpha[1]"])
                beta = float(res.params["beta[1]"])
            except Exception:  # noqa: BLE001 — a bad series skips, never aborts the fit
                continue
            if omega <= 0.0 or alpha < 0.0 or beta < 0.0 or alpha + beta >= 0.999:
                continue  # non-stationary / infeasible → discard
            accepted_mu.append(mu)
            accepted_omega.append(omega)
            accepted_alpha.append(alpha)
            accepted_beta.append(beta)

        if len(accepted_mu) >= MIN_GARCH_FITS:
            results[mt] = {
                "mu": float(np.median(accepted_mu)),
                "omega": float(np.median(accepted_omega)),
                "alpha": float(np.median(accepted_alpha)),
                "beta": float(np.median(accepted_beta)),
                "scale": GARCH_SCALE,
                "n_series": len(accepted_mu),
            }

    return results


# ----- B4: Arb-window durations -----


def arb_windows(df: pd.DataFrame) -> dict[str, dict]:
    """Per market_type: count + p50/p90 duration of overround < 1 windows."""
    results: dict[str, dict] = {}
    if df.empty:
        return results

    for mt, mt_group in df.groupby("market_type"):
        if not mt:
            continue
        mt_group = mt_group[mt_group["platform"].isin(HOT_LOOP_PLATFORMS)]
        if mt_group.empty:
            continue

        durations: list[float] = []
        for fk, fg in mt_group.groupby("join_key"):
            if fk is None:
                continue
            fg = fg.sort_values("time_epoch").copy()
            fg["prob"] = fg["decimal_odds"].apply(implied_prob)
            fg["grid"] = (fg["time_epoch"] // 60) * 60
            # Best odds per cell per grid point
            best = fg.groupby(["grid", "cell"])["prob"].min().reset_index()
            overround = best.groupby("grid")["prob"].sum()
            # Windows: maximal runs where overround < 1 - margin_floor
            is_arb = overround < (1.0 - ARB_MARGIN_FLOOR)
            in_window = False
            start = 0
            for idx, val in is_arb.items():
                if val and not in_window:
                    in_window = True
                    start = idx
                elif not val and in_window:
                    in_window = False
                    durations.append((idx - start) * 60.0)
            if in_window:
                durations.append((is_arb.index[-1] - start + 1) * 60.0)

        if durations:
            arr = np.array(durations)
            results[mt] = {
                "count": len(durations),
                "p50_s": float(np.percentile(arr, 50)),
                "p90_s": float(np.percentile(arr, 90)),
            }
        else:
            results[mt] = {"count": 0, "p50_s": 0.0, "p90_s": 0.0}

    return results


# ----- B5: Placement-half audit -----


def execution_audit(db_url: str) -> None:
    """Read opportunities + placements; print a per-platform drift summary."""
    print("\n" + "=" * 60)
    print("B5: Placement-half audit (opportunities + placements)")
    print("=" * 60)
    try:
        opps = pd.read_sql("SELECT * FROM opportunities", db_url)
        placements = pd.read_sql("SELECT * FROM placements", db_url)
    except Exception as exc:
        print(f"  (could not read: {exc})")
        return

    print(f"\n  Opportunities: {len(opps)} rows")
    print(f"  Placements:    {len(placements)} rows")

    if placements.empty:
        print("  (no placements to audit)")
        return

    # Per-platform: target vs filled odds delta on successful legs
    filled = placements[placements["success"] == True]  # noqa: E712
    if not filled.empty:
        filled = filled.copy()
        filled["odds_delta"] = filled["actual_filled_odds"] - filled["target_odds"]
        print("\n  Per-platform filled-odds drift (successful legs):")
        for platform, pg in filled.groupby("platform"):
            print(f"    {platform:20s}  n={len(pg):3d}  mean_drift={pg['odds_delta'].mean():+.4f}")

    # Execution reason / status breakdown
    if "error_message" in placements.columns:
        failed = placements[placements["success"].isna() | (placements["success"] == False)]  # noqa: E712
        if not failed.empty:
            print(f"\n  Failed/aborted placements: {len(failed)}")
            for err, eg in failed.groupby(failed["error_message"].fillna("(none)")):
                print(f"    {str(err)[:60]:60s}  n={len(eg)}")


# ----- B6: Artifact -----
def build_artifact(
    leaders: dict[str, dict],
    spreads: dict[str, float],
    arb_wins: dict[str, dict],
    window_days: int,
    garch: dict[str, dict] | None = None,
) -> dict:
    """Build the machine artifact ``data/lag_model.json``."""
    per_market_type: dict[str, dict] = {}
    staleness_rank: dict[str, dict[str, float]] = {}

    for mt, data in leaders.items():
        entry = {
            "leader_share": data["leader_share"],
            "lag_p50_s": data["lag_p50_s"],
            "lag_p90_s": data["lag_p90_s"],
            "lag_bounds_p90_s": data["lag_bounds_p90_s"],
            "spread_std": spreads.get(mt, 0.0),
            "n_matched_moves": data["n_matched_moves"],
            "arb_windows": arb_wins.get(mt, {"count": 0, "p50_s": 0.0, "p90_s": 0.0}),
        }
        per_market_type[mt] = entry

        # staleness_rank: only market types with enough matched moves
        if data["n_matched_moves"] >= MIN_MATCHED_MOVES:
            staleness_rank[mt] = {
                p: round(1.0 - data["leader_share"].get(p, 0.0), 4) for p in HOT_LOOP_PLATFORMS
            }

    burst_eligible = [
        mt
        for mt, data in leaders.items()
        if data["n_matched_moves"] >= MIN_MATCHED_MOVES and data["lag_p90_s"] >= MIN_LAG_P90_SEC
    ]

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "window_days": window_days,
        "hot_loop_platforms": HOT_LOOP_PLATFORMS,
        "per_market_type": per_market_type,
        "staleness_rank": staleness_rank,
        "burst_eligible_market_types": burst_eligible,
        "garch_per_market_type": garch or {},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze cross-platform update-lag structure")
    parser.add_argument("--days", type=int, default=7, help="Lookback window in days")
    parser.add_argument("--write-artifact", action="store_true", help="Write data/lag_model.json")
    parser.add_argument("--garch", action="store_true", help="Fit GARCH(1,1) on spread series")
    parser.add_argument("--execution-audit", action="store_true", help="Print B5 placement audit")
    args = parser.parse_args()

    settings = get_settings()
    db_url = settings.database_url_sync

    print("=" * 60)
    print(f"Cross-platform lag analysis — last {args.days} day(s)")
    print("=" * 60)

    # B5 runs regardless of tick data (reads opportunities + placements)
    if args.execution_audit:
        execution_audit(db_url)

    df = load_snapshots(args.days, db_url)
    print(f"\nLoaded {len(df)} odds_snapshot rows")

    if df.empty:
        print("No tick data — run the ingestion daemon with RECORD_TO_PG=1 first.")
        print("The --execution-audit section above still works with live data.\n")
        return 0

    # Filter to hot-loop platforms for the core analysis
    hot = df[df["platform"].isin(HOT_LOOP_PLATFORMS)]
    print(f"Hot-loop platform rows: {len(hot)}")

    # B1: Move extraction
    moves = extract_moves_per_outcome(hot)
    print(f"\nB1: Extracted {len(moves)} change-point moves")

    # B2: Leader/lag attribution
    leaders = attribute_leaders(moves)
    print("\nB2: Leader/lag attribution per market_type:")
    for mt, data in sorted(leaders.items()):
        leader = max(data["leader_share"], key=data["leader_share"].get)
        print(
            f"  {mt:15s}  leader={leader:15s}  share={data['leader_share'][leader]:.2f}"
            f"  lag_p50={data['lag_p50_s']:.0f}s  lag_p90={data['lag_p90_s']:.0f}s"
            f"  n={data['n_matched_moves']}"
        )

    # B3: Discrepancy variance
    spreads = discrepancy_variance(hot)
    print("\nB3: Discrepancy variance D(M):")
    for mt, std in sorted(spreads.items()):
        print(f"  {mt:15s}  spread_std={std:.6f}")

    # B3b: GARCH(1,1) per-market-type fit (offline; powers the live adaptive threshold)
    garch_fits = fit_garch_per_market_type(hot) if args.garch else {}
    if garch_fits:
        print("\nB3b: GARCH(1,1) per market_type (median across series):")
        for mt, params in sorted(garch_fits.items()):
            sigma2_uncond = params["omega"] / (1.0 - params["alpha"] - params["beta"])
            print(
                f"  {mt:15s}  n_series={params['n_series']:3d}  "
                f"omega={params['omega']:.4f}  alpha={params['alpha']:.4f}  "
                f"beta={params['beta']:.4f}  sigma2_uncond={sigma2_uncond:.2f}"
            )

    # B4: Arb-window durations
    arb_wins = arb_windows(hot)
    print("\nB4: Arb-window durations per market_type:")
    for mt, data in sorted(arb_wins.items()):
        print(
            f"  {mt:15s}  count={data['count']:4d}  p50={data['p50_s']:.0f}s  p90={data['p90_s']:.0f}s"
        )

    # B6: Artifact
    artifact = build_artifact(leaders, spreads, arb_wins, args.days, garch=garch_fits)
    if args.write_artifact:
        out = Path("data/lag_model.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(artifact, indent=2))
        print(f"\nB6: Artifact written to {out}")
        # Gate check
        eligible = artifact["burst_eligible_market_types"]
        if eligible:
            print(f"  Phase C/D gate: PASS (burst-eligible: {eligible})")
        else:
            print(
                "  Phase C/D gate: FAIL (no market type meets leader_share ≥ 0.6 + lag_p90 ≥ 60s + n ≥ 50)"
            )
    else:
        print("\nB6: Artifact (use --write-artifact to save):")
        print(json.dumps(artifact, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
