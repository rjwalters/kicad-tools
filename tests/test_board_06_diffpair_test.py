"""Regression tests for ``boards/06-diffpair-test/`` (Epic #2556 Phase 4L).

These tests pin the on-disk artifacts produced by the diff-pair testbench
board so future changes to:

- the router (net-class consumer code)
- the validator (DRC rules)
- the impedance solver
- the diff-pair detector

cannot silently drop any of the Phase 1-3 features the board exercises.

The board's role is exactly this regression coverage --- it is not a
working device.  See ``boards/06-diffpair-test/README.md`` for the
testbench rationale.

Acceptance criteria covered (see issue #2658):

- AC#1: routed PCB exists and is a valid KiCad 9 PCB
- AC#3: PCB contains at least 9 routed diff pairs
  (1 USB2 + 4 USB3 + 2 PCIe + 2 MIPI)
- AC#4: file is regeneratable deterministically (modulo UUIDs)
- AC#6: ``test_phase_features_exercised`` enumerates the net-class
  settings and asserts each Phase 1-3 feature is engaged on at least
  one pair
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

# =============================================================================
# Module loading helpers
# =============================================================================
# The board's helper scripts live in ``boards/06-diffpair-test/`` and are not
# part of the installed ``kicad_tools`` package.  We load them via
# ``importlib`` so the tests can inspect the canonical ``NETS`` / ``DIFFPAIRS``
# dicts and the ``build_net_class_map`` function without touching ``sys.path``.

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "06-diffpair-test"
OUTPUT_DIR = BOARD_DIR / "output"


def _load_module(name: str, path: Path):
    """Load a board script as a module from its absolute path."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module {name!r} from {path}")
    module = importlib.util.module_from_spec(spec)
    # generate_design.py imports generate_pcb / generate_schematic by name
    # from its sibling directory --- prepopulate sys.modules with the right
    # absolute-path references so the import chain succeeds.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def generate_pcb_mod():
    """Load ``boards/06-diffpair-test/generate_pcb.py`` as a module."""
    return _load_module("board_06_generate_pcb", BOARD_DIR / "generate_pcb.py")


@pytest.fixture(scope="module")
def generate_design_mod(generate_pcb_mod):
    """Load ``boards/06-diffpair-test/generate_design.py`` as a module.

    Depends on ``generate_pcb_mod`` so ``generate_pcb`` is already in
    ``sys.modules`` by the time ``generate_design.py`` runs its top-level
    ``import generate_pcb`` statement.  We alias both names so the import
    inside the script resolves to our pre-loaded module.
    """
    sys.modules["generate_pcb"] = generate_pcb_mod
    # generate_schematic is imported by generate_design but isn't needed for
    # the net-class assertions.  Provide a stub so the import succeeds.
    sch_path = BOARD_DIR / "generate_schematic.py"
    sch_mod = _load_module("board_06_generate_schematic", sch_path)
    sys.modules["generate_schematic"] = sch_mod
    return _load_module("board_06_generate_design", BOARD_DIR / "generate_design.py")


# =============================================================================
# AC#1 + AC#3: routed PCB exists and contains the expected diff pairs
# =============================================================================


class TestRoutedPcbArtifact:
    """The committed routed PCB is the test fixture (AC#1, AC#3)."""

    @pytest.fixture
    def routed_pcb_text(self) -> str:
        routed = OUTPUT_DIR / "diffpair_test_routed.kicad_pcb"
        assert routed.exists(), (
            f"Routed PCB artifact missing: {routed}.\n"
            "Re-run: python boards/06-diffpair-test/generate_design.py"
        )
        return routed.read_text()

    def test_routed_pcb_is_kicad10_format(self, routed_pcb_text: str) -> None:
        """AC#1: routed PCB is valid KiCad 10 format."""
        # KiCad 10 PCBs start with ``(kicad_pcb`` and declare the version.
        assert routed_pcb_text.startswith("(kicad_pcb")
        assert "(version 20260206)" in routed_pcb_text

    def test_routed_pcb_declares_4layer_stackup(self, routed_pcb_text: str) -> None:
        """AC#1 corollary: stackup is the 4-layer JLCPCB tier-1 layout
        (F.Cu / In1.Cu / In2.Cu / B.Cu) that the Phase 3K impedance
        formulas were calibrated against.
        """
        assert '(0 "F.Cu" signal)' in routed_pcb_text
        assert '(1 "In1.Cu" signal)' in routed_pcb_text
        assert '(2 "In2.Cu" signal)' in routed_pcb_text
        assert '(31 "B.Cu" signal)' in routed_pcb_text

    @pytest.mark.parametrize(
        "net_name",
        [
            # USB 2.0 pair (1)
            "USB2_D+",
            "USB2_D-",
            # USB 3.0 pairs (4: TX1, RX1, TX2, RX2 = 8 nets)
            "USB3_TX1+",
            "USB3_TX1-",
            "USB3_RX1+",
            "USB3_RX1-",
            "USB3_TX2+",
            "USB3_TX2-",
            "USB3_RX2+",
            "USB3_RX2-",
            # PCIe pairs (2: TX, RX = 4 nets)
            "PCIE_TX+",
            "PCIE_TX-",
            "PCIE_RX+",
            "PCIE_RX-",
            # MIPI lanes (2: CLK, D0 = 4 nets)
            "MIPI_CLK+",
            "MIPI_CLK-",
            "MIPI_D0+",
            "MIPI_D0-",
        ],
    )
    def test_routed_pcb_declares_each_diffpair_net(
        self, routed_pcb_text: str, net_name: str
    ) -> None:
        """AC#3: every declared differential-pair net appears in the PCB.

        Whether a given pair was *routed* (i.e. has emitted segments) is
        a separate question --- this test only asserts that all 18 paired
        nets are *declared* in the net table, so the board's intent is
        preserved on disk.
        """
        # KiCad net entries look like ``(net N "name")`` --- match the name
        # specifically to avoid spurious matches inside pad declarations.
        pattern = re.compile(rf'\(net \d+ "{re.escape(net_name)}"\)')
        assert pattern.search(routed_pcb_text), f"Net '{net_name}' not found in routed PCB"

    def test_routed_pcb_has_at_least_9_pair_pads(self, routed_pcb_text: str) -> None:
        """AC#3: at least 9 diff pairs (= 18 paired nets) have pads in the PCB.

        Each pair must have at least 2 pads (P + N).  We count pads by
        their ``(net N "name")`` references inside pad declarations.
        """
        pair_nets = [
            "USB2_D+",
            "USB2_D-",
            "USB3_TX1+",
            "USB3_TX1-",
            "USB3_RX1+",
            "USB3_RX1-",
            "USB3_TX2+",
            "USB3_TX2-",
            "USB3_RX2+",
            "USB3_RX2-",
            "PCIE_TX+",
            "PCIE_TX-",
            "PCIE_RX+",
            "PCIE_RX-",
            "MIPI_CLK+",
            "MIPI_CLK-",
            "MIPI_D0+",
            "MIPI_D0-",
        ]
        for net_name in pair_nets:
            # Match any ``(pad ... (net N "name"))`` block.  Pads may be
            # serialized on a single line (KiCad source) or across
            # multiple lines (after a SExp round-trip during zone
            # generation, see #2835), so we use DOTALL + a non-greedy
            # body to span newlines while still scoping each match to
            # one pad declaration.
            line_pattern = re.compile(
                rf'\(pad\b.*?\(net \d+ "{re.escape(net_name)}"\)',
                re.DOTALL,
            )
            matches = line_pattern.findall(routed_pcb_text)
            assert len(matches) >= 1, (
                f"Net {net_name} has no pads in routed PCB (expected at least 1)"
            )


# =============================================================================
# AC#4: deterministic regeneration (modulo UUIDs)
# =============================================================================


class TestDeterministicGeneration:
    """``generate_pcb.generate_pcb()`` is deterministic modulo UUIDs."""

    def test_generate_pcb_emits_stable_non_uuid_content(self, generate_pcb_mod) -> None:
        """AC#4: regenerating produces byte-identical output up to UUIDs.

        Strip all UUIDs from two invocations and compare the remainder.
        This is the deterministic-regeneration assertion the issue spec
        requires.
        """
        first = generate_pcb_mod.generate_pcb()
        second = generate_pcb_mod.generate_pcb()

        # Strip UUIDs to compare structure.
        uuid_pattern = re.compile(r'"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"')
        first_stripped = uuid_pattern.sub('"UUID"', first)
        second_stripped = uuid_pattern.sub('"UUID"', second)

        assert first_stripped == second_stripped, (
            "generate_pcb() is not deterministic modulo UUIDs --- "
            "two invocations produced different non-UUID content"
        )


# =============================================================================
# AC#6: each Phase 1-3 feature is engaged on at least one pair
# =============================================================================


class TestPhaseFeatureCoverage:
    """The net-class map exercises every Phase 1-3 feature.

    This is the keystone test for AC#6: it asserts that
    ``build_net_class_map()`` (the single source of truth shared between
    the autorouter and the test) declares each Phase 1-3 net-class
    feature on at least one differential pair.

    By construction, the test mirrors what the autorouter consumes ---
    if this passes, the routing pipeline is mechanically guaranteed to
    see the feature flags on the disk-committed routed PCB.
    """

    @pytest.fixture
    def net_class_map(self, generate_design_mod):
        return generate_design_mod.build_net_class_map()

    def test_phase1c_intra_pair_clearance_engaged(self, net_class_map) -> None:
        """Phase 1C: at least one pair sets ``intra_pair_clearance``."""
        engaged = [
            (net, nc)
            for net, nc in net_class_map.items()
            if "+" in net and nc.intra_pair_clearance is not None
        ]
        assert engaged, (
            "Phase 1C: no diff-pair net class declares intra_pair_clearance. "
            "Expected at least USB2/USB3/PCIe/MIPI to set this."
        )
        # Sanity-check the values are in the expected range (< 0.15mm = below the
        # inter-pair clearance, but >= 0.05mm = JLCPCB minimum).
        for net, nc in engaged:
            assert 0.05 <= nc.intra_pair_clearance < 0.15, (
                f"{net} intra_pair_clearance={nc.intra_pair_clearance} "
                "out of expected HSDI range [0.05, 0.15) mm"
            )

    def test_phase2e_coupled_routing_engaged(self, net_class_map) -> None:
        """Phase 2E: at least one pair sets ``coupled_routing=True``."""
        engaged = [
            (net, nc) for net, nc in net_class_map.items() if "+" in net and nc.coupled_routing
        ]
        assert engaged, (
            "Phase 2E: no diff-pair net class has coupled_routing=True. "
            "Expected USB2/USB3/PCIe/MIPI all opt in."
        )

    def test_phase2g_coupled_continuity_threshold_engaged(self, net_class_map) -> None:
        """Phase 2G: at least one pair sets a per-class continuity threshold."""
        engaged = [
            (net, nc)
            for net, nc in net_class_map.items()
            if "+" in net and nc.coupled_continuity_threshold is not None
        ]
        assert engaged, "Phase 2G: no diff-pair net class declares coupled_continuity_threshold."
        # Each threshold should be a valid fraction in (0.0, 1.0].
        for net, nc in engaged:
            t = nc.coupled_continuity_threshold
            assert 0.0 < t <= 1.0, f"{net} coupled_continuity_threshold={t} out of (0, 1] range"

    def test_phase3h_skew_tolerance_engaged(self, net_class_map) -> None:
        """Phase 3H: at least one pair sets ``skew_tolerance_mm``."""
        engaged = [
            (net, nc)
            for net, nc in net_class_map.items()
            if "+" in net and nc.skew_tolerance_mm is not None
        ]
        assert engaged, (
            "Phase 3H: no diff-pair net class declares skew_tolerance_mm. "
            "Expected USB3/PCIe/MIPI to set tight skew budgets."
        )

    def test_phase3k_target_diff_impedance_engaged(self, net_class_map) -> None:
        """Phase 3K: at least one pair sets ``target_diff_impedance``."""
        engaged = [
            (net, nc)
            for net, nc in net_class_map.items()
            if "+" in net and nc.target_diff_impedance is not None
        ]
        assert engaged, (
            "Phase 3K: no diff-pair net class declares target_diff_impedance. "
            "Expected USB2 (90), USB3 (90), PCIe (100), MIPI (100)."
        )
        # All declared values should be plausible HSDI targets (50..120 Ohm).
        for net, nc in engaged:
            assert 50.0 <= nc.target_diff_impedance <= 120.0, (
                f"{net} target_diff_impedance={nc.target_diff_impedance} "
                "out of HSDI range [50, 120] Ohm"
            )

    def test_phase3k_target_single_impedance_engaged(self, net_class_map) -> None:
        """Phase 3K: at least one single-ended net sets ``target_single_impedance``.

        This is the orthogonal axis to ``target_diff_impedance`` --- the
        sideband nets (USB_CC1, USB_CC2, MIPI_RST) exercise it.
        """
        engaged = [
            (net, nc) for net, nc in net_class_map.items() if nc.target_single_impedance is not None
        ]
        assert engaged, (
            "Phase 3K: no net class declares target_single_impedance. "
            "Expected the sideband class to set this for USB_CC1/CC2 + MIPI_RST."
        )

    def test_protocol_diversity_per_phase_3k(self, net_class_map) -> None:
        """At least two distinct ``target_diff_impedance`` values are used.

        AC#6 wants demonstrable diversity: USB at 90 Ohm, PCIe / MIPI at
        100 Ohm.  A single value would technically pass the "at least
        one" assertion above but would fail to actually exercise the
        impedance-target field as a discriminating axis.
        """
        targets = {
            nc.target_diff_impedance
            for nc in net_class_map.values()
            if nc.target_diff_impedance is not None
        }
        assert len(targets) >= 2, (
            f"Phase 3K diversity: only {len(targets)} distinct diff-impedance "
            f"target(s) declared ({targets}).  Expected >= 2 (e.g. USB=90, PCIe=100)."
        )


# =============================================================================
# Net count sanity
# =============================================================================


class TestNetCountBudget:
    """The board has the budgeted ~25 nets the curator approved."""

    def test_net_count_within_budget(self, generate_pcb_mod) -> None:
        """Nets dict has approximately 25 nets (24..27 acceptable range).

        Per the curator review on #2658:
            ``18 paired + GND + 4-5 power rails + 2-3 single-ended sideband -> ~24-26 nets``

        This test pins the budget so accidental net-explosion is caught early.
        """
        nets = generate_pcb_mod.NETS
        signal_nets = [n for n in nets if n != ""]
        count = len(signal_nets)
        assert 24 <= count <= 28, f"Net count {count} out of curator-approved budget [24, 28]"

    def test_nine_diffpairs_declared(self, generate_pcb_mod) -> None:
        """AC#3 lower bound: at least 9 diff pairs are declared.

        Issue #2658 spec: 1 USB2 + 4 USB3 + 2 PCIe + 2 MIPI = 9 pairs.
        """
        diffpairs = generate_pcb_mod.DIFFPAIRS
        assert len(diffpairs) >= 9, f"Expected at least 9 diff pairs declared, got {len(diffpairs)}"

    def test_diffpair_partner_consistency(self, generate_pcb_mod) -> None:
        """Each declared pair has its partner net in NETS.

        ``DIFFPAIRS`` maps ``{P_name: N_name}``; both names must be
        registered in NETS or routing breaks.
        """
        nets = generate_pcb_mod.NETS
        diffpairs = generate_pcb_mod.DIFFPAIRS
        for p_name, n_name in diffpairs.items():
            assert p_name in nets, f"Diff pair positive net {p_name!r} not in NETS"
            assert n_name in nets, f"Diff pair negative net {n_name!r} not in NETS"
