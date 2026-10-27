"""Scroll repeats wheel actions until the target is reached or the page stops moving, against a fake page."""

from zenpilot import browser as browser_mod
from zenpilot.browser import Browser


class FakePage:
    """Window scroll offsets. Each wheel action moves at most `per_wheel` px, like Zen's partial scrolls."""

    def __init__(self, height=5000, view=800, per_wheel=1000):
        self.height, self.view, self.per_wheel = height, view, per_wheel
        self.y = self.x = 0
        self.wheels: list[int] = []
        self.fallbacks = 0

    @property
    def max_y(self):
        return max(0, self.height - self.view)

    async def run(self, ctx, fn, *args, timeout=15):
        if fn is browser_mod.SCROLL_INFO_JS:
            return [1000, self.view, self.x, self.y, 1000, self.height]
        if fn is browser_mod.SCROLL_STATE_JS:
            return f"{self.x},{self.y}"
        if fn is browser_mod.SCROLL_JS_FALLBACK:
            self.fallbacks += 1
            self._move(args[0], args[1])
            return None
        raise AssertionError(f"unexpected script {fn[:40]!r}")

    async def call(self, method, params=None, timeout=30):
        assert method == "input.performActions", method
        wheel = params["actions"][0]["actions"][0]
        self.wheels.append(wheel["deltaY"])
        self._move(wheel["deltaX"], wheel["deltaY"])
        return {}

    def _move(self, dx, dy):
        step_y = max(-self.per_wheel, min(self.per_wheel, dy))
        self.y = max(0, min(self.max_y, self.y + step_y))


def browser_on(page):
    b = Browser()
    b.run, b.call = page.run, page.call
    return b


async def test_scroll_down_repeats_until_target_reached():
    page = FakePage()
    text = await browser_on(page).scroll({"context": "c1"}, "down", 2500)
    assert page.y == 2500
    assert "y=2500 " in text
    assert len(page.wheels) == 3  # 1000 + 1000 + 500


async def test_scroll_stops_at_page_end():
    page = FakePage()
    await browser_on(page).scroll({"context": "c1"}, "down", 5000)
    assert page.y == page.max_y == 4200
    assert len(page.wheels) <= 6


async def test_scroll_up_clamps_at_top():
    page = FakePage()
    page.y = 2000
    text = await browser_on(page).scroll({"context": "c1"}, "up", 5000)
    assert page.y == 0
    assert "y=0 " in text


async def test_scroll_gives_up_when_nothing_moves():
    page = FakePage(height=800, view=800)  # nothing to scroll
    await browser_on(page).scroll({"context": "c1"}, "down", 600)
    assert len(page.wheels) == 1  # no repeat when the first wheel did nothing
    assert page.fallbacks == 1


async def test_scroll_repeats_are_capped():
    page = FakePage(per_wheel=1)  # Zen moves 1px per wheel: never reaches the target
    await browser_on(page).scroll({"context": "c1"}, "down", 5000)
    assert 1 < len(page.wheels) <= browser_mod.SCROLL_ROUNDS


class InnerContainerPage(FakePage):
    """The window never moves; an inner container (reported after the window slots) scrolls."""

    def __init__(self):
        super().__init__()
        self.inner = 0

    async def run(self, ctx, fn, *args, timeout=15):
        if fn is browser_mod.SCROLL_STATE_JS:
            return f"{self.x},{self.y},0,{self.inner},0,{self.inner}"
        return await super().run(ctx, fn, *args, timeout=timeout)

    def _move(self, dx, dy):
        self.inner = max(0, self.inner + max(-1000, min(1000, dy)))


async def test_scroll_in_inner_container_stops_at_requested_amount():
    page = InnerContainerPage()
    await browser_on(page).scroll({"context": "c1"}, "down", 500)
    assert page.inner == 500
    assert page.wheels == [500]


class ShiftingChainPage(FakePage):
    """The element under the viewport center changes branch after the first wheel, so the chain length changes."""

    async def run(self, ctx, fn, *args, timeout=15):
        if fn is browser_mod.SCROLL_STATE_JS:
            if self.y <= 3000:  # [window, leaf, body, html]
                return f"{self.x},{self.y},0,0,0,0,0,{self.y}"
            return f"{self.x},{self.y},0,0,0,{self.y}"  # [window, child of html, html]
        return await super().run(ctx, fn, *args, timeout=timeout)


async def test_scroll_with_changing_ancestor_chain_pairs_shared_ancestors():
    page = ShiftingChainPage(height=10000)
    page.y = 3000
    await browser_on(page).scroll({"context": "c1"}, "down", 2500)
    assert page.y == 5500
    assert len(page.wheels) == 3  # 1000 + 1000 + 500, not stopped early by a false 4000 px reading
