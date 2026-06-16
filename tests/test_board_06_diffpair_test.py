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

        Issue #3413 phase 4: assertions are name-based rather than
        pinning numeric layer IDs.  The recipe's zone-fill round trip
        re-serialises the PCB through ``schema.pcb``'s writer, which
        normalises copper layer IDs to the modern KiCad numbering
        (F.Cu=0, B.Cu=2, In1.Cu=4, In2.Cu=6) -- the generator's
        original 0/1/2/31 numbering does not survive, and both forms
        are valid KiCad 10.
        """
        for layer_name in ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"):
            assert re.search(rf'\(\d+ "{re.escape(layer_name)}" signal\)', routed_pcb_text), (
                f"Copper layer {layer_name} missing from the routed PCB stackup"
            )

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


# =============================================================================
# Manufacturability floor (Issue #3262)
# =============================================================================


class TestManufacturabilityFloor:
    """Pin the committed routed PCB's signal-net completion floor.

    These assertions are about the *artifact* committed in
    ``boards/06-diffpair-test/output/diffpair_test_routed.kicad_pcb`` --
    not the routing algorithm's worst-case output.  The CI gate
    ``check_diffpair_coverage.py`` re-routes the board from scratch and
    enforces a DRC error allowlist + rule-coverage assertion.  These
    tests are the complementary "what we shipped" assertions: they catch
    a PR that accidentally commits a worse routed PCB even if the
    algorithm itself is fine.

    The floor is pinned at the Issue #3262 baseline.  When the routing
    algorithm improves and a tighter floor lands, raise these values in
    the same PR that improves the route.
    """

    @pytest.fixture(scope="class")
    def pcb_segments_by_net(self) -> dict[str, int]:
        """Count segments per net name in the committed routed PCB.

        Returns a dict of ``{net_name: segment_count}``.  A net with
        ``>= 1`` segment is considered to have at least begun routing;
        a net with ``0`` is unrouted.  This is a structural check (the
        connectivity-based ``kct net-status`` check is what the fleet
        report uses, but it requires a heavier loader); for a regression
        floor, "has at least one segment" is a cheap, stable proxy.
        """
        routed = OUTPUT_DIR / "diffpair_test_routed.kicad_pcb"
        assert routed.exists(), f"Routed PCB artifact missing: {routed}"
        text = routed.read_text()

        # Build a (net_number, net_name) map.
        net_id_to_name: dict[int, str] = {}
        for m in re.finditer(r'\(net (\d+) "([^"]+)"\)', text):
            net_id_to_name[int(m.group(1))] = m.group(2)

        # Count segments per net id.  KiCad segments are emitted as
        # multi-line s-expressions:
        #
        #     (segment
        #         (start X Y)
        #         (end   X Y)
        #         (width W)
        #         (layer "F.Cu")
        #         (net N)
        #         (uuid "...")
        #     )
        #
        # so we find each ``(segment`` opener and capture the
        # subsequent ``(net N)`` on a later line.  DOTALL is needed
        # because the body spans newlines; the non-greedy match keeps
        # each capture scoped to one segment.
        counts_by_id: dict[int, int] = {}
        seg_pattern = re.compile(r"\(segment\b.*?\(net (\d+)\)", re.DOTALL)
        for m in seg_pattern.finditer(text):
            nid = int(m.group(1))
            counts_by_id[nid] = counts_by_id.get(nid, 0) + 1
        # Also count via-only nets (occur for pad-bottom escapes).
        via_pattern = re.compile(r"\(via\b.*?\(net (\d+)\)", re.DOTALL)
        for m in via_pattern.finditer(text):
            nid = int(m.group(1))
            counts_by_id[nid] = counts_by_id.get(nid, 0) + 1

        return {
            net_id_to_name[nid]: count
            for nid, count in counts_by_id.items()
            if nid in net_id_to_name
        }

    def test_at_least_14_signal_nets_have_segments(
        self, pcb_segments_by_net: dict[str, int]
    ) -> None:
        """At least 14 of the 21 declared signal nets carry routing.

        Per the #3262 baseline (committed PCB on main):
        - 18 diff-pair nets (9 pairs)
        - 3 single-ended sideband nets (USB_CC1, USB_CC2, MIPI_RST)
        = 21 signal nets

        Post-#3313 (impedance-driven sizing enabled), the committed
        PCB routes 18-20 signal nets depending on seed-dependent
        per-net timeout ordering -- which specific pair gets timed out
        shifts because wider traces (0.375-0.475 mm impedance-resolved
        widths vs the pre-#3313 0.20 mm uniform width) take slightly
        longer per net.  The total-routed count is preserved or
        improved.

        Before #3313, 17 of 21 routed (USB3_RX1-, USB3_TX2-, USB3_RX2-,
        MIPI_RST were the residual unrouted signal nets, primarily
        BGA-49 escape gaps -- see #3270).  We pin the floor at 14 so a
        regression that drops 3+ more nets gets flagged while leaving
        headroom for minor seed-dependent variance.

        Note: a refresh attempt under PR #3273 produced a route that
        looked better on net-count but regressed impedance compliance
        10x (315 errors vs main's 30) because trace widths drifted off
        the 50 ohm target.  This floor catches *coverage* regressions;
        impedance regressions are caught by the strict CI sidecar gate
        (see #3151).
        """
        signal_nets = [
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
            "USB_CC1",
            "USB_CC2",
            "MIPI_RST",
        ]
        routed = [n for n in signal_nets if pcb_segments_by_net.get(n, 0) >= 1]
        assert len(routed) >= 14, (
            f"Manufacturability floor: only {len(routed)}/21 signal nets "
            f"have segments in the committed routed PCB.  Baseline is 17/21 "
            f"(see #3262).  Unrouted nets: "
            f"{[n for n in signal_nets if n not in routed]}"
        )

    def test_each_diffpair_has_at_least_one_side_routed(
        self, pcb_segments_by_net: dict[str, int]
    ) -> None:
        """At most one declared diff pair has BOTH halves unrouted.

        A pair with NEITHER half routed indicates the diff-pair
        detection or coupled-routing path failed catastrophically.
        With both halves unrouted, the diff-pair DRC rules cannot fire
        on the pair at all -- a silent regression that would slip past
        the rule-coverage gate.

        Issue #3313: Post-impedance-driven-sizing the wider corridor
        traces (0.375-0.475 mm) take longer to route than the
        pre-#3313 0.20 mm uniform width.  The negotiated-routing
        wall-clock timeout (240 s) occasionally consumes the budget
        before one pair (typically USB3_TX1+/-) gets routed.  The
        committed PCB chose a different pair-coverage trade-off (it
        had USB3_TX1 routed but missed USB3_RX1-, USB3_TX2-, USB3_RX2-
        instead).  Both have at most ONE pair fully-dropped, so we
        pin the floor at "<= 1 dropped pair" rather than "0 dropped
        pairs" to be robust against this seed-dependent swap while
        still catching catastrophic regressions (2+ pairs lost).
        """
        pairs = [
            ("USB2_D+", "USB2_D-"),
            ("USB3_TX1+", "USB3_TX1-"),
            ("USB3_RX1+", "USB3_RX1-"),
            ("USB3_TX2+", "USB3_TX2-"),
            ("USB3_RX2+", "USB3_RX2-"),
            ("PCIE_TX+", "PCIE_TX-"),
            ("PCIE_RX+", "PCIE_RX-"),
            ("MIPI_CLK+", "MIPI_CLK-"),
            ("MIPI_D0+", "MIPI_D0-"),
        ]
        dropped: list[tuple[str, str]] = []
        for p, n in pairs:
            if pcb_segments_by_net.get(p, 0) < 1 and pcb_segments_by_net.get(n, 0) < 1:
                dropped.append((p, n))
        max_dropped_pairs = 1
        assert len(dropped) <= max_dropped_pairs, (
            f"Manufacturability floor: {len(dropped)} diff pair(s) have "
            f"NEITHER side routed in the committed PCB (max allowed: "
            f"{max_dropped_pairs}): {dropped}.  This usually indicates a "
            "regression in diff-pair detection or the coupled-routing "
            "path."
        )

    def test_impedance_sidecar_trap_lifted(self) -> None:
        """Tripwire test documenting the PR #3273 / PR #3315 impedance-sidecar lift.

        **Trap (historical, lifted by Issue #3313)**: Before #3313 landed
        the committed PCB's trace widths were uniformly ``0.20mm`` for
        the impedance-targeted nets.  At the JLCPCB tier-1 4-layer
        stackup the impedance rule computed 68 Ω against the 50 Ω
        single-ended target (36 % deviation) -- 30 impedance violations
        under ``kct check --net-class-map`` on the committed PCB, ~588
        on a fresh re-route.

        ``kct check`` WITHOUT ``--net-class-map`` did not load the
        impedance solver, so the fresh route looked like an improvement
        (3 errors vs 34) when measured naively -- PR #3273 fell into
        this trap.  PR #3315 codified the tripwire (pinning the modal
        width at 0.20 mm so a future refresh PR would have to explicitly
        touch this assertion).

        **Lift (Issue #3313)**: ``boards/06-diffpair-test/generate_design.py``
        now sets ``APPLY_IMPEDANCE_DRIVEN_SIZING = True`` and the
        refreshed PCB carries the impedance-resolved widths:

        * ``Sideband`` (50 Ω SE -- USB_CC1/CC2, MIPI_RST) -> 0.375 mm
        * ``PCIe`` / ``MIPI`` (100 Ω diff) -> 0.375 mm
        * ``USB2`` / ``USB3`` (90 Ω diff) -> 0.475 mm
        * other nets -> 0.20 mm (unchanged)

        The sidecar measurement reports **0 impedance violations** on
        the refreshed PCB.  Pad-adjacent escape segments taper to
        ``min_trace_width = 0.10 mm`` via the existing neck-down
        mechanic (escape.py:3303-3304 + rules.py:get_neck_down_width's
        new ``base_width`` parameter from #3313) so the BGA-49 / QFN /
        FFC escapes still fit despite the wider corridor traces.

        This assertion pins the new modal width.  If a future PR
        intentionally widens the route again (e.g. switches stackup so
        the 50 Ω SE width changes), the PR must update this test AND
        re-run ``scripts/ci/check_routed_drc.py`` WITH the sidecar to
        prove the impedance-rule count stays at the floor.
        """
        routed = OUTPUT_DIR / "diffpair_test_routed.kicad_pcb"
        assert routed.exists(), f"Routed PCB artifact missing: {routed}"
        text = routed.read_text()

        # Sample width values used by segments.  We do not assert a count
        # because the absolute count varies with router output; we assert
        # the modal value matches the impedance-resolved 50 Ω SE width.
        seg_widths = re.findall(r"\(segment\b[^)]*?\(width ([0-9.]+)\)", text, re.DOTALL)
        if not seg_widths:
            # Pattern matched zero segments -- the regex needs to span
            # newlines, fall back to a permissive search.
            seg_widths = re.findall(r"\(segment\b.*?\(width ([0-9.]+)\)", text, re.DOTALL)

        # Convert to floats and count.
        from collections import Counter

        width_counts = Counter(float(w) for w in seg_widths)
        if not width_counts:
            pytest.fail(
                "Could not extract any segment widths from the committed PCB; "
                "the routed PCB may have been replaced with a fundamentally "
                "different structure.  Re-validate the impedance trap "
                "before loosening this test."
            )

        # Issue #3413 phase 6 update: the diff classes are now sized on
        # the TIGHTLY-COUPLED branch (width solved for the target Zdiff
        # at the recipe's intra_pair_clearance):
        #   USB3  90 Ω @ 0.100 mm gap -> 0.275 mm
        #   USB2  90 Ω @ 0.075 mm gap -> 0.250 mm
        #   PCIe/MIPI 100 Ω @ 0.100 mm gap -> 0.225 mm
        #   Sideband 50 Ω SE (resolver) -> 0.375 mm (unchanged)
        # The historical loosely-coupled 0.475 mm width must be GONE:
        # combined with the recipe's tight gap it measured ~62 Ω (31%
        # off target) and geometrically sealed J1's 0.7 mm channels
        # (0.475 + 2x0.15 clearance = 0.775 mm) -- the root cause of the
        # USB3_RX1- residual.  Modal width is NOT asserted anymore: the
        # phase-4 plane stitching adds ~100 pad-to-via stub traces at
        # 0.2 mm which dominate the modal count and carry no impedance
        # meaning.
        for canary, label in (
            (0.275, "90 Ω tightly-coupled USB3"),
            (0.250, "90 Ω tightly-coupled USB2"),
            (0.225, "100 Ω tightly-coupled PCIe/MIPI"),
            (0.375, "50 Ω SE sideband"),
        ):
            assert any(abs(w - canary) < 0.001 for w in width_counts), (
                f"Committed PCB does not carry the {label} impedance width "
                f"({canary} mm) on any segment.  This is the canary that "
                f"the impedance pipeline (tightly-coupled re-solve, Issue "
                f"#3413 phase 6) is wired through to the emitted geometry. "
                f"Width distribution: {dict(width_counts)}"
            )
        assert not any(abs(w - 0.475) < 0.001 for w in width_counts), (
            f"Committed PCB carries the loosely-coupled 0.475 mm width -- "
            f"the Issue #3413 phase-6 tightly-coupled re-solve regressed "
            f"(see boards/06-diffpair-test/generate_design.py).  Width "
            f"distribution: {dict(width_counts)}"
        )


# =============================================================================
# Issue #3338: strict-gate guard for the committed routed PCB
# =============================================================================


class TestBoard06StrictGateGuard:
    """Issue #3338 -- pin the strict CI gate's blocking-error count on the
    committed routed PCB so a future artifact refresh that drifts on the
    impedance sidecar (the PR #3273 trap) trips this fast unit test before
    the CI ``routed-pcb-drc-check`` job catches it.

    The strict gate (``scripts/ci/check_routed_drc.py``) runs ``kct check``
    with ``--net-class-map`` auto-resolved by
    ``scripts/ci/net_class_map_resolver.py`` (in-process derivation for
    board 06 because no committed ``net_class_map.json`` sidecar lives
    next to the routed PCB).  Without the sidecar the impedance /
    diff-pair-skew / diff-pair-continuity / match-group-skew rule families
    short-circuit to a no-op (see #3151), so a naive ``kct check`` would
    report a count that hides ~hundreds of impedance violations whenever
    the trace widths drift off the 50 / 90 / 100 Ω impedance-resolved
    targets.  PR #3273 fell into this trap; PR #3315 codified the
    impedance modal-width tripwire (see ``test_impedance_sidecar_trap_lifted``
    above) and this test pins the strict-gate count itself so a refresh
    PR is caught by an exact-count assertion rather than only the modal-
    width invariant.

    Mirrors ``tests/test_board_05_drc_allowlist.py`` in shape (per-board
    strict-gate guard) but routes through the sidecar resolver so the
    impedance / diff-pair rule families are actually counted.

    The expected count is sourced from the live measurement on the
    committed PCB (Issue #3413 phases 4-6 refresh):

    *   strict gate WITH sidecar = ``33`` blocking errors
    *   advisory ``connectivity`` = ``2`` (GND + +1V2 -- the analyzer's
        per-net model cannot follow the pad -> stub -> via -> plane-fill
        chain the phase-4 stitching uses; the recipe's copper-union
        audit (and ``TestPourCopperUnionAudit`` below) verifies all 5
        pour nets are GENUINELY one copper component, so these 2 are
        analyzer false positives, tracked with the #3482 analyzer gap)

    Blocking composition (Issue #3507 refresh -- the optimizer/nudge
    grid re-marking fix): 9 ``diffpair_length_skew`` + 9
    ``diffpair_routing_continuity`` (single-ended fallback measurements
    -- the coupled phase still converges 0/9, the board's remaining
    quality phase; 8+8 -> 9+9 because the refreshed route engages one
    more measurable pair) + 2 ``clearance_segment_via`` (USB_CC2 vs
    USB2_D+ grid-quantization grazes at the J1 fan-out).  The previous
    17-error USB3_RX1+/RX1- overlap cluster (7 intra + 10 seg-via) is
    RETIRED: the grid-transactional optimize/nudge passes
    (``optimize_routes_grid_synced`` + the resync inside
    ``drc_verify_and_nudge``) collision-check against the TRUE copper
    state and never introduce the overlap (the recipe's 6b solo
    re-route repair reports "No physically-overlapping pair sides
    detected").  33 -> 20.

    Issue #3527 (2026-06-11): +2 ``clearance_segment_zone`` (the new
    segment-vs-foreign-zone-fill rule surfaced two pre-existing USB_CC1
    grazes against the GND In1.Cu fill at (14.397, 12.083) -- stale-fill
    defects that were always in the committed copper, newly visible).
    Artifact fix tracked in Issue #3554.  20 -> 22.

    PR #3548 (2026-06-11, issues #3515 + #3554): the USB_CC1 In1.Cu
    corridor moved to clear the USB2_D- via barrel and the GND In1.Cu
    fill was regenerated via ``kct zones fill``, retiring both
    ``clearance_segment_zone`` findings.  22 -> 20.

    Re-baselined 2026-06-13 (Issue #3556): the new ``clearance_pad_zone``
    rule (the via/pad sibling of #3527's segment-vs-zone-fill rule)
    surfaces 1 pre-existing finding -- a GND pad 0.093mm from the +3V3
    fill (< 0.102mm jlcpcb minimum), a stale-pour-carve that was invisible
    because no gate compared pad copper to zone fill.  20 -> 21; the
    tolerance floor in .github/routed-drc-tolerance.yml rises to match.

    Re-baselined 2026-06-16 (Issue #3740): the residual combined-engine
    cleanup retired the 3 fixable findings.  The 2 ``clearance_segment_via``
    near-shorts (USB_CC2 vs USB2_D+ via, ~0.011/0.014mm) were re-routed to
    clear the via by >= 0.1016mm; the 2 sub-minimum 0.100mm MIPI_RST
    neck-down escapes were widened to 0.1016mm (and the recipe's
    ``min_trace_width`` corrected 0.10 -> 0.1016 so a future regenerate
    keeps them legal); the 1 ``clearance_pad_zone`` (J1-S2 GND vs +3V3 fill)
    was cleared by regenerating the +3V3 B.Cu fill via ``kct zones fill``.
    The +1V8 B.Cu zone was preserved from the pre-refill artifact because a
    fresh fill strands U4.6 (the historical incident behind PR #3725) -- the
    copper-union audit below confirms all 5 pour nets remain one component.
    kicad-cli now reports 0 errors / 0 unconnected against the board's own
    ``.kicad_dru``.  The remaining 21 -> 18 is the diff-pair quality block
    (9 ``diffpair_length_skew`` + 9 ``diffpair_routing_continuity``; coupled
    convergence is still 0/9, exit clause (a) tracked in #3540-#3544).
    """

    EXPECTED_STRICT_GATE_ERRORS = 18
    EXPECTED_ADVISORY_CONNECTIVITY = 2

    @pytest.fixture(scope="class")
    def routed_pcb(self) -> Path:
        routed = OUTPUT_DIR / "diffpair_test_routed.kicad_pcb"
        if not routed.exists():
            pytest.skip(
                f"Routed PCB artifact missing: {routed}.  Re-run "
                "`python boards/06-diffpair-test/generate_design.py --step route "
                "--seed 42` to regenerate."
            )
        return routed

    @pytest.fixture(scope="class")
    def strict_gate_result(self, routed_pcb: Path) -> dict:
        """Invoke ``kct check`` with the auto-resolved impedance sidecar.

        Mirrors the exact invocation used by
        ``scripts/ci/check_routed_drc.py`` so this fast unit test produces
        the same number CI gates on.
        """
        import json
        import subprocess

        # Import the resolver the same way the strict gate script does.
        ci_dir = REPO_ROOT / "scripts" / "ci"
        sys.path.insert(0, str(ci_dir))
        try:
            from net_class_map_resolver import resolve_net_class_map_sidecar  # type: ignore
        finally:
            sys.path.pop(0)

        with resolve_net_class_map_sidecar(routed_pcb) as sidecar:
            assert sidecar is not None, (
                "Board 06 net-class-map sidecar resolution returned None; the "
                "in-process derivation from "
                "``boards/06-diffpair-test/generate_design.py::build_net_class_map`` "
                "failed.  Without the sidecar the impedance / diff-pair / "
                "match-group rule families short-circuit to a no-op and the "
                "PR #3273 trap re-opens."
            )

            cmd = [
                "uv",
                "run",
                "kct",
                "check",
                str(routed_pcb),
                "--mfr",
                "jlcpcb",
                "--errors-only",
                "--format",
                "json",
                "--net-class-map",
                str(sidecar),
            ]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                cwd=REPO_ROOT,
                timeout=180,
            )
            assert proc.returncode in (0, 2), (
                f"kct check exited {proc.returncode} on {routed_pcb}.\nstderr:\n{proc.stderr}"
            )
            return json.loads(proc.stdout)

    def test_strict_gate_blocking_count_pinned(self, strict_gate_result: dict) -> None:
        """Blocking-error count under the impedance sidecar matches the floor.

        The CI gate's ``_count_blocking_errors`` filters advisory rules
        (``connectivity``) out of the count it compares against the
        allowlist.  We apply the same filter here so the pinned value
        matches what the CI gate sees.

        A FAILURE here means one of:

        * A real routing regression that introduced new DRC violations
          (e.g., a refresh PR drifted on impedance widths and triggered
          ~hundreds of impedance violations -- the PR #3273 trap).
        * A router improvement that DROPPED the count below 18 -- in that
          case tighten ``EXPECTED_STRICT_GATE_ERRORS`` in the SAME PR
          and tighten the allowlist in ``.github/routed-drc-tolerance.yml``
          (currently 18; the strict gate's drift warning will be your
          guide).  Driving the remaining 18 to 0 is the diff-pair coupled-
          convergence work tracked in #3540-#3544.
        """
        from kicad_tools.validate.checker import DRCChecker

        violations = strict_gate_result.get("violations", [])
        blocking = 0
        for v in violations:
            if not isinstance(v, dict):
                continue
            if v.get("severity", "error") != "error":
                continue
            rule_id = v.get("rule_id", "")
            if not isinstance(rule_id, str):
                continue
            if DRCChecker.is_advisory_rule(rule_id):
                continue
            blocking += 1

        assert blocking == self.EXPECTED_STRICT_GATE_ERRORS, (
            f"Strict-gate blocking-error count on the committed routed PCB "
            f"is {blocking}; expected {self.EXPECTED_STRICT_GATE_ERRORS} "
            f"(pinned by Issue #3338).  If a refresh PR INCREASED this, "
            f"the impedance sidecar likely drifted -- re-run "
            f"``scripts/ci/check_routed_drc.py "
            f"boards/06-diffpair-test/output/diffpair_test_routed.kicad_pcb`` "
            f"to confirm and either revert the refresh or accept the new "
            f"floor.  If a router improvement DECREASED this, tighten "
            f"``EXPECTED_STRICT_GATE_ERRORS`` AND "
            f"``.github/routed-drc-tolerance.yml:767`` in the same PR."
        )

    def test_strict_gate_within_allowlist(self, strict_gate_result: dict) -> None:
        """Belt-and-braces: also verify the blocking count is within the
        committed allowlist (the CI gate's actual comparison)."""
        import yaml

        from kicad_tools.validate.checker import DRCChecker

        allowlist_path = REPO_ROOT / ".github" / "routed-drc-tolerance.yml"
        if not allowlist_path.exists():
            pytest.skip(f"Allowlist file not found at {allowlist_path}")

        data = yaml.safe_load(allowlist_path.read_text())
        tolerances = data.get("tolerances", {})
        key = "boards/06-diffpair-test/output/diffpair_test_routed.kicad_pcb"
        assert key in tolerances, (
            f"Board 06 entry {key!r} missing from allowlist {allowlist_path}.  "
            f"Issue #3740 tightened the floor to 18 (the residual diff-pair "
            f"coupled-convergence block); if board 06 now reaches 0 the entry "
            f"should be removed entirely (per the file's policy header) and "
            f"this test updated."
        )
        allowed = tolerances[key]

        violations = strict_gate_result.get("violations", [])
        blocking = sum(
            1
            for v in violations
            if isinstance(v, dict)
            and v.get("severity", "error") == "error"
            and isinstance(v.get("rule_id", ""), str)
            and not DRCChecker.is_advisory_rule(v.get("rule_id", ""))
        )
        assert blocking <= allowed, (
            f"Board 06 routed PCB strict-gate blocking-error count "
            f"({blocking}) exceeds allowlist value ({allowed}) from "
            f"{allowlist_path.relative_to(REPO_ROOT)}.  This is a routing "
            f"regression -- revert the offending change or raise the "
            f"allowlist with reviewer sign-off and a tracking-issue link."
        )

    def test_advisory_connectivity_pinned(self, strict_gate_result: dict) -> None:
        """Advisory ``connectivity`` count pinned at 2 (GND + +1V2).

        Issue #3413 phases 4-6: all 21 signal nets are routed (the
        historical USB3_TX1+/USB3_TX1-/MIPI_RST incompletes are gone)
        and every pour net is GENUINELY one copper component per the
        copper-union audit (``TestPourCopperUnionAudit``).  The 2
        remaining advisory entries are ``NetStatusAnalyzer`` false
        positives: its per-net model cannot follow the
        pad -> stub -> via -> plane-fill chain the phase-4 stitching
        uses (the inverse face of the #3482 analyzer gap).  A drift in
        this count means the stitch/repair pipeline gained or lost
        coverage -- investigate with the copper-union audit before
        updating the pin.
        """
        violations = strict_gate_result.get("violations", [])
        connectivity = sum(
            1 for v in violations if isinstance(v, dict) and v.get("rule_id") == "connectivity"
        )
        assert connectivity == self.EXPECTED_ADVISORY_CONNECTIVITY, (
            f"Advisory connectivity count on the committed routed PCB is "
            f"{connectivity}; expected {self.EXPECTED_ADVISORY_CONNECTIVITY} "
            f"(GND + +1V2 analyzer false positives; see #3482).  A change "
            f"here indicates the stitch/repair pipeline gained or lost "
            f"coverage -- investigate before updating the pin."
        )


# =============================================================================
# Issue #3413 phase 4: copper-union pour-connectivity audit on the artifact
# =============================================================================


class TestPourCopperUnionAudit:
    """The committed artifact's pour nets must be GEOMETRICALLY connected.

    Issue #3413 phase 4 / issue #3482: ``NetStatusAnalyzer`` counts a pad
    as zone-connected when it falls inside the zone's *boundary* polygon
    even if the zone produced zero (or islanded) filled polygons -- the
    false-positive mode that masked board 06's dead +1V8/+1V2 pours and
    softstart's dead AC_NEUTRAL pour (PR #3481).  This test runs the
    recipe's shapely copper-union audit (``_audit_pour_nets``) on the
    committed routed PCB, so a future artifact refresh that ships
    boundary-only "planes" fails here even while the analyzer-based
    connectivity rule stays green.
    """

    def test_committed_artifact_pour_nets_connected(self, generate_design_mod):
        pytest.importorskip("shapely")
        routed = OUTPUT_DIR / "diffpair_test_routed.kicad_pcb"
        assert routed.exists(), f"Routed PCB artifact missing: {routed}"

        pour_nets = list(generate_design_mod.POUR_NETS)
        audit = generate_design_mod._audit_pour_nets(routed, pour_nets)

        failures: list[str] = []
        for net in pour_nets:
            info = audit[net]
            if not info["connected"]:
                stranded = [[p for p, _ in group] for group in info["pad_groups"][1:]]
                failures.append(
                    f"{net}: {len(info['pad_groups'])} disjoint copper "
                    f"components; stranded pads: {stranded}"
                )
            if info["zero_fill_zones"]:
                failures.append(
                    f"{net}: {info['zero_fill_zones']} fill-enabled zone(s) "
                    f"with ZERO filled polygons (dead pour)"
                )
        assert not failures, (
            "Copper-union pour audit failed on the committed artifact "
            "(plane nets are not genuinely connected):\n  "
            + "\n  ".join(failures)
            + "\nRe-run: PYTHONHASHSEED=42 python "
            "boards/06-diffpair-test/generate_design.py --step route --seed 42"
        )
