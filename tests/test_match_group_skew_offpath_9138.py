"""Off-path copper is reported alongside the derived match-group skew (#9138).

``derive_group_skew_data`` sums every segment and via on a member net, so a
same-net dead-end loop, an overlapping duplicate or a stub inflates that
net's "length" and can mask real skew.  The producer keeps the summed
kicad-cli figure -- the one ruler the router's match-group tuner
length-matches against; both ways of shedding off-path copper measurably
manufacture skew on this repo's boards -- and reports any net whose
off-path copper (Issue #9117's ``TraceLengthAnalyzer.measure_net_path``)
exceeds ``OFF_PATH_REPORT_EPSILON_MM``, so the inflation is loud instead of
silent.  Deciding which copper is truly defect and excluding exactly that
is split out of Issue #9138 (see the constant's rationale).
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
    and returns to it (the loop-through-own-pad case).  Summed, DQ0
    measures 40 mm, so the group's derived skew is 20.0 mm -- the loop
    masks nothing here, but on a group where every member carries a
    similar loop it would.
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


def test_loop_is_reported_and_summed_figure_kept(tmp_path: Path, caplog):
    """AC 2: the loop is reported; the figure stays the summed semantics."""
    pcb = _loop_board(tmp_path)
    lengths = [measure_pcb_net(pcb, n) for n in (1, 2, 3)]

    with caplog.at_level(logging.WARNING, logger="kicad_tools.validate.match_group_skew"):
        skew_data, _groups, _thresholds = derive_group_skew_data(pcb, NET_CLASS_MAP)

    assert skew_data["DDR_DATA"] == pytest.approx(max(lengths) - min(lengths), abs=1e-6)
    assert skew_data["DDR_DATA"] == pytest.approx(20.0, abs=0.05)
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


def test_sub_epsilon_off_path_stays_silent(tmp_path: Path, caplog):
    """Off-path copper at or below the epsilon is neither reported nor special."""
    rows = {1: ("DQ0", 10.0, 25.0), 2: ("DQ1", 20.0, 25.0), 3: ("DQ2", 30.0, 25.5)}
    # DQ0 carries a 2.0 mm dead-end stub: real off-path copper, below the
    # reporting epsilon, so derivation is log-silent.
    body = (
        _seg(5, 10, 25, 10, 1)
        + _seg(15, 10, 17, 10, 1)
        + _seg(5, 20, 25, 20, 2)
        + _seg(5, 30, 25.5, 30, 3)
    )
    pcb = _write(tmp_path, body, rows)
    lengths = [measure_pcb_net(pcb, n) for n in (1, 2, 3)]
    assert lengths[0] == pytest.approx(lengths[1] + 2.0, abs=0.05)  # premise

    with caplog.at_level(logging.WARNING, logger="kicad_tools.validate.match_group_skew"):
        skew_data, _g, _t = derive_group_skew_data(pcb, NET_CLASS_MAP)

    assert skew_data["DDR_DATA"] == pytest.approx(max(lengths) - min(lengths), abs=1e-6)
    assert not [r for r in caplog.records if "off-path" in r.getMessage()]


def test_member_above_old_threshold_measured_by_same_ruler_as_siblings(tmp_path: Path, caplog):
    """The review's cliff regression: no ruler switch at any off-path amount.

    One member carries 6.0 mm of dead-end stub (above both the 2.0 mm
    cutoff an earlier iteration of this change switched rulers at and
    the landed reporting epsilon), a sibling 5.0 mm, the third is
    clean.  Every member is measured with the same summed ruler, so
    the group's skew is exactly the summed spread -- the 2 mm step an
    earlier per-member threshold injected into ``max - min`` is gone
    -- and both stubs are reported.
    """
    rows = {1: ("DQ0", 10.0, 25.0), 2: ("DQ1", 20.0, 25.0), 3: ("DQ2", 30.0, 25.5)}
    body = (
        _seg(5, 10, 25, 10, 1)
        + _seg(15, 10, 21, 10, 1)  # 6.0 mm stub on DQ0
        + _seg(5, 20, 25, 20, 2)
        + _seg(15, 20, 20, 20, 2)  # 5.0 mm stub on DQ1
        + _seg(5, 30, 25.5, 30, 3)
    )
    pcb = _write(tmp_path, body, rows)
    lengths = [measure_pcb_net(pcb, n) for n in (1, 2, 3)]

    with caplog.at_level(logging.WARNING, logger="kicad_tools.validate.match_group_skew"):
        skew_data, _g, _t = derive_group_skew_data(pcb, NET_CLASS_MAP)

    assert skew_data["DDR_DATA"] == pytest.approx(max(lengths) - min(lengths), abs=1e-6)
    assert skew_data["DDR_DATA"] == pytest.approx(5.5, abs=0.05)
    messages = [r.getMessage() for r in caplog.records if "off-path" in r.getMessage()]
    assert len(messages) == 2
    assert any("DQ0" in m and "6.0" in m for m in messages)
    assert any("DQ1" in m and "5.0" in m for m in messages)


def test_via_net_keeps_summed_accounting_and_stays_silent(tmp_path: Path, caplog):
    """A clean layer-changing net measures as before: every barrel charged."""
    rows = {1: ("DQ0", 10.0, 25.0), 2: ("DQ1", 20.0, 25.0), 3: ("DQ2", 30.0, 25.0)}
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

    with caplog.at_level(logging.WARNING, logger="kicad_tools.validate.match_group_skew"):
        skew_data, _g, _t = derive_group_skew_data(pcb, NET_CLASS_MAP)

    assert skew_data["DDR_DATA"] == pytest.approx(max(summed) - min(summed), abs=1e-6)
    assert not [r for r in caplog.records if "off-path" in r.getMessage()]


def test_without_shapely_nothing_is_reported_and_summed_stands(tmp_path: Path, monkeypatch, caplog):
    pcb = _loop_board(tmp_path)
    monkeypatch.setattr("kicad_tools._shapely.has_shapely", lambda: False)

    with caplog.at_level(logging.WARNING, logger="kicad_tools.validate.match_group_skew"):
        skew_data, _g, _t = derive_group_skew_data(pcb, NET_CLASS_MAP)

    # No path-based figure -> nothing to report; the summed measurement
    # (loop included) is the figure, as on every environment without shapely.
    assert skew_data["DDR_DATA"] == pytest.approx(20.0, abs=0.05)
    assert not [r for r in caplog.records if "off-path" in r.getMessage()]


def test_open_net_keeps_summed_length_and_reports_nothing(tmp_path: Path, caplog):
    """DQ0's trace stops short of its far pad: no pad-to-pad path."""
    rows = {1: ("DQ0", 10.0, 25.0), 2: ("DQ1", 20.0, 25.0), 3: ("DQ2", 30.0, 25.0)}
    body = _seg(5, 10, 15, 10, 1) + _seg(5, 20, 25, 20, 2) + _seg(5, 30, 25, 30, 3)
    pcb = _write(tmp_path, body, rows)

    with caplog.at_level(logging.WARNING, logger="kicad_tools.validate.match_group_skew"):
        skew_data, _g, _t = derive_group_skew_data(pcb, NET_CLASS_MAP)

    assert skew_data["DDR_DATA"] == pytest.approx(10.0, abs=0.05)
    assert not [r for r in caplog.records if "off-path" in r.getMessage()]
