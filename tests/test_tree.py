"""Ref table, BiDi value unwrapping and read_page rendering."""

from zenpilot import tree
from zenpilot.tree import Item, Ref, Refs, render, unwrap


def test_unwrap_values():
    v = {"type": "array", "value": [
        {"type": "number", "value": 2},
        {"type": "string", "value": "x"},
        {"type": "null"},
        {"type": "node", "sharedId": "abc", "value": {"nodeType": 1}},
        {"type": "window", "value": {"context": "ctx-1"}},
        {"type": "object", "value": [["k", {"type": "boolean", "value": True}]]},
        {"type": "number", "value": "NaN"},
    ]}
    assert unwrap(v) == [2, "x", None, {"sharedId": "abc"}, {"context": "ctx-1"}, {"k": True}, None]


def test_refs_never_reuse_numbers():
    refs = Refs()
    a = refs.add(Ref("t1", "c", "n1"))
    refs.drop_tab("t1")
    b = refs.add(Ref("t1", "c", "n2"))
    assert (a, b) == ("r1", "r2")
    assert refs.get("r1") is None
    assert refs.get(" R2 ") == Ref("t1", "c", "n2")


def test_drop_tab_keeps_other_tabs():
    refs = Refs()
    refs.add(Ref("t1", "c1", "n1"))
    keep = refs.add(Ref("t2", "c2", "n2"))
    refs.drop_tab("t1")
    assert refs.get("r1") is None and refs.get(keep)


def test_render_indents_and_assigns_refs():
    refs = Refs()
    items = [
        Item(0, "nav", "top"),
        Item(1, 'link "Home" -> /', "top", "n1"),
        Item(0, 'frame "Login"', "top"),
        Item(1, 'button "Go"', "frame-ctx", "n2"),
    ]
    out = render(items, refs, "t1")
    assert out == 'nav\n  r1 link "Home" -> /\nframe "Login"\n  r2 button "Go"'
    assert refs.get("r2") == Ref("t1", "frame-ctx", "n2")


def test_render_empty():
    assert render([], Refs(), "t1") == "[no visible content]"


def test_render_drops_text_before_refs_when_over_budget(monkeypatch):
    monkeypatch.setattr(tree, "MAX_CHARS", 200)
    items = [Item(0, '"' + "x" * 50 + '"', "c") for _ in range(5)] + [Item(0, 'button "B"', "c", "n1")]
    out = render(items, Refs(), "t1")
    assert "r1 button" in out
    assert "xxxx" not in out
    assert "use get_text" in out


def test_render_truncates_tail(monkeypatch):
    monkeypatch.setattr(tree, "MAX_CHARS", 100)
    refs = Refs()
    items = [Item(0, f'button "{i}"', "c", f"n{i}") for i in range(50)]
    out = render(items, refs, "t1")
    assert len(out) < 200
    assert out.endswith("[truncated: page has more elements than fit]")
    # Refs are only assigned to lines that were actually shown.
    shown = out.count(" button ")
    assert refs.get(f"r{shown}") and refs.get(f"r{shown + 1}") is None


def test_render_passes_through_page_truncation():
    out = render([Item(0, 'button "a"', "c", "n")], Refs(), "t1", truncated=True)
    assert out.endswith("[truncated: page has more elements than fit]")


def test_walker_js_is_a_bare_function_expression():
    # Firefox wraps functionDeclaration in parentheses; a trailing ';' breaks it.
    src = tree.WALKER_JS.strip()
    assert src.endswith("}") and "(maxItems) =>" in src
