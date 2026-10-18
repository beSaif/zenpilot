"""Input that moves at a person's pace, as WebDriver BiDi action lists.

Sites score input by how it moves, not just what it does: a pointer that teleports onto the
exact center of a button and clicks in 0 ms, or keys arriving 5 ms apart, reads as a bot. This
builds the same actions a hand on a mouse and keyboard would produce: a curved, decelerating
path, a short hover, a press held for ~0.1 s, keys with Shift held for capitals and a typing
rhythm, and scrolling in wheel notches. Nothing here changes what the browser reports about
itself; it only paces trusted input.

Timing models: movement time follows Fitts's law with a minimum-jerk speed profile; key
intervals are log-normal (Dhakal et al., CHI 2018: 136M keystrokes, mean ~240 ms).
"""

import math
import random
from dataclasses import dataclass

SHIFT = ""
# Keys a US layout types with Shift held. Other characters are sent as-is.
SHIFTED = set('~!@#$%^&*()_+{}|:"<>?')


@dataclass(frozen=True)
class Style:
    name: str
    paths: bool  # move along a path (False: jump to the target, like plain automation)
    speed: float  # multiplier on movement and pause durations
    key_ms: float  # mean interval between keystrokes
    typing_budget_s: float  # long text types faster so a call stays well inside client timeouts


STYLES = {
    "fast": Style("fast", paths=False, speed=0.0, key_ms=0, typing_budget_s=0),
    "natural": Style("natural", paths=True, speed=1.0, key_ms=150, typing_budget_s=12),
    "careful": Style("careful", paths=True, speed=1.4, key_ms=210, typing_budget_s=25),
}


def pause(ms: float) -> dict:
    return {"type": "pause", "duration": max(0, round(ms))}


def click_point(rect: list[float], viewport: tuple[float, float], rng: random.Random) -> tuple[float, float]:
    """Where a person would click in rect [x, y, w, h]: near the center, never on the edge."""
    x, y, w, h = rect
    vw, vh = viewport
    # Only the part of the element that's on screen can be clicked.
    left, top = max(x, 0), max(y, 0)
    right, bottom = min(x + w, vw - 1), min(y + h, vh - 1)
    cx, cy, hw, hh = (left + right) / 2, (top + bottom) / 2, (right - left) / 2, (bottom - top) / 2
    # Gaussian around the center (sd = 1/6 of the size), kept inside the middle 70%.
    dx = max(-0.7, min(0.7, rng.gauss(0, 1 / 3))) * hw
    dy = max(-0.7, min(0.7, rng.gauss(0, 1 / 3))) * hh
    return cx + dx, cy + dy


def movement_ms(distance: float, width: float, style: Style, rng: random.Random) -> float:
    """Fitts's law: longer for far or small targets."""
    fitts = 90 + 150 * math.log2(distance / max(width, 8) + 1)
    return min(1100, fitts) * style.speed * rng.uniform(0.85, 1.15)


def _min_jerk(t: float) -> float:
    return t * t * t * (10 - 15 * t + 6 * t * t)


def _bezier(p0, p1, p2, p3, t):
    u = 1 - t
    return tuple(
        u**3 * a + 3 * u * u * t * b + 3 * u * t * t * c + t**3 * d for a, b, c, d in zip(p0, p1, p2, p3, strict=True)
    )


def path(
    start: tuple[float, float],
    end: tuple[float, float],
    width: float,
    viewport: tuple[float, float],
    style: Style,
    rng: random.Random,
) -> list[tuple[float, float, float]]:
    """Points (x, y, ms since the previous point) from start to end, ending exactly on end."""
    vw, vh = viewport

    def clamp(p):
        return min(max(p[0], 0), vw - 1), min(max(p[1], 0), vh - 1)

    def segment(a, b, total_ms, bow):
        dx, dy = b[0] - a[0], b[1] - a[1]
        dist = math.hypot(dx, dy)
        if dist < 1:
            return []
        # Control points bowed to one side of the straight line, like a wrist pivoting.
        nx, ny = -dy / dist, dx / dist
        offset = bow * min(dist * rng.uniform(0.08, 0.22), 120)
        c1 = (a[0] + dx * 0.3 + nx * offset, a[1] + dy * 0.3 + ny * offset)
        c2 = (a[0] + dx * 0.7 + nx * offset * 0.6, a[1] + dy * 0.7 + ny * offset * 0.6)
        steps = max(3, round(total_ms / rng.uniform(8, 12)))
        out, prev_t = [], 0.0
        for i in range(1, steps + 1):
            t_lin = i / steps
            x, y = _bezier(a, c1, c2, b, _min_jerk(t_lin))
            if i < steps:  # hand tremor; the last point lands exactly
                x, y = x + rng.gauss(0, 0.4), y + rng.gauss(0, 0.4)
            out.append((*clamp((x, y)), (t_lin - prev_t) * total_ms))
            prev_t = t_lin
        return out

    start, end = clamp(start), clamp(end)
    dist = math.hypot(end[0] - start[0], end[1] - start[1])
    total = movement_ms(dist, width, style, rng)
    bow = rng.choice((-1, 1))
    if dist > 500 and rng.random() < 0.3:
        # Overshoot a little, then correct: common on long, fast moves.
        ux, uy = (end[0] - start[0]) / dist, (end[1] - start[1]) / dist
        over = min(dist * rng.uniform(0.03, 0.06), 30)
        past = clamp((end[0] + ux * over + rng.gauss(0, 3), end[1] + uy * over + rng.gauss(0, 3)))
        return segment(start, past, total * 0.85, bow) + segment(past, end, 110 * style.speed, -bow)
    return segment(start, end, total, bow)


def pointer_moves(points: list[tuple[float, float, float]]) -> list[dict]:
    """BiDi pointer actions for a path: a move, then a pause, for each point."""
    actions = []
    for x, y, ms in points:
        actions.append(pause(ms))
        actions.append({"type": "pointerMove", "x": x, "y": y, "origin": "viewport"})
    return actions


def click_actions(points: list[tuple[float, float, float]], style: Style, rng: random.Random) -> list[dict]:
    """Move along points, hover briefly, then press and release."""
    if not style.paths:
        return [{"type": "pointerDown", "button": 0}, {"type": "pointerUp", "button": 0}]
    hold = max(45, min(200, rng.gauss(95, 25)))
    return [
        *pointer_moves(points),
        pause(rng.uniform(70, 220) * style.speed),
        {"type": "pointerDown", "button": 0},
        pause(hold * style.speed),
        {"type": "pointerUp", "button": 0},
    ]


def key_actions(keys: list[str], style: Style, rng: random.Random) -> list[dict]:
    """keyDown/keyUp pairs with Shift held where a keyboard needs it, paced like typing."""
    gaps = []
    if style.key_ms:
        sigma = 0.45
        mu = math.log(style.key_ms) - sigma * sigma / 2
        for i in range(len(keys)):
            gap = max(60.0, rng.lognormvariate(mu, sigma))
            if i and keys[i - 1] == " ":  # a beat before each new word
                gap *= 1.3
            gaps.append(0 if i == 0 else gap)
        budget = style.typing_budget_s * 1000
        if sum(gaps) > budget:  # long text: keep the rhythm, compress the tempo
            scale = budget / sum(gaps)
            gaps = [max(15.0, g * scale) for g in gaps]
    actions: list[dict] = []
    for i, k in enumerate(keys):
        shift = len(k) == 1 and (k in SHIFTED or (k.isalpha() and k.isupper() and k.lower() != k))
        if gaps and gaps[i]:
            actions.append(pause(gaps[i]))
        hold = max(30, min(150, rng.gauss(80, 20))) * style.speed if style.key_ms else 0
        if shift:
            actions += [{"type": "keyDown", "value": SHIFT}, pause(hold * 0.5)]
        actions.append({"type": "keyDown", "value": k})
        if hold:
            actions.append(pause(hold))
        actions.append({"type": "keyUp", "value": k})
        if shift:
            actions += [pause(hold * 0.3), {"type": "keyUp", "value": SHIFT}]
    return [a for a in actions if a["type"] != "pause" or a["duration"]]


def wheel_actions(x: int, y: int, dx: int, dy: int, style: Style, rng: random.Random) -> list[dict]:
    """Scroll by (dx, dy) as wheel notches of ~100 px, in short bursts."""
    if not style.paths:
        return [{"type": "scroll", "x": x, "y": y, "deltaX": dx, "deltaY": dy}]
    total = max(abs(dx), abs(dy))
    notches = max(1, math.ceil(total / 100))
    actions, sent_x, sent_y = [], 0, 0
    burst = rng.randint(3, 7)
    for i in range(1, notches + 1):
        nx, ny = round(dx * i / notches), round(dy * i / notches)
        if i > 1:
            actions.append(
                pause((rng.uniform(150, 320) if (i - 1) % burst == 0 else rng.uniform(18, 45)) * style.speed)
            )
        actions.append({"type": "scroll", "x": x, "y": y, "deltaX": nx - sent_x, "deltaY": ny - sent_y})
        sent_x, sent_y = nx, ny
    return actions


def duration_s(actions: list[dict]) -> float:
    return sum(a.get("duration", 0) for a in actions if a["type"] == "pause") / 1000
