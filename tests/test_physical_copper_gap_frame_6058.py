"""physical_copper_gap measures and reports in one frame (issue #6058).

Track arcs are read from the raw s-expression (sheet coordinates) while the
rest of the copper comes from the board-relative ``PCB`` model.  On a board
with a non-zero origin the arcs were therefore offset, findings were reported
board-relative, and the ev2 nearby-pad evidence was empty.  Expectations are
derived by hand in sheet coordinates.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp import parse_string
from kicad_tools.validate.checker import DRCChecker
from kicad_tools.validate.evidence import EvidenceContext, evidence_payload

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests/fixtures/projects/multilayer_zones.kicad_pcb"
FLEET03 = REPO / "boards/03-usb-joystick/output/usb_joystick_routed.kicad_pcb"

# Arc apex (centre line) at (161, 141), outer edge y = 141.1 (width 0.2).
ARC = (
    "(arc (start {x0} {y0}) (mid {x1} {y1}) (end {x2} {y0}) (width 0.2) "
    '(layer "F.Cu") (net {{net}}) (uuid "arc-6058"))'
)
# Track lower edge y0+1.3 -> arc-to-track gap 0.2 at the apex.
TRACK = (
    "(segment (start {x0} {ty}) (end {x2} {ty}) (width 0.2) "
    '(layer "F.Cu") (net {{net}}) (uuid "trk-6058"))'
)
# 0.5mm square pad centred 1.1 below the apex: top edge 0.75 from the arc.
PAD = (
    '(footprint "Test:P" (layer "F.Cu") (uuid "fp-6058") (at {px} {py})\n'
    '  (property "Reference" "TP6058" (at 0 0) (layer "F.SilkS") (uuid "r-6058"))\n'
    '  (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu") (net {{net}} "GND")))'
)


def _items(x, y, *, track=True, pad=False):
    """Probe items anchored at sheet position (x, y) = arc start."""
    out = [ARC.format(x0=x, y0=y, x1=x + 1, y1=y + 1, x2=x + 2)]
    if track:
        out.append(TRACK.format(x0=x, x2=x + 2, ty=y + 1.4))
    if pad:
        out.append(PAD.format(px=x + 1, py=y + 2.1))
    return out


def _build(base: Path, tmp_path: Path, items: list[str], *, drop_zones: bool = False) -> PCB:
    text = base.read_text()
    m = re.search(r'\(net\s+(\d+)\s+"GND"\)', text)
    assert m
    end = text.rstrip().rindex(")")
    body = "\n".join(i.format(net=m.group(1)) for i in items)
    text = text[:end] + body + "\n" + text[end:]
    if drop_zones:
        # Filled pours would union with the probe copper and hide its gaps.
        root = parse_string(text)
        root.children = [c for c in root.children if c.name != "zone"]
        return PCB(root)
    path = tmp_path / "b.kicad_pcb"
    path.write_text(text)
    return PCB.load(path)


def _mine(pcb, minimum=0.25):
    checker = DRCChecker(pcb, physical_copper_gap_mm=minimum)
    return [
        v
        for v in checker.check_physical_copper_gap().violations
        if v.rule_id == "physical_copper_gap" and "arc-6058" in v.items
    ]


def test_arc_to_track_gap_and_sheet_frame(tmp_path):
    pcb = _build(FIXTURE, tmp_path, _items(170, 150))
    assert pcb.board_origin == (100.0, 100.0)
    found = _mine(pcb)
    assert len(found) == 1
    v = found[0]
    assert v.actual_value == pytest.approx(0.2, abs=0.002)
    assert set(v.items) == {"arc-6058", "trk-6058"}
    # Sheet frame: gap is at x=171 between y=151.1 and y=151.3.
    assert v.location == pytest.approx((171.0, 151.2), abs=0.01)
    for p in v.closest_locations:
        assert 170.9 < p[0] < 171.1
        assert 151.05 < p[1] < 151.35
    # The message quotes the same (sheet) frame.
    assert "(171." in v.message and "151." in v.message


def test_arc_to_pad_gap(tmp_path):
    pcb = _build(FIXTURE, tmp_path, _items(170, 150, track=False, pad=True))
    found = _mine(pcb, minimum=0.8)
    assert len(found) == 1
    assert found[0].actual_value == pytest.approx(0.75, abs=0.005)
    assert found[0].location == pytest.approx((171.0, 151.475), abs=0.01)
    assert not _mine(pcb, minimum=0.7)


def test_evidence_includes_nearby_pad(tmp_path):
    pcb = _build(FIXTURE, tmp_path, _items(170, 150, pad=True))
    found = _mine(pcb)
    assert found
    nets = evidence_payload(found[0], EvidenceContext(pcb))["nets"]
    assert any(row[0] == "TP6058.1" for row in nets["GND"])


def test_zero_origin_board_is_unshifted():
    items = [i.format(net=1) for i in _items(170, 150)]
    pcb = PCB(
        parse_string(
            "(kicad_pcb (version 20240108) (generator pcbnew) (general (thickness 1.6))"
            ' (layers (0 "F.Cu" signal) (31 "B.Cu" signal)) (net 0 "") (net 1 "GND")'
            + "".join(items)
            + ")"
        )
    )
    found = _mine(pcb)
    assert found and found[0].location == pytest.approx((171.0, 151.2), abs=0.01)


@pytest.mark.skipif(not FLEET03.is_file(), reason="fleet board 03 not present")
def test_fleet_board_origin_matches_sheet_frame(tmp_path):
    ox, oy = PCB.load(FLEET03).board_origin
    assert (ox, oy) != (0.0, 0.0)
    cx, cy = ox + 20.0, oy + 15.0
    pcb = _build(FLEET03, tmp_path, _items(cx, cy), drop_zones=True)
    found = _mine(pcb)
    assert len(found) == 1
    assert found[0].actual_value == pytest.approx(0.2, abs=0.002)
    assert found[0].location == pytest.approx((cx + 1, cy + 1.2), abs=0.01)
