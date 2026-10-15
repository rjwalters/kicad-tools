"""kicad-cli-exact net length (Issue #6725).

``kct check`` and both skew tuners share one measurement,
``kicad_tools.router.kicad_length``: KiCad stackup span between the layers a
via connects, plus clipping every track chain at pad/via copper and replacing
the in-copper portion with a straight line to the centre.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path

import pytest

from kicad_tools.router.diffpair_length import DiffPairLengthTracker
from kicad_tools.router.kicad_length import (
    KicadLengthModel,
    KicadStackup,
    LPad,
    LSeg,
    LVia,
    net_length,
)
from kicad_tools.router.layers import Layer
from kicad_tools.router.match_group_length import MatchGroupTracker
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.schema.pcb import PCB

EXPLICIT_A = KicadStackup(
    [
        ("F.Cu", 0.035, True),
        ("d1", 0.5, False),
        ("In1.Cu", 0.035, True),
        ("d2", 0.5, False),
        ("In2.Cu", 0.035, True),
        ("d3", 0.5, False),
        ("B.Cu", 0.035, True),
    ]
)


def _board_text(stackup: bool) -> str:
    st = ""
    if stackup:
        st = """(setup (stackup
      (layer "F.SilkS" (type "Top Silk Screen"))
      (layer "F.Mask" (type "Top Solder Mask") (thickness 0.01))
      (layer "F.Cu" (type "copper") (thickness 0.035))
      (layer "dielectric 1" (type "core") (thickness 0.5) (material "FR4") (epsilon_r 4.5) (loss_tangent 0.02))
      (layer "In1.Cu" (type "copper") (thickness 0.035))
      (layer "dielectric 2" (type "prepreg") (thickness 0.5) (material "FR4") (epsilon_r 4.5) (loss_tangent 0.02))
      (layer "In2.Cu" (type "copper") (thickness 0.035))
      (layer "dielectric 3" (type "core") (thickness 0.5) (material "FR4") (epsilon_r 4.5) (loss_tangent 0.02))
      (layer "B.Cu" (type "copper") (thickness 0.035))
      (layer "B.Mask" (type "Bottom Solder Mask") (thickness 0.01))
      (copper_finish "None") (dielectric_constraints no)))"""
    return f"""(kicad_pcb (version 20241229) (generator "pcbnew") (generator_version "9.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers (0 "F.Cu" signal) (4 "In1.Cu" signal) (6 "In2.Cu" signal) (2 "B.Cu" signal)
    (9 "F.Adhes" user "F.Adhesive") (25 "Edge.Cuts" user) (1 "F.Mask" user) (3 "B.Mask" user) (5 "F.SilkS" user "F.Silkscreen") (7 "B.SilkS" user "B.Silkscreen"))
  {st}
  (net 0 "") (net 1 "N1")
  (footprint "T:R" (layer "F.Cu") (uuid "11111111-1111-1111-1111-111111111111") (at 50 50 30)
    (pad "1" smd rect (at 0 0 30) (size 0.45 0.45) (layers "F.Cu" "F.Mask") (net 1 "N1") (uuid "11111111-1111-1111-1111-111111111112"))
    (pad "2" smd roundrect (at 3 0 30) (size 0.9 0.5) (layers "F.Cu" "F.Mask") (roundrect_rratio 0.25) (net 0 "") (uuid "11111111-1111-1111-1111-111111111113")))
  (segment (start 50 50) (end 50.1 50.0) (width 0.2) (layer "F.Cu") (net 1) (uuid "21111111-1111-1111-1111-111111111111"))
  (segment (start 50.1 50.0) (end 50.45 50.35) (width 0.2) (layer "F.Cu") (net 1) (uuid "21111111-1111-1111-1111-111111111112"))
  (segment (start 50.45 50.35) (end 56 50.35) (width 0.2) (layer "F.Cu") (net 1) (uuid "21111111-1111-1111-1111-111111111113"))
  (segment (start 56 50.35) (end 56 55.0) (width 0.2) (layer "F.Cu") (net 1) (uuid "21111111-1111-1111-1111-111111111114"))
  (via (at 56 55.0) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "31111111-1111-1111-1111-111111111111"))
  (segment (start 56 55.0) (end 56 55.8) (width 0.2) (layer "In1.Cu") (net 1) (uuid "21111111-1111-1111-1111-111111111115"))
  (segment (start 56 55.8) (end 57 56.8) (width 0.2) (layer "In1.Cu") (net 1) (uuid "21111111-1111-1111-1111-111111111116"))
  (segment (start 57 56.8) (end 60 56.8) (width 0.2) (layer "In1.Cu") (net 1) (uuid "21111111-1111-1111-1111-111111111117"))
  (via (at 60.2 56.9) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 1) (uuid "31111111-1111-1111-1111-111111111112"))
  (segment (start 60 56.8) (end 60.2 56.9) (width 0.2) (layer "In1.Cu") (net 1) (uuid "21111111-1111-1111-1111-111111111118"))
  (segment (start 60.2 56.9) (end 60.2 59) (width 0.2) (layer "In2.Cu") (net 1) (uuid "21111111-1111-1111-1111-111111111119"))
  (gr_rect (start 40 40) (end 70 70) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts") (uuid "41111111-1111-1111-1111-111111111111"))
)
"""


def _write(tmp_path: Path, stackup: bool) -> Path:
    path = tmp_path / "board.kicad_pcb"
    path.write_text(_board_text(stackup))
    return path


class TestStackupHeights:
    def test_explicit_stackup_spans(self):
        s = EXPLICIT_A
        assert s.span("F.Cu", "B.Cu") == pytest.approx(1.640)
        assert s.span("F.Cu", "In1.Cu") == pytest.approx(0.5525)
        assert s.span("F.Cu", "In2.Cu") == pytest.approx(1.0875)
        assert s.span("In1.Cu", "In2.Cu") == pytest.approx(0.535)
        assert s.span("In1.Cu", "B.Cu") == pytest.approx(1.0875)

    def test_default_4l_stackup(self):
        s = KicadStackup.default(1.6, 4)
        assert s.span("F.Cu", "B.Cu") == pytest.approx(1.580)
        assert s.span("In1.Cu", "B.Cu") == pytest.approx(1.0475)
        assert s.span("In1.Cu", "In2.Cu") == pytest.approx(0.515)

    def test_default_scales_with_board_thickness(self):
        assert KicadStackup.default(2.0, 4).span("F.Cu", "B.Cu") == pytest.approx(1.980)
        assert KicadStackup.default(0.8, 2).span("F.Cu", "B.Cu") == pytest.approx(0.78)

    def test_explicit_stackup_read_from_pcb(self, tmp_path):
        pcb = PCB.load(str(_write(tmp_path, stackup=True)))
        assert KicadStackup.from_pcb(pcb).span("F.Cu", "B.Cu") == pytest.approx(1.640)

    def test_missing_stackup_uses_kicad_default(self, tmp_path):
        pcb = PCB.load(str(_write(tmp_path, stackup=False)))
        assert KicadStackup.from_pcb(pcb).span("F.Cu", "B.Cu") == pytest.approx(1.580)


class TestViaConnectedSpan:
    def test_declared_through_via_measures_connected_layers(self):
        stackup = KicadStackup.default(1.6, 4)
        via = LVia(5.0, 0.0, 0.6, ("F.Cu", "B.Cu"))
        segs = [LSeg(0, 0, 5, 0, "F.Cu"), LSeg(5, 0, 10, 0, "In1.Cu")]
        got = net_length(segs, [via], [], stackup)
        assert got == pytest.approx(10.0 + 0.5325)

    def test_unconnected_via_falls_back_to_declared_span(self):
        stackup = KicadStackup.default(1.6, 4)
        via = LVia(5.0, 0.0, 0.6, ("F.Cu", "B.Cu"))
        got = net_length([LSeg(0, 0, 5, 0, "F.Cu")], [via], [], stackup)
        assert got == pytest.approx(5.0 + 1.58)


class TestPadViaTrim:
    def test_diagonal_entry_into_via(self):
        via = LVia(44.2, 25.8, 0.6, ("F.Cu", "B.Cu"))
        segs = [LSeg(40.0, 25.6, 44.0, 25.6, "F.Cu"), LSeg(44.0, 25.6, 44.2, 25.8, "F.Cu")]
        raw = sum(math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in segs)
        assert raw - net_length(segs, [via], [], None) == pytest.approx(0.00645, abs=1e-5)

    def test_straight_to_centre_has_no_trim(self):
        via = LVia(5.0, 0.0, 0.6, ("F.Cu", "B.Cu"))
        segs = [LSeg(0.0, 0.0, 5.0, 0.0, "F.Cu")]
        assert net_length(segs, [via], [], None) == pytest.approx(5.0)

    def test_bend_inside_rect_pad(self):
        pad = LPad(0.0, 0.0, 0.45, 0.45, layers=frozenset({"F.Cu"}))
        # Enters at x=-0.225, bends at (-0.1, 0) to the centre at (0, 0.1)
        # is not straight: path (-0.225,0.1)->(-0.1,0.1)->(0,0).
        segs = [LSeg(-2.0, 0.1, -0.1, 0.1, "F.Cu"), LSeg(-0.1, 0.1, 0.0, 0.0, "F.Cu")]
        inside = 0.125 + math.hypot(0.1, 0.1)
        straight = math.hypot(0.225, 0.1)
        raw = sum(math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in segs)
        assert raw - net_length(segs, [], [pad], None) == pytest.approx(inside - straight)

    def test_pad_on_other_layer_does_not_trim(self):
        pad = LPad(0.0, 0.0, 0.45, 0.45, layers=frozenset({"B.Cu"}))
        segs = [LSeg(-2.0, 0.1, -0.1, 0.1, "F.Cu"), LSeg(-0.1, 0.1, 0.0, 0.0, "F.Cu")]
        raw = sum(math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in segs)
        assert net_length(segs, [], [pad], None) == pytest.approx(raw)


class TestRoundrectRatioDefault:
    """A roundrect pad with no ``roundrect_rratio`` uses KiCad's 0.25 (Judge, PR #6729)."""

    @staticmethod
    def _length(rratio: float | None) -> float:
        pad = Pad(
            x=0.0,
            y=0.0,
            width=1.0,
            height=0.6,
            net=1,
            net_name="N",
            layer=Layer.F_CU,
            shape="roundrect",
            roundrect_rratio=rratio,
        )
        model = KicadLengthModel.for_router(KicadStackup.default(1.6, 2), [pad])
        # Enters the pad through a bend near its rounded corner so the
        # roundrect boundary test is actually exercised.
        route = Route(
            net=1,
            net_name="N",
            segments=[
                Segment(-3.0, 0.28, -0.45, 0.28, 0.2, Layer.F_CU, net=1),
                Segment(-0.45, 0.28, 0.0, 0.0, 0.2, Layer.F_CU, net=1),
            ],
        )
        return model.route_length(route)

    def test_none_ratio_does_not_crash_and_uses_kicad_default(self):
        assert self._length(None) == pytest.approx(self._length(0.25), abs=1e-12)

    def test_router_pad_maps_none_to_default(self):
        from kicad_tools.router.kicad_length import router_pad

        pad = Pad(x=0, y=0, width=1, height=0.6, net=1, net_name="N", shape="roundrect")
        assert pad.roundrect_rratio is None
        assert router_pad(pad).rratio == 0.25


def _route_form(pcb: PCB) -> tuple[Route, KicadLengthModel]:
    """The fixture net as the router holds it (Route + router Pad)."""
    seg = lambda a, b, c, d, layer: Segment(a, b, c, d, 0.2, layer, net=1, net_name="N1")  # noqa: E731
    route = Route(
        net=1,
        net_name="N1",
        segments=[
            seg(50, 50, 50.1, 50.0, Layer.F_CU),
            seg(50.1, 50.0, 50.45, 50.35, Layer.F_CU),
            seg(50.45, 50.35, 56, 50.35, Layer.F_CU),
            seg(56, 50.35, 56, 55.0, Layer.F_CU),
            seg(56, 55.0, 56, 55.8, Layer.IN1_CU),
            seg(56, 55.8, 57, 56.8, Layer.IN1_CU),
            seg(57, 56.8, 60, 56.8, Layer.IN1_CU),
            seg(60, 56.8, 60.2, 56.9, Layer.IN1_CU),
            seg(60.2, 56.9, 60.2, 59, Layer.IN2_CU),
        ],
        vias=[
            Via(56, 55.0, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), net=1),
            Via(60.2, 56.9, 0.4, 0.8, (Layer.F_CU, Layer.B_CU), net=1),
        ],
    )
    pad = Pad(
        x=50,
        y=50,
        width=0.45,
        height=0.45,
        net=1,
        net_name="N1",
        rotation=30.0,
        shape="rect",
    )
    return route, KicadLengthModel.for_router(KicadStackup.from_pcb(pcb), [pad])


@pytest.mark.parametrize("stackup", [False, True])
class TestRouteFormEqualsPcbForm:
    """The tuners (Route form) and ``kct check`` (PCB form) measure identically."""

    def test_route_and_pcb_lengths_equal(self, tmp_path, stackup):
        pcb = PCB.load(str(_write(tmp_path, stackup)))
        route, model = _route_form(pcb)
        pcb_len = DiffPairLengthTracker.measure_net_from_pcb(pcb, 1)
        assert model.route_length(route) == pytest.approx(pcb_len, abs=1e-9)
        assert DiffPairLengthTracker._measure_route(
            route, None, 4, length_model=model
        ) == pytest.approx(pcb_len, abs=1e-9)
        assert MatchGroupTracker._measure_route_total(
            route, None, 4, length_model=model
        ) == pytest.approx(pcb_len, abs=1e-9)
        # And it is not the naive sum: both the pad trim and via spans matter.
        naive = sum(math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in route.segments)
        assert pcb_len != pytest.approx(naive, abs=1e-3)

    def test_pcb_length_matches_hand_computed_value(self, tmp_path, stackup):
        pcb = PCB.load(str(_write(tmp_path, stackup)))
        # Reference value measured with kicad-cli 10.0.7 on this fixture.
        expected = 19.3898 if stackup else 19.3498
        assert DiffPairLengthTracker.measure_net_from_pcb(pcb, 1) == pytest.approx(
            expected, abs=1e-4
        )


@pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="kicad-cli not installed")
@pytest.mark.parametrize("stackup", [False, True])
def test_matches_kicad_cli_within_one_micron(tmp_path, stackup):
    path = _write(tmp_path, stackup)
    (tmp_path / "board.kicad_dru").write_text(
        "(version 1)\n"
        "(rule len (constraint length (max 0.001mm)) (condition \"A.NetName == 'N1'\"))\n"
    )
    out = tmp_path / "drc.json"
    proc = subprocess.run(
        [
            "kicad-cli",
            "pcb",
            "drc",
            "--format",
            "json",
            "--severity-all",
            "-o",
            str(out),
            str(path),
        ],
        capture_output=True,
        timeout=180,
        check=False,
    )
    if not out.exists():
        pytest.skip(f"kicad-cli produced no DRC report: {proc.stderr[:200]!r}")
    data = json.loads(out.read_text())
    import re

    actual = [
        float(m.group(1))
        for v in data.get("violations", [])
        if (m := re.search(r"actual ([0-9.]+) mm", v.get("description", "")))
    ]
    if not actual:
        pytest.skip("kicad-cli reported no length constraint violation")
    pcb = PCB.load(str(path))
    assert DiffPairLengthTracker.measure_net_from_pcb(pcb, 1) == pytest.approx(actual[0], abs=1e-3)
