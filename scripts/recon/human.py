"""Human-behavior simulation for recon — the highest-leverage,
lowest-risk anti-detection technique.

Modern anti-bot systems (Cloudflare Bot Management, DataDome) no
longer just fingerprint the client — they score *behavior*. The
tells they flag are all forms of unrealistic perfection:

- mouse moving in straight lines / teleporting
- scrolling at constant velocity, or pixel-perfectly
- zero think-time before clicks
- clicks landing dead-center on elements
- perfectly uniform inter-action timing
- typing instantly with no errors

A real person is imperfect and non-deterministic. This module makes
the recon browser imperfect on purpose:

- **Mouse tremor + curved paths** — `HumanCursor.move_to` interpolates
  through jittered waypoints with a mid-path bulge, not a straight
  line, plus per-step micro-jitter.
- **Imperfect scrolling** — variable deltas, occasional up-corrections
  (overshoot), and the odd long "reading" pause.
- **Think-time / hesitation** — delays drawn from a right-skewed
  distribution (usually quick, occasionally a long pause), not a
  tight uniform band.
- **Off-center clicks** — clicks land at a random point inside the
  element's box, never dead-center.
- **Imperfect typing** — `type_text` varies per-key timing and
  occasionally fat-fingers a character then backspaces to correct
  (for the eventual logged-in flows; read-only recon doesn't type).

State (current cursor position) lives on a `HumanCursor` instance so
movement is continuous across the whole session — a real hand doesn't
teleport between actions.

What this module deliberately does NOT do: proxy rotation, IP
spoofing, or per-instance fingerprint fabrication. For a funded-
account betting operation the scraping identity must stay coherent
with the betting identity; see the 2026-05-28 LEDGER entry for why
those techniques are the wrong tool here.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import random
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page

_DEFAULT_W = 1440
_DEFAULT_H = 900


@dataclass(frozen=True)
class HumanPacing:
    """Bounded behavior/timing ranges. Durations in seconds, distances
    in pixels. Defaults are deliberately patient and imperfect — recon
    is not latency-sensitive, and haste/precision are what get flagged."""

    # Think-time
    dwell_range: tuple[float, float] = (4.0, 9.0)  # "reading" a page
    action_pause_range: tuple[float, float] = (0.5, 1.6)  # before an action
    hesitation_prob: float = 0.18  # chance an action_pause becomes a long think
    hesitation_mult_range: tuple[float, float] = (1.8, 4.0)

    # Mouse motion
    mouse_moves_range: tuple[int, int] = (2, 5)  # wander hops per warm-up
    path_waypoints_range: tuple[int, int] = (6, 14)  # interp points per move
    tremor_px: float = 2.2  # per-waypoint micro-jitter stddev
    curve_px: float = 22.0  # mid-path perpendicular bulge stddev
    step_pause_range: tuple[float, float] = (0.008, 0.03)  # between waypoints

    # Scrolling
    scroll_steps_range: tuple[int, int] = (2, 6)
    scroll_delta_range: tuple[int, int] = (220, 760)
    scroll_pause_range: tuple[float, float] = (0.4, 1.3)
    scroll_overshoot_prob: float = 0.25  # chance of a small up-correction
    reading_pause_prob: float = 0.30  # chance of a long pause mid-scroll
    reading_pause_range: tuple[float, float] = (1.5, 4.0)

    # Clicking
    click_offset_frac: float = 0.28  # max off-center, as frac of half-extent

    # Typing (future logged-in flows)
    type_delay_range: tuple[float, float] = (0.06, 0.22)
    typo_prob: float = 0.04  # per-char chance of a fat-finger + correction


DEFAULT_PACING = HumanPacing()
# Per-platform overrides; empty means everyone uses DEFAULT_PACING. Add
# an entry when a platform proves more or less twitchy.
PER_PLATFORM_PACING: dict[str, HumanPacing] = {}


def pacing_for(platform: str) -> HumanPacing:
    return PER_PLATFORM_PACING.get(platform, DEFAULT_PACING)


def _u(rng: tuple[float, float]) -> float:
    return random.uniform(rng[0], rng[1])


def _ui(rng: tuple[int, int]) -> int:
    return random.randint(rng[0], rng[1])


def _skewed_delay(rng: tuple[float, float], pacing: HumanPacing) -> float:
    """A think-time delay: usually uniform in `rng`, occasionally
    stretched into a long hesitation. Right-skewed, like human
    reaction times."""
    base = _u(rng)
    if random.random() < pacing.hesitation_prob:
        base *= _u(pacing.hesitation_mult_range)
    return base


def _jittered_path(
    start: tuple[float, float],
    end: tuple[float, float],
    pacing: HumanPacing,
) -> list[tuple[float, float]]:
    """Waypoints from start to end with (a) a smooth perpendicular bulge
    so the path curves rather than going straight, and (b) per-point
    micro-tremor. Bulge magnitude peaks mid-path (sin envelope) and is
    randomly signed, so successive moves don't all curve the same way."""
    (x0, y0), (x1, y1) = start, end
    n = _ui(pacing.path_waypoints_range)
    dx, dy = x1 - x0, y1 - y0
    length = math.hypot(dx, dy) or 1.0
    # Unit perpendicular to the travel direction.
    px, py = -dy / length, dx / length
    bulge = random.gauss(0, pacing.curve_px)
    points: list[tuple[float, float]] = []
    for i in range(1, n + 1):
        t = i / n
        env = math.sin(t * math.pi)  # 0 at ends, 1 in the middle
        bx = px * bulge * env
        by = py * bulge * env
        jx = random.gauss(0, pacing.tremor_px)
        jy = random.gauss(0, pacing.tremor_px)
        points.append((x0 + dx * t + bx + jx, y0 + dy * t + by + jy))
    return points


class HumanCursor:
    """Stateful human-like input driver bound to one Playwright page.

    Tracks the cursor position so motion is continuous across actions
    (a real hand doesn't teleport). All methods are best-effort and
    never raise on transient Playwright errors — recon shouldn't crash
    because a hover lost a race with navigation."""

    def __init__(self, page: Page, pacing: HumanPacing) -> None:
        self.page = page
        self.pacing = pacing
        vp = page.viewport_size or {"width": _DEFAULT_W, "height": _DEFAULT_H}
        self._w = vp["width"]
        self._h = vp["height"]
        # Start somewhere plausible in the upper-left reading area.
        self.x = random.uniform(self._w * 0.2, self._w * 0.5)
        self.y = random.uniform(self._h * 0.2, self._h * 0.5)

    async def dwell(self) -> None:
        await asyncio.sleep(_u(self.pacing.dwell_range))

    async def _think(self) -> None:
        await asyncio.sleep(_skewed_delay(self.pacing.action_pause_range, self.pacing))

    async def move_to(self, x: float, y: float) -> None:
        """Glide the cursor to (x, y) along a jittered, curved path."""
        path = _jittered_path((self.x, self.y), (x, y), self.pacing)
        for wx, wy in path:
            with contextlib.suppress(Exception):
                await self.page.mouse.move(wx, wy, steps=random.randint(2, 5))
            await asyncio.sleep(_u(self.pacing.step_pause_range))
        self.x, self.y = x, y

    async def wander(self) -> None:
        """Move the cursor to a few random points — idle hand motion."""
        for _ in range(_ui(self.pacing.mouse_moves_range)):
            tx = random.uniform(self._w * 0.1, self._w * 0.9)
            ty = random.uniform(self._h * 0.1, self._h * 0.9)
            await self.move_to(tx, ty)

    async def scroll(self) -> None:
        """Scroll down in imperfect steps: variable deltas, occasional
        up-corrections (overshoot) and the odd long reading pause."""
        for _ in range(_ui(self.pacing.scroll_steps_range)):
            delta = _ui(self.pacing.scroll_delta_range)
            with contextlib.suppress(Exception):
                await self.page.mouse.wheel(0, delta)
            if random.random() < self.pacing.scroll_overshoot_prob:
                await asyncio.sleep(_u((0.1, 0.3)))
                with contextlib.suppress(Exception):
                    await self.page.mouse.wheel(0, -random.randint(40, 160))
            if random.random() < self.pacing.reading_pause_prob:
                await asyncio.sleep(_u(self.pacing.reading_pause_range))
            else:
                await asyncio.sleep(_u(self.pacing.scroll_pause_range))

    async def warm_up(self) -> None:
        """Landing warm-up: dwell, wander, scroll, settle. Run right
        after the homepage loads, before navigating anywhere."""
        await self.dwell()
        await self.wander()
        await self.scroll()
        await self._think()

    async def click(self, selectors: list[str], label: str) -> str | None:
        """Try each selector; on the first visible match: move the
        cursor to an OFF-CENTER point inside the element (tremor path),
        hesitate, then click there. Returns the winning selector or None.

        Mirrors the old `human_click` contract so recon.py is a drop-in,
        but adds realistic targeting + think-time."""
        for sel in selectors:
            locator = self.page.locator(sel).first
            try:
                await locator.wait_for(state="visible", timeout=2500)
                box = await locator.bounding_box()
            except Exception:
                continue
            if box is None:
                continue
            # Pick a point inside the box, off-center but away from edges.
            off = self.pacing.click_offset_frac
            tx = box["x"] + box["width"] * (0.5 + random.uniform(-off, off))
            ty = box["y"] + box["height"] * (0.5 + random.uniform(-off, off))
            await self.move_to(tx, ty)
            await self._think()
            try:
                await self.page.mouse.click(tx, ty)
            except Exception:
                # Fall back to the locator click if coordinate click
                # raced with a layout shift.
                with contextlib.suppress(Exception):
                    await locator.click(timeout=2500)
                    print(f"  ✓ {label} via {sel!r} (locator fallback)")
                    return sel
                continue
            print(f"  ✓ {label} via {sel!r}")
            return sel
        print(f"  ✗ {label}: none of {len(selectors)} selectors matched")
        return None

    async def type_text(self, selector: str, text: str) -> bool:
        """Type into an element key-by-key with variable timing and
        occasional fat-finger-then-correct. For future logged-in flows
        (login, deposit-limit forms) — read-only recon never calls this.
        Returns True if the field was found and typed into."""
        locator = self.page.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=2500)
            box = await locator.bounding_box()
        except Exception:
            return False
        if box is not None:
            await self.move_to(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5)
        with contextlib.suppress(Exception):
            await locator.click(timeout=2500)
        await self._think()
        kb = self.page.keyboard
        for ch in text:
            if random.random() < self.pacing.typo_prob:
                wrong = random.choice("asdfghjklqwertyuiop")
                with contextlib.suppress(Exception):
                    await kb.type(wrong, delay=_u(self.pacing.type_delay_range) * 1000)
                await asyncio.sleep(_u((0.08, 0.25)))  # notice the mistake
                with contextlib.suppress(Exception):
                    await kb.press("Backspace")
                await asyncio.sleep(_u((0.05, 0.15)))
            with contextlib.suppress(Exception):
                await kb.type(ch, delay=_u(self.pacing.type_delay_range) * 1000)
        return True
