"""Passive long-session watcher for the responsible-gambling LOCKOUT popups.

Opens all three armed platforms' logged-in windows (Betano, Betsson, BetWarrior) and
leaves them open. Every ``--interval-min`` it screenshots each window and runs the
lockout DOM scan, writing to ``recon/artifacts/popup_monitor/`` so we can (a) see WHEN
each popup emerges and at what session age, (b) spot UI changes over time, and (c)
ground the exact overlay selectors for ``check_session_blocked``.

Deliberately PASSIVE: it navigates each window ONCE at startup and then only screenshots
— it never reloads or clicks. A reload could reset the play-time counter (the very thing
we're trying to observe) and a busy nav pattern is a bot signal. We want the NATURAL
emergence of the popup during a long idle-ish session. Read-only; never places a bet.

No login gate (so it runs unattended in the background): the windows open, you log into
all three live, and every screenshot is timestamped regardless of state. Early shots may
show a login page; that's fine.

    uv run python scripts/monitor_popups.py                      # 30-min cadence, 12h
    uv run python scripts/monitor_popups.py --interval-min 15 --hours 8
    uv run python scripts/monitor_popups.py --platforms betano,betsson
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import random
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
_ARTIFACTS = REPO_ROOT / "recon" / "artifacts" / "popup_monitor"

_HOME = {
    "betano": "https://www.betano.bet.ar/",
    "betsson": "https://pba.betsson.bet.ar/apuestas-deportivas",
    "betwarrior": "https://pba.betwarrior.bet.ar/",
}

# Visible-overlay scan: lockout phrases + any visible modal/overlay. Mirrors
# trial_place.py --capture-popup so both produce comparable artifacts.
_SCAN_JS = """(phrases) => {
    const body = (document.body && document.body.innerText || '').toLowerCase();
    const hits = phrases.filter(p => body.includes(p));
    const sel = '[role=dialog],[aria-modal=true],.modal,.overlay,.modal-overlay';
    const dialogs = [...document.querySelectorAll(sel)]
        .filter(el => el.offsetParent !== null && el.getBoundingClientRect().width > 0)
        .map(el => ({tag: el.tagName, cls: String(el.className), html: el.outerHTML.slice(0, 6000)}));
    return {hits, dialogs};
}"""


async def _open(platform: str) -> tuple[Any, Any, Any]:
    """Launch one platform's persistent (logged-in) profile and land on its home.
    Returns (playwright, context, page)."""
    from playwright.async_api import async_playwright

    from scripts.recon.stealth import apply_stealth

    pw = await async_playwright().start()
    ctx = await pw.chromium.launch_persistent_context(
        user_data_dir=str(REPO_ROOT / "recon" / "profile" / platform),
        headless=False,
        channel="chrome",
        locale="es-AR",
        timezone_id="America/Argentina/Buenos_Aires",
        permissions=["geolocation"],
        geolocation={"latitude": -34.9215, "longitude": -57.9545},  # La Plata (PBA)
    )
    await apply_stealth(ctx)
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    await page.goto(_HOME[platform], wait_until="domcontentloaded", timeout=60000)
    return pw, ctx, page


async def _keepalive(page: Any) -> None:
    """Minimal human-like activity to defeat the inactivity logout (BetWarrior/Kambi
    ends the session after a few idle minutes) WITHOUT navigating — a reload would
    reset the play-time counter we're trying to observe AND is a bot signal. Just a
    mouse move + a tiny scroll nudge that returns to where it was; no clicks, no keys,
    no nav. Best-effort: a fault on one page must not stop the watch."""
    with contextlib.suppress(Exception):
        x, y = random.randint(150, 900), random.randint(150, 550)
        await page.mouse.move(x, y, steps=4)
        await page.mouse.wheel(0, 120)
        await asyncio.sleep(0.3)
        await page.mouse.wheel(0, -120)


async def _sample(platform: str, page: Any, phrases: list[str], started: float) -> dict[str, Any]:
    """One observation of a window: screenshot + lockout/overlay scan. Best-effort —
    a fault on one window must not stop the watch."""
    ts = int(time.time())
    elapsed_min = round((time.time() - started) / 60.0, 1)
    rec: dict[str, Any] = {
        "platform": platform,
        "ts": ts,
        "wall_clock": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_min": elapsed_min,
    }
    try:
        shot = _ARTIFACTS / f"{platform}_{ts}.png"
        await page.screenshot(path=str(shot), full_page=False)
        rec["screenshot"] = shot.name
        rec["url"] = page.url
        found = await page.evaluate(_SCAN_JS, phrases)
        rec["phrase_hits"] = found["hits"]
        rec["dialogs"] = found["dialogs"]
        rec["blocked"] = bool(found["hits"])
    except Exception as exc:  # noqa: BLE001 — keep watching the others
        rec["error"] = str(exc)
    return rec


async def main() -> int:
    from src.execution.session import _RG_BLOCK_PHRASES

    p = argparse.ArgumentParser()
    p.add_argument("--interval-min", type=float, default=30.0)
    p.add_argument("--hours", type=float, default=12.0)
    p.add_argument("--platforms", default="betano,betsson,betwarrior")
    p.add_argument(
        "--keepalive-sec",
        type=float,
        default=120.0,
        help="minimal mouse/scroll activity this often, to defeat the inactivity logout",
    )
    args = p.parse_args()

    platforms = [s.strip() for s in args.platforms.split(",") if s.strip()]
    bad = [p_ for p_ in platforms if p_ not in _HOME]
    if bad:
        raise SystemExit(f"unknown platform(s): {bad} (known: {list(_HOME)})")

    _ARTIFACTS.mkdir(parents=True, exist_ok=True)
    phrases = list(_RG_BLOCK_PHRASES)
    started = time.time()
    deadline = started + args.hours * 3600.0
    log_path = _ARTIFACTS / f"monitor_{int(started)}.jsonl"

    print(
        f"⚠️  POPUP MONITOR — {platforms}\n"
        f"  screenshot {args.interval_min:.0f} min · keep-alive {args.keepalive_sec:.0f} s · "
        f"runs {args.hours:.0f} h · artifacts → {_ARTIFACTS}\n"
        "  ▶ Log into ALL the windows that open; I screenshot + scan regardless of state.\n"
        "  (Windows are NOT reloaded — only mouse/scroll keep-alive; natural popup emergence.)"
    )

    opened: list[tuple[str, Any, Any, Any]] = []
    for platform in platforms:
        try:
            pw, ctx, page = await _open(platform)
            opened.append((platform, pw, ctx, page))
            print(f"  opened {platform}")
        except Exception as exc:  # noqa: BLE001 — one window failing shouldn't sink the rest
            print(f"  FAILED to open {platform}: {exc}")

    if not opened:
        raise SystemExit("no windows opened (profiles locked by another process?)")

    try:
        cycle = 0
        interval_sec = args.interval_min * 60.0
        last_shot = 0.0  # 0 ⇒ a screenshot fires on the first tick
        while time.time() < deadline:
            # Keep-alive every tick (defeats the inactivity logout).
            for _platform, _pw, _ctx, page in opened:
                await _keepalive(page)
            # Screenshot + scan only every interval.
            if time.time() - last_shot >= interval_sec:
                last_shot = time.time()
                cycle += 1
                for platform, _pw, _ctx, page in opened:
                    rec = await _sample(platform, page, phrases, started)
                    with log_path.open("a") as fh:
                        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    flag = (
                        "🚫 LOCKOUT"
                        if rec.get("blocked")
                        else ("modal" if rec.get("dialogs") else "clear")
                    )
                    err = f" ERR:{rec['error']}" if rec.get("error") else ""
                    print(
                        f"  [{rec['wall_clock']}] cycle {cycle} {platform}: {flag} "
                        f"(+{rec['elapsed_min']:.0f}m){err}"
                    )
            await asyncio.sleep(args.keepalive_sec)
        print(f"\n  watch ended after {args.hours:.0f} h — log: {log_path.name}")
    finally:
        for _platform, pw, ctx, _page in opened:
            with contextlib.suppress(Exception):
                await ctx.close()
                await pw.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
