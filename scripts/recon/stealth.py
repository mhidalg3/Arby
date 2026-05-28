"""Playwright stealth shim — patch automation tells, not hardware.

Philosophy (see the 2026-05-27 LEDGER entries on the Bplay + Betano
blocks): the goal is to look like a normal, consistent browser, NOT
to fabricate a fake one. A real headed Chromium on real hardware
already has a legitimate fingerprint — genuine WebGL strings, real
fonts, real screen metrics. We do NOT spoof those, because a
*mismatched* fabricated fingerprint (UA says Chrome 130 but the GL
renderer is a Linux VM) is a stronger bot signal than the honest one.

What we DO patch are the artifacts that automation frameworks leave
behind regardless of hardware:

- `navigator.webdriver === true` — the single biggest giveaway.
  Real browsers report `false`/`undefined`; CDP-driven ones report
  `true`.
- `navigator.permissions.query({name:'notifications'})` returning
  `prompt` while `Notification.permission` is `denied` — a classic
  headless inconsistency.
- `navigator.languages` empty — headless sometimes drops it.

Everything else (plugins, WebGL, canvas, screen) is left as the real
browser reports it. Consistency over fabrication.

Apply once per context, before the first navigation:

    from scripts.recon.stealth import apply_stealth
    await apply_stealth(context)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext

# Runs in the page before any site script. Idempotent and defensive —
# wrapped in try/catch so a failure here never breaks page load.
STEALTH_INIT_JS = """
(() => {
  try {
    // 1. The big one: navigator.webdriver. CDP sets this true.
    Object.defineProperty(navigator, 'webdriver', {
      get: () => false,
      configurable: true,
    });
  } catch (e) {}

  try {
    // 2. languages consistency — match the es-AR locale we launch with.
    if (!navigator.languages || navigator.languages.length === 0) {
      Object.defineProperty(navigator, 'languages', {
        get: () => ['es-AR', 'es'],
        configurable: true,
      });
    }
  } catch (e) {}

  try {
    // 3. permissions.query notification inconsistency. Real browsers
    // return a state matching Notification.permission.
    const originalQuery = window.navigator.permissions &&
      window.navigator.permissions.query;
    if (originalQuery) {
      window.navigator.permissions.query = (parameters) =>
        parameters && parameters.name === 'notifications'
          ? Promise.resolve({ state: Notification.permission })
          : originalQuery(parameters);
    }
  } catch (e) {}

  try {
    // 4. Ensure window.chrome exists (headed Chrome has it; some
    // automation contexts strip it). Minimal stub — we don't fake
    // the full API surface, just the presence real Chrome has.
    if (!window.chrome) {
      window.chrome = { runtime: {} };
    }
  } catch (e) {}
})();
"""


async def apply_stealth(context: BrowserContext) -> None:
    """Register the stealth init script on a context. Applies to the
    current page's next navigation and every subsequently-opened page."""
    await context.add_init_script(STEALTH_INIT_JS)
