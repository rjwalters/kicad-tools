"""Off-path copper is reported alongside the derived match-group skew (#9138).

``derive_group_skew_data`` sums every segment and via on a member net, so a
same-net dead-end loop, an overlapping duplicate or a stub inflates that
net's "length" and can mask real skew.  The producer reports any net whose
off-path copper (Issue #9117's ``TraceLengthAnalyzer.measure_net_path``)
exceeds ``OFF_PATH_REPORT_EPSILON_MM``, so the inflation is loud instead of
silent.  Since Issue #9304 the figure is also corrected: each member's
skew figure is its summed kicad-cli length minus the defect copper one
uniform classifier isolates (dead-end branches, terminal-rooted loops --
per connected structure, epsilon-gated), with rejoined cycle copper and
the sub-epsilon escape residual kept, so defect-free members measure the
summed figure byte-for-byte and no ruler ever switches per member.
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
from tests.test_trace_length import PATH_BASE_PCB

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


def _write(tmp_path: Path, body: str, rows, header: str = HEADER) -> PCB:
    path = tmp_path / "board.kicad_pcb"
    path.write_text(header + _pads(rows) + body + ")\n")
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


def test_loop_is_reported_and_excluded_from_figure(tmp_path: Path, caplog):
    """AC 2: the loop is reported; #9304: it is also shed from the figure.

    DQ0's 20 mm pad-rooted loop is defect copper above the epsilon, so
    its figure is the summed 40 mm minus the loop; the group's skew
    collapses to the real 0.5 mm spread instead of the loop-masked
    20.0 mm the summed ruler reports.
    """
    pcb = _loop_board(tmp_path)
    lengths = [measure_pcb_net(pcb, n) for n in (1, 2, 3)]
    assert lengths[0] == pytest.approx(40.0, abs=0.05)  # premise: loop summed in

    with caplog.at_level(logging.WARNING, logger="kicad_tools.validate.match_group_skew"):
        skew_data, _groups, _thresholds = derive_group_skew_data(pcb, NET_CLASS_MAP)

    assert skew_data["DDR_DATA"] == pytest.approx(0.5, abs=0.05)
    messages = [r.getMessage() for r in caplog.records if "off-path" in r.getMessage()]
    assert len(messages) == 1
    assert "DQ0" in messages[0]
    assert "20.0" in messages[0]
    assert "excluded from the measured length" in messages[0]


def test_partially_routed_group_emits_no_off_path_warning(tmp_path: Path, caplog):
    """Issue #9726: a group the eligibility check omits reports nothing.

    The loop board minus DQ2's only segment: DQ2 is unrouted, so the
    group is omitted from both result maps -- and must not report DQ0's
    20 mm loop either.  Before the reporting pass moved after the
    eligibility check, DQ0 was warned about beside no derived skew at
    all (#9138 AC 2 asks for the warning alongside the derived skew).
    """
    rows = {1: ("DQ0", 10.0, 25.0), 2: ("DQ1", 20.0, 25.0), 3: ("DQ2", 30.0, 25.5)}
    body = (
        _seg(5, 10, 25, 10, 1)
        + _seg(25, 10, 35, 10, 1)
        + _seg(35, 10, 25, 10, 1)
        + _seg(5, 20, 25, 20, 2)
        # DQ2 unrouted: its segment is omitted.
    )
    pcb = _write(tmp_path, body, rows)

    with caplog.at_level(logging.WARNING, logger="kicad_tools.validate.match_group_skew"):
        skew_data, _g, thresholds = derive_group_skew_data(pcb, NET_CLASS_MAP)

    assert skew_data == {}
    assert thresholds == {}
    assert not [r for r in caplog.records if "off-path" in r.getMessage()]


def test_connected_third_pad_reports_difference_from_selected_path(tmp_path: Path, caplog):
    path = tmp_path / "three-pad.kicad_pcb"
    path.write_text(PATH_BASE_PCB)
    pcb = PCB.load(str(path))
    net_classes = dict.fromkeys(("BRANCH", "VIAS", "DUP"), NET_CLASS)

    derive_group_skew_data(pcb, net_classes)

    assert (
        "[match-group-skew] DDR_DATA: net BRANCH total track length exceeds the selected "
        "pad-to-pad path length by 5.000 mm (off-path copper; no part of it is "
        "topologically isolable as defect, the measured length keeps it all)"
    ) in caplog.messages


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
    clean.  Every member is measured by the same #9304 rule -- both
    stubs exceed the epsilon, so both are shed, neither member's ruler
    differs from its siblings' and the 2 mm step an earlier per-member
    threshold injected into ``max - min`` is gone: the skew is the real
    0.5 mm spread -- and both stubs are reported.
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
    assert lengths[0] == pytest.approx(lengths[2] + 5.5, abs=0.05)  # premise

    with caplog.at_level(logging.WARNING, logger="kicad_tools.validate.match_group_skew"):
        skew_data, _g, _t = derive_group_skew_data(pcb, NET_CLASS_MAP)

    assert skew_data["DDR_DATA"] == pytest.approx(0.5, abs=0.05)
    stubs_reported = [
        r.getMessage() for r in caplog.records if r.getMessage().count("excluded") >= 1
    ]
    assert len(stubs_reported) == 2
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


# ---------------------------------------------------------------------------
# Reporting order vs the eligibility check (Issue #9726)
# ---------------------------------------------------------------------------

PAIR_NET_CLASS_MAP = dict.fromkeys(("CLK_P", "CLK_N", "DAT_P", "DAT_N"), NET_CLASS)


def _pair_board(tmp_path: Path, *, route_dat_n: bool = True) -> PCB:
    """Two differential pairs in DDR_DATA; loops on one leg per pair.

    Every leg is 20.0 mm pad-to-pad.  CLK_P carries a 20 mm same-net
    loop and DAT_N a 6 mm dead-end stub -- off-path copper above the
    reporting epsilon on exactly one leg of each pair, so a fully
    eligible group reports both pairs' qualifying legs.  With
    ``route_dat_n=False`` DAT_N has no geometry at all, so the group is
    omitted by the unrouted-leg gate (Issue #3916) while every other
    member keeps its off-path copper.
    """
    rows = {
        4: ("CLK_P", 10.0, 25.0),
        5: ("CLK_N", 20.0, 25.0),
        6: ("DAT_P", 30.0, 25.0),
        7: ("DAT_N", 40.0, 25.0),
    }
    header = HEADER.replace(
        '  (net 3 "DQ2")\n',
        '  (net 4 "CLK_P")\n  (net 5 "CLK_N")\n  (net 6 "DAT_P")\n  (net 7 "DAT_N")\n',
    )
    body = (
        _seg(5, 10, 25, 10, 4)
        + _seg(25, 10, 35, 10, 4)
        + _seg(35, 10, 25, 10, 4)  # 20 mm loop on CLK_P
        + _seg(5, 20, 25, 20, 5)
        + _seg(5, 30, 25, 30, 6)
    )
    if route_dat_n:
        body += _seg(5, 40, 25, 40, 7) + _seg(15, 40, 21, 40, 7)  # 6 mm stub on DAT_N
    return _write(tmp_path, body, rows, header=header)


def test_routed_pair_group_reports_both_qualifying_legs(tmp_path: Path, caplog):
    """A fully routed group visits both net IDs of each differential pair.

    Lane entries stay pair-averaged: CLK = (40 + 20) / 2 = 30 mm,
    DAT = (20 + 26) / 2 = 23 mm, so the skew is 7.0 mm with the default
    tolerance, while the off-path reporting names each qualifying leg
    (CLK_P's loop, DAT_N's stub) individually.
    """
    pcb = _pair_board(tmp_path)

    with caplog.at_level(logging.WARNING, logger="kicad_tools.validate.match_group_skew"):
        skew_data, _g, thresholds = derive_group_skew_data(pcb, PAIR_NET_CLASS_MAP)

    assert skew_data["DDR_DATA"] == pytest.approx(7.0, abs=0.05)
    assert thresholds["DDR_DATA"] == pytest.approx(0.5)
    messages = [r.getMessage() for r in caplog.records if "off-path" in r.getMessage()]
    assert len(messages) == 2
    assert any("CLK_P" in m and "20.0" in m for m in messages)
    assert any("DAT_N" in m and "6.0" in m for m in messages)


def test_group_skipped_for_unrouted_pair_leg_emits_no_warning(tmp_path: Path, caplog):
    """Issue #9726: an unrouted pair leg omits the group, so nothing is
    reported for its already-visited siblings either."""
    pcb = _pair_board(tmp_path, route_dat_n=False)

    with caplog.at_level(logging.WARNING, logger="kicad_tools.validate.match_group_skew"):
        skew_data, _g, thresholds = derive_group_skew_data(pcb, PAIR_NET_CLASS_MAP)

    assert skew_data == {}
    assert thresholds == {}
    assert not [r for r in caplog.records if "off-path" in r.getMessage()]
