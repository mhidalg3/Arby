"""Probe: does the automated in-app nav establish Betsson's betting context?

Read-only, NO bets. Validates `InSessionTransport.establish_betsson_context()` —
the automation of the operator's manual "My Account" click. You log in; the probe
(1) dumps the live nav (links + buttons, shadow-DOM-piercing) so we can see the
*actual* account element, then (2) runs the auto-nav and reports whether the
authenticated `ctx-` resolved.

If it resolves → the auto-nav works (hot sessions can self-establish). If not →
the dumped nav shows the right element to target, and the fix is a one-line
selector change.

Usage (run in your terminal — it waits for you to log in):
    uv run python scripts/probe_betsson_nav.py
"""

from __future__ import annotations

import asyncio

import structlog

from src.execution.session import InSessionTransport
from src.logging_setup import configure_logging


async def _dump_nav(page: object) -> None:
    """Print link + button labels the app exposes (Playwright locators pierce
    open shadow roots, where Betsson's nav lives)."""
    for role in ("link", "button"):
        loc = page.get_by_role(role)  # type: ignore[attr-defined]
        n = await loc.count()
        labels: list[str] = []
        for i in range(min(n, 60)):
            try:
                text = (await loc.nth(i).inner_text()).strip()
            except Exception:  # noqa: BLE001
                continue
            if text and text not in labels:
                labels.append(text[:30])
        print(f"\n  {role}s ({n} total) — labels:")
        for lab in labels:
            print(f"      · {lab}")


async def main() -> int:
    configure_logging()
    log = structlog.get_logger("probe_betsson_nav")
    transport = InSessionTransport("betsson", dry_run=False)
    async with transport:
        await transport.goto("https://pba.betsson.bet.ar/apuestas-deportivas")
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            input,
            "\n  ▶ LOG IN to Betsson (stay on PBA). When you see your balance, press ENTER "
            "(do NOT click around — let the probe drive)… ",
        )
        page = transport._page  # noqa: SLF001 — diagnostic probe
        print("\n=== NAV ON THE LOGGED-IN PAGE (find the account element here) ===")
        await _dump_nav(page)

        print("\n=== running establish_betsson_context() (cold-load → in-app nav → verify) ===")
        ok = await transport.establish_betsson_context()
        print(f"\n=== RESULT === ctx- resolved by the auto-nav: {ok}")
        if not ok:
            print("  Auto-nav did NOT establish the context. From the NAV dump above, tell me")
            print("  which link/button is 'My Account' (or how you reach the placeable state),")
            print("  and I'll fix the selector. The nav AFTER the cold reload:")
            await _dump_nav(page)
        log.info("probe.done", established=ok)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
