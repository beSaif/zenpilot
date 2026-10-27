from zenpilot.browser import _arg


def test_plain_dict_is_serialized_as_bidi_object():
    out = _arg({"x": 40, "y": 40.5, "label": "ok"})
    assert out == {
        "type": "object",
        "value": [
            ["x", {"type": "number", "value": 40}],
            ["y", {"type": "number", "value": 40.5}],
            ["label", {"type": "string", "value": "ok"}],
        ],
    }


def test_shared_id_reference_passes_through():
    ref = {"sharedId": "abc"}
    assert _arg(ref) is ref
