"""Tests for the independent copper-extracted LVS gate (issue #3742).

This gate is the "third leg" of board soundness: unlike the label-based
:func:`kicad_tools.lvs.board_lvs.compare_netlists`, it ignores every pad's
declared ``(net ...)`` label and diffs the *physical* copper partition
against the schematic.  The crux test is the 90°/270° regression fixture:
adversarial copper that shorts two nets must FAIL, while corrected copper
that connects same-net pads must PASS — proving "passes DRC" now implies
"electrically correct" independent of the shared pad-rotation convention.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from kicad_tools.core.geometry import rotate_pad_offset
from kicad_tools.lvs import (
    CopperLVSResult,
    compare_copper_netlist,
    compare_partitions,
)
from kicad_tools.validate.connectivity import ConnectivityValidator

# ---------------------------------------------------------------------------
# Pure partition-diff unit tests (compare_partitions)
# ---------------------------------------------------------------------------


def test_clean_board_no_mismatches() -> None:
    """Same-net pads together, different-net pads apart -> clean."""
    sch = {
        ("R1", "1"): "VCC",
        ("R1", "2"): "LED_ANODE",
        ("D1", "1"): "LED_ANODE",
        ("D1", "2"): "GND",
    }
    # Copper joins R1.2<->D1.1 (LED_ANODE) and leaves VCC / GND isolated.
    partition = [
        frozenset({"R1.1"}),
        frozenset({"R1.2", "D1.1"}),
        frozenset({"D1.2"}),
    ]
    result = compare_partitions(sch, partition)
    assert result.clean
    assert result.mismatches == ()


def test_short_detected_when_two_nets_share_a_copper_island() -> None:
    """Different schematic nets fused in one copper component -> short."""
    sch = {
        ("R1", "1"): "VCC",
        ("R1", "2"): "LED_ANODE",
        ("D1", "1"): "GND",
    }
    # Copper fuses LED_ANODE (R1.2) with GND (D1.1) — the board-00 bug.
    partition = [
        frozenset({"R1.1"}),
        frozenset({"R1.2", "D1.1"}),
    ]
    result = compare_partitions(sch, partition)
    assert not result.clean
    assert len(result.shorts) == 1
    assert result.opens == ()
    short = result.shorts[0]
    assert {short.net_a, short.net_b} == {"GND", "LED_ANODE"}
    assert {short.pad_a, short.pad_b} == {"R1.2", "D1.1"}


def test_open_detected_when_same_net_splits_across_islands() -> None:
    """Same schematic net split across copper components -> open."""
    sch = {
        ("R1", "1"): "NET1",
        ("R2", "1"): "NET1",
    }
    # The two NET1 pads land in different copper islands (unrouted).
    partition = [
        frozenset({"R1.1"}),
        frozenset({"R2.1"}),
    ]
    result = compare_partitions(sch, partition)
    assert not result.clean
    assert len(result.opens) == 1
    assert result.shorts == ()
    open_rec = result.opens[0]
    assert open_rec.net_a == open_rec.net_b == "NET1"
    assert {open_rec.pad_a, open_rec.pad_b} == {"R1.1", "R2.1"}


def test_floating_schematic_pin_excluded_from_diff() -> None:
    """A pin with no schematic net (None) is ignored, not flagged."""
    sch = {
        ("R1", "1"): "VCC",
        ("R1", "2"): None,  # floating in schematic
    }
    partition = [frozenset({"R1.1", "R1.2"})]
    # R1.2 is floating, so the shared copper island is not a short.
    result = compare_partitions(sch, partition)
    assert result.clean


def test_pcb_only_pad_does_not_crash_or_flag() -> None:
    """A pad on the PCB but absent from the schematic is ignored here."""
    sch = {("R1", "1"): "VCC"}
    partition = [frozenset({"R1.1", "TP1.1"})]  # TP1 not in schematic
    result = compare_partitions(sch, partition)
    assert result.clean


def test_schematic_only_pad_does_not_crash() -> None:
    """A schematic pin with no PCB pad is ignored (label path's concern)."""
    sch = {
        ("R1", "1"): "VCC",
        ("R9", "1"): "VCC",  # not on the board
    }
    partition = [frozenset({"R1.1"})]
    result = compare_partitions(sch, partition)
    # Only one VCC pad is on the board, so no open is reported.
    assert result.clean


def test_multi_pad_power_net_one_island_is_clean() -> None:
    """A power net spanning many pads in one copper island is clean."""
    sch = {
        ("C1", "1"): "GND",
        ("C2", "1"): "GND",
        ("U1", "5"): "GND",
    }
    partition = [frozenset({"C1.1", "C2.1", "U1.5"})]
    result = compare_partitions(sch, partition)
    assert result.clean


def test_short_reported_once_per_net_pair() -> None:
    """Three pads of three nets in one island -> 3 unique short pairs."""
    sch = {
        ("A", "1"): "N1",
        ("B", "1"): "N2",
        ("C", "1"): "N3",
    }
    partition = [frozenset({"A.1", "B.1", "C.1"})]
    result = compare_partitions(sch, partition)
    pairs = {frozenset({m.net_a, m.net_b}) for m in result.shorts}
    assert pairs == {
        frozenset({"N1", "N2"}),
        frozenset({"N1", "N3"}),
        frozenset({"N2", "N3"}),
    }


# ---------------------------------------------------------------------------
# 90°/270° regression fixture — the crux test (issue #3742)
# ---------------------------------------------------------------------------
#
# A two-pad footprint at 90°.  Pad "1" sits at local (-1, 0), pad "2" at
# local (+1, 0).  Under the CORRECT (current, #3739-fixed) transform a 90°
# footprint maps local x -> board -y: pad "1" -> (origin_x, origin_y+1),
# pad "2" -> (origin_x, origin_y-1).  Under the OLD standard-CCW transform
# the signs flip (local x -> board +y), mirroring pad 1 and pad 2.
#
# We place TWO 90° footprints and route copper between specific *physical*
# board points.  The same copper connects different *pads* depending on
# which transform is used — exactly the convention coupling the gate must
# expose.

# The schematic side is exercised end-to-end by the committed board-00
# artifacts (tests/test_board_00_lvs.py).  Here we drive the partition diff
# with an explicit schematic mapping so the regression isolates the *copper
# extraction* under the 90° transform — the part the gate adds — without
# depending on synthetic schematic-wire geometry resolution.
SCHEMATIC_90_NETS: dict[tuple[str, str], str | None] = {
    ("R1", "1"): "SIG",
    ("R1", "2"): "OUT1",
    ("R2", "1"): "SIG",
    ("R2", "2"): "OUT2",
}


def _pcb_90(seg_start: tuple[float, float], seg_end: tuple[float, float]) -> str:
    """Build a PCB with two 90° footprints and a single routed segment.

    Both footprints use local pad offsets ``"1"`` at (-1, 0) and ``"2"`` at
    (+1, 0), placed at 90°.  The single copper segment runs between the two
    given board points.  Pad labels are deliberately written *correctly*
    (the router claims the right nets) so that only a copper-extracted gate
    — not the label-based one — can catch a physical mis-route.
    """
    (sx, sy), (ex, ey) = seg_start, seg_end
    return f"""(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "SIG")
  (net 2 "OUT1")
  (net 3 "OUT2")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000b1")
    (at 100 100 90)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-r1-ref"))
    (property "Value" "1k" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-r1-val"))
    (pad "1" smd roundrect (at -1 0) (size 0.6 0.6) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net 1 "SIG"))
    (pad "2" smd roundrect (at 1 0) (size 0.6 0.6) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net 2 "OUT1"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000b2")
    (at 120 100 90)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-r2-ref"))
    (property "Value" "1k" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-r2-val"))
    (pad "1" smd roundrect (at -1 0) (size 0.6 0.6) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net 1 "SIG"))
    (pad "2" smd roundrect (at 1 0) (size 0.6 0.6) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net 3 "OUT2"))
  )
  (segment (start {sx} {sy}) (end {ex} {ey}) (width 0.25) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-0000000000c1"))
)
"""


def _board_positions() -> dict[str, tuple[float, float]]:
    """Compute the board-frame pad positions under the *current* transform."""
    pos: dict[str, tuple[float, float]] = {}
    for ref, (ox, oy) in (("R1", (100.0, 100.0)), ("R2", (120.0, 100.0))):
        for pad, local in (("1", (-1.0, 0.0)), ("2", (1.0, 0.0))):
            rx, ry = rotate_pad_offset(local[0], local[1], 90.0)
            pos[f"{ref}.{pad}"] = (ox + rx, oy + ry)
    return pos


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content)
    return p


def _old_ccw_mirror(ref_origin: tuple[float, float]) -> tuple[float, float]:
    """Board point the OLD (pre-#3739) CCW transform gave pad "1" at 90°.

    The pre-fix standard-CCW form mapped local x -> board +y (mirror of the
    #3739-fixed -y).  For pad "1" at local (-1, 0) on a 90° footprint this
    lands on the *opposite* side from the corrected position.
    """
    a = math.radians(90.0)
    rx = -1.0 * math.cos(a) - 0.0 * math.sin(a)
    ry = -1.0 * math.sin(a) + 0.0 * math.cos(a)
    return (ref_origin[0] + rx, ref_origin[1] + ry)


def test_90deg_corrected_copper_passes(tmp_path: Path) -> None:
    """Copper routed between the true SIG pads (R1.1, R2.1) -> clean.

    Under the current (#3739-fixed) transform, pad "1" at local (-1, 0) on a
    90° footprint maps to board +y of its origin: R1.1 = (100, 101),
    R2.1 = (120, 101).  A segment between those two physical points connects
    the two SIG pads, matching the schematic, so the copper-extracted gate
    must pass.
    """
    pos = _board_positions()
    pcb_path = _write(tmp_path, "b.kicad_pcb", _pcb_90(pos["R1.1"], pos["R2.1"]))

    partition = ConnectivityValidator(pcb_path).extract_pad_partition()
    result = compare_partitions(SCHEMATIC_90_NETS, partition)
    assert result.clean, f"expected clean, got {result.mismatches}"


def test_90deg_adversarial_copper_fails(tmp_path: Path) -> None:
    """Copper routed to the *mirrored* pads shorts/opens the SIG net.

    The adversarial segment runs between the board points the OLD
    (pre-#3739) CCW transform would have computed for the SIG pads.  Under
    the correct transform those points physically coincide with the *other*
    (OUT) pads R1.2 / R2.2, so the copper connects the wrong pads.  The
    copper-extracted gate must FAIL — SIG is left open (its true pads not
    joined) and/or OUT1<->OUT2 are wrongly fused — even though the pad
    labels were written correctly (a label-based check stays blind).
    """
    seg_start = _old_ccw_mirror((100.0, 100.0))  # where OLD transform put R1.1
    seg_end = _old_ccw_mirror((120.0, 100.0))  # where OLD transform put R2.1
    pcb_path = _write(tmp_path, "b.kicad_pcb", _pcb_90(seg_start, seg_end))

    partition = ConnectivityValidator(pcb_path).extract_pad_partition()
    result = compare_partitions(SCHEMATIC_90_NETS, partition)
    assert not result.clean, (
        "adversarial copper routed to mirrored pads must be flagged, "
        f"but the partition diff was clean; partition={partition}"
    )
    # The mirrored segment physically lands on R1.2/R2.2 (OUT1/OUT2), so it
    # shorts OUT1<->OUT2 and leaves SIG (R1.1/R2.1) open.
    short_pairs = {frozenset({m.net_a, m.net_b}) for m in result.shorts}
    assert frozenset({"OUT1", "OUT2"}) in short_pairs
    assert any(m.net_a == "SIG" for m in result.opens)


def test_90deg_label_based_lvs_is_blind_to_the_misroute(tmp_path: Path) -> None:
    """The label-based comparator passes adversarial copper (motivation).

    Documents *why* the copper-extracted gate is needed: the pad labels are
    written correctly, so ``_pcb_pin_to_net`` reads SIG/OUT exactly as the
    schematic expects regardless of where the copper physically runs.
    """
    from kicad_tools.lvs.board_lvs import _pcb_pin_to_net

    pcb_path = _write(
        tmp_path,
        "b.kicad_pcb",
        _pcb_90(_old_ccw_mirror((100.0, 100.0)), _old_ccw_mirror((120.0, 100.0))),
    )

    # Label-based view: pads report their declared nets, which match the
    # schematic — the very blind spot #3742 closes.
    pcb_labels = _pcb_pin_to_net(pcb_path)
    assert pcb_labels[("R1", "1")] == "SIG"
    assert pcb_labels[("R2", "1")] == "SIG"
    assert pcb_labels[("R1", "2")] == "OUT1"
    assert pcb_labels[("R2", "2")] == "OUT2"


# ---------------------------------------------------------------------------
# Independent geometry reference: 90° pad geometry vs golden (decoupling)
# ---------------------------------------------------------------------------
#
# The gate's *correctness* must not silently rely on rotate_pad_offset being
# right.  This asserts the extractor's 90°/270° pad geometry against an
# independent hand-computed golden (the values pcbnew 10.0.1 reports, per
# rotate_pad_offset's own docstring table).  A live kicad-cli cross-check is
# a documented follow-up; the golden keeps the decoupling assertion running
# in CI without a KiCad install.

# Footprint at (100, 100), pad local offset (2, 0), per the pcbnew-verified
# table in core.geometry.rotate_pad_offset:
#   0   -> (102, 100)
#   90  -> (100,  98)
#   180 -> ( 98, 100)
#   270 -> (100, 102)
GOLDEN_PAD_GEOMETRY = {
    0.0: (102.0, 100.0),
    90.0: (100.0, 98.0),
    180.0: (98.0, 100.0),
    270.0: (100.0, 102.0),
}


@pytest.mark.parametrize("rotation", [0.0, 90.0, 180.0, 270.0])
def test_transform_pad_position_matches_golden(rotation: float) -> None:
    """_transform_pad_position must match the pcbnew-verified golden.

    This is the decoupling guard (issue #3742): if a future refactor
    reintroduces a coordinate-convention bug in the shared transform, the
    90°/270° rows of this golden break, flagging that the copper-LVS gate's
    pad geometry has drifted from KiCad's ground truth.
    """
    # ConnectivityValidator needs a PCB; build a trivial one in-memory.
    validator = object.__new__(ConnectivityValidator)
    bx, by = validator._transform_pad_position((2.0, 0.0), 100.0, 100.0, rotation)
    gx, gy = GOLDEN_PAD_GEOMETRY[rotation]
    assert bx == pytest.approx(gx, abs=1e-6)
    assert by == pytest.approx(gy, abs=1e-6)


def test_extract_pad_partition_ignores_pad_labels(tmp_path: Path) -> None:
    """The extractor must not consult (net ...) labels at all.

    Two pads physically joined by copper land in one component even when
    their declared labels differ; two pads with the *same* label but no
    copper between them stay in separate components.
    """
    pcb = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "A")
  (net 2 "B")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000d1")
    (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-d1-ref"))
    (property "Value" "1k" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-d1-val"))
    (pad "1" smd roundrect (at 0 0) (size 0.6 0.6) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net 1 "A"))
    (pad "2" smd roundrect (at 2 0) (size 0.6 0.6) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net 2 "B"))
  )
  (segment (start 100 100) (end 102 100) (width 0.25) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-0000000000d2"))
)
"""
    pcb_path = _write(tmp_path, "b.kicad_pcb", pcb)
    partition = ConnectivityValidator(pcb_path).extract_pad_partition()
    # R1.1 (label A) and R1.2 (label B) are joined by copper despite the
    # differing labels -> one component.
    assert frozenset({"R1.1", "R1.2"}) in partition
    assert len(partition) == 1


def test_extract_pad_partition_unconnected_pad_is_singleton(tmp_path: Path) -> None:
    """A pad with no copper touching it forms its own component."""
    pcb = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000e1")
    (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-e1-ref"))
    (property "Value" "1k" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-e1-val"))
    (pad "1" smd roundrect (at 0 0) (size 0.6 0.6) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25))
    (pad "2" smd roundrect (at 2 0) (size 0.6 0.6) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25))
  )
)
"""
    pcb_path = _write(tmp_path, "b.kicad_pcb", pcb)
    partition = ConnectivityValidator(pcb_path).extract_pad_partition()
    assert frozenset({"R1.1"}) in partition
    assert frozenset({"R1.2"}) in partition
    assert len(partition) == 2


def test_compare_partitions_returns_result_type() -> None:
    """Smoke: the pure partition diff returns a CopperLVSResult."""
    result = compare_partitions({("R1", "1"): "VCC"}, [frozenset({"R1.1"})])
    assert isinstance(result, CopperLVSResult)


def test_compare_copper_netlist_on_board00_artifacts() -> None:
    """End-to-end: the file-level entry point runs clean on board 00.

    Exercises the real schematic-side resolution (``_schematic_pin_to_net``)
    plus copper extraction against the committed board-00 artifacts, so the
    full ``compare_copper_netlist`` path is covered end-to-end.  Skips when
    the generated artifacts are absent (same policy as test_board_00_lvs).
    """
    repo_root = Path(__file__).resolve().parent.parent
    board_out = repo_root / "boards" / "00-simple-led" / "output"
    sch = board_out / "simple_led.kicad_sch"
    pcb = board_out / "simple_led_routed.kicad_pcb"
    if not (sch.exists() and pcb.exists()):
        pytest.skip("board 00 artifacts not present; run generate_design.py")
    result = compare_copper_netlist(sch, pcb)
    assert isinstance(result, CopperLVSResult)
    assert result.clean, f"board 00 copper LVS unexpectedly dirty: {result.mismatches}"
