"""Issue #6684: a stitch via dropped by a filter is never recorded.

``run_stitch`` used to drop placements with two filters that ran AFTER the
pad loop: the ``--avoid-pad-overlap`` via_in_pad filter (#3271) and the drill
hole-to-hole filter (#3855/#4177/#6289).  By then each dropped via had already
been

* appended to ``existing_vias``, which feeds ``is_pad_connected`` -- so a later
  same-net pad was counted "already connected" through copper that is never
  written (board 07: U4.E4 credited via U4.D5's via_in_pad-filtered via);
* recorded in the cross-net obstacle pool, so later foreign-net pads avoided
  phantom copper;
* counted in ``micro_vias_placed``.

The keep-or-drop decision is now taken per placement, before any of that.
"""

from __future__ import annotations

from pathlib import Path

from kicad_tools.cli.stitch_cmd import run_stitch

from .test_stitch_cmd import PAD_OVERLAP_TEST_PCB
from .test_stitch_mfr import TWO_LAYER_GND_ZONE_PCB

VIA = 0.6
DRILL = 0.3
H2H = 0.5

# A pre-existing GND via with a 0.8 mm drill 0.9 mm below C1.1's first-choice
# stitch site (109.49, 110.82).  The per-candidate guard only demands
# ``min_hole_to_hole + candidate_drill`` (0.8 mm) centre-to-centre from a
# same-net via, so the site is chosen; the drill-aware commit filter then
# measures 0.9 - 0.15 - 0.4 = 0.35 mm < 0.5 mm and drops it.  The via is
# 1.72 mm from C1.1's centre, too far to count as connecting C1.1 itself.
BIG_GND_VIA = (
    '  (via (at 109.49 111.72) (size 1.2) (drill 0.8) (layers "F.Cu" "B.Cu") '
    '(net 1) (uuid "00000000-0000-0000-0000-000000006684"))\n'
)
C1_SITE = (109.49, 110.82)


def _smd(ref: str, x: float, y: float, net: int, name: str, uuid: str) -> str:
    return (
        f'  (footprint "R" (layer "F.Cu") (uuid "00000000-0000-0000-0000-{uuid}") '
        f"(at {x} {y})\n"
        f'    (property "Reference" "{ref}" (at 0 -1.5 0) (layer "F.SilkS") '
        f'(uuid "ref-{ref}"))\n'
        f'    (pad "1" smd roundrect (at 0 0) (size 0.54 0.64) '
        f'(layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) '
        f'(net {net} "{name}")))\n'
    )


# R9 (GND) sits 0.71 mm from C1.1's would-be via: inside the
# ``0.5 + max(w, h) / 2`` = 0.82 mm proximity radius of ``is_pad_connected``.
R9_NEAR_C1_SITE = _smd("R9", 110.2, 110.82, 1, "GND", "000000000200")

# R7 (VCC) asks for a reserved site 0.76 mm from C1.1's would-be via -- inside
# that via's copper halo (0.3 + 0.3 + 0.2 clearance = 0.8 mm), clear of all
# copper that is actually written.
R7_VCC = _smd("R7", 111.07, 110.82, 2, "VCC", "000000000300") + (
    '  (zone (net 2) (net_name "VCC") (layer "F.Cu") (uuid "zone-2") (hatch edge 0.5) '
    "(connect_pads (clearance 0.2)) (min_thickness 0.2) (filled_areas_thickness no) "
    "(fill yes (thermal_gap 0.2) (thermal_bridge_width 0.3)) "
    "(polygon (pts (xy 130 130) (xy 139 130) (xy 139 139) (xy 130 139))))\n"
)
R7_PREFERRED = (110.25, 110.82)


def _write(tmp_path: Path, base: str, extra: str = "", name: str = "board") -> Path:
    text = base.rstrip()
    assert text.endswith(")")
    text = text[:-1] + extra + ")\n"
    if '(net 2 "VCC")' in extra:
        text = text.replace('(net 1 "GND")\n', '(net 1 "GND")\n  (net 2 "VCC")\n', 1)
    if '(net 2 "SIG")' in extra or "(net 2) (uuid" in extra:
        text = text.replace('(net 1 "GND")\n', '(net 1 "GND")\n  (net 2 "SIG")\n', 1)
    pcb = tmp_path / f"{name}.kicad_pcb"
    pcb.write_text(text)
    return pcb


def _h2h_stitch(pcb: Path, nets: list[str] | None = None, **kw):
    return run_stitch(
        pcb,
        nets or ["GND"],
        via_size=VIA,
        drill=DRILL,
        dry_run=True,
        min_hole_to_hole=H2H,
        **kw,
    )


def _refs(result) -> list[str]:
    return [f"{v.pad.reference}.{v.pad.pad_number}" for v in result.vias_added]


def _skipped(result) -> dict[str, str]:
    return {f"{p.reference}.{p.pad_number}": reason for p, reason in result.pads_skipped}


def _assert_consistent(result) -> None:
    """Every count describes only the vias that are actually written."""
    assert len(result.vias_added) == len(result.traces_added)
    for via, trace in zip(result.vias_added, result.traces_added, strict=True):
        assert via.pad is trace.pad
    assert result.micro_vias_placed == sum(v.via_type == "micro" for v in result.vias_added)


# ---------------------------------------------------------------------------
# via_in_pad filter (#3271)
# ---------------------------------------------------------------------------


def test_via_in_pad_drop_does_not_credit_neighbour(tmp_path: Path) -> None:
    """U1.1's via lands in U1.2 and is dropped; U1.2 must not be credited by it.

    The board 07 U4.D5 / U4.E4 shape: D5's placement is filtered as
    via_in_pad, and E4 -- the neighbouring same-net pad the dropped drill sat
    in -- was counted ``already_connected`` and got no via.
    """
    result = run_stitch(
        _write(tmp_path, PAD_OVERLAP_TEST_PCB), ["GND"], dry_run=True, avoid_pad_overlap=True
    )

    assert result.via_in_pad_filtered == 1
    assert result.already_connected == 0
    skipped = _skipped(result)
    assert skipped["U1.1"].startswith("via_in_pad: standard placement would land drill")
    # U1.2 gets its own via (or, failing that, its own skip entry).
    assert "U1.2" in _refs(result) or "U1.2" in skipped
    assert _refs(result) == ["U1.2"]
    _assert_consistent(result)


def test_via_in_pad_keep_is_unchanged_without_the_flag(tmp_path: Path) -> None:
    """No-drop control: without the filter U1.1's via is kept and does connect U1.2."""
    result = run_stitch(_write(tmp_path, PAD_OVERLAP_TEST_PCB), ["GND"], dry_run=True)

    assert result.via_in_pad_filtered == 0
    assert result.already_connected == 1
    assert _refs(result) == ["U1.1"]
    assert (result.vias_added[0].via_x, result.vias_added[0].via_y) == (110.8, 110.0)
    assert result.pads_skipped == []
    _assert_consistent(result)


def test_dropped_micro_via_is_not_counted(tmp_path: Path) -> None:
    """A dropped micro via does not inflate ``micro_vias_placed``.

    Two foreign vias leave no room for a 5 mm standard via within a 1 mm
    escape, so U1.1 gets a micro via -- inside U1.2, where via_in_pad drops it.
    """
    blockers = "".join(
        f'  (via (at 110 {y}) (size 0.4) (drill 0.2) (layers "F.Cu" "B.Cu") '
        f'(net 2) (uuid "00000000-0000-0000-0000-00000000077{i}"))\n'
        for i, y in enumerate((112, 108))
    )
    pcb = _write(tmp_path, PAD_OVERLAP_TEST_PCB, blockers)
    kw = {"dry_run": True, "micro_via": True, "via_size": 5.0, "escape_distance": 1.0}

    # Control: without the filter the micro via is kept and counted.
    kept = run_stitch(pcb, ["GND"], **kw)
    assert kept.micro_vias_placed == 1
    assert [v.via_type for v in kept.vias_added] == ["micro"]

    dropped = run_stitch(pcb, ["GND"], avoid_pad_overlap=True, **kw)
    assert dropped.via_in_pad_filtered == 1
    assert dropped.micro_vias_placed == 0
    assert "U1.1" not in _refs(dropped)
    _assert_consistent(dropped)


# ---------------------------------------------------------------------------
# hole-to-hole filter (#3855/#4177/#6289)
# ---------------------------------------------------------------------------


def test_hole_to_hole_drop_does_not_credit_neighbour(tmp_path: Path) -> None:
    """C1.1's via is dropped for hole-to-hole; R9 must not be credited by it."""
    pcb = _write(tmp_path, TWO_LAYER_GND_ZONE_PCB, BIG_GND_VIA + R9_NEAR_C1_SITE)
    result = _h2h_stitch(pcb)

    assert result.hole_to_hole_rejected == 1
    assert _skipped(result)["C1.1"].startswith(
        f"hole_to_hole: stitch via drill within {H2H:.2f}mm of an existing drilled hole"
    )
    assert result.already_connected == 0
    assert _refs(result) == ["R9.1"]
    _assert_consistent(result)


def test_hole_to_hole_keep_still_credits_neighbour(tmp_path: Path) -> None:
    """No-drop control: with C1.1's via kept, R9 is still credited by it."""
    pcb = _write(tmp_path, TWO_LAYER_GND_ZONE_PCB, R9_NEAR_C1_SITE)
    result = _h2h_stitch(pcb)

    assert result.hole_to_hole_rejected == 0
    assert result.already_connected == 1
    assert _refs(result) == ["C1.1"]
    assert (result.vias_added[0].via_x, result.vias_added[0].via_y) == C1_SITE
    assert result.pads_skipped == []
    _assert_consistent(result)


def test_dropped_via_is_not_a_cross_net_obstacle(tmp_path: Path) -> None:
    """A later foreign-net pad may use a site only the dropped via's halo covered."""
    pcb = _write(tmp_path, TWO_LAYER_GND_ZONE_PCB, BIG_GND_VIA + R7_VCC)
    preferred = {"R7.1": [R7_PREFERRED]}

    alone = _h2h_stitch(pcb, ["VCC"], preferred_sites=preferred)
    assert [(v.via_x, v.via_y) for v in alone.vias_added] == [R7_PREFERRED]

    both = _h2h_stitch(pcb, ["GND", "VCC"], preferred_sites=preferred)
    assert both.hole_to_hole_rejected == 1
    assert "C1.1" in _skipped(both)
    # GND's dropped via must not push VCC off its reserved site.
    assert [(v.pad.reference, v.via_x, v.via_y) for v in both.vias_added] == [("R7", *R7_PREFERRED)]
    _assert_consistent(both)
