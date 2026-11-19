"""Copper arcs in the net-aware pairwise clearance/short rules (Issue #9673).

Before #9673, ``ClearanceRule`` walked ``pcb.segments``, pads and vias only:
authored ``(arc ...)`` copper was invisible to the pairwise rules, so a
foreign-net segment crossing an applied arc's apex read clean while the same
geometry with straight segments reported the short.  These tests pin the
fix -- arcs are collected as one copper element per arc (the arc, never a
tessellation chord, is the reporting unit), measured through the covering
chord tessellation shared with ``kct probe``
(:data:`kicad_tools.validate.rules.clearance.ARC_COPPER_SAGITTA_MM`).

Controls come from the issue's measured reproduction (board 02 geometry):
GND arc on ``B.Cu`` ``start (128,135) mid (130,137) end (132,135)``, width
0.3 -- a circle of radius 2 mm centred (130,135), apex at (130,137) -- with
a VCC segment crossing it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

pytest.importorskip("shapely")

from kicad_tools.cli import main  # noqa: E402
from kicad_tools.schema.pcb import PCB  # noqa: E402
from kicad_tools.validate import DRCChecker  # noqa: E402

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
        assert shorts[0]["proposed_item"] == 1
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
