"""Copper arcs in the net-aware pairwise clearance/short rules (Issue #9673).

Before #9673, ``ClearanceRule`` walked ``pcb.segments``, pads and vias only:
authored ``(arc ...)`` copper was invisible to the pairwise rules, so a
foreign-net segment crossing an applied arc's apex read clean while the same
geometry with straight segments reported the short.  These tests pin the
fix -- arcs are collected as one copper element per arc (the arc, never a
tessellation chord, is the reporting unit).

Issue #9702 tightened the *measurement*: the covering chord tessellation
(width inflated by twice ``ARC_COPPER_SAGITTA_MM``) treats arc copper as
1-2 um wider than it is, so gaps at or above the requirement were flagged
while pinned kicad-cli accepted them, and the reported ``actual``
understated the true gap by the same amount.  The verdict now measures the
authored arc exactly (a capsule of the authored width swept along the
finite centreline, round end caps included), compared with the probed
kicad-cli arc acceptance band
(:data:`kicad_tools.validate.rules.clearance.ARC_CLEARANCE_BAND_MM`).

Controls come from the issue's measured reproduction (board 02 geometry):
GND arc on ``B.Cu`` ``start (128,135) mid (130,137) end (132,135)``, width
0.3 -- a circle of radius 2 mm centred (130,135), apex at (130,137) -- with
a VCC segment crossing it.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from pathlib import Path

import pytest

from kicad_tools.cli import main
from kicad_tools.cli.pcb_move_via import run_move_via
from kicad_tools.manufacturers import get_profile
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate import DRCChecker

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_HEADER = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "VCC")
"""

#: The issue's reproduction arc: upper half of the circle centred (130,135),
#: radius 2 mm, apex (130,137).  Sheet/board coordinates coincide (no
#: Edge.Cuts outline in the bare fixture -> board origin (0,0)).
_ARC = (
    "  (arc (start 128 135) (mid 130 137) (end 132 135) (width 0.3) "
    '(layer "B.Cu") (uuid "arc-gnd") (net 1 "GND"))\n'
)

_ARC_REF = "Arc@B.Cu:w0.3:128/135~130/137~132/135"


def _write_board(tmp_path: Path, body: str, name: str = "b.kicad_pcb") -> Path:
    path = tmp_path / name
    path.write_text(_HEADER + body + "\n)\n")
    return path


def _load(path: Path) -> PCB:
    return PCB.load(str(path))


def _clearance_findings(pcb: PCB):
    checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2, copper_oz=1.0)
    return checker.check_clearances().violations


def _arc_short_board(tmp_path: Path, segment: str) -> Path:
    return _write_board(tmp_path, _ARC + segment)


def _vcc_segment(x1: float, y1: float, x2: float, y2: float, layer: str = "B.Cu") -> str:
    return (
        f"  (segment (start {x1} {y1}) (end {x2} {y2}) (width 0.3) "
        f'(layer "{layer}") (net 2 "VCC") (uuid "seg-vcc"))\n'
    )


# ---------------------------------------------------------------------------
# AC1: foreign-net segment crossing the swept copper reports a pairwise short
# ---------------------------------------------------------------------------


class TestArcSegmentShort:
    def test_segment_through_apex_names_both_nets_and_layer(self, tmp_path: Path):
        board = _arc_short_board(tmp_path, _vcc_segment(130, 134, 130, 140))
        pcb = _load(board)
        findings = _clearance_findings(pcb)
        assert len(findings) == 1
        v = findings[0]
        assert v.rule_id == "clearance_arc_segment"
        assert v.severity == "error"
        assert v.layer == "B.Cu"
        assert set(v.nets) == {"GND", "VCC"}
        assert v.message.startswith("SHORT:")
        assert "'GND'" in v.message and "'VCC'" in v.message
        assert v.actual_value < 0
        # Item identity: the authored arc descriptor and the counterpart
        # segment -- an unrelated physical-gap finding cannot satisfy this.
        assert set(v.items) == {_ARC_REF, "Trace@B.Cu:w0.3:130/134~130/140"}

    def test_apex_hit_short_segment(self, tmp_path: Path):
        # 0.4 mm VCC segment ending 0.2 mm either side of the apex: the
        # crossing is real copper-to-copper overlap.
        board = _arc_short_board(tmp_path, _vcc_segment(130, 136.8, 130, 137.2))
        findings = _clearance_findings(_load(board))
        assert [v.rule_id for v in findings] == ["clearance_arc_segment"]
        assert findings[0].actual_value < 0

    def test_chord_only_miss_passes(self, tmp_path: Path):
        # Same geometry moved down to y=135: the segment crosses the
        # imaginary straight line between the arc's endpoints but no swept
        # copper -- the checker must see the curve, not its chord.
        board = _arc_short_board(tmp_path, _vcc_segment(130, 134.8, 130, 135.2))
        assert _clearance_findings(_load(board)) == []

    def test_gap_below_floor_reports_clearance_not_short(self, tmp_path: Path):
        # Horizontal VCC segment 0.35 mm above the apex: edge-to-edge gap
        # ~0.049 mm -- below the 0.127 mm JLCPCB floor but positive, so a
        # clearance finding, not a SHORT promotion.
        board = _arc_short_board(tmp_path, _vcc_segment(128, 137.35, 132, 137.35))
        findings = _clearance_findings(_load(board))
        assert [v.rule_id for v in findings] == ["clearance_arc_segment"]
        v = findings[0]
        assert 0 < v.actual_value < v.required_value
        assert not v.message.startswith("SHORT")

    def test_straight_segment_control_still_reports(self, tmp_path: Path):
        # The control the issue measured: the same overlap geometry with a
        # straight GND segment instead of the arc keeps reporting the
        # segment-vs-segment short (the fix added arc vision; it did not
        # move the segment baseline).
        body = (
            "  (segment (start 128 137) (end 132 137) (width 0.3) "
            '(layer "B.Cu") (net 1 "GND") (uuid "seg-gnd"))\n' + _vcc_segment(130, 134, 130, 140)
        )
        findings = _clearance_findings(_load(_write_board(tmp_path, body)))
        assert [v.rule_id for v in findings] == ["clearance_segment_segment"]
        assert findings[0].message.startswith("SHORT:")
        assert "'GND'" in findings[0].message and "'VCC'" in findings[0].message


# ---------------------------------------------------------------------------
# AC2: arc interactions with every other collected copper object
# ---------------------------------------------------------------------------


class TestArcPairKinds:
    def test_arc_vs_pad(self, tmp_path: Path):
        pad = (
            '  (footprint "test:pad" (layer "B.Cu") (at 130 137)\n'
            '    (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "B.Cu") '
            '(net 2 "VCC")))\n'
        )
        findings = _clearance_findings(_load(_write_board(tmp_path, _ARC + pad)))
        assert [v.rule_id for v in findings] == ["clearance_arc_pad"]
        v = findings[0]
        assert set(v.nets) == {"GND", "VCC"}
        assert v.layer == "B.Cu"
        assert _ARC_REF in v.items

    def test_arc_vs_via(self, tmp_path: Path):
        # Foreign via centred between apex and chord: positive gap ~0.049 mm,
        # below the 0.127 mm floor.
        via = (
            '  (via (at 130 136.5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") '
            '(net 2 "VCC") (uuid "via-vcc"))\n'
        )
        findings = _clearance_findings(_load(_write_board(tmp_path, _ARC + via)))
        assert [v.rule_id for v in findings] == ["clearance_arc_via"]
        v = findings[0]
        assert 0 < v.actual_value < v.required_value
        assert set(v.nets) == {"GND", "VCC"}
        assert "Via@130/136.5:F.Cu-B.Cu:d0.3/s0.6" in v.items

    def test_arc_vs_arc(self, tmp_path: Path):
        arc2 = (
            "  (arc (start 128 139) (mid 130 137) (end 132 139) (width 0.3) "
            '(layer "B.Cu") (uuid "arc-vcc") (net 2 "VCC"))\n'
        )
        findings = _clearance_findings(_load(_write_board(tmp_path, _ARC + arc2)))
        assert [v.rule_id for v in findings] == ["clearance_arc_arc"]
        v = findings[0]
        assert v.actual_value < 0
        assert set(v.items) == {_ARC_REF, "Arc@B.Cu:w0.3:128/139~130/137~132/139"}

    def test_same_net_segment_through_apex_passes(self, tmp_path: Path):
        segment = _vcc_segment(130, 134, 130, 140).replace('(net 2 "VCC")', "(net 1)")
        assert _clearance_findings(_load(_arc_short_board(tmp_path, segment))) == []

    def test_different_layer_segment_passes(self, tmp_path: Path):
        board = _arc_short_board(tmp_path, _vcc_segment(130, 134, 130, 140, layer="F.Cu"))
        assert _clearance_findings(_load(board)) == []

    def test_comfortably_clear_segment_passes(self, tmp_path: Path):
        board = _arc_short_board(tmp_path, _vcc_segment(133, 134, 133, 140))
        assert _clearance_findings(_load(board)) == []


# ---------------------------------------------------------------------------
# Via colocation exemption: authored arc endpoints only, never chord joints
# ---------------------------------------------------------------------------


class TestViaEndpointExemption:
    def test_via_at_authored_arc_endpoint_exempt(self, tmp_path: Path):
        # Issue #2706 semantics: copper ENDING exactly at a foreign via
        # centre is the escape-join modeling artifact, not a short.
        via = (
            '  (via (at 128 135) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") '
            '(net 2 "VCC") (uuid "via-vcc"))\n'
        )
        assert _clearance_findings(_load(_write_board(tmp_path, _ARC + via))) == []

    def test_via_at_internal_arc_point_not_exempt(self, tmp_path: Path):
        # The apex is an internal tessellation joint, not a trace endpoint:
        # the via overlaps real mid-arc copper and must report the short.
        via = (
            '  (via (at 130 137) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") '
            '(net 2 "VCC") (uuid "via-vcc"))\n'
        )
        findings = _clearance_findings(_load(_write_board(tmp_path, _ARC + via)))
        assert [v.rule_id for v in findings] == ["clearance_arc_via"]
        assert findings[0].actual_value < 0


# ---------------------------------------------------------------------------
# AC1 tail: stable identification of the authored arc after reload, with a
# nonzero board origin (descriptor keyed in sheet coordinates)
# ---------------------------------------------------------------------------


class TestReferenceStability:
    def test_arc_reference_stable_after_reload_with_nonzero_origin(self, tmp_path: Path):
        outline = (
            "  (gr_rect (start 100 100) (end 160 160) (stroke (width 0.1) "
            '(type default)) (fill none) (layer "Edge.Cuts"))\n'
        )
        path = _write_board(tmp_path, outline + _ARC + _vcc_segment(130, 134, 130, 140))
        pcb = _load(path)
        assert pcb.board_origin == (100.0, 100.0)  # sheet coords in the fixture

        first = _clearance_findings(pcb)
        assert [v.rule_id for v in first] == ["clearance_arc_segment"]
        # The descriptor is sheet-frame: the literal file coordinates.
        assert _ARC_REF in first[0].items

        saved = tmp_path / "reloaded.kicad_pcb"
        pcb.save(saved)
        again = _clearance_findings(_load(saved))
        assert [v.rule_id for v in again] == ["clearance_arc_segment"]
        assert again[0].items == first[0].items
        assert again[0].nets == first[0].nets
        assert again[0].layer == first[0].layer
        assert again[0].key == first[0].key
        # The reported location is sheet-absolute in both runs.
        assert again[0].location == first[0].location


# ---------------------------------------------------------------------------
# AC3: separate-batch probe/apply CLI regression (PR #9617 commands)
# ---------------------------------------------------------------------------

_CLI_BOARD = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "VCC")
  (gr_rect (start 100 100) (end 160 160) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
)
"""

_ARC_ITEM = {
    "type": "arc",
    "net": "GND",
    "layer": "B.Cu",
    "width": 0.3,
    "start": [128, 135],
    "mid": [130, 137],
    "end": [132, 135],
}
_CROSSING_ITEM = {
    "type": "segment",
    "net": "VCC",
    "layer": "B.Cu",
    "width": 0.3,
    "start": [130, 134],
    "end": [130, 140],
}


def _items_file(tmp_path: Path, *items: dict) -> Path:
    path = tmp_path / "items.json"
    path.write_text(json.dumps({"items": list(items)}))
    return path


def _run(capsys, *argv: str) -> tuple[int, dict]:
    rc = main(list(argv))
    out = capsys.readouterr().out
    return rc, json.loads(out)


class TestSeparateBatchProbeApply:
    def test_apply_arc_then_probe_crossing_segment_refuses(self, tmp_path: Path, capsys):
        board = tmp_path / "cli.kicad_pcb"
        board.write_text(_CLI_BOARD)

        # Batch 1: the clean arc lands on the board as a real node.
        rc, doc = _run(
            capsys,
            "apply",
            str(board),
            str(_items_file(tmp_path, _ARC_ITEM)),
            "--checked",
            "--format",
            "json",
        )
        assert rc == 0 and doc["ok"] is True
        reloaded = _load(board)
        assert len(reloaded.arcs) == 1

        # Batch 2 (separate): the crossing VCC segment must be attributed to
        # the proposed item and fail the probe.
        before = hashlib.md5(board.read_bytes()).hexdigest()
        rc, doc = _run(
            capsys,
            "probe",
            str(board),
            str(_items_file(tmp_path, _CROSSING_ITEM)),
            "--format",
            "json",
        )
        assert rc == 1 and doc["ok"] is False
        shorts = [p for p in doc["problems"] if p["rule_id"] == "clearance_arc_segment"]
        assert len(shorts) == 1
        # Both members of the pair are proposed (arc item 1, track item 2).
        assert shorts[0]["proposed_item"] in (1, 2)
        assert set(shorts[0]["nets"]) == {"GND", "VCC"}

        # Checked apply refuses the batch and preserves the board bytes.
        rc, doc = _run(
            capsys,
            "apply",
            str(board),
            str(_items_file(tmp_path, _CROSSING_ITEM)),
            "--checked",
            "--format",
            "json",
        )
        assert rc == 1 and doc["written"] is None
        assert hashlib.md5(board.read_bytes()).hexdigest() == before

    def test_scratch_board_with_both_items_reports_same_short(self, tmp_path: Path):
        board = tmp_path / "cli.kicad_pcb"
        board.write_text(_CLI_BOARD)
        pcb = _load(board)
        pcb.add_arc((28.0, 35.0), (30.0, 37.0), (32.0, 35.0), width=0.3, layer="B.Cu", net="GND")
        pcb.add_trace((30.0, 34.0), (30.0, 40.0), width=0.3, layer="B.Cu", net="VCC")
        findings = _clearance_findings(pcb)
        assert [v.rule_id for v in findings] == ["clearance_arc_segment"]
        assert set(findings[0].items) == {_ARC_REF, "Trace@B.Cu:w0.3:130/134~130/140"}
        assert set(findings[0].nets) == {"GND", "VCC"}


# ---------------------------------------------------------------------------
# Regression (PR #9678 review): every other consumer of the shared collector
# and the public element helpers must answer an arc element, not crash on
# the pad/via 4-tuple geometry unpack.
# ---------------------------------------------------------------------------

_HEADER4 = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "VCC")
"""

_PTH_PAD = (
    '  (footprint "test:pth" (layer "F.Cu") (at 128 134.5)\n'
    '    (pad "1" thru_hole circle (at 0 0) (size 0.8 0.8) (drill 0.4) '
    '(layers "*.Cu" "*.Mask") (net 1 "GND")))\n'
)


class TestFactoryPthHoleArc:
    """``check_pth_hole_clearance`` sees an inner-layer arc (Blocking 1).

    Before the fix the inner-layer arm handed the arc to the pad/via
    shapely builder and died on ``ValueError: too many values to unpack``.
    """

    def test_inner_arc_through_pth_hole_reports(self, tmp_path: Path):
        from kicad_tools.validate.rules.factory_clearance import check_pth_hole_clearance

        # The fixture arc is GND; flip it to VCC so it is foreign to the
        # GND hole (same geometry, foreign net).
        arc = _ARC.replace('(layer "B.Cu")', '(layer "In1.Cu")').replace(
            '(net 1 "GND")', '(net 2 "VCC")'
        )
        # The hole sits 0.5 mm below the arc's authored endpoint (128,135):
        # drill edge to swept copper = 0.5 - 0.2 - 0.151 = 0.149 mm, short
        # of the 0.30 mm JLCPCB inner minimum.
        path = tmp_path / "inner.kicad_pcb"
        path.write_text(_HEADER4 + _PTH_PAD + arc + "\n)\n")
        rules = get_profile("jlcpcb").get_design_rules(layers=4)
        violations = check_pth_hole_clearance(PCB.load(path), rules).violations
        assert [v.rule_id for v in violations] == ["pth_hole_clearance"]
        v = violations[0]
        assert v.layer == "In1.Cu"
        assert v.actual_value == pytest.approx(0.149, abs=0.005)
        assert v.required_value == pytest.approx(0.3)
        assert "Arc@In1.Cu:w0.3:128/135~130/137~132/135" in v.items

    def test_distant_inner_arc_stays_clean(self, tmp_path: Path):
        from kicad_tools.validate.rules.factory_clearance import check_pth_hole_clearance

        arc = (
            "  (arc (start 128 145) (mid 130 147) (end 132 145) (width 0.3) "
            '(layer "In1.Cu") (uuid "arc-far") (net 2 "VCC"))\n'
        )
        path = tmp_path / "far.kicad_pcb"
        path.write_text(_HEADER4 + _PTH_PAD + arc + "\n)\n")
        rules = get_profile("jlcpcb").get_design_rules(layers=4)
        assert check_pth_hole_clearance(PCB.load(path), rules).violations == []


class TestMoveViaCheckArc:
    """``kct pcb move-via --check`` measures arc copper (Blocking 2).

    Before the fix the collector loop unpacked the arc's 5-tuple geometry
    (``ValueError``) and, past that, ``kinds['arc']`` raised ``KeyError``.
    """

    def _board(self, tmp_path: Path) -> Path:
        via = (
            '  (via (at 130 141) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") '
            '(net 2 "VCC") (uuid "via-mv"))\n'
        )
        outline = (
            "  (gr_rect (start 0 0) (end 200 200) (stroke (width 0.1) "
            '(type default)) (fill none) (layer "Edge.Cuts"))\n'
        )
        f = _write_board(tmp_path, outline + _ARC + via, name="mv.kicad_pcb")
        f.with_suffix(".kicad_pro").write_text(
            json.dumps({"board": {"design_settings": {"rules": {"min_hole_clearance": 0.5}}}})
        )
        return f

    def test_move_onto_arc_refused(self, tmp_path: Path, capsys):
        f = self._board(tmp_path)
        before = f.read_bytes()
        # Destination (130, 136.5): the drill edge sits ~0.2 mm from the
        # apex copper, short of the 0.5 mm project hole_clearance.
        assert (
            run_move_via(f, uuid="via-mv", to=(130, 136.5), check=True, output_format="json") == 1
        )
        out = json.loads(capsys.readouterr().out)
        ids = {d["rule_id"] for d in out["check"]["details"]}
        assert "hole_clearance" in ids
        assert f.read_bytes() == before

    def test_move_clear_of_arc_allowed(self, tmp_path: Path):
        f = self._board(tmp_path)
        assert run_move_via(f, uuid="via-mv", to=(130, 141.5), check=True) == 0


class TestArcElementHelpers:
    """The public CopperElement helpers answer arcs (Blocking 3)."""

    def test_kernel_shape_and_shapely_geom_answer_arc(self, tmp_path: Path):
        from shapely.geometry import Point, Polygon  # type: ignore[import-untyped]

        from kicad_tools.validate.clearance_shapes import KZonePoly
        from kicad_tools.validate.rules.clearance import (
            ClearanceRule,
            _element_to_shapely_geom,
        )

        pcb = _load(_write_board(tmp_path, _ARC))
        (elem,) = (
            e for e in ClearanceRule()._collect_elements(pcb, "B.Cu") if e.element_type == "arc"
        )
        shape = elem.kernel_shape()  # raised ValueError before the fix
        assert isinstance(shape, KZonePoly)
        poly = Polygon(shape.rings[0])
        assert poly.contains(Point(130, 137))  # the apex centreline
        assert not poly.contains(Point(130, 139))  # 2 mm above the apex
        geom = _element_to_shapely_geom(elem)  # raised ValueError before the fix
        assert geom is not None and geom.contains(Point(130, 137))


# ---------------------------------------------------------------------------
# Regression (PR #9678 second review): the probe's check board must measure a
# proposed arc with the SAME exemptions as the authored node.  Arcs were
# appended as chords only, so an internal tessellation chord joint borrowed
# the via-endpoint colocation exemption (a via plated onto mid-arc copper
# read clean) and the chords were eligible for the straight-segment
# differential-pair skip (a short across a coupled pair read clean) --
# both written by ``kct apply --checked`` and immediately reported by
# ``kct check`` on the saved board.
# ---------------------------------------------------------------------------

_SHALLOW_ARC_ITEM = {
    "type": "arc",
    "net": "GND",
    "layer": "B.Cu",
    "width": 0.3,
    "start": [110, 112],
    "mid": [114, 112.09],
    "end": [118, 112],
}


def _via_item(x: float, y: float) -> dict:
    return {
        "type": "via",
        "net": "VCC",
        "at": [x, y],
        "size": 0.6,
        "drill": 0.3,
        "layers": ["F.Cu", "B.Cu"],
    }


_USB_BOARD = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "USB_D+")
  (net 2 "USB_D-")
  (gr_rect (start 100 100) (end 160 160) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
  (segment (start 110 110) (end 120 110) (width 0.3) (layer "B.Cu") (net 1 "USB_D+"))
  (segment (start 110 110.5) (end 120 110.5) (width 0.3) (layer "B.Cu") (net 2 "USB_D-"))
  (segment (start 130 134) (end 130 140) (width 0.3) (layer "B.Cu") (net 2 "USB_D-"))
)
"""

_USB_ARC_ITEM = {
    "type": "arc",
    "net": "USB_D+",
    "layer": "B.Cu",
    "width": 0.3,
    "start": [128, 135],
    "mid": [130, 137],
    "end": [132, 135],
}


def _internal_chord_joint() -> tuple[float, float]:
    """A strictly-interior tessellation joint of ``_SHALLOW_ARC_ITEM``.

    ``Arc.centerline_points`` places vertices at uniform angular steps, so a
    joint must be computed, not assumed (only start/end are guaranteed).
    The sweep-middle joint of this symmetric arc is the false-accept site:
    both chords meeting there END at it, so the chord view lent it the
    via-endpoint exemption while the authored arc has no endpoint there.
    """
    from kicad_tools.schema.pcb import Arc as SchemaArc

    points = SchemaArc(
        start=(110.0, 112.0),
        mid=(114.0, 112.09),
        end=(118.0, 112.0),
        width=0.3,
        layer="B.Cu",
        net_number=0,
    ).centerline_points(max_error_mm=0.001)
    joint = points[len(points) // 2]
    assert joint != points[0] and joint != points[-1]
    return joint


class TestCheckedApplyArcExemptions:
    def test_via_at_internal_chord_joint_refuses_and_preserves_bytes(self, tmp_path, capsys):
        # False accept 1: the via lands on an internal chord joint (mid-arc
        # copper), not an authored endpoint -- the saved board's own check
        # reports clearance_arc_via, so the probe must refuse the write.
        joint = _internal_chord_joint()
        board = tmp_path / "joint.kicad_pcb"
        board.write_text(_CLI_BOARD)
        before = hashlib.md5(board.read_bytes()).hexdigest()

        rc, doc = _run(
            capsys,
            "probe",
            str(board),
            str(_items_file(tmp_path, _SHALLOW_ARC_ITEM, _via_item(*joint))),
            "--format",
            "json",
        )
        assert rc == 1 and doc["ok"] is False
        shorts = [p for p in doc["problems"] if p["rule_id"] == "clearance_arc_via"]
        assert len(shorts) == 1
        # Both members of the pair are proposed (arc item 1, track item 2).
        assert shorts[0]["proposed_item"] in (1, 2)
        assert set(shorts[0]["nets"]) == {"GND", "VCC"}

        rc, doc = _run(
            capsys,
            "apply",
            str(board),
            str(_items_file(tmp_path, _SHALLOW_ARC_ITEM, _via_item(*joint))),
            "--checked",
            "--format",
            "json",
        )
        assert rc == 1 and doc["written"] is None
        assert hashlib.md5(board.read_bytes()).hexdigest() == before

    def test_arc_shorting_coupled_diffpair_segment_refuses(self, tmp_path, capsys):
        # False accept 2: USB_D+/USB_D- are a name-inferred pair corroborated
        # by the parallel coupling tracks, so segment-segment edges between
        # them are delegated to the intra-pair rule -- but an ARC of the pair
        # crossing the other net's segment is a real short the pairwise rule
        # must report (the diff-pair skip is segment-segment only).
        board = tmp_path / "usb.kicad_pcb"
        board.write_text(_USB_BOARD)
        before = hashlib.md5(board.read_bytes()).hexdigest()

        rc, doc = _run(
            capsys,
            "probe",
            str(board),
            str(_items_file(tmp_path, _USB_ARC_ITEM)),
            "--format",
            "json",
        )
        assert rc == 1 and doc["ok"] is False
        shorts = [p for p in doc["problems"] if p["rule_id"] == "clearance_arc_segment"]
        assert len(shorts) == 1
        # Both members of the pair are proposed (arc item 1, track item 2).
        assert shorts[0]["proposed_item"] in (1, 2)
        assert set(shorts[0]["nets"]) == {"USB_D+", "USB_D-"}

        rc, doc = _run(
            capsys,
            "apply",
            str(board),
            str(_items_file(tmp_path, _USB_ARC_ITEM)),
            "--checked",
            "--format",
            "json",
        )
        assert rc == 1 and doc["written"] is None
        assert hashlib.md5(board.read_bytes()).hexdigest() == before

    def test_via_at_arc_authored_endpoint_is_accepted(self, tmp_path, capsys):
        # Endpoint control: copper ENDING at a foreign via centre is the
        # escape-join artifact (Issue #2706), so the arc+via batch is clean
        # and the checked apply writes it -- matching the authored-arc
        # behaviour the chord view got wrong in the other direction (four
        # false clearance_segment_via findings at the semicircle's start).
        board = tmp_path / "endpoint.kicad_pcb"
        board.write_text(_CLI_BOARD)

        rc, doc = _run(
            capsys,
            "probe",
            str(board),
            str(_items_file(tmp_path, _ARC_ITEM, _via_item(128, 135))),
            "--format",
            "json",
        )
        assert rc == 0 and doc["ok"] is True

        rc, doc = _run(
            capsys,
            "apply",
            str(board),
            str(_items_file(tmp_path, _ARC_ITEM, _via_item(128, 135))),
            "--checked",
            "--format",
            "json",
        )
        assert rc == 0 and doc["written"] is not None
        reloaded = _load(board)
        assert len(reloaded.arcs) == 1 and len(reloaded.vias) == 1


# ---------------------------------------------------------------------------
# Issue #9702: the verdict measures the TRUE arc, and the acceptance band
# matches pinned kicad-cli.  Reproduction frame: F.Cu semicircle
# (40,40) -> (50,50) -> (60,40), width 0.2, net N1 -- centre (50,40),
# radius 10, apex (50,50) -- with a radial 0.2 mm N2 track; the rule's
# minimum is 0.2 mm.
# ---------------------------------------------------------------------------

_REQ = 0.2
_ARCW = 0.2
_ARC_C = (50.0, 40.0)
_ARC_R = 10.0

_HEADER2L = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "N1")
  (net 2 "N2")
"""

_BIG_ARC = (
    f"  (arc (start 40 40) (mid 50 50) (end 60 40) (width {_ARCW}) "
    '(layer "F.Cu") (uuid "arc-n1") (net 1))\n'
)


def _check_at(tmp_path: Path, extra: str, minimum: float = _REQ):
    """``ClearanceRule._check_layer`` findings for the semicircle board."""
    from kicad_tools.validate.rules.clearance import ClearanceRule

    p = tmp_path / "b.kicad_pcb"
    p.write_text(_HEADER2L + _BIG_ARC + extra + "\n)\n")
    return ClearanceRule()._check_layer(PCB.load(str(p)), "F.Cu", minimum)


def _radial_track(orient: str, shortfall: float) -> tuple[str, float]:
    """A radial N2 track at ``shortfall`` mm below the 0.2 mm requirement.

    Returns ``(segment sexp, expected true copper gap)`` computed from the
    SERIALIZED coordinates (the issue's test plan: expected gaps come from
    the board coordinates, particularly for the rotated case).
    """

    def f(v: float) -> str:
        return f"{v:.6f}"

    gap = _REQ - shortfall
    if orient == "convex_apex":
        ey = 50.4 - shortfall
        p1, p2 = (50.0, ey), (50.0, ey + 2.0)
        expected = round(ey, 6) - 50.2
    elif orient == "concave_apex":
        ey = 49.6 + shortfall
        p1, p2 = (50.0, ey), (50.0, ey - 2.0)
        expected = 49.8 - round(ey, 6)
    else:
        ang = math.radians(60.0)
        ux, uy = math.cos(ang), math.sin(ang)
        re = (_ARC_R + _ARCW + gap) if orient == "convex_30" else (_ARC_R - _ARCW - gap)
        ex, ey = round(_ARC_C[0] + ux * re, 6), round(_ARC_C[1] + uy * re, 6)
        sign = 1.0 if orient == "convex_30" else -1.0
        p1, p2 = (ex, ey), (ex + sign * ux * 2.0, ey + sign * uy * 2.0)
        r_e = math.hypot(ex - _ARC_C[0], ey - _ARC_C[1])
        expected = (
            (r_e - _ARCW / 2) - (_ARC_R + _ARCW / 2)
            if orient == "convex_30"
            else (_ARC_R - _ARCW / 2) - (r_e + _ARCW / 2)
        )
    seg = (
        f"  (segment (start {f(p1[0])} {f(p1[1])}) (end {f(p2[0])} {f(p2[1])}) "
        f'(width {_ARCW}) (layer "F.Cu") (net 2))\n'
    )
    return seg, expected


class TestExactArcGap:
    """AC1: at/above the minimum every arc pair kind passes; failing
    fixtures report the true edge-to-edge gap; a real overlap stays a
    SHORT and one finding per authored pair."""

    @pytest.mark.parametrize("orient", ["convex_apex", "concave_apex", "convex_30", "concave_30"])
    def test_at_minimum_passes(self, tmp_path, orient):
        seg, _ = _radial_track(orient, 0.0)
        assert _check_at(tmp_path, seg) == []

    @pytest.mark.parametrize("orient", ["convex_apex", "concave_apex", "convex_30", "concave_30"])
    def test_above_minimum_passes(self, tmp_path, orient):
        seg, _ = _radial_track(orient, -0.001)  # 1 um clear of the minimum
        assert _check_at(tmp_path, seg) == []

    @pytest.mark.parametrize("shortfall", [0.0006, 0.001, 0.010])
    @pytest.mark.parametrize("orient", ["convex_apex", "concave_apex", "convex_30", "concave_30"])
    def test_below_minimum_reports_true_gap(self, tmp_path, orient, shortfall):
        seg, expected = _radial_track(orient, shortfall)
        findings = _check_at(tmp_path, seg)
        assert [v.rule_id for v in findings] == ["clearance_arc_segment"]
        assert findings[0].actual_value == pytest.approx(expected, abs=1e-6)
        assert findings[0].actual_value < _REQ

    def test_arc_via_at_minimum_passes_and_reports_true_gap(self, tmp_path):
        # Via copper radius 0.3 (size 0.6); arc outer copper edge 50.1:
        # centre 50.6 is exactly the 0.2 mm requirement, 50.55 is 50 um short.
        at_min = '  (via (at 50 50.6) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2))\n'
        assert _check_at(tmp_path, at_min) == []
        short = '  (via (at 50 50.55) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2))\n'
        findings = _check_at(tmp_path, short)
        assert [v.rule_id for v in findings] == ["clearance_arc_via"]
        assert findings[0].actual_value == pytest.approx(0.15, abs=1e-6)

    def test_arc_pad_at_minimum_passes_and_reports_true_gap(self, tmp_path):
        def pad(y: float) -> str:
            return (
                f'  (footprint "t:p" (layer "F.Cu") (at 50 {y})\n'
                '    (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") (net 2)))\n'
            )

        # The circle curves toward the pad's outer corners, so the closest
        # copper sits over a corner, not the face midpoint: the at-minimum
        # centre solves |corner - (50,40)| = 10 - 0.3 (gap exactly 0.2).
        assert _check_at(tmp_path, pad(49.395360)) == []
        findings = _check_at(tmp_path, pad(49.45))
        assert [v.rule_id for v in findings] == ["clearance_arc_pad"]
        # 10 - hypot(0.3, 9.75) - 0.1, from the serialized pad centre.
        assert findings[0].actual_value == pytest.approx(0.145386, abs=5e-5)  # 0.1454 at 4 dp

    def test_arc_roundrect_pad_measures_true_outline(self, tmp_path):
        # A roundrect pad is core-rect + corner radius (0.25 x 0.6 => 0.15).
        # Facing the concave apex the closest copper is the corner arc the
        # circle curves toward, not the face midpoint -- the at-minimum
        # centre (bisected against the shapely outline oracle) lands 1.6 um
        # above the rect pad's, and the checker must place it exactly.
        pad = (
            '  (footprint "t:p" (layer "F.Cu") (at 50 49.398845)\n'
            '    (pad "1" smd roundrect (at 0 0) (size 0.6 0.6) (roundrect_rratio 0.25) '
            '(layers "F.Cu") (net 2)))\n'
        )
        assert _check_at(tmp_path, pad) == []
        at_corner = (
            '  (footprint "t:p" (layer "F.Cu") (at 42.460740 47.539260)\n'
            '    (pad "1" smd roundrect (at 0 0) (size 0.6 0.6) (roundrect_rratio 0.25) '
            '(layers "F.Cu") (net 2)))\n'
        )
        assert _check_at(tmp_path, at_corner) == []  # exactly 0.2 on the diagonal
        corner = (
            '  (footprint "t:p" (layer "F.Cu") (at 42.496096 47.503904)\n'
            '    (pad "1" smd roundrect (at 0 0) (size 0.6 0.6) (roundrect_rratio 0.25) '
            '(layers "F.Cu") (net 2)))\n'
        )
        findings = _check_at(tmp_path, corner)
        assert [v.rule_id for v in findings] == ["clearance_arc_pad"]
        # Expected from the serialized centre by the independent sampler:
        # the corner arc the circle curves toward is the closest copper.
        expected = _sampled_pad_gap(tmp_path, corner)
        assert findings[0].actual_value == pytest.approx(expected, abs=5e-4)

    def test_arc_arc_at_minimum_passes_and_reports_true_gap(self, tmp_path):
        # Facing semicircles, apex to apex (interior-interior): the second
        # arc's copper bottom edge at y2-0.1 vs the first arc's top 50.1.
        def mirror(y2: float) -> str:
            return (
                f"  (arc (start 40 {y2 + 10}) (mid 50 {y2}) (end 60 {y2 + 10}) "
                f'(width {_ARCW}) (layer "F.Cu") (net 2))\n'
            )

        assert _check_at(tmp_path, mirror(50.4)) == []  # exactly 0.2
        findings = _check_at(tmp_path, mirror(50.3))
        assert [v.rule_id for v in findings] == ["clearance_arc_arc"]
        assert findings[0].actual_value == pytest.approx(0.1, abs=1e-6)

    def test_real_overlap_stays_short_single_finding(self, tmp_path):
        seg, _ = _radial_track("convex_apex", 0.5)  # track pierces the arc
        findings = _check_at(tmp_path, seg)
        assert [v.rule_id for v in findings] == ["clearance_arc_segment"]
        v = findings[0]
        assert v.actual_value < 0
        assert v.message.startswith("SHORT:")
        assert set(v.nets) == {"N1", "N2"}
        assert "Arc@F.Cu:w0.2:40/40~50/50~60/40" in v.items


def _sampled_pad_gap(tmp_path: Path, pad_sexp: str) -> float:
    """Independent numeric oracle: dense on-arc samples vs the pad polygon."""
    from shapely.geometry import Point  # type: ignore[import-untyped]

    from kicad_tools.validate.rules.clearance import ClearanceRule, _pad_polygon

    p = tmp_path / "b.kicad_pcb"
    p.write_text(_HEADER2L + _BIG_ARC + pad_sexp + "\n)\n")
    pcb = PCB.load(str(p))
    (arc_elem,) = (
        e for e in ClearanceRule()._collect_elements(pcb, "F.Cu") if e.element_type == "arc"
    )
    (pad_elem,) = (
        e for e in ClearanceRule()._collect_elements(pcb, "F.Cu") if e.element_type == "pad"
    )
    polygon = _pad_polygon(pad_elem.source_pad, pad_elem.source_footprint)
    points = arc_elem.source_arc.centerline_points(max_error_mm=1e-6)
    best = min(polygon.exterior.distance(Point(q)) for q in points)
    return best - _ARCW / 2


class TestArcClosestPointFamilies:
    """Interior, arc-endpoint, track-endpoint and intersection minima, a
    noncircular pad and a second finite arc (issue test plan)."""

    def test_interior_closest_point_both_sides(self, tmp_path):
        # Apex (radial foot) and 30 deg off apex: interior-interior minima.
        for orient in ("convex_apex", "concave_apex", "convex_30", "concave_30"):
            seg, expected = _radial_track(orient, 0.0008)
            findings = _check_at(tmp_path, seg)
            assert [v.rule_id for v in findings] == ["clearance_arc_segment"], orient
            assert findings[0].actual_value == pytest.approx(expected, abs=1e-6), orient

    def test_arc_endpoint_closest(self, tmp_path):
        # A horizontal track beyond the arc's START endpoint (40,40): the
        # closest skeleton point is the authored endpoint, not any interior
        # radial foot.
        def track(x: float) -> str:
            return (
                f'  (segment (start {x} 39.9) (end {x} 40.1) (width 0.2) (layer "F.Cu") (net 2))\n'
            )

        assert _check_at(tmp_path, track(39.6)) == []  # gap exactly 0.2
        findings = _check_at(tmp_path, track(39.65))
        assert [v.rule_id for v in findings] == ["clearance_arc_segment"]
        # Endpoint (40,40) is the arc's leftmost skeleton point; the track
        # spans y 39.9..40.1, so the skeleton distance is 0.35 horizontal.
        assert findings[0].actual_value == pytest.approx(0.35 - _ARCW, abs=1e-6)

    def test_track_endpoint_closest(self, tmp_path):
        # A track END cap is the nearest feature: its closest arc point is
        # interior, at 30 deg off the apex, on the concave side.
        seg, expected = _radial_track("concave_30", 0.0007)
        findings = _check_at(tmp_path, seg)
        assert [v.rule_id for v in findings] == ["clearance_arc_segment"]
        assert findings[0].actual_value == pytest.approx(expected, abs=1e-6)

    def test_intersection_reports_short(self, tmp_path):
        seg = '  (segment (start 50 45) (end 50 55) (width 0.2) (layer "F.Cu") (net 2))\n'
        findings = _check_at(tmp_path, seg)
        assert [v.rule_id for v in findings] == ["clearance_arc_segment"]
        assert findings[0].actual_value < 0
        assert findings[0].message.startswith("SHORT:")

    def test_second_finite_arc_endpoint_governed(self, tmp_path):
        # Two disjoint facing arcs whose minimum is governed by ENDPOINTS
        # (not the apex-to-apex interior): expected from an independent
        # dense sampling of both skeletons.
        arc2 = (
            "  (arc (start 39.85 39.7) (mid 40 39.55) (end 40.15 39.7) "
            '(width 0.2) (layer "F.Cu") (net 2))\n'
        )
        findings = _check_at(tmp_path, arc2)
        assert [v.rule_id for v in findings] == ["clearance_arc_arc"]
        expected = _sampled_arc_arc_gap(tmp_path, arc2)
        assert findings[0].actual_value == pytest.approx(expected, abs=1e-4)


def _sampled_arc_arc_gap(tmp_path: Path, arc2_sexp: str) -> float:
    """Independent numeric oracle: dense on-arc samples of both centrelines."""
    from kicad_tools.core.geometry import point_to_segment_distance  # noqa: F401

    p = tmp_path / "b.kicad_pcb"
    p.write_text(_HEADER2L + _BIG_ARC + arc2_sexp + "\n)\n")
    pcb = PCB.load(str(p))
    arcs = list(pcb.arcs)
    assert len(arcs) == 2
    pts1 = arcs[0].centerline_points(max_error_mm=1e-6)
    pts2 = arcs[1].centerline_points(max_error_mm=1e-6)
    best = min(math.dist(p, q) for p in pts1 for q in pts2)
    return best - arcs[0].width / 2 - arcs[1].width / 2


class TestArcAcceptanceBand:
    """The checker band agrees with the probed kicad-cli arc transition:
    a 490 nm-short gap passes, a 500 nm-short one is flagged, on all four
    measured orientations (Issue #9702's black-box probe on the pinned
    image; the native parity test below re-proves it live)."""

    @pytest.mark.parametrize("orient", ["convex_apex", "concave_apex", "convex_30", "concave_30"])
    def test_490_passes_500_flags(self, tmp_path, orient):
        seg, _ = _radial_track(orient, 0.00049)
        assert _check_at(tmp_path, seg) == []
        seg, _ = _radial_track(orient, 0.0005)
        findings = _check_at(tmp_path, seg)
        assert [v.rule_id for v in findings] == ["clearance_arc_segment"]
        assert findings[0].actual_value == pytest.approx(0.1995, abs=1e-6)

    def test_band_is_separate_from_measurement(self):
        from kicad_tools.validate.rules.base import KICAD_CLEARANCE_BAND_MM
        from kicad_tools.validate.rules.clearance import (
            ARC_CLEARANCE_BAND_MM,
            CopperElement,
            pair_clearance_tolerance_mm,
        )

        assert (
            pair_clearance_tolerance_mm(
                CopperElement(
                    element_type="segment",
                    layer="F.Cu",
                    net_number=1,
                    geometry=(0, 0, 1, 0, 0.2),
                    reference="s",
                ),
                CopperElement(
                    element_type="segment",
                    layer="F.Cu",
                    net_number=2,
                    geometry=(0, 1, 1, 1, 0.2),
                    reference="t",
                ),
            )
            == KICAD_CLEARANCE_BAND_MM
        )
        arc = CopperElement(
            element_type="arc",
            layer="F.Cu",
            net_number=1,
            geometry=(0, 0, 1, 0, 0.2),
            reference="a",
        )
        assert pair_clearance_tolerance_mm(arc, arc) == ARC_CLEARANCE_BAND_MM


# ---------------------------------------------------------------------------
# AC2: pinned native DRC and the checker agree across the arc-track
# transition, probed at 10 nm steps (Issue #9702's test plan).  There is
# deliberately NO skip: an unresolvable pinned toolchain fails the test
# loudly (operator ruling on added skips), same as
# tests/test_edge_cuts_filled_native_parity.py.
# ---------------------------------------------------------------------------

_PARITY_SHORTS = [round(0.00046 + i * 1e-5, 8) for i in range(9)]  # 460..540 nm
_PARITY_ORIENTS = ("convex_apex", "concave_apex", "convex_30", "concave_30")
_PARITY_SPACING = 30.0


def _parity_board(orient: str, tmp_path: Path) -> tuple[Path, dict[int, str]]:
    """One board per orientation: sample i at arc centre x = 40 + i*30."""
    import uuid

    parts = [_HEADER2L]
    centres: dict[int, str] = {}
    for i, shortfall in enumerate(_PARITY_SHORTS):
        cx = 40.0 + i * _PARITY_SPACING
        seg, _ = _radial_track_at(orient, shortfall, cx)
        parts.append(
            f"  (arc (start {cx - _ARC_R} 40) (mid {cx} 50) (end {cx + _ARC_R} 40) "
            f'(width {_ARCW}) (layer "F.Cu") (uuid "{uuid.uuid4()}") (net 1))\n'
        )
        parts.append(seg)
        centres[i] = f"{cx:.6f}"
    parts.append(
        "  (gr_rect (start 0 0) (end 340 120) (stroke (width 0.05) "
        '(type default)) (fill none) (layer "Edge.Cuts"))\n'
    )
    parts.append(")\n")
    board = tmp_path / f"parity_{orient}.kicad_pcb"
    board.write_text("".join(parts))
    board.with_suffix(".kicad_dru").write_text(
        '(version 1)\n(rule "probe clearance" (constraint clearance (min 0.2mm)))\n'
    )
    return board, centres


def _radial_track_at(orient: str, shortfall: float, cx: float) -> tuple[str, float]:
    """``_radial_track`` translated to arc centre ``(cx, 40)`` (same math)."""

    def f(v: float) -> str:
        return f"{v:.6f}"

    gap = _REQ - shortfall
    if orient == "convex_apex":
        ey = 50.4 - shortfall
        p1, p2 = (cx, ey), (cx, ey + 2.0)
    elif orient == "concave_apex":
        ey = 49.6 + shortfall
        p1, p2 = (cx, ey), (cx, ey - 2.0)
    else:
        ang = math.radians(60.0)
        ux, uy = math.cos(ang), math.sin(ang)
        re = (_ARC_R + _ARCW + gap) if orient == "convex_30" else (_ARC_R - _ARCW - gap)
        ex, ey = round(cx + ux * re, 6), round(40 + uy * re, 6)
        sign = 1.0 if orient == "convex_30" else -1.0
        p1, p2 = (ex, ey), (ex + sign * ux * 2.0, ey + sign * uy * 2.0)
    seg = (
        f"  (segment (start {f(p1[0])} {f(p1[1])}) (end {f(p2[0])} {f(p2[1])}) "
        f'(width {_ARCW}) (layer "F.Cu") (net 2))\n'
    )
    return seg, gap


def _kct_flagged(board: Path, centres: dict[int, str]) -> set[int]:
    """Sample indices ``ClearanceRule`` flags at the 0.2 mm minimum."""
    from kicad_tools.validate.rules.clearance import ClearanceRule

    pcb = PCB.load(str(board))
    flagged: set[int] = set()
    for v in ClearanceRule()._check_layer(pcb, "F.Cu", _REQ):
        for i, cx in centres.items():
            if abs(v.location[0] - float(cx)) < _PARITY_SPACING / 2:
                flagged.add(i)
                break
    return flagged


def _kicad_flagged(board: Path, tmp_path: Path, centres: dict[int, str]) -> set[int]:
    """Sample indices pinned kicad-cli flags (type ``clearance``)."""
    from kicad_tools.cli.kicad_toolchain import KiCadEnvironmentError, evidence_toolchain
    from kicad_tools.cli.runner import run_drc

    try:
        with evidence_toolchain(mounts=[tmp_path]):
            result = run_drc(board, schematic_parity=False)
    except KiCadEnvironmentError as exc:
        pytest.fail(f"pinned kicad-cli unavailable for arc band parity: {exc}")
    if not result.success:
        pytest.fail(f"kicad-cli DRC failed: {result.stderr[:200]}")
    report = json.loads(Path(result.output_path).read_text())
    flagged: set[int] = set()
    for violation in report.get("violations", []):
        if violation.get("type") != "clearance":
            continue
        for item in violation.get("items", []):
            if "[N2]" not in item.get("description", ""):
                continue
            x = float(item["pos"]["x"])
            for i, cx in centres.items():
                if abs(x - float(cx)) < _PARITY_SPACING / 2:
                    flagged.add(i)
                    break
            break
    return flagged


class TestNativeArcBandParity:
    @pytest.mark.parametrize("orient", _PARITY_ORIENTS)
    def test_checker_agrees_with_pinned_kicad_cli_across_the_transition(self, tmp_path, orient):
        board, centres = _parity_board(orient, tmp_path)
        kct = _kct_flagged(board, centres)
        native = _kicad_flagged(board, tmp_path, centres)
        assert kct == native, f"kct flagged {sorted(kct)} but kicad-cli flagged {sorted(native)}"
        # The measured transition: the 490 nm-short sample passes and the
        # 500 nm-short one is flagged, on both engines (the issue's coarse
        # 400/500 bracket resolves to these adjacent 10 nm samples).
        idx_490 = _PARITY_SHORTS.index(0.00049)
        idx_500 = _PARITY_SHORTS.index(0.0005)
        assert idx_490 not in kct and idx_500 in kct
        assert idx_490 not in native and idx_500 in native


# ---------------------------------------------------------------------------
# AC3: probe, checked apply and the saved board's own check agree at the
# minimum; a below-band control produces a finding and refuses the write.
# The probe floor is the default manufacturer's (jlcpcb, 0.127 mm).
# ---------------------------------------------------------------------------

_MIN_GAP = 0.127

_ARC_ITEM_BIG = {
    "type": "arc",
    "net": "N1",
    "layer": "F.Cu",
    "width": 0.2,
    "start": [40, 40],
    "mid": [50, 50],
    "end": [60, 40],
}


def _radial_item(y: float) -> dict:
    return {
        "type": "segment",
        "net": "N2",
        "layer": "F.Cu",
        "width": 0.2,
        "start": [50, y],
        "end": [50, y + 2],
    }


class TestProbeApplyCheckAgreement:
    def _board(self, tmp_path: Path) -> Path:
        board = tmp_path / "pac.kicad_pcb"
        board.write_text(
            _HEADER2L + "  (gr_rect (start 0 0) (end 100 100) (stroke (width 0.05) "
            '(type default)) (fill none) (layer "Edge.Cuts"))\n' + ")\n"
        )
        return board

    def test_at_minimum_probe_apply_and_saved_board_check_agree(self, tmp_path, capsys):
        board = self._board(tmp_path)
        # Track copper edge at y-0.1; arc outer copper at 50.1: the gap is
        # y - 50.2, so y = 50.327 is exactly the 0.127 mm floor.
        items = _items_file(tmp_path, _ARC_ITEM_BIG, _radial_item(50.327))
        rc, doc = _run(capsys, "probe", str(board), str(items), "--format", "json")
        assert rc == 0 and doc["ok"] is True

        rc, doc = _run(capsys, "apply", str(board), str(items), "--checked", "--format", "json")
        assert rc == 0 and doc["written"] is not None

        saved = _clearance_findings(_load(board))
        assert [v.rule_id for v in saved if v.rule_id.startswith("clearance_arc")] == []

    def test_below_band_control_refuses_and_preserves_bytes(self, tmp_path, capsys):
        board = self._board(tmp_path)
        before = hashlib.md5(board.read_bytes()).hexdigest()
        # 600 nm below the floor: y - 50.2 = 0.1264.
        items = _items_file(tmp_path, _ARC_ITEM_BIG, _radial_item(50.3264))
        rc, doc = _run(capsys, "probe", str(board), str(items), "--format", "json")
        assert rc == 1 and doc["ok"] is False
        shorts = [p for p in doc["problems"] if p["rule_id"] == "clearance_arc_segment"]
        assert len(shorts) == 1
        # Both members of the pair are proposed (arc item 1, track item 2).
        assert shorts[0]["proposed_item"] in (1, 2)

        rc, doc = _run(capsys, "apply", str(board), str(items), "--checked", "--format", "json")
        assert rc == 1 and doc["written"] is None
        assert hashlib.md5(board.read_bytes()).hexdigest() == before


class TestNetclassArcBand:
    """The netclass copper rule consumes the same measured band (its
    comparison site asks ``pair_clearance_tolerance_mm``), so a named
    class stricter than the fab floor sees the same arc verdict."""

    def test_arc_pair_at_class_minimum_passes_and_below_flags(self, tmp_path):
        from kicad_tools.manufacturers import get_profile
        from kicad_tools.validate.rules.netclass_clearance_copper import (
            check_netclass_clearance_copper,
        )

        cls = 0.3  # stricter than the 0.2 fab floor used for the geometry
        project = {
            "meta": {"filename": "netclass.kicad_pro", "version": 3},
            "net_settings": {
                "meta": {"version": 4},
                "classes": [
                    {
                        "name": "Default",
                        "clearance": 0.127,
                        "track_width": 0.2,
                        "via_diameter": 0.6,
                        "via_drill": 0.3,
                        "priority": 2147483647,
                    },
                    {
                        "name": "HV",
                        "clearance": cls,
                        "track_width": 0.2,
                        "via_diameter": 0.6,
                        "via_drill": 0.3,
                        "priority": 0,
                    },
                ],
                "netclass_assignments": None,
                "netclass_patterns": [{"netclass": "HV", "pattern": "N2"}],
            },
        }
        rules = get_profile("jlcpcb").get_design_rules(layers=2)

        def results_for(shortfall: float):
            p = tmp_path / "netclass.kicad_pcb"
            seg, _ = _radial_track("convex_apex", shortfall)
            # Shift the segment to N2 so the class governs the pair.
            p.write_text(_HEADER2L + _BIG_ARC + seg + "\n)\n")
            p.with_suffix(".kicad_pro").write_text(json.dumps(project))
            pcb = PCB.load(str(p))
            return check_netclass_clearance_copper(p, pcb, rules).violations

        at_min = results_for(_REQ - cls)  # gap exactly 0.3 = the class minimum
        assert [v.rule_id for v in at_min] == []
        below = results_for(_REQ - cls + 0.0006)  # 600 nm short of the class
        assert [v.rule_id for v in below] == ["netclass_clearance_copper"]
        assert below[0].actual_value == pytest.approx(0.2994, abs=1e-5)


class TestAnalyticArcDistances:
    """Differential oracle for the exact skeleton distances (Issue #9702).

    The analytic helpers are compared against an independent method: dense
    on-circle samples (the sample points are exact -- only the spacing is
    discrete) and, for zero answers, a shapely intersection of the sampled
    polylines.  Seeded, so every run is the same cases.
    """

    def test_point_segment_arc_match_dense_sampling(self):
        from kicad_tools.core.geometry import point_to_segment_distance
        from kicad_tools.validate.rules.clearance import _segment_arc_distance

        rng = random.Random(9702)

        def arc_pts(geom, n):
            cx, cy, r, a0, sw = geom
            return [
                (cx + r * math.cos(a0 + sw * i / (n - 1)), cy + r * math.sin(a0 + sw * i / (n - 1)))
                for i in range(n)
            ]

        pts_n = 6000
        for _ in range(60):
            geom = (
                rng.uniform(-5, 5),
                rng.uniform(-5, 5),
                rng.uniform(0.3, 12),
                rng.uniform(-math.pi, math.pi),
                rng.choice([1, -1]) * rng.uniform(0.05, math.tau - 1e-6),
            )
            cx, cy, r = geom[0], geom[1], geom[2]
            x1, y1 = rng.uniform(cx - r - 2, cx + r + 2), rng.uniform(cy - r - 2, cy + r + 2)
            x2, y2 = rng.uniform(cx - r - 2, cx + r + 2), rng.uniform(cy - r - 2, cy + r + 2)
            got = _segment_arc_distance(x1, y1, x2, y2, geom)[0]
            if got == 0.0:
                from shapely.geometry import LineString  # type: ignore[import-untyped]

                inter = LineString(arc_pts(geom, 40000)).intersection(
                    LineString([(x1, y1), (x2, y2)])
                )
                assert not inter.is_empty, (geom, (x1, y1, x2, y2))
            else:
                want = min(
                    point_to_segment_distance(q[0], q[1], x1, y1, x2, y2)
                    for q in arc_pts(geom, pts_n)
                )
                assert got == pytest.approx(want, abs=3e-3), (geom, (x1, y1, x2, y2))

    def test_arc_arc_match_dense_sampling(self):
        from shapely.geometry import LineString  # type: ignore[import-untyped]

        from kicad_tools.validate.rules.clearance import _arc_arc_distance

        rng = random.Random(9703)

        def arc_pts(geom, n):
            cx, cy, r, a0, sw = geom
            return [
                (cx + r * math.cos(a0 + sw * i / (n - 1)), cy + r * math.sin(a0 + sw * i / (n - 1)))
                for i in range(n)
            ]

        for _ in range(25):

            def rnd():
                return (
                    rng.uniform(-3, 3),
                    rng.uniform(-3, 3),
                    rng.uniform(0.3, 8),
                    rng.uniform(-math.pi, math.pi),
                    rng.choice([1, -1]) * rng.uniform(0.1, math.tau - 0.01),
                )

            g1, g2 = rnd(), rnd()
            got = _arc_arc_distance(g1, g2)[0]
            if got == 0.0:
                inter = LineString(arc_pts(g1, 30000)).intersection(LineString(arc_pts(g2, 30000)))
                assert not inter.is_empty, (g1, g2)
            else:
                p1, p2 = arc_pts(g1, 400), arc_pts(g2, 400)
                want = min(math.dist(a, b) for a in p1 for b in p2)
                assert got == pytest.approx(want, abs=5e-2), (g1, g2)


# ---------------------------------------------------------------------------
# Issue #9714: conservative pruning of the arc-vs-arc chord search.
#
# The pair verdict is minimised over the covering chords of both arcs; the
# landed #9678 loop evaluated every chord pair.  These tests pin the
# optimisation's contract: the pruned search returns the SAME gap and the
# SAME witness (reported location) as an exhaustive chord-pair reference --
# including pairs where several chords attain an equal minimum -- while
# provably skipping exact kernel evaluations that cannot improve either.
# ---------------------------------------------------------------------------

_S2 = 2.0**0.5


def _arc_element(
    start: tuple[float, float],
    mid: tuple[float, float],
    end: tuple[float, float],
    *,
    width: float = 0.2,
    net: int = 1,
):
    from kicad_tools.schema.pcb import Arc as SchemaArc
    from kicad_tools.validate.rules.clearance import CopperElement

    return CopperElement.from_arc(
        SchemaArc(start=start, mid=mid, end=end, width=width, layer="B.Cu", net_number=net)
    )


def _segment_element(
    start: tuple[float, float], end: tuple[float, float], *, width: float = 0.2, net: int = 2
):
    from kicad_tools.schema.pcb import Segment as SchemaSegment
    from kicad_tools.validate.rules.clearance import CopperElement

    return CopperElement.from_segment(
        SchemaSegment(start=start, end=end, width=width, layer="B.Cu", net_number=net)
    )


def _exhaustive_reference(elem1, elem2) -> tuple[float, tuple[float, float]]:
    """The pre-#9714 exhaustive chord loop, reimplemented as the oracle.

    Same first-strict-minimum selection (the witness whose four endpoints
    feed the track-vs-track location), so equality with the pruned search
    asserts both the gap and the reported location.
    """
    from kicad_tools.validate.clearance_shapes import copper_gap
    from kicad_tools.validate.rules.clearance import _element_chord_shapes

    best: tuple[float, object, object] | None = None
    for c1 in _element_chord_shapes(elem1):
        for c2 in _element_chord_shapes(elem2):
            gap = copper_gap(c1, c2)
            if best is None or gap < best[0]:
                best = (gap, c1, c2)
    assert best is not None
    gap, s1, s2 = best
    loc = ((s1.x1 + s1.x2 + s2.x1 + s2.x2) / 4, (s1.y1 + s1.y2 + s2.y1 + s2.y2) / 4)
    return gap, loc


class TestArcPairChordPruning:
    # Since Issue #9702 authored arcs take the exact path in
    # ``_arc_pair_clearance``; the covering-chord search these tests pin
    # (Issue #9714) is the fallback, so they call it directly.
    def test_pruned_search_matches_exhaustive_reference(self):
        from kicad_tools.validate.rules.clearance import _arc_chord_pair_clearance

        clear = _arc_element((2, 0), (_S2, _S2), (0, 2))
        wide = _arc_element((5, 0), (5 / _S2, 5 / _S2), (0, 5), net=2)
        near = _arc_element((2.25, 0), (2.25 / _S2, 2.25 / _S2), (0, 2.25), net=2)
        # Two quarter arcs sharing both endpoints and bulging toward each
        # other: the minimum (-width) is attained at the TWO endpoint chord
        # pairs -- different locations, first in row-major order must win.
        lens_a = _arc_element((2, 0), (_S2, _S2), (0, 2))
        lens_b = _arc_element((0, 2), (2 - _S2, 2 - _S2), (2, 0), net=2)
        # One interior crossing (circle (0,0) r=2 vs circle (1,0) r=2).
        crossing_a = _arc_element((2, 0), (_S2, _S2), (0, 2))
        crossing_b = _arc_element((1, 2), (1 - _S2, _S2), (-1, 0), net=2)
        # The issue's geometry: concentric 50 mm quarter arcs, 0.3 mm apart,
        # so every corresponding chord pair attains an equal minimum.
        big1 = _arc_element((50, 0), (50 / _S2, 50 / _S2), (0, 50))
        big2 = _arc_element((50.3, 0), (50.3 / _S2, 50.3 / _S2), (0, 50.3), net=2)
        # Identical geometry, foreign nets: the first chord pair saturates.
        twin = _arc_element((2, 0), (_S2, _S2), (0, 2), net=2)
        # Mixed kinds: many chords against one straight segment.
        seg = _segment_element((2.3, -1.0), (2.3, 1.0))
        cases = [
            ("clear_concentric", clear, wide),
            ("near_threshold_concentric", clear, near),
            ("shared_endpoint_equal_minima", lens_a, lens_b),
            ("interior_crossing", crossing_a, crossing_b),
            ("large_concentric_equal_minima", big1, big2),
            ("identical_geometry", clear, twin),
            ("arc_vs_segment_near_threshold", clear, seg),
        ]
        for name, e1, e2 in cases:
            gap, loc_x, loc_y = _arc_chord_pair_clearance(e1, e2)
            ref_gap, ref_loc = _exhaustive_reference(e1, e2)
            assert gap == ref_gap, f"{name}: gap {gap!r} != exhaustive {ref_gap!r}"
            assert (loc_x, loc_y) == ref_loc, (
                f"{name}: location {(loc_x, loc_y)!r} != exhaustive {ref_loc!r}"
            )

    def test_concentric_large_arcs_prune_exact_evaluations(self, monkeypatch):
        # The reported superlinear case as one pair: ~124 x ~127 chords =
        # ~15.7k exhaustive kernel calls; the pruned search must evaluate
        # strictly fewer and still answer the exhaustive gap.
        from kicad_tools.validate.rules import clearance as clearance_mod
        from kicad_tools.validate.rules.clearance import (
            _arc_chord_pair_clearance,
            _element_chord_shapes,
        )

        e1 = _arc_element((50, 0), (50 / _S2, 50 / _S2), (0, 50))
        e2 = _arc_element((50.3, 0), (50.3 / _S2, 50.3 / _S2), (0, 50.3), net=2)
        n1 = len(_element_chord_shapes(e1))
        n2 = len(_element_chord_shapes(e2))
        assert n1 * n2 > 10_000  # the fixture is the expensive shape

        real = clearance_mod._kernel_copper_gap
        calls = [0]

        def counting(a, b):
            calls[0] += 1
            return real(a, b)

        monkeypatch.setattr(clearance_mod, "_kernel_copper_gap", counting)
        gap, _, _ = _arc_chord_pair_clearance(e1, e2)
        ref_gap, _ = _exhaustive_reference(e1, e2)
        assert gap == ref_gap
        assert calls[0] < n1 * n2  # pruning skips exact evaluations
        assert calls[0] * 8 < n1 * n2  # ...by a solid margin, not one pair

    def test_chord_shapes_memoised_per_element(self):
        # A sweep asks the same element for its chords once per candidate
        # pair (Issue #9714 suspects the rebuild); the list must be built
        # once per element and handed to every caller.
        from kicad_tools.validate.rules.clearance import _element_chord_shapes

        elem = _arc_element((50, 0), (50 / _S2, 50 / _S2), (0, 50))
        assert _element_chord_shapes(elem) is _element_chord_shapes(elem)
