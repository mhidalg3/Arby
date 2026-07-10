"""Probe: ground Betsson's auth selectors for auto re-login.

Read-only, NO bets, NO login with credentials by the script. This is the Step-0
decision record for ``InSessionTransport.attempt_betsson_relogin`` and the Betsson
auth scan in ``check_session_blocked``. You (the operator) drive the page state
through ``input()`` gates; the probe dumps the shadow-DOM elements and prints a
fill-in summary naming the constants the implementation expects.

Already grounded (read-only recon 2026-07-02 + operator-provided DOM facts) and
NOT re-asked here unless you want to re-confirm:

* login trigger        ``[data-test-id="login-button"]``          (router-link-v2, open shadow DOM)
* logged-in balance    ``[data-test-id="balance-button"]``        (account-menu trigger; absent when logged out)
* logged-in header text ``Retirar`` / ``Depósito``                 (scoped to the header area in the scan)
* logout menu item     ``[data-test-id="site-menu-link-anchor-cerrar-sesión"]``
* login submit button  ``[data-test-id="account-login-btn-1"]``

Email/password inputs were grounded live on 2026-07-03:

* email input          ``[data-test-id="email-input"]``
* password input       ``[data-test-id="password-input"]``

Keep this probe as the decision-record tool if Betsson changes the login popup DOM; rerun it
to re-confirm selectors rather than guessing.

Usage (run in your terminal — you drive the page):
    uv run python scripts/probe_betsson_auth.py
"""

from __future__ import annotations

import asyncio
import json

import structlog

from src.execution.session import InSessionTransport
from src.logging_setup import configure_logging

# Bounded open-shadow walk (mirrors session._BETSSON_REALITY_CHECK_SCAN_JS) that
# collects auth-relevant visible elements. Runs in the transport's page. `want.re`
# is a regex SOURCE string (e.g. "iniciar|sesi|login"); compile it server-side so
# .test() works (a raw string has no .test()).
_DUMP_JS = """(want) => {
    const re = new RegExp(want.re, 'i');
    const vis = (el) => {
        try {
            const r = el.getBoundingClientRect();
            const cs = getComputedStyle(el);
            return r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden';
        } catch (_) { return false; }
    };
    let visited = 0;
    const hits = [];
    const seen = new Set();
    const walk = (root, depth) => {
        if (!root || !root.querySelectorAll || depth > 18 || visited > 12000) return;
        let els;
        try { els = Array.from(root.querySelectorAll('*')); } catch (_) { return; }
        for (const el of els) {
            if (++visited > 12000) return;
            const tag = el.tagName.toLowerCase();
            const testId = el.getAttribute('data-test-id') || el.getAttribute('data-testid') || '';
            const type = el.getAttribute('type') || '';
            const name = el.getAttribute('name') || '';
            const placeholder = el.getAttribute('placeholder') || '';
            const autocomplete = el.getAttribute('autocomplete') || '';
            const inputmode = el.getAttribute('inputmode') || '';
            const aria = el.getAttribute('aria-label') || '';
            let text = '';
            try { text = String(el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 40); } catch (_) {}
            const key = `${tag}|${testId}|${type}|${name}|${placeholder}|${aria}|${text}`;
            if (!seen.has(key)) {
                seen.add(key);
                const isInput = tag === 'input' || tag.includes('input');
                const isBtn = tag === 'fds-button' || tag === 'button' || tag === 'a' || tag.includes('link');
                const match = isInput
                    || (testId && re.test(testId))
                    || (text && re.test(text))
                    || (isBtn && re.test(text));
                if (match) hits.push({ tag, testId, type, name, placeholder, autocomplete, inputmode, aria, text, vis: vis(el) });
            }
            if (el.shadowRoot) walk(el.shadowRoot, depth + 1);
        }
    };
    walk(document, 0);
    return hits;
}"""


async def _dump(page: object, label: str, regex: str) -> None:
    print(f"\n=== {label} ===")
    try:
        hits = await page.evaluate(_DUMP_JS, {"re": regex})  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 — diagnostic probe
        print(f"  (dump failed: {exc!s})")
        return
    visible = [h for h in hits if h.get("vis")]
    print(f"  {len(visible)} visible match(es) of {len(hits)} total:")
    for h in visible:
        bits = [f"tag={h['tag']}"]
        if h.get("testId"):
            bits.append(f"data-test-id={h['testId']!r}")
        if h.get("type"):
            bits.append(f"type={h['type']!r}")
        if h.get("name"):
            bits.append(f"name={h['name']!r}")
        if h.get("placeholder"):
            bits.append(f"placeholder={h['placeholder']!r}")
        if h.get("autocomplete"):
            bits.append(f"autocomplete={h['autocomplete']!r}")
        if h.get("aria"):
            bits.append(f"aria-label={h['aria']!r}")
        if h.get("text"):
            bits.append(f"text={h['text']!r}")
        print("    · " + " ".join(bits))
    print(f"  raw json: {json.dumps(visible, ensure_ascii=False)}")


async def _gate(msg: str) -> None:
    # Block until the operator signals ready — mirroring probe_betsson_nav.py
    # (await run_in_executor), so each dump runs against the page state the
    # operator drove, not a stale one. Fire-and-forget here would race the dumps.
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, input, f"\n  ▶ {msg} ")
    except EOFError:
        print(f"\n  ▶ {msg} (stdin EOF — background launch; touch the gate file)")


async def main() -> int:
    configure_logging()
    log = structlog.get_logger("probe_betsson_auth")
    transport = InSessionTransport("betsson", dry_run=False)
    async with transport:
        await transport.goto("https://pba.betsson.bet.ar/apuestas-deportivas")

        print(
            "\nThis probe re-confirms Betsson's auth selectors. It does NOT log in for you.\n"
            "Current in-code login-popup selectors are email-input/password-input; rerun "
            "this only if the live DOM appears to have changed."
        )

        await _gate(
            "LOG OUT of Betsson if you are logged in (balance menu → Cerrar Sesión). "
            "When the header shows 'Iniciar sesión', press ENTER…"
        )
        await _dump(
            transport._page,  # noqa: SLF001 — diagnostic probe
            "LOGGED-OUT HEADER (confirm login trigger; capture nothing new here)",
            r"iniciar|sesi|login|registr",
        )

        await _gate(
            "Click 'Iniciar sesión' so the LOGIN POPUP is OPEN (dismiss any cookie or "
            "geolocation interstitial first; do NOT type your creds), press ENTER…"
        )
        # Re-confirm every visible input + login/account button in the popup.
        await _dump(
            transport._page,  # noqa: SLF001
            "LOGIN POPUP — EMAIL + PASSWORD INPUTS (current code expects "
            "data-test-id='email-input' and data-test-id='password-input')",
            r"login|account|iniciar|ingres|email|usuario|password|contrase|recordar|continuar",
        )

        await _gate(
            "Now LOG IN manually with your keyring creds, press ENTER when you see "
            "your balance + 'Retirar'/'Depósito' in the header…"
        )
        await _dump(
            transport._page,  # noqa: SLF001
            "LOGGED-IN HEADER (confirm balance-button + Retirar/Depósito markers)",
            r"balance|retirar|dep[oó]sito|cerrar",
        )

        print(
            "\n=== SELECTOR SUMMARY ===\n"
            "Current src/execution/session.py values:\n"
            "  _BETSSON_LOGIN_EMAIL_SEL     = [data-test-id='email-input']\n"
            "  _BETSSON_LOGIN_PASSWORD_SEL  = [data-test-id='password-input']\n"
            "Already grounded (only re-edit if this run contradicts them):\n"
            "  trigger   [data-test-id='login-button']\n"
            "  balance   [data-test-id='balance-button']   + header text Retirar/Depósito\n"
            "  logout    [data-test-id='site-menu-link-anchor-cerrar-sesión']\n"
            "  submit    [data-test-id='account-login-btn-1']\n"
            "Note in LEDGER.md if a captured attribute differs from the in-code value."
        )
        log.info("probe.done")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
