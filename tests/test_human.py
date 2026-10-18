"""Natural input: paths, click points, typing rhythm and wheel notches, as BiDi actions."""

import math
import random

import pytest

from zenpilot import human

NATURAL = human.STYLES["natural"]


@pytest.fixture
def rng():
    return random.Random(7)


def test_path_ends_on_target_and_stays_on_screen(rng):
    for _ in range(200):
        start = (rng.uniform(-50, 900), rng.uniform(-50, 700))
        end = (rng.uniform(0, 799), rng.uniform(0, 599))
        points = human.path(start, end, 40, (800, 600), NATURAL, rng)
        if math.dist(start, end) < 1:
            continue
        assert points[-1][:2] == pytest.approx(end)
        assert all(0 <= x <= 799 and 0 <= y <= 599 for x, y, _ in points)  # off screen, Firefox throws


def test_path_timing_follows_distance(rng):
    def total(dist):
        return sum(ms for *_, ms in human.path((10, 300), (10 + dist, 300), 40, (2000, 600), NATURAL, rng))

    short, long = total(50), total(1500)
    assert 90 < short < 400 and 400 < long < 1400 and long > short
    points = human.path((10, 300), (700, 300), 40, (2000, 600), NATURAL, rng)
    assert len(points) > 20  # a stream of moves, about every 10 ms
    assert all(5 <= ms <= 40 for *_, ms in points[:-1])


def test_path_curves(rng):
    points = human.path((0, 300), (700, 300), 40, (800, 600), NATURAL, rng)
    assert max(abs(y - 300) for _, y, _ in points) > 10  # not a ruler-straight line


def test_click_point_stays_inside(rng):
    rect, viewport = [100, 200, 80, 30], (800, 600)
    points = [human.click_point(rect, viewport, rng) for _ in range(500)]
    assert all(100 + 80 * 0.15 - 0.01 <= x <= 180 - 80 * 0.15 + 0.01 for x, _ in points)
    assert all(200 + 30 * 0.15 - 0.01 <= y <= 230 - 30 * 0.15 + 0.01 for _, y in points)
    assert len({round(x) for x, _ in points}) > 10  # not always the exact center
    # Only the visible part of a partly off-screen element counts.
    assert all(y <= 599 for _, y in (human.click_point([0, 580, 100, 100], viewport, rng) for _ in range(50)))


def test_click_holds_the_button(rng):
    actions = human.click_actions([(10, 10, 12)], NATURAL, rng)
    kinds = [a["type"] for a in actions]
    assert kinds[-4:] == ["pause", "pointerDown", "pause", "pointerUp"] or kinds[-3:] == [
        "pointerDown",
        "pause",
        "pointerUp",
    ]
    hold = actions[kinds.index("pointerDown") + 1]["duration"]
    assert 45 <= hold <= 200
    assert human.click_actions([], human.STYLES["fast"], rng) == [
        {"type": "pointerDown", "button": 0},
        {"type": "pointerUp", "button": 0},
    ]


def test_keys_hold_shift_for_capitals_and_symbols(rng):
    actions = [a for a in human.key_actions(list("aB!"), NATURAL, rng) if a["type"] != "pause"]
    values = [(a["type"], a["value"]) for a in actions]
    assert values == [
        ("keyDown", "a"),
        ("keyUp", "a"),
        ("keyDown", human.SHIFT),
        ("keyDown", "B"),
        ("keyUp", "B"),
        ("keyUp", human.SHIFT),
        ("keyDown", human.SHIFT),
        ("keyDown", "!"),
        ("keyUp", "!"),
        ("keyUp", human.SHIFT),
    ]
    fast = human.key_actions(list("ab"), human.STYLES["fast"], rng)
    assert all(a["type"] != "pause" for a in fast)


def test_typing_rhythm_and_budget(rng):
    gaps = [a["duration"] for a in human.key_actions(list("hello world"), NATURAL, rng) if a["type"] == "pause"]
    assert gaps and min(gaps) >= 1
    secs = human.duration_s(human.key_actions(list("x" * 20), NATURAL, rng))
    assert 2 < secs < 8  # ~150 ms a key plus holds
    long = human.key_actions(list("x" * 2000), NATURAL, rng)
    assert human.duration_s(long) < NATURAL.typing_budget_s + 2000 * 0.2  # compressed, but still paced


def test_wheel_notches_add_up(rng):
    actions = human.wheel_actions(400, 300, 0, 1234, NATURAL, rng)
    scrolls = [a for a in actions if a["type"] == "scroll"]
    assert sum(a["deltaY"] for a in scrolls) == 1234
    assert len(scrolls) == 13 and all(isinstance(a["deltaY"], int) for a in scrolls)
    assert any(a["type"] == "pause" for a in actions)
    assert human.wheel_actions(1, 2, 0, -500, human.STYLES["fast"], rng) == [
        {"type": "scroll", "x": 1, "y": 2, "deltaX": 0, "deltaY": -500}
    ]
