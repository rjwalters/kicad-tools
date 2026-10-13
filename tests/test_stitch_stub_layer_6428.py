"""Issue #6428: every stitch stub check is layer-aware for tracks.

A stitch stub (straight, dog-leg or multi-leg) is emitted on ``pad.layer``,
so only other-net TRACKS on that layer can short it.  Vias, pads and fills
are still tested on every layer, and a through-hole pad (copper on every
layer) still meets tracks on every layer.
"""

from __future__ import annotations

import pytest

from kicad_tools.cli.stitch_cmd import (
    FilledPolygon,
    PadInfo,
    TrackSegment,
    _check_dogleg_path_clearance,
    _check_multileg_path_clearance,
    _stub_layer,
    calculate_dogleg_via_position,
    calculate_via_position,
)

#: An L-shaped stub: (0, 0) -> (0, 1) -> (1, 1).
_DOGLEG = (0.0, 0.0, 0.0, 1.0, 1.0, 1.0)
_MULTILEG = [(0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 2.0)]
_HALF = 0.1
_CLEARANCE = 0.2


def _crossing_track(layer: str) -> TrackSegment:
    """A foreign track crossing both paths' first leg at y = 0.5."""
    return TrackSegment(-1.0, 0.5, 0.5, 0.5, 0.2, layer, 7)


def _dogleg_clear(stub_layer, tracks=(), vias=(), pads=(), fills=None) -> bool:
    return _check_dogleg_path_clearance(
        *_DOGLEG,
        _HALF,
        list(tracks),
        list(vias),
        list(pads),
        _CLEARANCE,
        fills,
        stub_layer=stub_layer,
    )


def _multileg_clear(stub_layer, tracks=(), vias=(), pads=()) -> bool:
    return _check_multileg_path_clearance(
        _MULTILEG, _HALF, list(tracks), list(vias), list(pads), _CLEARANCE, stub_layer=stub_layer
    )


@pytest.mark.parametrize("check", [_dogleg_clear, _multileg_clear])
class TestLegChecks:
    def test_other_layer_track_is_ignored(self, check):
        assert check("F.Cu", tracks=[_crossing_track("B.Cu")])

    def test_same_layer_track_refuses(self, check):
        assert not check("F.Cu", tracks=[_crossing_track("F.Cu")])

    def test_no_stub_layer_keeps_every_layer(self, check):
        """``None`` (a through-hole pad) meets tracks on every layer."""
        assert not check(None, tracks=[_crossing_track("B.Cu")])

    def test_vias_and_pads_are_never_filtered(self, check):
        assert not check("F.Cu", vias=[(0.0, 0.5, 0.6, 7)])
        assert not check("F.Cu", pads=[(0.0, 0.5, 0.3, 7)])


def test_dogleg_fill_is_never_filtered():
    fill = FilledPolygon(7, "VCC", "B.Cu", [(-0.5, 0.4), (0.5, 0.4), (0.5, 0.6), (-0.5, 0.6)])
    assert not _dogleg_clear("F.Cu", fills=[fill])


def test_stub_layer_is_the_pad_layer_for_smd_only():
    pad = PadInfo("U1", "1", 1, "GND", 0.0, 0.0, "B.Cu", 0.3, 1.5)
    assert _stub_layer(pad) == "B.Cu"
    pad.pad_type = "thru_hole"
    assert _stub_layer(pad) is None


# -- end to end: the placement strategies pass the pad's layer ----------------


def _boxed_pad(pad_type: str = "smd") -> PadInfo:
    return PadInfo("U1", "1", 1, "GND", 10.0, 10.0, "F.Cu", 0.3, 1.5, pad_type)


def _straight_blockers(layer: str) -> list[TrackSegment]:
    """Foreign tracks hugging the pad on every side, all on ``layer``: no via
    candidate touches them, but every straight stub crosses one."""
    return [
        TrackSegment(9.0, 10.55, 11.0, 10.55, 0.1, layer, 7),
        TrackSegment(9.0, 9.45, 11.0, 9.45, 0.1, layer, 7),
        TrackSegment(10.55, 9.0, 10.55, 11.0, 0.1, layer, 7),
        TrackSegment(9.45, 9.0, 9.45, 11.0, 0.1, layer, 7),
    ]


_KW = {"offset": 0.5, "via_size": 0.3, "existing_vias": [], "clearance": 0.15, "trace_width": 0.1}


def test_straight_offset_candidate_ignores_other_layer_tracks():
    assert calculate_via_position(_boxed_pad(), other_net_tracks=_straight_blockers("B.Cu"), **_KW)
    assert (
        calculate_via_position(_boxed_pad(), other_net_tracks=_straight_blockers("F.Cu"), **_KW)
        is None
    )
    assert (
        calculate_via_position(
            _boxed_pad("thru_hole"), other_net_tracks=_straight_blockers("B.Cu"), **_KW
        )
        is None
    )


def test_dogleg_candidate_ignores_other_layer_tracks():
    blockers_b = _straight_blockers("B.Cu")
    blockers_f = _straight_blockers("F.Cu")
    assert calculate_dogleg_via_position(_boxed_pad(), other_net_tracks=blockers_b, **_KW)
    assert calculate_dogleg_via_position(_boxed_pad(), other_net_tracks=blockers_f, **_KW) is None
    assert (
        calculate_dogleg_via_position(_boxed_pad("thru_hole"), other_net_tracks=blockers_b, **_KW)
        is None
    )
