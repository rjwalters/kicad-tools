"""Tests for the thermal-via stitching feature (issue #2900).

The thermal-stitch CLI mode (``kct stitch --thermal``) selects MOSFET /
heat-sink pads via a multi-signal heuristic and drops an array of vias
under or around each qualifying pad.  These tests exercise:

* :func:`find_thermal_pad_candidates` heuristic (footprint pattern,
  reference prefix, pad-size signal, AND target-net membership).
* :func:`generate_thermal_via_positions` placement geometry
  (under-pad grid vs. halo ring).
* :func:`run_thermal_stitch` end-to-end on a synthetic 2-FET fixture:
  via counts, idempotency, plane-net safety on non-MOSFET boards.
"""

from pathlib import Path

import pytest

from kicad_tools.cli.stitch_cmd import (
    DEFAULT_THERMAL_FOOTPRINT_PATTERNS,
    PadInfo,
    _matches_footprint_pattern,
    _matches_reference_prefix,
    find_thermal_pad_candidates,
    generate_thermal_via_positions,
    main,
    run_thermal_stitch,
)
from kicad_tools.core.sexp_file import load_pcb

# Synthetic 2-FET fixture: two TO-220-3 MOSFETs on F.Cu with their drain
# pads (pad "2") on net "VMOTOR", plus a GND zone underneath on In1.Cu.
# Layer stack: F.Cu / In1.Cu / In2.Cu / B.Cu.
TWO_FET_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "VMOTOR")
  (net 3 "GATE_AH")
  (net 4 "PHASE_A")
  (zone (net 2) (net_name "VMOTOR") (layer "In1.Cu") (uuid "zone-vm-uuid")
    (name "VMOTOR_plane")
    (connect_pads (clearance 0.2))
    (min_thickness 0.2)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
    (polygon (pts (xy 100 100) (xy 150 100) (xy 150 150) (xy 100 150)))
  )
  (footprint "Package_TO_SOT_THT:TO-220-3_Vertical"
    (layer "F.Cu")
    (uuid "11111111-1111-1111-1111-111111111111")
    (at 110 120)
    (property "Reference" "Q1" (at 0 -5) (layer "F.SilkS") (uuid "ref-q1"))
    (pad "1" thru_hole rect (at -2.54 0) (size 1.8 1.8) (drill 1.0) (layers "*.Cu" "*.Mask") (net 3 "GATE_AH"))
    (pad "2" thru_hole oval (at 0 0) (size 1.8 1.8) (drill 1.0) (layers "*.Cu" "*.Mask") (net 2 "VMOTOR"))
    (pad "3" thru_hole oval (at 2.54 0) (size 1.8 1.8) (drill 1.0) (layers "*.Cu" "*.Mask") (net 4 "PHASE_A"))
  )
  (footprint "Package_TO_SOT_THT:TO-220-3_Vertical"
    (layer "F.Cu")
    (uuid "22222222-2222-2222-2222-222222222222")
    (at 130 120)
    (property "Reference" "Q2" (at 0 -5) (layer "F.SilkS") (uuid "ref-q2"))
    (pad "1" thru_hole rect (at -2.54 0) (size 1.8 1.8) (drill 1.0) (layers "*.Cu" "*.Mask") (net 3 "GATE_AH"))
    (pad "2" thru_hole oval (at 0 0) (size 1.8 1.8) (drill 1.0) (layers "*.Cu" "*.Mask") (net 2 "VMOTOR"))
    (pad "3" thru_hole oval (at 2.54 0) (size 1.8 1.8) (drill 1.0) (layers "*.Cu" "*.Mask") (net 4 "PHASE_A"))
  )
)
"""


# Negative fixture: a board with only SMD caps on GND -- no MOSFETs.
# After thermal-stitch we must not generate ANY vias.
NO_MOSFET_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3.3V")
  (zone (net 1) (net_name "GND") (layer "In1.Cu") (uuid "zone-gnd-uuid")
    (name "GND_plane")
    (connect_pads (clearance 0.2))
    (min_thickness 0.2)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
    (polygon (pts (xy 100 100) (xy 150 100) (xy 150 150) (xy 100 150)))
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "33333333-3333-3333-3333-333333333333")
    (at 110 110)
    (property "Reference" "C1" (at 0 -1.5) (layer "F.SilkS") (uuid "ref-c1"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "+3.3V"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "44444444-4444-4444-4444-444444444444")
    (at 120 110)
    (property "Reference" "R1" (at 0 -1.5) (layer "F.SilkS") (uuid "ref-r1"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "+3.3V"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND"))
  )
)
"""


# Large exposed-pad fixture: a single QFN-style footprint with a 5x5 mm
# central thermal pad on GND.  Exercises the under-pad placement mode.
LARGE_EP_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (zone (net 1) (net_name "GND") (layer "In1.Cu") (uuid "zone-gnd2-uuid")
    (name "GND_plane")
    (connect_pads (clearance 0.2))
    (min_thickness 0.2)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
    (polygon (pts (xy 100 100) (xy 150 100) (xy 150 150) (xy 100 150)))
  )
  (footprint "Package_DFN_QFN:QFN-32-1EP_5x5mm_P0.5mm_EP3.45x3.45mm"
    (layer "F.Cu")
    (uuid "55555555-5555-5555-5555-555555555555")
    (at 125 125)
    (property "Reference" "U1" (at 0 -4) (layer "F.SilkS") (uuid "ref-u1"))
    (pad "33" smd rect (at 0 0) (size 3.45 3.45) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND"))
  )
)
"""


@pytest.fixture
def two_fet_pcb(tmp_path: Path) -> Path:
    pcb = tmp_path / "two_fet.kicad_pcb"
    pcb.write_text(TWO_FET_PCB)
    return pcb


@pytest.fixture
def no_mosfet_pcb(tmp_path: Path) -> Path:
    pcb = tmp_path / "no_mosfet.kicad_pcb"
    pcb.write_text(NO_MOSFET_PCB)
    return pcb


@pytest.fixture
def large_ep_pcb(tmp_path: Path) -> Path:
    pcb = tmp_path / "large_ep.kicad_pcb"
    pcb.write_text(LARGE_EP_PCB)
    return pcb


class TestFootprintPatternMatch:
    """:func:`_matches_footprint_pattern` heuristic helper."""

    def test_to220_matches(self) -> None:
        assert _matches_footprint_pattern(
            "Package_TO_SOT_THT:TO-220-3_Vertical",
            DEFAULT_THERMAL_FOOTPRINT_PATTERNS,
        )

    def test_dpak_matches(self) -> None:
        assert _matches_footprint_pattern(
            "Package_TO_SOT_SMD:TO-252-3_TabPin2",
            DEFAULT_THERMAL_FOOTPRINT_PATTERNS,
        )

    def test_qfn_ep_matches(self) -> None:
        assert _matches_footprint_pattern(
            "Package_DFN_QFN:QFN-32-1EP_5x5mm_P0.5mm",
            DEFAULT_THERMAL_FOOTPRINT_PATTERNS,
        )

    def test_capacitor_does_not_match(self) -> None:
        assert not _matches_footprint_pattern(
            "Capacitor_SMD:C_0402_1005Metric",
            DEFAULT_THERMAL_FOOTPRINT_PATTERNS,
        )

    def test_resistor_does_not_match(self) -> None:
        assert not _matches_footprint_pattern(
            "Resistor_SMD:R_0402_1005Metric",
            DEFAULT_THERMAL_FOOTPRINT_PATTERNS,
        )

    def test_empty_returns_false(self) -> None:
        assert not _matches_footprint_pattern("", DEFAULT_THERMAL_FOOTPRINT_PATTERNS)


class TestReferencePrefixMatch:
    """:func:`_matches_reference_prefix` heuristic helper."""

    def test_q_prefix_with_digit(self) -> None:
        assert _matches_reference_prefix("Q1", ("Q",))
        assert _matches_reference_prefix("Q12", ("Q",))

    def test_q_prefix_alone(self) -> None:
        # Bare "Q" with no digit is unusual but should match (designators
        # rarely lack numbers but we don't want a false negative).
        assert _matches_reference_prefix("Q", ("Q",))

    def test_r_does_not_match_q(self) -> None:
        assert not _matches_reference_prefix("R1", ("Q",))

    def test_qfn_reference_does_not_match_q_prefix(self) -> None:
        # "QFN1" starts with "Q" but the next char is a letter, not a
        # digit -- should not match as a transistor designator.
        assert not _matches_reference_prefix("QFN1", ("Q",))

    def test_multiple_prefixes(self) -> None:
        assert _matches_reference_prefix("U5", ("Q", "U"))
        assert _matches_reference_prefix("Q1", ("Q", "U"))
        assert not _matches_reference_prefix("R5", ("Q", "U"))


class TestFindThermalPadCandidates:
    """The thermal-pad selector pulls pads that satisfy the multi-signal
    heuristic AND are on a target plane net."""

    def test_finds_to220_drain_pads(self, two_fet_pcb: Path) -> None:
        sexp = load_pcb(two_fet_pcb)
        candidates = find_thermal_pad_candidates(sexp, net_names={"VMOTOR"})

        # Q1 pad 2 + Q2 pad 2 -- the gates / sources are on non-plane
        # nets so they are filtered out by the net membership precondition.
        refs = {(c.pad.reference, c.pad.pad_number) for c in candidates}
        assert refs == {("Q1", "2"), ("Q2", "2")}

        # All candidates should be flagged by both footprint-name match
        # (TO-220) and reference prefix (Q*).
        for cand in candidates:
            assert cand.matched_by_footprint
            assert cand.matched_by_reference

    def test_ignores_non_target_net(self, two_fet_pcb: Path) -> None:
        sexp = load_pcb(two_fet_pcb)
        # GND has no pads on this fixture; thermal stitcher must return
        # nothing rather than dumping vias randomly.
        candidates = find_thermal_pad_candidates(sexp, net_names={"GND"})
        assert candidates == []

    def test_ignores_capacitors_on_gnd(self, no_mosfet_pcb: Path) -> None:
        sexp = load_pcb(no_mosfet_pcb)
        # C1 / R1 are on GND but neither footprint family nor reference
        # prefix nor pad size match the heuristic.  Must return [].
        candidates = find_thermal_pad_candidates(sexp, net_names={"GND"})
        assert candidates == []

    def test_finds_large_pad_via_size_signal(self, large_ep_pcb: Path) -> None:
        sexp = load_pcb(large_ep_pcb)
        candidates = find_thermal_pad_candidates(
            sexp,
            net_names={"GND"},
            # Override defaults so the reference-prefix and footprint
            # signals are NOT used -- the only signal left is pad size.
            footprint_patterns=("__nonexistent_pattern__",),
            reference_prefixes=("__nonexistent_prefix__",),
            min_pad_size=2.0,
        )
        assert len(candidates) == 1
        assert candidates[0].pad.reference == "U1"
        assert candidates[0].matched_by_size
        assert not candidates[0].matched_by_footprint
        assert not candidates[0].matched_by_reference


class TestGenerateThermalViaPositions:
    """:func:`generate_thermal_via_positions` placement geometry."""

    def test_small_pad_uses_halo_ring(self) -> None:
        # TO-220 pad: 1.8 x 1.8 mm.  Cannot fit a 2x2 grid of 0.6mm vias
        # with 0.2mm clearance under the pad (needs ≥1.6mm; pad is 1.8mm
        # but tight, see min_under_pad = 2 * (0.6 + 0.2) = 1.6mm so it
        # passes the threshold).  Force halo mode by raising clearance.
        pad = PadInfo(
            reference="Q1", pad_number="2", net_number=2, net_name="VMOTOR",
            x=110.0, y=120.0, layer="F.Cu", width=1.8, height=1.8,
            pad_type="thru_hole",
        )
        positions = generate_thermal_via_positions(
            pad, vias_per_pad=4, thermal_radius=2.5,
            via_size=0.6, clearance=0.3,  # min_under_pad = 1.8 -> halo
        )
        # Halo mode generates the requested 4 primary candidates plus
        # fallback candidates (intermediate angles + wider ring) so the
        # caller's clearance filter has alternatives when the primary
        # positions are blocked.  Should be at least 4 (the target) and
        # at most ~3 * vias_per_pad (3 passes).
        assert 4 <= len(positions) <= 3 * 4 + 4
        # The first ``vias_per_pad`` positions should sit on the base
        # ring at radius ≥ pad_half + via_size/2 + clearance.
        import math
        for x, y in positions[:4]:
            r = math.hypot(x - pad.x, y - pad.y)
            assert r >= 0.9 + 0.3 + 0.3 - 0.001

    def test_large_pad_uses_under_pad_grid(self) -> None:
        # 5x5 mm exposed pad -- under-pad grid mode.
        pad = PadInfo(
            reference="U1", pad_number="33", net_number=1, net_name="GND",
            x=100.0, y=100.0, layer="F.Cu", width=5.0, height=5.0,
            pad_type="smd",
        )
        positions = generate_thermal_via_positions(
            pad, vias_per_pad=4, thermal_radius=2.5,
            via_size=0.45, clearance=0.2,
        )
        # Should produce at least 4 positions, all inside the pad
        # bounding box.
        assert len(positions) >= 4
        for x, y in positions:
            assert 100.0 - 2.5 <= x <= 100.0 + 2.5
            assert 100.0 - 2.5 <= y <= 100.0 + 2.5

    def test_zero_count_returns_empty(self) -> None:
        pad = PadInfo(
            reference="Q1", pad_number="2", net_number=2, net_name="VMOTOR",
            x=0.0, y=0.0, layer="F.Cu", width=2.0, height=2.0,
            pad_type="smd",
        )
        positions = generate_thermal_via_positions(
            pad, vias_per_pad=0, thermal_radius=2.5,
            via_size=0.45, clearance=0.2,
        )
        assert positions == []


class TestRunThermalStitch:
    """End-to-end thermal-stitch on the 2-FET fixture."""

    def test_places_min_4_vias_per_fet(self, two_fet_pcb: Path) -> None:
        result = run_thermal_stitch(
            pcb_path=two_fet_pcb,
            net_names=["VMOTOR"],
            via_size=0.45,
            drill=0.2,
            clearance=0.2,
            vias_per_pad=4,
            thermal_radius=2.5,
            dry_run=True,  # check positions without writing
        )
        # 2 FETs × 4 vias each minimum
        assert len(result.vias_added) >= 8

        # All vias should be on the VMOTOR net.
        for via in result.vias_added:
            assert via.pad.net_name == "VMOTOR"

        # Vias should cluster near Q1 (110, 120) and Q2 (130, 120).
        near_q1 = [v for v in result.vias_added if abs(v.via_x - 110.0) <= 3.0]
        near_q2 = [v for v in result.vias_added if abs(v.via_x - 130.0) <= 3.0]
        assert len(near_q1) >= 4
        assert len(near_q2) >= 4

    def test_writes_pcb_when_not_dry_run(self, two_fet_pcb: Path) -> None:
        original = two_fet_pcb.read_text()
        result = run_thermal_stitch(
            pcb_path=two_fet_pcb,
            net_names=["VMOTOR"],
            via_size=0.45,
            drill=0.2,
            clearance=0.2,
            vias_per_pad=4,
            dry_run=False,
        )
        assert len(result.vias_added) >= 8
        # File modified.
        new_content = two_fet_pcb.read_text()
        assert new_content != original
        # New vias appear in the PCB text (single-line or multi-line form).
        via_count = new_content.count("(via\n") + new_content.count("(via ")
        assert via_count >= 8

    def test_idempotent_second_run_adds_zero(self, two_fet_pcb: Path) -> None:
        # First run
        r1 = run_thermal_stitch(
            pcb_path=two_fet_pcb,
            net_names=["VMOTOR"],
            via_size=0.45,
            drill=0.2,
            clearance=0.2,
            vias_per_pad=4,
            dry_run=False,
        )
        added_first = len(r1.vias_added)
        assert added_first >= 8

        # Second run: same parameters.  Existing vias should block all
        # new candidate positions (same-net stacking prevention).
        r2 = run_thermal_stitch(
            pcb_path=two_fet_pcb,
            net_names=["VMOTOR"],
            via_size=0.45,
            drill=0.2,
            clearance=0.2,
            vias_per_pad=4,
            dry_run=False,
        )
        assert len(r2.vias_added) == 0

    def test_no_mosfets_no_vias(self, no_mosfet_pcb: Path) -> None:
        """Boards without MOSFETs must produce zero thermal vias."""
        result = run_thermal_stitch(
            pcb_path=no_mosfet_pcb,
            net_names=["GND", "+3.3V"],
            via_size=0.45,
            drill=0.2,
            clearance=0.2,
            vias_per_pad=4,
            dry_run=False,
        )
        assert result.vias_added == []
        assert result.pads_skipped == []
        # The PCB file should NOT have been modified (no vias).
        text = no_mosfet_pcb.read_text()
        assert "(via " not in text and "(via\n" not in text

    def test_under_pad_mode_for_large_exposed_pad(
        self, large_ep_pcb: Path
    ) -> None:
        """A 3.45x3.45 mm QFN exposed pad should get under-pad vias."""
        result = run_thermal_stitch(
            pcb_path=large_ep_pcb,
            net_names=["GND"],
            via_size=0.45,
            drill=0.2,
            clearance=0.2,
            vias_per_pad=4,
            dry_run=True,
        )
        # The QFN exposed pad is on GND and ≥ min_pad_size (2.0mm), so
        # the pad-size signal fires.
        assert len(result.vias_added) >= 4

        # All vias should fall inside the pad bounding box (under-pad
        # mode places them ON the pad copper).
        pad_x, pad_y = 125.0, 125.0
        half = 3.45 / 2
        for via in result.vias_added:
            assert pad_x - half <= via.via_x <= pad_x + half
            assert pad_y - half <= via.via_y <= pad_y + half


class TestCLIThermalFlag:
    """`kct stitch --thermal` end-to-end via the CLI entry point."""

    def test_thermal_flag_invokes_thermal_stitch(
        self, two_fet_pcb: Path, capsys
    ) -> None:
        argv = [str(two_fet_pcb), "--thermal", "--net", "VMOTOR"]
        rc = main(argv)
        assert rc == 0

        # Vias should be in the PCB now.
        content = two_fet_pcb.read_text()
        via_count = content.count("(via\n") + content.count("(via ")
        assert via_count >= 8

    def test_thermal_dry_run_does_not_modify(
        self, two_fet_pcb: Path
    ) -> None:
        original = two_fet_pcb.read_text()
        argv = [str(two_fet_pcb), "--thermal", "--net", "VMOTOR", "--dry-run"]
        rc = main(argv)
        # Dry-run can return 0 (vias would be placed) or 1 (no work);
        # the contract is "no file modification".
        assert rc in (0, 1)
        assert two_fet_pcb.read_text() == original

    def test_thermal_no_mosfets_returns_no_vias(
        self, no_mosfet_pcb: Path
    ) -> None:
        argv = [str(no_mosfet_pcb), "--thermal", "--net", "GND"]
        rc = main(argv)
        # Exit code 1 because no vias were added and no pads already
        # connected (run_thermal_stitch does not set already_connected).
        assert rc == 1
        # PCB should remain via-free.
        text = no_mosfet_pcb.read_text()
        assert "(via " not in text and "(via\n" not in text
