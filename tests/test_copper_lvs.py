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
    VACUOUS_KIND,
    VACUOUS_NET,
    CopperLVSResult,
    compare_copper_netlist,
    compare_partitions,
)
from kicad_tools.lvs.copper_lvs import result_from_json, result_to_json
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


def test_pour_opens_suppressed() -> None:
    """Advisory pour nets suppress opens; signal nets still report them (#3914).

    A pour-routed net (GND) whose fill has not yet been stitched leaves its
    pads in separate copper islands.  Reporting one advisory "open" per
    stranded pad drowns real signal opens in noise (88-105 of them on board
    05), so opens are suppressed for nets in ``advisory_net_names``.  A signal
    net split across two islands is a genuine open and is still reported, and
    a pour net copper-fused to a *foreign* net is still a short.
    """
    sch = {
        ("U1", "1"): "GND",
        ("U2", "1"): "GND",
        ("R1", "1"): "SIG",
        ("R2", "1"): "SIG",
    }
    # GND split across two islands (unstitched pour); SIG split across two
    # islands (genuine signal open).
    partition = [
        frozenset({"U1.1"}),
        frozenset({"U2.1"}),
        frozenset({"R1.1"}),
        frozenset({"R2.1"}),
    ]

    # Without the advisory filter both nets report an open.
    strict = compare_partitions(sch, partition)
    assert {o.net_a for o in strict.opens} == {"GND", "SIG"}

    # With GND marked advisory, only the SIG open survives.
    filtered = compare_partitions(sch, partition, advisory_net_names=frozenset({"GND"}))
    assert [o.net_a for o in filtered.opens] == ["SIG"]
    assert filtered.shorts == ()


def test_pour_advisory_filter_does_not_suppress_shorts() -> None:
    """Opens are suppressed for advisory nets, but shorts never are (#3914)."""
    sch = {
        ("U1", "1"): "GND",
        ("R1", "1"): "SIG",
    }
    # GND and SIG copper-fused into one island -> a real short.
    partition = [frozenset({"U1.1", "R1.1"})]
    result = compare_partitions(sch, partition, advisory_net_names=frozenset({"GND"}))
    assert len(result.shorts) == 1
    assert {result.shorts[0].net_a, result.shorts[0].net_b} == {"GND", "SIG"}


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
# Vacuity guard (#4006, PR #4005 review): zero bound pins must not be "clean"
# ---------------------------------------------------------------------------


def test_wireless_schematic_is_vacuous_not_clean() -> None:
    """A schematic that binds zero pins (all floating) must NOT read clean.

    This is the exact PR #4005 review hole: PCB-first fixture schematics
    with no ``(wire ...)`` elements resolve every pin to ``None``, so the
    partition diff observes nothing.  The old behavior returned a
    zero-evidence ``clean=True``; the guard now returns a dirty result
    carrying a single synthetic ``vacuous`` mismatch.
    """
    sch: dict[tuple[str, str], str | None] = {
        ("J1", "1"): None,
        ("J1", "2"): None,
        ("R1", "1"): None,
    }
    partition = [frozenset({"J1.1", "J1.2"}), frozenset({"R1.1"})]
    result = compare_partitions(sch, partition)
    assert not result.clean
    assert result.vacuous
    assert result.bound_pad_count == 0
    assert len(result.mismatches) == 1
    m = result.mismatches[0]
    assert m.kind == VACUOUS_KIND
    assert m.net_a == m.net_b == VACUOUS_NET
    assert m.pad_a == "bound_pads=0"
    assert m.pad_b == "board_pads=3"
    # The synthetic entry is neither a short nor an open.
    assert result.shorts == ()
    assert result.opens == ()


def test_no_schematic_board_overlap_is_vacuous() -> None:
    """Wired schematic pins that match NO board pad also bind nothing."""
    sch = {("R9", "1"): "VCC", ("R9", "2"): "GND"}  # R9 not on the board
    partition = [frozenset({"U1.1"}), frozenset({"U1.2"})]
    result = compare_partitions(sch, partition)
    assert not result.clean
    assert result.vacuous
    assert result.bound_pad_count == 0


def test_single_bound_pad_is_evidence_not_vacuous() -> None:
    """The floor is exactly zero: one bound pad is real (if thin) evidence."""
    sch = {("R1", "1"): "VCC", ("R1", "2"): None}
    partition = [frozenset({"R1.1"}), frozenset({"R1.2"})]
    result = compare_partitions(sch, partition)
    assert result.clean
    assert not result.vacuous
    assert result.bound_pad_count == 1


def test_wired_clean_result_records_bound_pad_count() -> None:
    """Non-vacuous results carry their evidence count (#4006)."""
    sch = {
        ("R1", "1"): "VCC",
        ("R1", "2"): "LED_ANODE",
        ("D1", "1"): "LED_ANODE",
        ("D1", "2"): "GND",
    }
    partition = [
        frozenset({"R1.1"}),
        frozenset({"R1.2", "D1.1"}),
        frozenset({"D1.2"}),
    ]
    result = compare_partitions(sch, partition)
    assert result.clean
    assert not result.vacuous
    assert result.bound_pad_count == 4


def test_vacuous_result_survives_json_round_trip() -> None:
    """The subprocess gate marshals results as JSON; vacuity must survive."""
    sch: dict[tuple[str, str], str | None] = {("J1", "1"): None}
    result = compare_partitions(sch, [frozenset({"J1.1"})])
    round_tripped = result_from_json(result_to_json(result))
    assert round_tripped == result
    assert round_tripped.vacuous
    assert not round_tripped.clean
    assert round_tripped.bound_pad_count == 0


def test_legacy_payload_without_bound_pad_count_round_trips() -> None:
    """Older JSON payloads (no bound_pad_count key) still parse (additive)."""
    result = result_from_json({"clean": True, "mismatches": []})
    assert result.clean
    assert result.bound_pad_count is None
    assert not result.vacuous


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
    # Board 00's schematic is genuinely wired: its clean verdict must carry
    # real evidence, not the vacuous zero-comparison kind (#4006).
    assert not result.vacuous
    assert result.bound_pad_count is not None and result.bound_pad_count > 0


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
    """End-to-end: pour-heavy extraction runs, and the verdict is VACUOUS.

    Board 07 (match-group test) carries the GND / +1V2 / +1V8 plane pours
    (one zone per net since #3818 de-duplicated the router/recipe overlap),
    fragmented into multiple ``filled_polygons`` by thermal reliefs and
    foreign-pad clearance moats, so it exercises the hole-aware solid-region
    extraction and the pad-box erosion guard on real routed copper without
    crashing.

    The verdict itself, however, is *vacuous* (#4006): board 07's fixture
    schematic has no ``(wire ...)`` elements, so zero pins bind and the
    historical ``clean=True`` this test pinned was zero-evidence (it would
    have masked board 07's 5 real copper opens).  The honest contract is
    now ``clean=False`` + ``vacuous`` — this is the guard's end-to-end
    regression pin on a real pour-heavy board.
    """
    repo_root = Path(__file__).resolve().parent.parent
    board_out = repo_root / "boards" / "07-matchgroup-test" / "output"
    sch = board_out / "matchgroup_test.kicad_sch"
    pcb = board_out / "matchgroup_test_routed.kicad_pcb"
    if not (sch.exists() and pcb.exists()):
        pytest.skip("board 07 artifacts not present; run generate_design.py")
    result = compare_copper_netlist(sch, pcb)
    assert isinstance(result, CopperLVSResult)
    assert result.vacuous, (
        "board 07's fixture schematic was expected to bind 0 pins (vacuous, "
        f"#4006) but bound {result.bound_pad_count}; if the schematic gained "
        "wired nets, upgrade this test to a real clean assertion"
    )
    assert not result.clean
    assert result.bound_pad_count == 0


def test_compare_copper_netlist_on_board06_wireless_fixture_is_vacuous() -> None:
    """End-to-end #4006 pin: board 06's unwired schematic yields VACUOUS.

    Board 06's committed ``lvs.json`` (merged in #4004) claimed
    ``clean=true`` off exactly this pair of artifacts while binding zero
    pins — the vacuity hole from PR #4005's review.  The comparator must
    now refuse that: dirty, vacuous, zero bound pads.
    """
    repo_root = Path(__file__).resolve().parent.parent
    board_out = repo_root / "boards" / "06-diffpair-test" / "output"
    sch = board_out / "diffpair_test.kicad_sch"
    pcb = board_out / "diffpair_test_routed.kicad_pcb"
    if not (sch.exists() and pcb.exists()):
        pytest.skip("board 06 artifacts not present; run generate_design.py")
    result = compare_copper_netlist(sch, pcb)
    assert not result.clean
    assert result.vacuous
    assert result.bound_pad_count == 0
    assert [m.kind for m in result.mismatches] == [VACUOUS_KIND]


# ---------------------------------------------------------------------------
# Via-to-foreign-pour short detection (issue #3909)
# ---------------------------------------------------------------------------
#
# A stitch/connectivity via whose *copper ring* overlaps a foreign-net pour is
# a real electrical short that kicad-cli's ``--refill-zones`` DRC flags but the
# Python-fill-based copper-LVS historically missed: KiCad carves an antipad
# clearance hole centred on every foreign via, so the via *centre* always sits
# in that hole.  The pre-#3909 extractor tested each via as a bare centre POINT
# against the pour solid region, so a via whose ring poked past a marginal /
# too-small antipad into the solid pour was never bonded and the short was
# invisible.  Representing the via as its copper CIRCLE catches the overlap.

# Solid GND pour on F.Cu over x=8..20, y=0..20, with a SMALL antipad hole
# (0.4 x 0.4, centred on the via at (10, 10)) carved out the way KiCad
# flattens a hole: one ring whose boundary dips into the cutout through a
# narrow slit.  ``_fill_solid_region`` (shapely buffer(0)) re-derives the hole.
# The via centre lands INSIDE this hole (a bare-point test bonds nothing), but
# the via's copper ring (size 1.0 -> radius 0.5, eroded to 0.4) reaches 0.2 mm
# past the hole edge (at 0.2 mm) into the solid pour -> a real short.
_VIA_POUR_FILL_RING = (
    "(xy 8 0) (xy 9.98 0) (xy 9.98 9.8) "
    "(xy 9.8 9.8) (xy 9.8 10.2) (xy 10.2 10.2) (xy 10.2 9.8) "
    "(xy 10.02 9.8) (xy 10.02 0) "
    "(xy 20 0) (xy 20 20) (xy 8 20) (xy 8 0)"
)


def _pcb_via_grazes_foreign_pour() -> str:
    """A SIG via whose copper ring pokes into a GND pour past a marginal antipad.

    * ``R2`` pad "1" at board (5, 10): declared **SIG**, sits OUTSIDE the pour
      (pour starts at x=8) so it is not bonded to GND by its own copper.
    * A track segment ties R2.1 to a via at (10, 10) (also SIG copper).
    * The via at (10, 10) sits in the GND pour's antipad hole, but its copper
      ring pokes past the too-small hole into the solid GND pour.
    * ``R3`` pad "1" at board (15, 10): declared **GND**, copper in the solid
      pour -> bonded, the GND anchor the via shorts against.

    Result: R2.1 --seg--> via --(ring overlap)--> GND pour --> R3.1, fusing SIG
    and GND into one copper island (a short) that a bare-centre-point via test
    would miss (the via centre is moated out by the antipad hole).
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
    (uuid "00000000-0000-0000-0000-0000000000c1")
    (at 5 10)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-c1-ref"))
    (property "Value" "1k" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-c1-val"))
    (pad "1" smd roundrect (at 0 0) (size 1.5 1.5) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net 2 "SIG"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000c3")
    (at 15 10)
    (property "Reference" "R3" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-c3-ref"))
    (property "Value" "1k" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-c3-val"))
    (pad "1" smd roundrect (at 0 0) (size 1.5 1.5) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net 1 "GND"))
  )
  (segment (start 5 10) (end 10 10) (width 0.25) (layer "F.Cu") (net 2))
  (via (at 10 10) (size 1.0) (drill 0.4) (layers "F.Cu" "B.Cu") (net 2 "SIG"))
  (zone
    (net 1 "GND")
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000z3")
    (hatch edge 0.5)
    (connect_pads (clearance 0.2))
    (min_thickness 0.2)
    (fill yes (thermal_gap 0.2) (thermal_bridge_width 0.3))
    (polygon (pts (xy 8 0) (xy 20 0) (xy 20 20) (xy 8 20)))
    (filled_polygon (layer "F.Cu") (pts {_VIA_POUR_FILL_RING}))
  )
)
"""


_VIA_POUR_SCHEMATIC: dict[tuple[str, str], str | None] = {
    ("R2", "1"): "SIG",
    ("R3", "1"): "GND",
}


def _point_via_partition(pcb_path: Path) -> list[frozenset[str]]:
    """Extract the partition under the LEGACY bare-centre-point via model.

    Patches :meth:`ConnectivityValidator._via_copper_geom` to return a bare
    ``Point`` (the pre-#3909 behaviour) so the very same fixture can be run
    through the old via-vs-pour test on which the short was invisible.
    """
    from unittest import mock

    from shapely.geometry import Point as _P  # type: ignore[import-untyped]

    def _point_only(self: ConnectivityValidator, pos, radius):  # noqa: ANN001, ARG001
        return _P(pos)

    with mock.patch.object(ConnectivityValidator, "_via_copper_geom", _point_only):
        return ConnectivityValidator(pcb_path).extract_pad_partition()


@requires_shapely
def test_via_grazing_foreign_pour_is_a_short(tmp_path: Path) -> None:
    """Crux (#3909): same fixture, POINT via model masks, CIRCLE model catches.

    A SIG via's copper ring overlaps a GND pour (its antipad hole is marginal /
    too small).  The via centre is moated out, so the old bare-point test never
    bonds it and copper-LVS reports CLEAN — the exact blind spot that let a
    board ship ``lvs_clean: true`` while kicad-cli reported ``shorting_items``.
    The copper-circle test bonds the ring into the pour, surfacing the short.
    """
    pcb_path = _write(tmp_path, "via_pour.kicad_pcb", _pcb_via_grazes_foreign_pour())

    # --- OLD bare-point via model: masks the short (clean) ---
    old_partition = _point_via_partition(pcb_path)
    old_result = compare_partitions(_VIA_POUR_SCHEMATIC, old_partition)
    assert old_result.clean, (
        "the legacy bare-centre-point via test should MASK this via-to-foreign-"
        f"pour short (the via centre is moated out); got {old_result.mismatches}"
    )

    # --- NEW copper-circle via model: catches the short (dirty) ---
    new_partition = ConnectivityValidator(pcb_path).extract_pad_partition()
    new_result = compare_partitions(_VIA_POUR_SCHEMATIC, new_partition)
    assert not new_result.clean, (
        "the via copper-circle test must FLAG the via-to-foreign-pour short; "
        f"partition={new_partition}"
    )
    short_pairs = {frozenset({m.net_a, m.net_b}) for m in new_result.shorts}
    assert frozenset({"GND", "SIG"}) in short_pairs, (
        f"expected a GND/SIG short named on both nets; shorts={new_result.shorts}"
    )
    # R2.1 (SIG) and R3.1 (GND) are fused into one copper island by the via.
    fused = next(c for c in new_partition if "R2.1" in c)
    assert {"R2.1", "R3.1"} <= fused, (
        f"the via must fuse the SIG pad and the GND pour anchor; got {fused}"
    )


@requires_shapely
def test_via_on_net0_adjacent_to_pour_is_not_a_false_short(tmp_path: Path) -> None:
    """Edge case (#3909): a net-0 via with a proper antipad raises no false SHORT.

    Relabel the via to net 0 (unconnected) and widen the antipad hole so the
    via copper stays clear of the solid pour (a DRC-clean placement).  The
    copper-circle test must NOT fabricate a short: with a proper clearance moat
    the eroded ring never reaches the solid pour, and a net-0 via carries no
    schematic net to short in any case.
    """
    # Wider antipad hole (1.2 x 1.2 around the via) so the eroded via ring
    # (radius 0.4) stays inside the hole -> no bond to the solid pour.
    wide_hole_ring = (
        "(xy 8 0) (xy 9.98 0) (xy 9.98 9.4) "
        "(xy 9.4 9.4) (xy 9.4 10.6) (xy 10.6 10.6) (xy 10.6 9.4) "
        "(xy 10.02 9.4) (xy 10.02 0) "
        "(xy 20 0) (xy 20 20) (xy 8 20) (xy 8 0)"
    )
    src = (
        _pcb_via_grazes_foreign_pour()
        .replace(
            '(via (at 10 10) (size 1.0) (drill 0.4) (layers "F.Cu" "B.Cu") (net 2 "SIG"))',
            '(via (at 10 10) (size 1.0) (drill 0.4) (layers "F.Cu" "B.Cu") (net 0 ""))',
        )
        .replace(_VIA_POUR_FILL_RING, wide_hole_ring)
    )
    pcb_path = _write(tmp_path, "via_pour_clean.kicad_pcb", src)
    partition = ConnectivityValidator(pcb_path).extract_pad_partition()
    result = compare_partitions(_VIA_POUR_SCHEMATIC, partition)
    assert result.clean, (
        "a net-0 via with a proper antipad clearance moat must not be reported "
        f"as a short; mismatches={result.mismatches}"
    )


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
#   * ``OSC_IN <-> OSC_OUT`` (witnesses C10.1, C11.1) — REAL routing defect,
#     now CLEARED by #3785.  A B.Cu track segment used to run straight from
#     U2.6 (26.8375, 21.75 = OSC_OUT) through U2.5 (26.8375, 21.25 = OSC_IN),
#     galvanically bridging the STM32's two crystal pins on a single copper
#     layer (the documented #2834/#3033 OSC_OUT-escape stub landing on the
#     adjacent OSC_IN pad).  #3785 performed a localized OSC_OUT-only re-route:
#     the offending B.Cu stub was deleted and replaced with an escape that jogs
#     west of the OSC_IN pad column (via -> (26.6875, 21.55) -> (26.6875, 21.1)
#     -> ... -> C11), so the two crystal nets are no longer bridged.  NRST/SWO
#     and all other nets stayed byte-identical (no full re-route).
#
# These assertions pin both verdicts as executable regression guards: if a
# future change either re-introduces the +5V<->GND short or re-introduces the
# real OSC short (e.g. by re-routing the board straight through the OSC_IN pad),
# this test flags it.


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


def test_board04_osc_in_out_short_absent() -> None:
    """Issue #3785: the ``OSC_IN<->OSC_OUT`` B.Cu escape stub short is CLEARED.

    Previously (issues #3781/#3786) a single B.Cu segment ran straight from
    U2.6 (OSC_OUT, 26.8375, 21.75) through the U2.5 (OSC_IN, 26.8375, 21.25)
    pad center, galvanically bridging the STM32's HSE crystal pins -- a
    manufacturing-fatal short.  #3785 re-routed only OSC_OUT so its B.Cu
    escape jogs west of the OSC_IN pad column (via -> (26.6875, 21.55) ->
    (26.6875, 21.1) -> ... -> C11) instead of dropping straight through pad 5.

    This is now a permanent regression guard: it asserts the OSC pair is
    ABSENT from the copper-LVS shorts AND that no single track segment joins
    the two OSC pad centers.  If a future re-route re-introduces the stub,
    this test flags it.
    """
    paths = _load_board_04()
    if paths is None:
        pytest.skip("board 04 artifacts not present; run generate_design.py")
    sch, pcb_path = paths

    # 1. The comparator no longer reports an OSC_IN<->OSC_OUT short.
    result = compare_copper_netlist(sch, pcb_path)
    osc_short = [m for m in result.shorts if {m.net_a, m.net_b} == {"OSC_IN", "OSC_OUT"}]
    assert not osc_short, (
        "regression: the board-04 OSC_IN<->OSC_OUT copper short re-appeared "
        f"(witnesses: {[(m.pad_a, m.pad_b) for m in osc_short]}). "
        "Per #3785 the OSC_OUT escape must jog clear of the OSC_IN pad."
    )

    # 2. No single track segment joins the two OSC pad centers, i.e. the
    #    OSC_OUT pad-6 center (26.8375, 21.75) straight to the OSC_IN pad-5
    #    center (26.8375, 21.25).  The fusing escape stub must be gone.
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
    assert not bridging, (
        "regression: a track segment still directly joins the OSC pads "
        f"({[(seg.start, seg.end, seg.layer) for seg in bridging]}); "
        "the OSC_OUT escape stub must not pass through the OSC_IN pad center."
    )


# ---------------------------------------------------------------------------
# Layer-aware crossover (issue #3783): a via-less F.Cu/B.Cu crossover of two
# different nets is a legal layer crossover and must NOT be reported as a
# copper short.  This is the end-to-end gate behaviour behind board-02's
# false-positive NODE_B<->NODE_C "short".
# ---------------------------------------------------------------------------


def _pcb_crossover_via_less() -> str:
    """Two different-net traces crossing at the same XY on opposite layers.

    R1.1 (NODE_B) routes vertically on F.Cu through (110, 110); R2.1
    (NODE_C) routes horizontally on B.Cu through (110, 110).  No via joins
    them, so the copper-LVS gate must keep them on separate nets.
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
  (net 1 "NODE_B")
  (net 2 "NODE_C")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000fb1")
    (at 110 105)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-fb1-ref"))
    (property "Value" "1k" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-fb1-val"))
    (pad "1" smd roundrect (at 0 0) (size 0.6 0.6) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net 1 "NODE_B"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "B.Cu")
    (uuid "00000000-0000-0000-0000-000000000fb2")
    (at 105 110)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "B.SilkS") (uuid "fp-fb2-ref"))
    (property "Value" "1k" (at 0 1.5 0) (layer "B.Fab") (uuid "fp-fb2-val"))
    (pad "1" smd roundrect (at 0 0) (size 0.6 0.6) (layers "B.Cu" "B.Paste" "B.Mask")
      (roundrect_rratio 0.25) (net 2 "NODE_C"))
  )
  (segment (start 110 105) (end 110 110) (width 0.25) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000fb3"))
  (segment (start 110 110) (end 110 115) (width 0.25) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000fb4"))
  (segment (start 105 110) (end 110 110) (width 0.25) (layer "B.Cu") (net 2)
    (uuid "00000000-0000-0000-0000-000000000fb5"))
  (segment (start 110 110) (end 115 110) (width 0.25) (layer "B.Cu") (net 2)
    (uuid "00000000-0000-0000-0000-000000000fb6"))
)
"""


def test_via_less_crossover_copper_lvs_clean(tmp_path: Path) -> None:
    """A via-less F.Cu/B.Cu crossover is clean copper-LVS (issue #3783).

    The two traces cross at one XY on opposite layers with no via — a legal,
    DRC-clean layer crossover.  The copper extractor must keep NODE_B and
    NODE_C separate, so the partition diff against the schematic is clean
    (no spurious NODE_B<->NODE_C short).
    """
    pcb_path = _write(tmp_path, "crossover.kicad_pcb", _pcb_crossover_via_less())
    partition = ConnectivityValidator(pcb_path).extract_pad_partition()

    # Each net keeps its own pad — no phantom short.
    assert frozenset({"R1.1"}) in partition
    assert frozenset({"R2.1"}) in partition

    sch = {("R1", "1"): "NODE_B", ("R2", "1"): "NODE_C"}
    result = compare_partitions(sch, partition)
    assert result.clean
    assert result.mismatches == ()


# ---------------------------------------------------------------------------
# Via-into-pour + via-in-pad bonding (issue #3794)
# ---------------------------------------------------------------------------
#
# Board-04's GND pour is B.Cu-only, so a GND SMD pad on F.Cu reaches the plane
# through ``pad -> F.Cu trace -> stitch via -> B.Cu pour``.  Before #3794 the
# label-free partition (``extract_pad_partition``) only tested *pad* boxes
# against a pour and only fused a via's *coincident* pads — so the via-into-
# pour hop and an off-centre via-in-pad tie were both invisible, stranding the
# GND pads as false same-net opens.  These fixtures pin the two new bonds:
#
#   * a via / trace-endpoint that lands inside a pour's solid region unions the
#     pads reaching it into that pour island (synthetic via node, step 1b/2d);
#   * a via whose centre sits inside a pad's copper box (off-centre via-in-pad)
#     bonds to that pad (step 2c2).
#
# Both reuse the existing ``_fill_solid_region`` / ``POUR_PAD_ERODE`` guards,
# so neither relaxes the #3769/#3772/#3792 moat/erosion adversarial guards.


def _pcb_via_into_pour() -> str:
    """A B.Cu-only GND pour reached only through a stitch via (issue #3794).

    Mirrors board-04's GND topology in miniature:

    * GND pour fills a solid square 0..20 on **B.Cu** only.
    * ``C1`` pad "2" at board (5, 5) is an F.Cu SMD GND pad.  It is moated out
      of any F.Cu copper (there is none) and does NOT sit over the pour on its
      own layer, so a pad-box-only pour test cannot bond it.
    * An ``F.Cu``->``B.Cu`` GND via at (10, 10) lands squarely inside the B.Cu
      pour, and an F.Cu trace runs ``(5, 5) -> (10, 10)`` from the pad to the
      via.  The only galvanic path C1.2 has to the pour is
      ``pad -> F.Cu trace -> via -> B.Cu pour``.
    * ``C2`` pad "2" at board (15, 15) is a B.Cu SMD GND pad sitting directly
      in the solid pour (the pour "anchor" C1.2 must join).

    Pre-#3794 the via-into-pour hop is invisible: C1.2 lands in its own
    singleton (a false GND open against C2.2).  Post-#3794 the via node inside
    the pour unions C1.2 into the pour island with C2.2.
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
    (uuid "00000000-0000-0000-0000-0000000000c1")
    (at 5 5)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-c1-ref"))
    (property "Value" "100n" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-c1-val"))
    (pad "2" smd roundrect (at 0 0) (size 1.5 1.5) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net 1 "GND"))
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "B.Cu")
    (uuid "00000000-0000-0000-0000-0000000000c2")
    (at 15 15)
    (property "Reference" "C2" (at 0 -1.5 0) (layer "B.SilkS") (uuid "fp-c2-ref"))
    (property "Value" "100n" (at 0 1.5 0) (layer "B.Fab") (uuid "fp-c2-val"))
    (pad "2" smd roundrect (at 0 0) (size 1.5 1.5) (layers "B.Cu" "B.Paste" "B.Mask")
      (roundrect_rratio 0.25) (net 1 "GND"))
  )
  (segment (start 5 5) (end 10 10) (width 0.25) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-0000000000s1"))
  (via (at 10 10) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu")
    (uuid "00000000-0000-0000-0000-0000000000v1") (net 1))
  (zone
    (net 1 "GND")
    (layer "B.Cu")
    (uuid "00000000-0000-0000-0000-0000000000z3")
    (hatch edge 0.5)
    (connect_pads (clearance 0.2))
    (min_thickness 0.2)
    (fill yes (thermal_gap 0.2) (thermal_bridge_width 0.3))
    (polygon (pts (xy 0 0) (xy 20 0) (xy 20 20) (xy 0 20)))
    (filled_polygon (layer "B.Cu") (pts (xy 0 0) (xy 20 0) (xy 20 20) (xy 0 20)))
  )
)
"""


@requires_shapely
def test_via_into_pour_bonds_pad_reaching_pour_through_via(tmp_path: Path) -> None:
    """A pad reaching a pour only via a stitch via is bonded (issue #3794).

    C1.2 (F.Cu) reaches the B.Cu GND pour only through
    ``pad -> F.Cu trace -> via -> B.Cu pour``.  The via node inside the pour
    must union C1.2 into the same component as C2.2 (the pad sitting directly
    in the pour).  Pre-#3794 this hop was invisible and C1.2 was a singleton
    (a false GND open).
    """
    pcb_path = _write(tmp_path, "via_pour.kicad_pcb", _pcb_via_into_pour())
    partition = ConnectivityValidator(pcb_path).extract_pad_partition()

    c1_component = next(c for c in partition if "C1.2" in c)
    assert "C2.2" in c1_component, (
        "C1.2 should bond to the B.Cu GND pour through its stitch via and join "
        f"C2.2; partition={sorted(sorted(c) for c in partition)}"
    )
    # No synthetic via node leaks into the returned partition.
    assert all(not p.startswith("__via") for c in partition for p in c)


@requires_shapely
def test_via_into_pour_copper_lvs_clean(tmp_path: Path) -> None:
    """The via-into-pour board is copper-LVS clean (no false GND open)."""
    pcb_path = _write(tmp_path, "via_pour.kicad_pcb", _pcb_via_into_pour())
    partition = ConnectivityValidator(pcb_path).extract_pad_partition()
    sch = {("C1", "2"): "GND", ("C2", "2"): "GND"}
    result = compare_partitions(sch, partition)
    assert result.clean, f"unexpected mismatches: {result.mismatches}"
    assert result.opens == ()
    assert result.shorts == ()


def _pcb_via_in_pad_offcenter() -> str:
    """An off-centre via-in-pad tie into a B.Cu pour (issue #3794).

    Distilled from board-04's congested LQFP VSS pads, where a centred stitch
    via cannot clear the neighbour escape so the tie via is pushed off-centre
    but still under the pad copper:

    * ``U1`` pad "8" at board (5, 5) is a tall F.Cu SMD GND pad
      (size 0.3 x 1.5, long axis = y).
    * A GND ``F.Cu``->``B.Cu`` via sits at (5, 5.5) — 0.5 mm *off* the pad
      centre along the long axis but well inside the pad copper (and outside
      ``POSITION_TOLERANCE`` of the centre, so step-2c coincidence does NOT
      fire).  It lands in the B.Cu GND pour.
    * ``U2`` pad "1" at board (15, 15) is a B.Cu GND pad in the solid pour.

    Only the via-in-pad bond (step 2c2) ties U1.8 to its via; the via-into-pour
    bond then joins it to U2.1.  Pre-#3794 U1.8 is a singleton (false open).
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
  (footprint "Package_QFP:LQFP-48_7x7mm_P0.5mm"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000d1")
    (at 5 5)
    (property "Reference" "U1" (at 0 -3 0) (layer "F.SilkS") (uuid "fp-d1-ref"))
    (property "Value" "MCU" (at 0 3 0) (layer "F.Fab") (uuid "fp-d1-val"))
    (pad "8" smd roundrect (at 0 0) (size 0.3 1.5) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net 1 "GND"))
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "B.Cu")
    (uuid "00000000-0000-0000-0000-0000000000d2")
    (at 15 15)
    (property "Reference" "U2" (at 0 -1.5 0) (layer "B.SilkS") (uuid "fp-d2-ref"))
    (property "Value" "100n" (at 0 1.5 0) (layer "B.Fab") (uuid "fp-d2-val"))
    (pad "1" smd roundrect (at 0 0) (size 1.5 1.5) (layers "B.Cu" "B.Paste" "B.Mask")
      (roundrect_rratio 0.25) (net 1 "GND"))
  )
  (via micro (at 5 5.5) (size 0.3) (drill 0.15) (layers "F.Cu" "B.Cu")
    (uuid "00000000-0000-0000-0000-0000000000v2") (net 1))
  (zone
    (net 1 "GND")
    (layer "B.Cu")
    (uuid "00000000-0000-0000-0000-0000000000z4")
    (hatch edge 0.5)
    (connect_pads (clearance 0.2))
    (min_thickness 0.2)
    (fill yes (thermal_gap 0.2) (thermal_bridge_width 0.3))
    (polygon (pts (xy 0 0) (xy 20 0) (xy 20 20) (xy 0 20)))
    (filled_polygon (layer "B.Cu") (pts (xy 0 0) (xy 20 0) (xy 20 20) (xy 0 20)))
  )
)
"""


@requires_shapely
def test_via_in_pad_offcenter_bonds_pad(tmp_path: Path) -> None:
    """An off-centre via-in-pad tie bonds the pad into the pour (issue #3794).

    U1.8's only copper is a micro-via sitting 0.5 mm off its centre but inside
    the pad copper; that via lands in the B.Cu GND pour.  The via-in-pad bond
    (step 2c2) plus the via-into-pour bond must join U1.8 to U2.1.  The via is
    outside ``POSITION_TOLERANCE`` of the pad centre, so the pre-#3794
    coincidence test (step 2c) does NOT fire — this exercises the new bond.
    """
    pcb_path = _write(tmp_path, "via_in_pad.kicad_pcb", _pcb_via_in_pad_offcenter())
    partition = ConnectivityValidator(pcb_path).extract_pad_partition()
    u1_component = next(c for c in partition if "U1.8" in c)
    assert "U2.1" in u1_component, (
        "U1.8 should bond through its off-centre via-in-pad into the B.Cu GND "
        f"pour and join U2.1; partition={sorted(sorted(c) for c in partition)}"
    )


@requires_shapely
def test_via_in_pad_does_not_bond_foreign_pad(tmp_path: Path) -> None:
    """A via well clear of a pad's eroded copper does NOT bond it (guard).

    Move the via fully outside U1.8's pad box (and off the pour anchor's path).
    The via-in-pad bond must NOT fire — only a via inside the eroded pad copper
    counts — so U1.8 must NOT be fused to that via's island.  This pins the
    soundness guard: the new bond cannot manufacture a short by grabbing a via
    that merely passes near a foreign pad.
    """
    # Push the via to (8, 5.5): 3 mm east of U1.8's pad box (half-width 0.15),
    # still in the B.Cu pour but nowhere near the pad copper.
    text = _pcb_via_in_pad_offcenter().replace("(at 5 5.5)", "(at 8 5.5)")
    pcb_path = _write(tmp_path, "via_far.kicad_pcb", text)
    partition = ConnectivityValidator(pcb_path).extract_pad_partition()
    # U1.8 has no copper of its own reaching the pour -> singleton (open).
    assert frozenset({"U1.8"}) in partition, (
        "a via 3 mm clear of U1.8's pad copper must NOT bond it (no via-in-pad "
        f"false bond); partition={sorted(sorted(c) for c in partition)}"
    )


# ---------------------------------------------------------------------------
# Layer-aware endpoint matching + via-barrel/track overlap (softstart fixes)
# ---------------------------------------------------------------------------


def test_trace_under_smd_pad_on_other_layer_does_not_fuse(tmp_path: Path) -> None:
    """A B.Cu trace ending at the XY of an F.Cu-only SMD pad is NOT a bond.

    Regression for the softstart false shorts (VGATE<->SRC_POS<->SRC_NEG,
    AC_LINE<->AC_NEUTRAL): the extractor matched segment endpoints to pads
    by XY alone, so a legal DRC-clean inner/B.Cu trace routed under an SMD
    pad fused the pad's net into the trace's chain.
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
    (uuid "00000000-0000-0000-0000-0000000000f1")
    (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-f1-ref"))
    (property "Value" "1k" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-f1-val"))
    (pad "1" smd roundrect (at 0 0) (size 0.6 0.6) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net 1 "A"))
    (pad "2" smd roundrect (at 2 0) (size 0.6 0.6) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net 2 "B"))
  )
  (segment (start 100 100) (end 102 100) (width 0.25) (layer "B.Cu") (net 2)
    (uuid "00000000-0000-0000-0000-0000000000f2"))
)
"""
    pcb_path = _write(tmp_path, "b.kicad_pcb", pcb)
    partition = ConnectivityValidator(pcb_path).extract_pad_partition()
    # The B.Cu segment runs exactly under both F.Cu pads but touches
    # neither -> two singleton components, no false short.
    assert frozenset({"R1.1"}) in partition
    assert frozenset({"R1.2"}) in partition
    assert len(partition) == 2


def test_via_barrel_overlapping_track_mid_segment_bonds(tmp_path: Path) -> None:
    """A thru via grazed mid-segment by tracks on two layers bonds them.

    Regression for the softstart false open (NRST_FS_POS): the router hops
    F.Cu -> inner/B.Cu through a via whose barrel overlaps both tracks
    mid-segment (no endpoint coincides with the via centre).  KiCad's own
    connectivity treats that as connected; the extractor must too.
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
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000f3")
    (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-f3-ref"))
    (property "Value" "1k" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-f3-val"))
    (pad "1" smd roundrect (at 0 0) (size 0.6 0.6) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net 1 "A"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "B.Cu")
    (uuid "00000000-0000-0000-0000-0000000000f4")
    (at 110 100)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-f4-ref"))
    (property "Value" "1k" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-f4-val"))
    (pad "1" smd roundrect (at 0 0) (size 0.6 0.6) (layers "B.Cu" "B.Paste" "B.Mask")
      (roundrect_rratio 0.25) (net 1 "A"))
  )
  (segment (start 100 100) (end 105 100.25) (width 0.2) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-0000000000f5"))
  (segment (start 105 99.75) (end 110 100) (width 0.2) (layer "B.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-0000000000f6"))
  (via (at 105 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-0000000000f7"))
)
"""
    pcb_path = _write(tmp_path, "b.kicad_pcb", pcb)
    partition = ConnectivityValidator(pcb_path).extract_pad_partition()
    # F.Cu track ends 0.25 mm above the via centre, B.Cu track starts
    # 0.25 mm below it; neither endpoint hits the via centre within the
    # 0.01 mm tolerance, but both swept tracks overlap the 0.3 mm-radius
    # barrel -> one component through the via.
    assert frozenset({"R1.1", "R2.1"}) in partition, (
        f"via-barrel overlap must bond the F.Cu and B.Cu tracks; "
        f"partition={sorted(sorted(c) for c in partition)}"
    )


def test_via_barrel_clear_of_foreign_track_does_not_fuse(tmp_path: Path) -> None:
    """A via separated from a foreign track by real clearance must NOT bond."""
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
    (uuid "00000000-0000-0000-0000-0000000000f8")
    (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-f8-ref"))
    (property "Value" "1k" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-f8-val"))
    (pad "1" smd roundrect (at 0 0) (size 0.6 0.6) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net 1 "A"))
    (pad "2" smd roundrect (at 5 0) (size 0.6 0.6) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net 2 "B"))
  )
  (segment (start 100 100) (end 103 100) (width 0.2) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-0000000000f9"))
  (segment (start 105 100) (end 106 100) (width 0.2) (layer "F.Cu") (net 2)
    (uuid "00000000-0000-0000-0000-0000000000fa"))
  (via (at 105 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2)
    (uuid "00000000-0000-0000-0000-0000000000fb"))
)
"""
    pcb_path = _write(tmp_path, "b.kicad_pcb", pcb)
    partition = ConnectivityValidator(pcb_path).extract_pad_partition()
    # Net-1 copper (R1.1 + its track) ends 2 mm + clearance away from the
    # net-2 via; only R1.2's net-2 chain may own the via.
    assert frozenset({"R1.1"}) in partition, (
        f"net-1 track ends 1.6 mm clear of the via barrel and must not bond; "
        f"partition={sorted(sorted(c) for c in partition)}"
    )
