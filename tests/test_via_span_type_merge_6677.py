"""Via.span_type survives the access-witness sidecar and stays valid on merge (#6677)."""

from kicad_tools.router.access_witness import _via_from_list, _via_to_list
from kicad_tools.router.drc_nudge import _expand_via_layers
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Via


def _via(a, b, span_type=None):
    return Via(x=0, y=0, drill=0.3, diameter=0.6, layers=(a, b), net=1, span_type=span_type)


def test_sidecar_round_trips_span_type():
    for st in ("blind", "buried", None):
        v = _via(Layer.IN1_CU, Layer.IN2_CU, st)
        assert _via_from_list(_via_to_list(v, "N"), "N").span_type == st
        v = _via(Layer.F_CU, Layer.IN1_CU, st)
        assert _via_from_list(_via_to_list(v, "OTHER"), "OTHER").span_type == st


def test_old_sidecar_without_span_type_loads_none():
    v = _via(Layer.F_CU, Layer.B_CU)
    payload = _via_to_list(v, "N")
    assert len(payload) == 8
    assert _via_from_list(payload, "N").span_type is None
    assert _via_from_list([*payload, "N"], "N").span_type is None


def test_widen_to_both_outer_clears_span_type():
    s = _via(Layer.IN1_CU, Layer.IN2_CU, "buried")
    _expand_via_layers(s, _via(Layer.F_CU, Layer.B_CU))
    assert s.span_type is None


def test_partial_widen_keeps_valid_keyword():
    s = _via(Layer.IN1_CU, Layer.IN2_CU, "buried")
    _expand_via_layers(s, _via(Layer.F_CU, Layer.IN1_CU, "blind"))
    assert s.layers == (Layer.F_CU, Layer.IN2_CU)
    assert s.span_type == "blind"
    s = _via(Layer.F_CU, Layer.IN1_CU, "blind")
    _expand_via_layers(s, _via(Layer.IN1_CU, Layer.IN2_CU, "buried"))
    assert s.span_type == "blind"
    s = _via(Layer.IN1_CU, Layer.IN1_CU, "buried")
    s.layers = (Layer.IN1_CU, Layer.IN2_CU)
    s.refresh_span_type()
    assert s.span_type == "buried"


def test_through_via_never_becomes_blind():
    s = _via(Layer.F_CU, Layer.IN1_CU)
    _expand_via_layers(s, _via(Layer.F_CU, Layer.IN2_CU))
    assert s.span_type is None
