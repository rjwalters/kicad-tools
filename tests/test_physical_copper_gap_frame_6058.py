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


#: Copper item kinds the crop window filters; everything else (header,
#: layers, setup, nets, Edge.Cuts graphics) is kept so the board frame --
#: ``board_origin`` is derived from the Edge.Cuts outline -- is untouched.
_CROPPABLE = {"segment", "arc", "via", "footprint"}

#: A footprint is kept when its anchor lies within this distance of the
#: window, so pads offset from the anchor (connectors, QFNs) are not lost.
_FOOTPRINT_REACH_MM = 15.0


def _xy(node, key):
    child = node.find_child(key)
    if child is None:
        return None
    return child.get_float(0), child.get_float(1)


def _in_window(node, window) -> bool:
    x0, y0, x1, y1 = window
    if node.name == "footprint":
        at = _xy(node, "at")
        r = _FOOTPRINT_REACH_MM
        return at is not None and x0 - r <= at[0] <= x1 + r and y0 - r <= at[1] <= y1 + r
    keys = ("at",) if node.name == "via" else ("start", "mid", "end")
    pts = [p for p in (_xy(node, k) for k in keys) if p is not None]
    if not pts:
        return True  # unknown shape: keep rather than silently drop copper
    # Bounding-box overlap, so a long segment crossing the window is kept.
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs) <= x1 and max(xs) >= x0 and min(ys) <= y1 and max(ys) >= y0


def _build(
    base: Path,
    tmp_path: Path,
    items: list[str],
    *,
    drop_zones: bool = False,
    crop: tuple[float, float, float, float] | None = None,
) -> PCB:
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
        if crop is not None:
            # Issue #6128: the full-board scan of fleet board 03 took ~50-70 s
            # against CI's 60 s budget.  A finding on the probe arc needs
            # copper within the gap minimum of it, so copper outside a
            # generous window around the probe cannot change the arc's
            # findings; dropping it only removes unrelated board-wide work.
            root.children = [
                c for c in root.children if c.name not in _CROPPABLE or _in_window(c, crop)
            ]
        return PCB(root)
    path = tmp_path / "b.kicad_pcb"
    path.write_text(text)
    return PCB.load(path)


def _mine(pcb, minimum=0.25):
    checker = DRCChecker(pcb, physical_copper_gap_mm=minimum)
    return [
        v
        for v in checker.check_physical_copper_gap().violations
        if v.rule_id == "physical_copper_gap" and any(i.startswith("Arc@") for i in v.items)
    ]


def test_arc_to_track_gap_and_sheet_frame(tmp_path):
    pcb = _build(FIXTURE, tmp_path, _items(170, 150))
    assert pcb.board_origin == (100.0, 100.0)
    found = _mine(pcb)
    assert len(found) == 1
    v = found[0]
    assert v.actual_value == pytest.approx(0.2, abs=0.002)
    # Issue #6106: copper is named by sheet-frame geometry, not UUID.
    assert set(v.items) == {
        "Arc@F.Cu:w0.2:170/150~171/151~172/150",
        "Trace@F.Cu:w0.2:170/151.4~172/151.4",
    }
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
    # Probe copper spans [cx, cx+2] x [cy, cy+1.5]; a 5 mm margin is 20x the
    # 0.25 mm gap minimum, so every real-board item that could pair with the
    # arc is retained (see _build's crop note, issue #6128).
    window = (cx - 5.0, cy - 5.0, cx + 7.0, cy + 6.5)
    pcb = _build(FLEET03, tmp_path, _items(cx, cy), drop_zones=True, crop=window)
    # The crop must keep the real board frame and some real copper around
    # the probe -- otherwise this degenerates into the zero-origin case.
    assert pcb.board_origin == (ox, oy)
    assert len(pcb.segments) > 2 or len(pcb.vias) > 0 or len(pcb.footprints) > 0
    found = _mine(pcb)
    assert len(found) == 1
    assert found[0].actual_value == pytest.approx(0.2, abs=0.002)
    assert found[0].location == pytest.approx((cx + 1, cy + 1.2), abs=0.01)
