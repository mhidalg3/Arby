"""Read-only CDP observer for the armed hot_loop's live bookmaker windows.

ATTACHES (does not launch) to the Chrome windows that the running ``run_hot_loop``
opened on CDP 9222 (betano) / 9223 (betsson) / 9224 (betwarrior). Every tick it
screenshots + scans Betsson and BetWarrior for: responsible-gambling lockout popups,
session-expired / inactivity-logout popups, Betsson's shadow-DOM reality-check modal
(invisible to the bot's own light-DOM scan — see ledger 2026-06-21), and a coarse
login/liveness state. It tails the hot_loop log for arbitrage-execution events and, on
the FIRST arb execution of the session, switches to dense capture (timed screenshots of
both windows through placement) and writes a post-mortem so we can see what went well or
wrong.

HARD READ-ONLY CONTRACT — this script never navigates, clicks, types, or reloads a live
betting window. It only calls ``screenshot()`` and read-only ``page.evaluate()``. The
armed bot owns every side effect (popup dismiss, nav, placement); touching its windows
during real-money placement can lose money. CDP attach is the designed read-only path
(see ``src/execution/session.py:_cdp_debug_args``).

Usage::

    uv run python scripts/view_hot_sessions.py                      # betsson+betwarrior, default ports
    uv run python scripts/view_hot_sessions.py --interval-sec 45 --hours 8
    uv run python scripts/view_hot_sessions.py --keep-watching       # don't stop after 1st arb execution
    uv run python scripts/view_hot_sessions.py --log /tmp/arby_hot_loop.log --base-port 9222

Run AFTER ``run_hot_loop`` has opened its windows (CDP ports listening). It reconnects
if a window navigates or the attach drops.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
_ARTIFACTS_ROOT = REPO_ROOT / "recon" / "artifacts" / "session_viewer"

# CDP ports = base + offset (betano +0, betsson +1, betwarrior +2). Mirrors
# _CDP_PORT_OFFSETS in src/execution/session.py; duplicated here only so the viewer can
# target a platform without importing the whole transport stack at module load.
_PORT_OFFSET = {"betano": 0, "betsson": 1, "betwarrior": 2}

# Reuse the bot's EXACT detection vocab so the viewer and the bot agree on what a
# "block" / "session-expired" looks like. Private names, but a sibling recon script
# importing them keeps the two in lockstep (a divergence here would be a bug).
from src.execution.session import (  # noqa: E402
    _BLOCK_SCAN_JS,
    _RG_BLOCK_PHRASES,
    _SESSION_EXPIRED_PHRASES,
)

# Betsson renders its ENTIRE SPA in open shadow DOM, so the bot's _BLOCK_SCAN_JS (which
# reads document.body.innerText + light-DOM querySelector) is BLIND to Betsson popups.
# This bounded, READ-ONLY shadow walk collects visible dialog/modal hosts and their text
# so the viewer can see a Betsson popup the bot cannot (the bot only auto-dismisses the
# reality-check variant). Depth- and count-capped so it can't hang on a huge DOM. No
# clicks — purely observational.
_SHADOW_COLLECT_JS = """() => {
    const vis = (el) => {
        try { const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
              return r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden';
        } catch (_) { return false; }
    };
    // Known popup-shaped shadow hosts (Betsson reality-check is responsible-gaming_*
    // in an fds-dialog; a future lockout would surface as a session/lockout host).
    const POPUP_HOST = /responsible|reality.?check|fds-dialog|fds-modal|sg-modal|session.?summary|lockout|rg[_-]?(banner|modal|popup)|dialog/i;
    // Content tags to NEVER mistake for a popup (Betsson OBG-EVENT-* scorecards carry an
    // `on-overlay` CSS class but are highlighted event cards, not modals). Grounded from the
    // 2026-06-22 smoke test false positive.
    const SKIP_TAG = /^obg-|^fds-(button|list|table|card|tab)/i;
    const out = [];
    let visited = 0;
    const walk = (root, depth) => {
        if (!root || !root.querySelectorAll || depth > 12 || visited > 4000) return;
        let els; try { els = Array.from(root.querySelectorAll('*')); } catch (e) { return; }
        for (const el of els) {
            if (++visited > 4000) return;
            if (SKIP_TAG.test(el.tagName)) continue;
            const id = (el.tagName + ' ' + String(el.className || '')).toLowerCase();
            const isHost = POPUP_HOST.test(el.tagName) || POPUP_HOST.test(String(el.className || ''));
            // Generic fallback: a true blocking modal is a fixed element covering most of the
            // viewport (a backdrop). Content cards on an `on-overlay` layer are not full-viewport.
            let isBackdrop = false;
            try { const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
                  if (cs.position === 'fixed' && r.width > window.innerWidth * 0.4 && r.height > window.innerHeight * 0.4) isBackdrop = true;
            } catch (e) {}
            if ((isHost || isBackdrop) && vis(el)) {
                const txt = (el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 600);
                if (txt) out.push({ tag: el.tagName, cls: String(el.className || '').slice(0, 200), text: txt });
            }
            if (el.shadowRoot) walk(el.shadowRoot, depth + 1);
        }
    };
    walk(document, 0);
    return out;
}"""

# Logged-out / login markers in the light DOM (BetWarrior is regular DOM; Betsson login
# is also reachable this way before the SPA hydrates). Lowercased substring match.
_LOGGED_OUT_MARKERS = ("entrar", "unirse", "iniciar sesión", "iniciar sesion", "log in", "sign in")

# hot_loop log events we care about. The "first arb execution" trigger is
# orchestrator.arb_found / orchestrator.executing; the outcome is orchestrator.executed.
_ARB_EVENTS = {
    "orchestrator.arb_found",
    "orchestrator.executing",
    "orchestrator.executed",
    "orchestrator.manual_handoff",
    "orchestrator.rejected",
}
_EXEC_EVENTS = {
    "executor.dry_run_place",
    "executor.completed",
    "executor.aborted",
    "executor.naked_exposure",
    "executor.pending_unknown",
    "executor.escalating",
}
_SESSION_EVENTS = {
    "transport.betwarrior_auth_dead",
    "transport.block_detected",
    "transport.block_probe_error",
    "transport.reality_check_dismissed",
    "transport.reality_check_dismiss_error",
    "transport.session_expired",
    "guardrails.kill_switch_tripped",
    "guardrails.kill_switch_reset",
    "orchestrator.platform_stale",
    "orchestrator.cycle_error",
}
_BW_POLL_EVENTS = {
    # The prove surface for the LIVE_DELAY_PENDING poll. `betwarrior_non_success` carries
    # the pending body (incl. couponRef); resolved/rejected/timeout/error show whether the
    # coupon/history.json poll actually settled the bet. `delay_rejected` (betStatus hit a
    # known reject literal) is a clean reject; `delay_timeout` is pending_unknown (may be
    # placed) — the prove pair that tells the operator whether to act.
    "leg_placer.betwarrior_non_success",
    "leg_placer.betwarrior_delay_resolved",
    "leg_placer.betwarrior_delay_rejected",
    "leg_placer.betwarrior_delay_timeout",
    "leg_placer.betwarrior_poll_error",
    "leg_placer.betwarrior_poll_http_error",
}
_WATCH_EVENTS = _ARB_EVENTS | _EXEC_EVENTS | _SESSION_EVENTS | _BW_POLL_EVENTS


def _classify(platform: str, scan: dict[str, Any], shadow: list[Any], url: str) -> str:
    """Coarse state label for one window. Independent of the bot's own classification —
    a divergence here is itself a post-mortem signal (e.g. viewer sees a Betsson popup
    the bot logged as clear)."""
    overlay = (scan.get("overlayText") or "").lower()
    body = (scan.get("bodyText") or "").lower()
    blob = overlay + " " + body + " " + " ".join(s.get("text", "") for s in shadow).lower()
    for p in _SESSION_EXPIRED_PHRASES:
        if p in blob:
            return "session_expired"
    for m in _LOGGED_OUT_MARKERS:
        if m in blob and "balance" not in blob:
            return "login_page"
    for p in _RG_BLOCK_PHRASES:
        if p in blob:
            return "rg_lockout"
    if platform == "betsson" and any(
        "reality" in s.get("text", "").lower() or "reality" in s.get("cls", "").lower()
        for s in shadow
    ):
        return "reality_check"
    if any(s.get("text") for s in shadow):
        return "modal_visible"
    if "login" in url.lower() or "auth" in url.lower():
        return "login_page"
    return "ok"


class Viewer:
    def __init__(
        self,
        platforms: list[str],
        base_port: int,
        interval: float,
        dense_interval: float,
        dense_window: float,
        log_path: Path,
        deadline: float,
        keep_watching: bool,
        out: Path,
    ) -> None:
        self.platforms = platforms
        self.base_port = base_port
        self.interval = interval
        self.dense_interval = dense_interval
        self.dense_window = dense_window
        self.log_path = log_path
        self.deadline = deadline
        self.keep_watching = keep_watching
        self.out = out
        self.obs_log = (out / "viewer.jsonl").open("a")
        self.evt_log = (out / "events.jsonl").open("a")
        self.started = time.time()
        self.browsers: dict[str, Any] = {}  # platform -> playwright Browser proxy
        self.pws: dict[str, Any] = {}  # platform -> playwright instance
        self.log_offset = 0
        self.first_arb_seen = False
        self.dense_until = 0.0
        self.last_state: dict[str, str] = {}
        self.cycle = 0
        # Last kill-switch reason from the bot log (None = reset / not tripped). A window
        # can look `ok` (logged in) while the bot can't place — e.g. BetWarrior's bearer
        # not captured ⇒ "session not ready". Surfacing this caveat is the whole point: it
        # is the gap that hid the 2026-06-22 startup stall (viewer "ok" ≠ placement-ready).
        self.kill_switch: str | None = None

    # --- CDP attach (lazy, reconnect-on-drop) ---------------------------------
    async def _page(self, platform: str) -> Any | None:
        port = self.base_port + _PORT_OFFSET[platform]
        try:
            br = self.browsers.get(platform)
            if br is None:
                from playwright.async_api import async_playwright

                pw = await async_playwright().start()
                self.pws[platform] = pw
                br = await pw.chromium.connect_over_cdp(f"http://localhost:{port}")
                self.browsers[platform] = br
            # Pick the page on this platform's domain; fall back to the newest page.
            pages: list[Any] = []
            for ctx in br.contexts:
                pages.extend(ctx.pages)
            if not pages:
                return None
            home = {
                "betano": "betano.bet.ar",
                "betsson": "betsson.bet.ar",
                "betwarrior": "betwarrior.bet.ar",
            }[platform]
            on_domain = [p for p in pages if home in (p.url or "")]
            return (on_domain or pages)[-1]
        except Exception:
            # Attach dropped (window navigated/reloaded/closed) — drop the cached proxy
            # so the next tick reconnects fresh.
            self.browsers.pop(platform, None)
            return None

    # --- one observation of one window ----------------------------------------
    async def _sample(self, platform: str) -> dict[str, Any]:
        ts = int(time.time())
        rec: dict[str, Any] = {
            "platform": platform,
            "ts": ts,
            "wall_clock": time.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_min": round((time.time() - self.started) / 60.0, 1),
        }
        page = await self._page(platform)
        if page is None:
            rec["state"] = "attach_failed"
            return rec
        try:
            rec["url"] = page.url
            rec["title"] = await page.title()
            shot = self.out / f"{platform}_{ts}.png"
            await page.screenshot(path=str(shot), full_page=False)
            rec["screenshot"] = shot.name
            scan = await page.evaluate(
                _BLOCK_SCAN_JS,
                {"sel": "[role=dialog],[aria-modal=true],.modal,.overlay,.modal-overlay"},
            )
            rec["phrase_hits"] = [
                p
                for p in (*_RG_BLOCK_PHRASES, *_SESSION_EXPIRED_PHRASES)
                if p in ((scan.get("overlayText") or "") + (scan.get("bodyText") or "")).lower()
            ]
            shadow: list[Any] = []
            if platform == "betsson":
                with contextlib.suppress(Exception):
                    shadow = await page.evaluate(_SHADOW_COLLECT_JS)
            rec["shadow_modals"] = shadow[:5]
            rec["state"] = _classify(platform, scan, shadow, page.url)
        except Exception as exc:  # noqa: BLE001 — one window faulting must not sink the watch
            rec["state"] = "error"
            rec["error"] = str(exc)[:300]
            self.browsers.pop(platform, None)
        return rec

    def _emit_obs(self, rec: dict[str, Any]) -> None:
        if self.kill_switch:
            rec["kill_switch"] = self.kill_switch
        self.obs_log.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.obs_log.flush()
        prev = self.last_state.get(rec["platform"])
        flag = rec["state"]
        marker = ""
        if flag in ("rg_lockout", "session_expired", "naked"):
            marker = " 🚫"
        elif flag in ("reality_check", "modal_visible", "login_page"):
            marker = " ⚠️"
        changed = f" (was {prev})" if prev and prev != flag else ""
        hits = f" hits={rec['phrase_hits']}" if rec.get("phrase_hits") else ""
        err = f" ERR:{rec['error'][:80]}" if rec.get("error") else ""
        # Caveat every line with the bot's kill-switch state: without it a logged-in
        # window reads "ok" while auto-placement is off (the bearer-capture blind spot).
        ks = f" · 🛑 auto-placement OFF ({self.kill_switch})" if self.kill_switch else ""
        print(
            f"  [{rec['wall_clock']}] c{self.cycle} {rec['platform']}: {flag}{marker}{changed}{hits}{err}{ks}"
        )

    # --- hot_loop log tail (parse JSON lines, keep notable events) -------------
    async def _drain_log(self) -> list[dict[str, Any]]:
        """Return notable events appended since the last drain."""
        try:
            size = self.log_path.stat().st_size
        except OSError:
            return []
        if size < self.log_offset:
            self.log_offset = 0  # log was rotated/truncated
        if size == self.log_offset:
            return []
        with self.log_path.open("rb") as fh:
            fh.seek(self.log_offset)
            chunk = fh.read(size - self.log_offset).decode("utf-8", "replace")
        self.log_offset = size
        events: list[dict[str, Any]] = []
        for line in chunk.splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            ev = obj.get("event") or obj.get("component")
            if ev and ev in _WATCH_EVENTS:
                obj["_wall"] = time.strftime("%Y-%m-%d %H:%M:%S")
                events.append(obj)
        return events

    def _emit_events(self, events: list[dict[str, Any]]) -> None:
        for e in events:
            self.evt_log.write(json.dumps(e, ensure_ascii=False) + "\n")
            self.evt_log.flush()
            ev = e.get("event", "?")
            tail = {
                k: e[k]
                for k in (
                    "market_id",
                    "platform",
                    "outcome",
                    "reason",
                    "roi_pct",
                    "age_sec",
                    "status",
                    "bet_status",
                    "coupon_ref",
                    "attempts",
                    "body",
                    "error",
                )
                if k in e
            }
            print(f"  ⚡ [{e.get('_wall')}] {ev} {json.dumps(tail, ensure_ascii=False)}")
            if ev in _ARB_EVENTS:
                self.first_arb_seen = True
            if ev == "guardrails.kill_switch_tripped":
                self.kill_switch = e.get("reason", "unknown")
            elif ev == "guardrails.kill_switch_reset":
                self.kill_switch = None
            if ev == "orchestrator.executing" or ev == "orchestrator.arb_found":
                # Go dense: capture through placement + a grace window after.
                self.dense_until = time.time() + max(self.dense_window, 180.0)

    # --- post-mortem on first arb execution -----------------------------------
    def _write_postmortem(self, executed: dict[str, Any], all_events: list[dict[str, Any]]) -> None:
        md = self.out / "postmortem.md"
        shots = sorted(p.name for p in self.out.glob("*.png"))
        lines = [
            "# First arbitrage execution — post-mortem",
            "",
            f"- session started: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.started))}",
            f"- executed at:    {executed.get('_wall', '?')}",
            f"- market_id:      {executed.get('market_id', '?')}",
            f"- outcome:        {executed.get('outcome', '?')}",
            "",
            "## event timeline",
            "",
        ]
        for e in all_events:
            keep = {
                k: e[k]
                for k in (
                    "event",
                    "market_id",
                    "platform",
                    "outcome",
                    "reason",
                    "roi_pct",
                    "age_sec",
                    "error",
                    "_wall",
                )
                if k in e
            }
            lines.append(f"- `{e.get('_wall', '?')}` {json.dumps(keep, ensure_ascii=False)}")
        lines += ["", "## screenshots (chronological)", ""]
        for s in shots:
            lines.append(f"- ![{s}]({s})")
        lines += [
            "",
            "## how to read this",
            "",
            "Compare each screenshot's visible state to the bot's logged classification",
            "at the same timestamp (events.jsonl). A divergence (e.g. viewer sees a Betsson",
            "popup the bot logged `clear`) is the highest-value finding here — it marks a",
            "blind spot in `check_session_blocked` (Betsson shadow DOM).",
            "",
        ]
        md.write_text("\n".join(lines))
        print(f"\n  📝 post-mortem written → {md.relative_to(REPO_ROOT)}")

    # --- main loop ------------------------------------------------------------
    async def run(self) -> int:
        print(
            f"👁  SESSION VIEWER — {self.platforms}\n"
            f"  CDP base {self.base_port} · tick {self.interval:.0f}s · dense {self.dense_interval:.0f}s\n"
            f"  log {self.log_path} · artifacts → {self.out.relative_to(REPO_ROOT)}\n"
            f"  stop after 1st arb execution: {not self.keep_watching}"
        )
        all_events: list[dict[str, Any]] = []
        executed_seen: dict[str, Any] | None = None
        try:
            while time.time() < self.deadline:
                self.cycle += 1
                # 1) drain log events first (drives dense-capture + first-arb trigger)
                events = await self._drain_log()
                if events:
                    self._emit_events(events)
                    all_events.extend(events)
                    for e in events:
                        if e.get("event") == "orchestrator.executed":
                            executed_seen = e
                # 2) observe each window
                dense = time.time() < self.dense_until
                for platform in self.platforms:
                    rec = await self._sample(platform)
                    if dense:
                        rec["dense"] = True
                    self._emit_obs(rec)
                    self.last_state[platform] = rec.get("state", "?")
                # 3) if the first execution landed, write post-mortem + decide exit
                if executed_seen is not None:
                    self._write_postmortem(executed_seen, all_events)
                    if not self.keep_watching:
                        print(
                            "\n  ✅ first arb execution captured — viewer stopping "
                            "(--keep-watching to continue)."
                        )
                        break
                    executed_seen = None  # avoid re-writing; keep watching for more
                interval = self.dense_interval if dense else self.interval
                await asyncio.sleep(interval)
            else:
                print(
                    f"\n  ⏰ deadline reached after {(time.time() - self.started) / 3600.0:.1f} h"
                )
        finally:
            self.obs_log.close()
            self.evt_log.close()
            for _platform, br in list(self.browsers.items()):
                with contextlib.suppress(Exception):
                    await br.close()
            for pw in self.pws.values():
                with contextlib.suppress(Exception):
                    await pw.stop()
        return 0


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--platforms",
        default="betsson,betwarrior",
        help="comma list from betano,betsson,betwarrior (default: betsson,betwarrior)",
    )
    p.add_argument("--base-port", type=int, default=9222, help="CDP_PORT_BASE the hot_loop used")
    p.add_argument("--interval-sec", type=float, default=60.0)
    p.add_argument(
        "--dense-sec",
        type=float,
        default=10.0,
        help="screenshot cadence during/after an arb execution",
    )
    p.add_argument(
        "--dense-window",
        type=float,
        default=180.0,
        help="min seconds to stay dense after a trigger",
    )
    p.add_argument("--hours", type=float, default=8.0)
    p.add_argument("--log", default="/tmp/arby_hot_loop.log")
    p.add_argument(
        "--keep-watching", action="store_true", help="don't stop after the first arb execution"
    )
    args = p.parse_args()

    platforms = [s.strip() for s in args.platforms.split(",") if s.strip()]
    bad = [x for x in platforms if x not in _PORT_OFFSET]
    if bad:
        raise SystemExit(f"unknown platform(s): {bad} (known: {list(_PORT_OFFSET)})")

    out = _ARTIFACTS_ROOT / time.strftime("%Y%m%d_%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + args.hours * 3600.0

    viewer = Viewer(
        platforms=platforms,
        base_port=args.base_port,
        interval=args.interval_sec,
        dense_interval=args.dense_sec,
        dense_window=args.dense_window,
        log_path=Path(args.log),
        deadline=deadline,
        keep_watching=args.keep_watching,
        out=out,
    )
    return await viewer.run()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
