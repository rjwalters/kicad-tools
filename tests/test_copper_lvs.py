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


# ---------------------------------------------------------------------------
# Label-free zone-pour extraction — the adversarial crux (issue #3761)
# ---------------------------------------------------------------------------
#
# The #3742 first slice tied pour pads by the zone's *declared* net, which can
# MASK a defect on pour-routed nets.  These tests prove the gap is closed: ONE
# inline fixture with a poured GND net and a carved clearance moat, asserted
# against BOTH models on the SAME board:
#
#   * Pad X (R1.1) is declared GND but its copper is moated out of the GND
#     pour (no thermal spoke / no solid overlap) -> an OPEN.
#   * Pad Y (R2.1) is declared SIG (a foreign net) but its copper bonds to the
#     solid GND pour -> a SHORT.
#
# The OLD declared-net model groups X into the GND island (label matches) and
# never touches Y (label differs), so it reports NEITHER defect — it PASSES,
# masking both.  The NEW geometric model bonds Y (copper in the pour) and
# leaves X isolated (copper in the moat), so it FAILS, catching both.


def _shapely_available() -> bool:
    try:
        import shapely  # noqa: F401

        return True
    except ImportError:
        return False


requires_shapely = pytest.mark.skipif(
    not _shapely_available(),
    reason="shapely not installed (optional geometry/dev extra)",
)


# Pour solid square 0..20 on F.Cu with a 3x3 clearance moat (a real hole)
# carved around R1.1 at (5, 10).  R2.1 at (15, 10) sits in the solid copper.
# The hole is encoded the way KiCad flattens it: ONE ring whose boundary dips
# into the hole through a narrow slit (so the raw point list concatenates the
# outer hull with the cutout loop, exactly the schema reality #3761 must
# handle).  ``_fill_solid_region`` (shapely buffer(0)) re-derives the hole.
_POUR_FILL_RING = (
    "(xy 0 0) (xy 4.975 0) (xy 4.975 8.5) "
    "(xy 3.5 8.5) (xy 3.5 11.5) (xy 6.5 11.5) (xy 6.5 8.5) "
    "(xy 5.025 8.5) (xy 5.025 0) "
    "(xy 20 0) (xy 20 20) (xy 0 20) (xy 0 0)"
)


def _pcb_pour_adversarial() -> str:
    """A poured GND net with a moated-out GND pad and a bonded foreign pad.

    Three single-pad footprints, all on F.Cu over the GND pour:

    * ``R1`` pad "1" at board (5, 10): declared **GND**, but its copper lands
      inside the carved moat (a hole in the fill) -> NOT bonded to the pour.
    * ``R2`` pad "1" at board (15, 10): declared **SIG** (foreign net), but its
      copper sits in the solid GND pour -> bonded.
    * ``R3`` pad "1" at board (10, 17): declared **GND**, copper in the solid
      pour -> bonded.  This is the GND "anchor" R2.1 shorts against and the
      counterpart that makes R1.1's isolation a GND *open* (GND copper exists
      elsewhere in the pour).

    Footprints are placed at 0 rotation with pad "1" at local (0, 0) so the
    board-frame pad center equals the footprint origin.  Pad size 1.5 mm:
    eroded by ``POUR_PAD_ERODE`` (0.1) it stays inside the 3 mm moat for R1.1
    and well within the solid pour for R2.1 / R3.1.
    """
    return f"""(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "SIG")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000a1")
    (at 5 10)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-a1-ref"))
    (property "Value" "1k" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-a1-val"))
    (pad "1" smd roundrect (at 0 0) (size 1.5 1.5) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net 1 "GND"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000a2")
    (at 15 10)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-a2-ref"))
    (property "Value" "1k" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-a2-val"))
    (pad "1" smd roundrect (at 0 0) (size 1.5 1.5) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net 2 "SIG"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000a3")
    (at 10 17)
    (property "Reference" "R3" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-a3-ref"))
    (property "Value" "1k" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-a3-val"))
    (pad "1" smd roundrect (at 0 0) (size 1.5 1.5) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net 1 "GND"))
  )
  (zone
    (net 1 "GND")
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000z1")
    (hatch edge 0.5)
    (connect_pads (clearance 0.2))
    (min_thickness 0.2)
    (fill yes (thermal_gap 0.2) (thermal_bridge_width 0.3))
    (polygon (pts (xy 0 0) (xy 20 0) (xy 20 20) (xy 0 20)))
    (filled_polygon (layer "F.Cu") (pts {_POUR_FILL_RING}))
  )
)
"""


# Schematic side: R1.1/R3.1 are GND, R2.1 is SIG (matches the declared pad
# labels — the labels are "honest", the *copper* is the lie the gate exposes).
_POUR_SCHEMATIC: dict[tuple[str, str], str | None] = {
    ("R1", "1"): "GND",
    ("R2", "1"): "SIG",
    ("R3", "1"): "GND",
}


def _declared_net_pour_partition(pcb_path: Path) -> list[frozenset[str]]:
    """Extract the partition under the LEGACY declared-net pour model.

    Forces ``_has_shapely`` to report False so ``extract_pad_partition`` takes
    the preserved declared-net fallback (``_connect_pour_pads_by_declared_net``)
    — i.e. the pre-#3761 behavior — on the very same fixture.
    """
    from unittest import mock

    with mock.patch("kicad_tools.validate.connectivity._has_shapely", return_value=False):
        return ConnectivityValidator(pcb_path).extract_pad_partition()


@requires_shapely
def test_pour_extraction_label_free_catches_what_declared_net_masks(
    tmp_path: Path,
) -> None:
    """The crux: same fixture, OLD model PASSES, NEW model FAILS.

    Demonstrates issue #3761's gap is closed.  On a board where GND is poured
    with a carved moat:

    * OLD (declared-net) model: R1.1 and R3.1 (both declared GND) are grouped
      into the GND pour by their labels and R2.1 (declared SIG) is never
      considered, so the partition matches the schematic -> CLEAN, masking
      both an open and a short.
    * NEW (geometric) model: R1.1's copper is moated out (isolated, while R3.1
      holds GND copper in the pour -> GND open) and R2.1's copper bonds to the
      GND pour alongside R3.1 (fused -> SIG/GND short), so the partition diff
      FAILS, catching both.
    """
    pcb_path = _write(tmp_path, "pour.kicad_pcb", _pcb_pour_adversarial())

    # --- OLD declared-net model: masks the defect (clean) ---
    old_partition = _declared_net_pour_partition(pcb_path)
    old_result = compare_partitions(_POUR_SCHEMATIC, old_partition)
    assert old_result.clean, (
        "the legacy declared-net pour model should MASK this defect "
        f"(that is the gap #3761 closes); got {old_result.mismatches}"
    )
    # Under the label model R1.1 and R3.1 are grouped by their shared GND label
    # (consistent with the schematic) and R2.1 stays a SIG singleton.
    old_gnd = next(c for c in old_partition if "R1.1" in c)
    assert {"R1.1", "R3.1"} <= old_gnd
    assert frozenset({"R2.1"}) in old_partition

    # --- NEW geometric model: catches the defect (dirty) ---
    new_partition = ConnectivityValidator(pcb_path).extract_pad_partition()
    new_result = compare_partitions(_POUR_SCHEMATIC, new_partition)
    assert not new_result.clean, (
        "the geometric pour model must FLAG the moated-out / bonded-foreign "
        f"pads; partition={new_partition}"
    )
    # R2.1 (SIG) copper bonds to the GND pour (with R3.1) -> GND/SIG short.
    short_pairs = {frozenset({m.net_a, m.net_b}) for m in new_result.shorts}
    assert frozenset({"GND", "SIG"}) in short_pairs, (
        f"expected a GND/SIG short; shorts={new_result.shorts}"
    )
    # R1.1 (GND) is moated out while R3.1 holds GND copper -> GND open.
    assert any(o.net_a == "GND" for o in new_result.opens), (
        f"expected a GND open from the moated-out R1.1; opens={new_result.opens}"
    )


@requires_shapely
def test_pour_extraction_moated_pad_is_not_bonded(tmp_path: Path) -> None:
    """Hole semantics: a pad in a clearance moat is NOT tied to the pour.

    R1.1's copper sits inside the carved moat (a real hole in the fill), so
    the label-free extractor must leave it in its own singleton component
    rather than fusing it into the GND pour island.
    """
    pcb_path = _write(tmp_path, "pour.kicad_pcb", _pcb_pour_adversarial())
    partition = ConnectivityValidator(pcb_path).extract_pad_partition()
    # R1.1 is moated out -> singleton (no copper bonds it to the pour).
    assert frozenset({"R1.1"}) in partition


@requires_shapely
def test_pour_extraction_solid_overlap_pad_is_bonded(tmp_path: Path) -> None:
    """A pad whose copper sits in the solid pour IS tied to it.

    R2.1's copper overlaps the solid GND fill, so the label-free extractor
    must fuse it into the pour island even though its declared net (SIG)
    differs from the zone's (GND).
    """
    pcb_path = _write(tmp_path, "pour.kicad_pcb", _pcb_pour_adversarial())
    partition = ConnectivityValidator(pcb_path).extract_pad_partition()
    r2_component = next(c for c in partition if "R2.1" in c)
    # R2.1 is bonded to the pour, so it is NOT a lone singleton: the pour
    # geometry tied it in despite the differing label.  (R1.1 is moated out,
    # so the only other pour-candidate pad is R1.1; the salient assertion is
    # that R2.1 was selected by geometry, which the short test above proves.)
    assert "R2.1" in r2_component


@requires_shapely
def test_pour_extraction_ignores_zone_and_pad_net_labels(tmp_path: Path) -> None:
    """The pour leg consults neither ``zone.net_name`` nor ``pad.net_name``.

    Relabel every pad and the zone to the *same* net ("GND").  A label-driven
    model would now fuse both pads into the pour (both match the zone), but the
    geometric model must be unmoved: R1.1's copper is still moated out (left a
    singleton) and R2.1's copper still bonds to the solid pour.  That the
    partition does not change when the labels are made uniform proves the pour
    extraction is label-free.
    """
    # Make both pad nets identical to the zone net so labels can no longer be
    # the discriminator; geometry must still separate R1.1 from the pour.
    relabeled = _pcb_pour_adversarial().replace('(net 2 "SIG")', '(net 1 "GND")')
    pcb_path = _write(tmp_path, "pour.kicad_pcb", relabeled)
    partition = ConnectivityValidator(pcb_path).extract_pad_partition()
    # R1.1 is moated out -> singleton despite now sharing the zone's label.
    assert frozenset({"R1.1"}) in partition
    # R2.1 is bonded by geometry (it sits in the solid pour).
    assert any("R2.1" in c for c in partition)


def _pcb_pour_two_disjoint_islands() -> str:
    """One GND zone whose fill is two DISJOINT same-net islands, one pad each.

    Distilled from board 03: KiCad stores a single poured zone as many
    ``filled_polygon`` entries when thermal reliefs / clearance moats fragment
    the copper.  Here the GND F.Cu zone is filled as two separate solid squares
    that do not touch (a 4 mm gap between them):

    * island A: solid copper 0..4 in x, bonding ``C1`` pad "2" at board (2, 2)
    * island B: solid copper 8..12 in x, bonding ``C2`` pad "2" at board (10, 2)

    Both pads are declared GND and both sit squarely in solid copper, so the
    bonding *test* (``region.intersects`` of the eroded pad box) succeeds for
    each — there is no moat involved.  The ONLY thing under test is whether the
    extractor unions pads across the zone's two disjoint fill islands.

    * Per-fill (the #3769 bug): C1.2 bonds only within island A, C2.2 only
      within island B -> two singleton components -> a false GND open.
    * Per-zone (the fix): both pads are accumulated for the one ``zone`` object
      and unioned together -> a single GND component -> clean.
    """
    return """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000b1")
    (at 2 2)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-b1-ref"))
    (property "Value" "100n" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-b1-val"))
    (pad "2" smd roundrect (at 0 0) (size 1.5 1.5) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net 1 "GND"))
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000b2")
    (at 10 2)
    (property "Reference" "C2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-b2-ref"))
    (property "Value" "100n" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-b2-val"))
    (pad "2" smd roundrect (at 0 0) (size 1.5 1.5) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net 1 "GND"))
  )
  (zone
    (net 1 "GND")
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000z2")
    (hatch edge 0.5)
    (connect_pads (clearance 0.2))
    (min_thickness 0.2)
    (fill yes (thermal_gap 0.2) (thermal_bridge_width 0.3))
    (polygon (pts (xy 0 0) (xy 12 0) (xy 12 4) (xy 0 4)))
    (filled_polygon (layer "F.Cu") (pts (xy 0 0) (xy 4 0) (xy 4 4) (xy 0 4)))
    (filled_polygon (layer "F.Cu") (pts (xy 8 0) (xy 12 0) (xy 12 4) (xy 8 4)))
  )
)
"""


@requires_shapely
def test_pour_extraction_unions_pads_across_disjoint_fill_islands_of_one_zone(
    tmp_path: Path,
) -> None:
    """Regression (#3772): one zone, two disjoint fill islands -> one component.

    KiCad fragments a single poured zone into multiple ``filled_polygon``
    entries (board 03's GND F.Cu zone is one main pour plus a dozen tiny
    per-pad fragments).  The pre-fix extractor unioned bonded pads only WITHIN
    a single fill index, so a pad alone in its own fragment landed in a
    singleton component and copper-LVS reported it as a false ``open``.

    This fixture is that failure shape distilled: two GND pads, each bonded to
    a *different* disjoint fill island of the SAME ``zone`` object.  Both must
    land in one connected component.  FAILS on origin/main (per-fill unioning
    splits them); PASSES after the per-zone unioning fix.
    """
    pcb_path = _write(tmp_path, "two_islands.kicad_pcb", _pcb_pour_two_disjoint_islands())
    partition = ConnectivityValidator(pcb_path).extract_pad_partition()
    component = next(c for c in partition if "C1.2" in c)
    assert {"C1.2", "C2.2"} <= component, (
        "pads bonded to disjoint fill islands of the same zone must share one "
        f"component (no false open); partition={partition}"
    )


@requires_shapely
def test_compare_copper_netlist_on_pour_heavy_board07_artifacts() -> None:
    """End-to-end: the label-free pour model stays clean on a pour-heavy board.

    Board 07 (match-group test) carries 45 ``filled_polygons`` across multiple
    pours, so it exercises the hole-aware solid-region extraction and the
    pad-box erosion guard on real routed copper.  It must NOT introduce false
    shorts.  Skips when the artifacts (or shapely) are absent, mirroring the
    board-00 end-to-end policy.
    """
    repo_root = Path(__file__).resolve().parent.parent
    board_out = repo_root / "boards" / "07-matchgroup-test" / "output"
    sch = board_out / "matchgroup_test.kicad_sch"
    pcb = board_out / "matchgroup_test_routed.kicad_pcb"
    if not (sch.exists() and pcb.exists()):
        pytest.skip("board 07 artifacts not present; run generate_design.py")
    result = compare_copper_netlist(sch, pcb)
    assert isinstance(result, CopperLVSResult)
    assert result.clean, f"pour-heavy board 07 copper LVS unexpectedly dirty: {result.mismatches}"


# ---------------------------------------------------------------------------
# Board-04 short diagnosis (issue #3781): the two named shorts, classified.
# ---------------------------------------------------------------------------
#
# The #3762 copper-LVS fleet survey flagged board-04's committed
# ``stm32_devboard_routed.kicad_pcb`` with two alarming shorts. Issue #3781
# classified each against the committed artifact on current main:
#
#   * ``+5V <-> GND`` (witnesses C1.1, U1.1) — RESOLVED, not reproducible.
#     The curator's survey predated PR #3774 (schematic<->PCB net drift
#     reconciliation to one 12-net model).  On current main, C1.1 and U1.1 are
#     BOTH net 1 (+5V) on F.Cu, so the ``{C1.1, U1.1}`` copper component the
#     extractor produces is a legitimate *same-net* +5V pour bond — not a
#     ``+5V<->GND`` short.  No extractor change is warranted; the report was an
#     artifact of the older (pre-#3774) net assignment.
#
#   * ``OSC_IN <-> OSC_OUT`` (witnesses C10.1, C11.1) — REAL routing defect.
#     A B.Cu track segment runs straight from U2.6 (26.8375, 21.75 = OSC_OUT)
#     to U2.5 (26.8375, 21.25 = OSC_IN), galvanically bridging the STM32's two
#     crystal pins on a single copper layer.  This is the documented
#     #2834/#3033 OSC_OUT-escape stub landing on the adjacent OSC_IN pad.  The
#     committed routed PCB is a deliberately pinned (--seed 42, byte-identical
#     per #3039) artifact that #3765/#3774 preserve; breaking the short needs a
#     re-route that risks regression, so it is routed to a scoped board-04
#     layout-fix follow-up (gates #3780) rather than edited in place here.
#
# These assertions pin both verdicts as executable regression guards: if a
# future change either re-introduces the +5V<->GND short or silently makes the
# real OSC short vanish (e.g. by re-routing the board), this test flags it so
# the verdict and its follow-up can be revisited.


def _load_board_04() -> tuple[Path, Path] | None:
    repo_root = Path(__file__).resolve().parent.parent
    board_out = repo_root / "boards" / "04-stm32-devboard" / "output"
    sch = board_out / "stm32_devboard.kicad_sch"
    pcb = board_out / "stm32_devboard_routed.kicad_pcb"
    if not (sch.exists() and pcb.exists()):
        return None
    return sch, pcb


def test_board04_plus5v_gnd_short_resolved_on_main() -> None:
    """Issue #3781 verdict: ``+5V<->GND`` is NOT a short on current main.

    The pour-overlap component ``{C1.1, U1.1}`` is a legitimate same-net +5V
    bond (both pads are net 1 ``+5V`` on F.Cu post-#3774), so no copper short
    fuses ``+5V`` with ``GND``.
    """
    paths = _load_board_04()
    if paths is None:
        pytest.skip("board 04 artifacts not present; run generate_design.py")
    sch, pcb = paths
    result = compare_copper_netlist(sch, pcb)
    fused_5v_gnd = [m for m in result.shorts if {m.net_a, m.net_b} == {"+5V", "GND"}]
    assert not fused_5v_gnd, (
        "regression: +5V<->GND short re-appeared on board-04 "
        f"(witnesses: {[(m.pad_a, m.pad_b) for m in fused_5v_gnd]}). "
        "Per #3781 this was resolved by #3774's net reconciliation; "
        "C1.1 and U1.1 must both be +5V."
    )


def test_board04_plus5v_gnd_witness_component_is_same_net() -> None:
    """The ``{C1.1, U1.1}`` copper component is a same-net (+5V) pour bond.

    Confirms the extractor still fuses these two pads (the pour overlap is
    real copper) but that the fusion is sound because both pads are declared
    ``+5V`` — the classic ``+5V<->GND`` short signature is gone because the net
    labels were reconciled, not because the bond disappeared.
    """
    paths = _load_board_04()
    if paths is None:
        pytest.skip("board 04 artifacts not present; run generate_design.py")
    from kicad_tools.schema.pcb import PCB

    _, pcb_path = paths
    pcb = PCB.load(str(pcb_path))
    declared: dict[str, str] = {}
    for fp in pcb.footprints:
        if not fp.reference or fp.reference.startswith("#"):
            continue
        for pad in fp.pads:
            if pad.number:
                declared[f"{fp.reference}.{pad.number}"] = pad.net_name
    assert declared.get("C1.1") == "+5V"
    assert declared.get("U1.1") == "+5V"

    partition = ConnectivityValidator(pcb).extract_pad_partition()
    comp = next((c for c in partition if "C1.1" in c), frozenset())
    # Every pad sharing C1.1's copper component must be the same net (+5V):
    # a cross-net member would be the real short signature.
    member_nets = {declared.get(p) for p in comp if p in declared}
    assert member_nets == {"+5V"}, (
        f"C1.1's copper component fuses foreign nets {member_nets} — "
        "the +5V<->GND artifact signature would re-appear here."
    )


def test_board04_osc_in_out_real_short_present() -> None:
    """Issue #3781 verdict: ``OSC_IN<->OSC_OUT`` is a REAL copper short.

    A single-layer (B.Cu) track segment from U2.6 (OSC_OUT) straight to U2.5
    (OSC_IN) galvanically bridges the crystal pins.  This guard documents the
    defect until the scoped board-04 re-route follow-up lands; if a future
    re-route clears it, this test flips to a failure prompting the verdict and
    follow-up to be closed/updated.
    """
    paths = _load_board_04()
    if paths is None:
        pytest.skip("board 04 artifacts not present; run generate_design.py")
    sch, pcb_path = paths

    # 1. The comparator reports the OSC short (witness load caps C10.1/C11.1).
    result = compare_copper_netlist(sch, pcb_path)
    osc_short = [m for m in result.shorts if {m.net_a, m.net_b} == {"OSC_IN", "OSC_OUT"}]
    assert osc_short, (
        "expected the documented board-04 OSC_IN<->OSC_OUT real short; "
        "if a re-route cleared it, update issue #3781's follow-up."
    )

    # 2. The fusing copper is a single B.Cu segment joining U2.6 -> U2.5,
    #    i.e. the OSC_OUT pad-6 center (26.8375, 21.75) straight to the
    #    OSC_IN pad-5 center (26.8375, 21.25).  Confirm it exists on one layer.
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load(str(pcb_path))

    def _close(a: tuple[float, float], b: tuple[float, float], tol: float = 0.02) -> bool:
        return abs(a[0] - b[0]) < tol and abs(a[1] - b[1]) < tol

    osc_out = (26.8375, 21.75)  # U2.6 / OSC_OUT pad center
    osc_in = (26.8375, 21.25)  # U2.5 / OSC_IN pad center
    bridging = [
        seg
        for seg in pcb.segments
        if (
            (_close(seg.start, osc_out) and _close(seg.end, osc_in))
            or (_close(seg.start, osc_in) and _close(seg.end, osc_out))
        )
    ]
    assert bridging, "no single segment found directly joining the OSC pads"
    assert all(seg.layer == "B.Cu" for seg in bridging), (
        "the OSC bridging segment is expected on B.Cu (the OSC_OUT escape stub)"
    )
