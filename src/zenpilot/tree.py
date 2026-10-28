"""Ref table and rendering for read_page."""

import itertools
from collections import Counter
from dataclasses import dataclass
from importlib.resources import files

WALKER_JS = (files("zenpilot") / "walker.js").read_text()
MAX_ITEMS = 600  # per frame, enforced in the page
MAX_CHARS = 12_000  # whole output


def unwrap(v: dict):
    """Convert a BiDi RemoteValue into plain Python.

    Nodes become {"sharedId": ...} and windows become {"context": ...}.
    """
    t = v["type"]
    if t in ("string", "boolean"):
        return v["value"]
    if t == "number":
        return v["value"] if isinstance(v["value"], (int, float)) else None  # NaN, Infinity
    if t in ("null", "undefined"):
        return None
    if t == "array":
        return [unwrap(x) for x in v.get("value", [])]
    if t == "object":
        return {k if isinstance(k, str) else str(unwrap(k)): unwrap(x) for k, x in v.get("value", [])}
    if t == "node":
        return {"sharedId": v["sharedId"]}
    if t == "window":
        return {"context": v["value"]["context"]}
    return None


@dataclass(frozen=True)
class Ref:
    tab: str
    context: str
    shared_id: str
    key: str = ""  # role and name, e.g. 'button "Apply"'
    # The page it was read on, set when it's in the top frame and no other control there has
    # the same key: then it can be found again after a re-render or a reconnect.
    url: str = ""
    session: int = 0  # BiDi session whose node ids these are


class Refs:
    """Maps short refs (r1, r2, ...) to nodes.

    The counter never resets, so a ref from an old read_page can't silently
    point at a different element; it's simply unknown.
    """

    def __init__(self):
        self._counter = itertools.count(1)
        self._refs: dict[str, Ref] = {}

    def add(self, ref: Ref) -> str:
        key = f"r{next(self._counter)}"
        self._refs[key] = ref
        return key

    def get(self, key: str) -> Ref | None:
        return self._refs.get(key.strip().lower())

    def replace(self, key: str, ref: Ref) -> None:
        self._refs[key.strip().lower()] = ref

    def drop_tab(self, tab: str) -> None:
        self._refs = {k: r for k, r in self._refs.items() if r.tab != tab}

    def clear(self) -> None:
        self._refs.clear()


@dataclass
class Item:
    depth: int
    line: str
    context: str
    shared_id: str | None = None
    key: str | None = None
    unique: bool = False  # no other control in its frame has this key


def mark_unique(items: list[Item], context: str) -> None:
    keys = Counter(it.key for it in items if it.key and it.context == context)
    for it in items:
        it.unique = it.context == context and keys[it.key] == 1


def render(items: list[Item], refs: Refs, tab: str, truncated: bool = False, url: str = "", session: int = 0) -> str:
    """Format items as an indented outline, assigning refs to interactive nodes.

    Over budget, plain text lines go first (get_text covers prose), then the tail.
    """
    notes = []
    if sum(len(it.line) + 2 * it.depth + 8 for it in items) > MAX_CHARS:
        items = [it for it in items if it.shared_id or not it.line.startswith('"')]
        notes.append("[plain text omitted to fit; use get_text to read it]")
    lines, size = [], 0
    for it in items:
        line = "  " * it.depth + it.line
        if size + len(line) + 8 > MAX_CHARS:
            truncated = True
            break
        if it.shared_id:
            ref = Ref(tab, it.context, it.shared_id, it.key or "", url if it.unique else "", session)
            line = "  " * it.depth + refs.add(ref) + " " + it.line
        lines.append(line)
        size += len(line) + 1
    if truncated:
        notes.append("[truncated: page has more elements than fit]")
    return "\n".join(lines + notes) if lines else "[no visible content]"
