"""Standalone drill for Betsson auto re-login — test it WITHOUT the hot loop.

Opens ONLY the Betsson window (same persistent profile as the bot, but this process
owns the transport + its page lock, so there is no sidecar lock hazard) and drives
one scenario against `InSessionTransport.attempt_betsson_relogin` / the debounced
auth scan. No placement code is reachable: the transport is never armed and no
placer is constructed. `dry_run=False` is required — relogin is a real-window
interaction and short-circuits in dry-run.

Scenarios (pick one per run):

  scan   READ-ONLY. Reloads the sportsbook, then prints a RAW single-sample auth
         scan immediately after the goto AND the debounced settled read. If the raw
         sample shows `loggedOut: true` while settled says healthy, you are looking
         at the exact hydration window that caused the 2026-07-05 false-relogin
         logout. Repeatable on request.

  noop   THE REGRESSION DRILL. With the window logged IN, runs
         attempt_betsson_relogin() and verifies it is a no-op: expect
         `betsson_relogin_already_logged_in`, return True, and the balance still
         visible afterwards. Pre-fix code could log the session out here.

  full   Runs the real logout→login choreography: you log OUT manually, the drill
         verifies the settled read agrees, then relogin opens the form, types the
         keyring creds and submits. Expect `transport.betsson_relogin_ok`.
         Requires keyring creds and a typed confirmation.

Run from the operator's terminal, with the hot loop STOPPED (the persistent profile
`recon/profile/betsson` holds a SingletonLock — a running bot blocks the launch;
for an in-bot drill use `touch /tmp/arby_force_betsson_reauth` instead):

    uv run python scripts/drill_betsson_relogin.py scan
    uv run python scripts/drill_betsson_relogin.py noop
    uv run python scripts/drill_betsson_relogin.py full
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

import structlog

import src.execution.session as session_mod
from src.credentials import get_credential
from src.execution.session import InSessionTransport
from src.logging_setup import configure_logging

_BETSSON_HOME = "https://pba.betsson.bet.ar/apuestas-deportivas"


async def _gate(msg: str) -> str:
    """Block until the operator answers — same pattern as probe_betsson_auth.py."""
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, input, f"\n  ▶ {msg} ")
    except EOFError:
        print("\n  ✋ stdin EOF — this drill is interactive; run it in a real terminal.")
        raise SystemExit(2) from None


def _fmt(auth: dict[str, Any] | None) -> str:
    return json.dumps(auth, ensure_ascii=False) if isinstance(auth, dict) else str(auth)


async def _raw_sample(t: InSessionTransport) -> dict[str, Any] | None:
    res = await t._page.evaluate(session_mod._BETSSON_AUTH_SCAN_JS)  # noqa: SLF001 — drill
    return res if isinstance(res, dict) else None


async def _scenario_scan(t: InSessionTransport) -> int:
    while True:
        print("\n  reloading the sportsbook, then sampling IMMEDIATELY (raw, no debounce)…")
        await t.goto(_BETSSON_HOME)
        raw = await _raw_sample(t)
        print(f"  raw immediate sample : {_fmt(raw)}")
        settled = await t._betsson_auth_settled()  # noqa: SLF001 — drill
        print(f"  debounced settled    : {_fmt(settled)}")
        if isinstance(raw, dict) and raw.get("loggedOut") is True:
            if isinstance(settled, dict) and settled.get("hasBalance") is True:
                print(
                    "  ⚠️ raw read said logged-out but settled healed to healthy — that is "
                    "the hydration window the debounce exists for."
                )
            elif isinstance(settled, dict) and settled.get("loggedOut") is True:
                print("  🔌 settled agrees: genuinely logged out.")
        again = await _gate("Sample again? [y/N]")
        if again.strip().lower() != "y":
            return 0


async def _scenario_noop(t: InSessionTransport) -> int:
    await _gate(
        "Make sure you are LOGGED IN (balance visible in the header), press ENTER "
        "to run attempt_betsson_relogin() — it MUST be a no-op…"
    )
    before = await t._betsson_auth_settled()  # noqa: SLF001 — drill
    print(f"  settled before: {_fmt(before)}")
    if not (isinstance(before, dict) and before.get("hasBalance") is True):
        print("  ✋ header does not read logged-in; log in first, then rerun. Aborting.")
        return 1
    ok = await t.attempt_betsson_relogin()
    after = await t._betsson_auth_settled()  # noqa: SLF001 — drill
    print(f"  relogin returned: {ok}")
    print(f"  settled after   : {_fmt(after)}")
    still_in = isinstance(after, dict) and after.get("hasBalance") is True
    if still_in and ok:
        print("  ✅ NO-OP CONFIRMED: session untouched, ctx re-established.")
        return 0
    if still_in:
        print(
            "  ⚠️ session untouched (good) but relogin returned False — expect "
            "betsson_relogin_no_ctx_after_login in the log (ctx, not auth, failed)."
        )
        return 1
    print("  ❌ REGRESSION: the session is no longer logged in after a healthy relogin call.")
    return 1


async def _scenario_full(t: InSessionTransport) -> int:
    if get_credential("betsson") is None:
        print("  ✋ no keyring credential for 'betsson' — the form flow cannot run. Aborting.")
        return 1
    await _gate(
        "LOG OUT of Betsson manually (balance menu → Cerrar Sesión). When the header "
        "shows 'Iniciar sesión', press ENTER…"
    )
    settled = await t._betsson_auth_settled()  # noqa: SLF001 — drill
    print(f"  settled read: {_fmt(settled)}")
    if not (isinstance(settled, dict) and settled.get("loggedOut") is True):
        print("  ✋ settled read does not agree the session is logged out. Aborting.")
        return 1
    answer = await _gate(
        "About to run the REAL login choreography (opens the form, types keyring "
        "creds, submits). Type RELOGIN to proceed:"
    )
    if answer.strip() != "RELOGIN":
        print("  aborted.")
        return 1
    ok = await t.attempt_betsson_relogin()
    after = await t._betsson_auth_settled()  # noqa: SLF001 — drill
    print(f"  relogin returned: {ok}")
    print(f"  settled after   : {_fmt(after)}")
    if ok:
        print("  ✅ RELOGIN OK — expect transport.betsson_relogin_ok in the log above.")
        return 0
    print(
        "  ❌ relogin failed — check the transport.betsson_relogin_* events above "
        "(no_form / form_incomplete / timeout / auth_unreadable) to see which leg broke."
    )
    return 1


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("scenario", choices=("scan", "noop", "full"))
    args = p.parse_args()
    configure_logging()
    log = structlog.get_logger("drill_betsson_relogin")
    # Same construction as the hot loop's Betsson transport (run_hot_loop.py) so the
    # launch-time session state matches production; only the surrounding machinery
    # (manager, heartbeat, placers) is absent.
    t = InSessionTransport("betsson", dry_run=False, restore_session=True)
    async with t:
        await t.goto(_BETSSON_HOME)
        print(
            "\nBetsson relogin drill — scenario:",
            args.scenario,
            "\n(If the window failed to open: is the hot loop still running? Its "
            "profile SingletonLock blocks this drill — run the END block first.)",
        )
        rc = await {
            "scan": _scenario_scan,
            "noop": _scenario_noop,
            "full": _scenario_full,
        }[args.scenario](t)
    log.info("drill.done", scenario=args.scenario, rc=rc)
    return rc


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
