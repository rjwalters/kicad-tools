"""Off-path copper must not count toward match-group skew (Issue #9138).

``derive_group_skew_data`` used to sum every segment and via on a net, so a
same-net dead-end loop, an overlapping duplicate or a stub inflated that
net's "length" and masked real skew.  It now measures the pad-to-pad copper
path (Issue #9117's ``TraceLengthAnalyzer.measure_net_path``) plus the
height of the vias on that path, and reports off-path copper.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from kicad_tools._shapely import has_shapely
from kicad_tools.router.kicad_length import measure_pcb_net
from kicad_tools.router.rules import NetClassRouting
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.match_group_skew import derive_group_skew_data

pytestmark = pytest.mark.skipif(not has_shapely(), reason="path measurement needs shapely")

HEADER = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "DQ0")
  (net 2 "DQ1")
  (net 3 "DQ2")
  (gr_rect (start 0 0) (end 60 60) (stroke (width 0.1)) (layer "Edge.Cuts"))
"""


def _pads(rows: dict[int, tuple[str, float, float]]) -> str:
    """One two-pad footprint; ``rows`` maps net id -> (name, y, x_end)."""
    pads = []
    for net_id, (name, y, x_end) in rows.items():
        for idx, x in enumerate((5.0, x_end)):
            pads.append(
                f'    (pad "{net_id}{idx}" smd rect (at {x} {y}) (size 1.0 1.0)'
                f' (layers "F.Cu" "F.Mask") (net {net_id} "{name}"))'
            )
    return (
        '  (footprint "Test:Pads" (layer "F.Cu") (at 0 0)\n'
        '    (property "Reference" "J1")\n' + "\n".join(pads) + "\n  )\n"
    )


def _seg(x1, y1, x2, y2, net, layer="F.Cu") -> str:
    return (
        f'  (segment (start {x1} {y1}) (end {x2} {y2}) (width 0.2) (layer "{layer}") (net {net}))\n'
    )


def _write(tmp_path: Path, body: str, rows) -> PCB:
    path = tmp_path / "board.kicad_pcb"
    path.write_text(HEADER + _pads(rows) + body + ")\n")
    return PCB.load(str(path))


NET_CLASS = NetClassRouting(name="DDR", length_match_group="DDR_DATA")
NET_CLASS_MAP = {"DQ0": NET_CLASS, "DQ1": NET_CLASS, "DQ2": NET_CLASS}


def _loop_board(tmp_path: Path) -> PCB:
    """DQ0/DQ1/DQ2 are 20.0 / 20.0 / 20.5 mm pad-to-pad.

    DQ0 additionally carries a 20 mm same-net loop that leaves its far pad
    and returns to it (the loop-through-own-pad case).  Summed, DQ0 would
    measure 40 mm; on the path it is 20 mm.
    """
    rows = {1: ("DQ0", 10.0, 25.0), 2: ("DQ1", 20.0, 25.0), 3: ("DQ2", 30.0, 25.5)}
    body = (
        _seg(5, 10, 25, 10, 1)
        + _seg(25, 10, 35, 10, 1)
        + _seg(35, 10, 25, 10, 1)
        + _seg(5, 20, 25, 20, 2)
        + _seg(5, 30, 25.5, 30, 3)
    )
    return _write(tmp_path, body, rows)


def test_loop_through_own_pad_is_not_counted(tmp_path: Path):
    pcb = _loop_board(tmp_path)
    # Premise: the summed kicad-cli measurement (the pre-#9138 source) does
    # see the loop, so the old producer reported ~20 mm of skew.
    assert measure_pcb_net(pcb, 1) == pytest.approx(40.0, abs=0.05)

    skew_data, _groups, _thresholds = derive_group_skew_data(pcb, NET_CLASS_MAP)

    assert skew_data["DDR_DATA"] == pytest.approx(0.5, abs=0.05)


def test_off_path_copper_is_reported(tmp_path: Path, caplog):
    pcb = _loop_board(tmp_path)

    with caplog.at_level(logging.WARNING, logger="kicad_tools.validate.match_group_skew"):
        derive_group_skew_data(pcb, NET_CLASS_MAP)

    messages = [r.getMessage() for r in caplog.records if "off-path" in r.getMessage()]
    assert len(messages) == 1
    assert "DQ0" in messages[0]
    assert "20.0" in messages[0]


def test_clean_nets_match_summed_length_and_stay_silent(tmp_path: Path, caplog):
    rows = {1: ("DQ0", 10.0, 25.0), 2: ("DQ1", 20.0, 25.0), 3: ("DQ2", 30.0, 25.5)}
    body = _seg(5, 10, 25, 10, 1) + _seg(5, 20, 25, 20, 2) + _seg(5, 30, 25.5, 30, 3)
    pcb = _write(tmp_path, body, rows)
    lengths = [measure_pcb_net(pcb, n) for n in (1, 2, 3)]

    with caplog.at_level(logging.WARNING, logger="kicad_tools.validate.match_group_skew"):
        skew_data, _g, _t = derive_group_skew_data(pcb, NET_CLASS_MAP)

    assert skew_data["DDR_DATA"] == pytest.approx(max(lengths) - min(lengths), abs=1e-6)
    assert not [r for r in caplog.records if "off-path" in r.getMessage()]


def test_via_on_path_keeps_its_stackup_height(tmp_path: Path):
    """A clean layer-changing net measures as before: path + via height."""
    rows = {1: ("DQ0", 10.0, 25.0), 2: ("DQ1", 20.0, 25.0), 3: ("DQ2", 30.0, 25.0)}
    # DQ0 runs 10 mm on F.Cu, drops through a via, runs 10 mm on B.Cu to a
    # B.Cu... pad is F.Cu-only, so return through a second via instead.
    body = (
        _seg(5, 10, 10, 10, 1)
        + '  (via (at 10 10) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))\n'
        + _seg(10, 10, 20, 10, 1, layer="B.Cu")
        + '  (via (at 20 10) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))\n'
        + _seg(20, 10, 25, 10, 1)
        + _seg(5, 20, 25, 20, 2)
        + _seg(5, 30, 25, 30, 3)
    )
    pcb = _write(tmp_path, body, rows)
    summed = [measure_pcb_net(pcb, n) for n in (1, 2, 3)]
    assert summed[0] > summed[1] + 1.0  # two 1.58 mm barrels charged

    skew_data, _g, _t = derive_group_skew_data(pcb, NET_CLASS_MAP)

    assert skew_data["DDR_DATA"] == pytest.approx(max(summed) - min(summed), abs=0.05)


def test_falls_back_to_summed_length_without_shapely(tmp_path: Path, monkeypatch):
    pcb = _loop_board(tmp_path)
    monkeypatch.setattr("kicad_tools._shapely.has_shapely", lambda: False)

    skew_data, _g, _t = derive_group_skew_data(pcb, NET_CLASS_MAP)

    # No path available -> the legacy summed measurement (loop counted).
    assert skew_data["DDR_DATA"] == pytest.approx(20.0, abs=0.05)


def test_open_net_falls_back_to_summed_length(tmp_path: Path):
    """DQ0's trace stops short of its far pad: no pad-to-pad path."""
    rows = {1: ("DQ0", 10.0, 25.0), 2: ("DQ1", 20.0, 25.0), 3: ("DQ2", 30.0, 25.0)}
    body = _seg(5, 10, 15, 10, 1) + _seg(5, 20, 25, 20, 2) + _seg(5, 30, 25, 30, 3)
    pcb = _write(tmp_path, body, rows)

    skew_data, _g, _t = derive_group_skew_data(pcb, NET_CLASS_MAP)

    assert skew_data["DDR_DATA"] == pytest.approx(10.0, abs=0.05)
