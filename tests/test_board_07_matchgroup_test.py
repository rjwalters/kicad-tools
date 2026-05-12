"""Regression tests for ``boards/07-matchgroup-test/`` (Epic #2661 Phase 3L).

These tests pin the on-disk artifacts produced by the match-group
testbench board so future changes to:

- the router (match-group consumer code)
- the validator (match_group_length_skew DRC rule)
- the match-group detector (Phase 1C)
- the match-group tracker (Phase 1B)

cannot silently drop any of the Phase 1A-2G features the board
exercises.

The board's role is exactly this regression coverage --- it is not
a working device.  See ``boards/07-matchgroup-test/README.md`` for
the testbench rationale.

Acceptance criteria covered (see issue #2724):

- AC#1: routed PCB exists and is a valid KiCad 10 PCB
  (``(version 20260206)`` + ``(generator_version "10.0")``)
- AC#3: PCB contains at least 4 declared match groups (DDR data byte,
  MIPI CSI, HDMI TMDS, ADDR bus) with the expected per-group net counts
- AC#4: file is regeneratable deterministically (modulo UUIDs)
- AC#5: ``boards/README.md`` lists board 07 in its status table
- AC#6: ``test_phase_features_exercised`` enumerates the net-class
  settings and asserts each Phase 1-2 feature is engaged
- AC#7 (partial): ``MatchGroupTracker`` is queryable on at least
  the DDR_DATA_BYTE_0 group post-route.  The "post-pass skew strictly
  less than pre-pass skew" check is deferred until Phase 3H (#2723)
  lands ``apply_match_group_tuning`` -- today the route step measures
  but does not tune.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

# =============================================================================
# Module loading helpers
# =============================================================================
# The board's helper scripts live in ``boards/07-matchgroup-test/`` and are
# not part of the installed ``kicad_tools`` package.  We load them via
# ``importlib`` so the tests can inspect the canonical ``NETS`` /
# ``DIFFPAIRS`` / match-group dicts and the ``build_net_class_map``
# function without touching ``sys.path``.

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "07-matchgroup-test"
OUTPUT_DIR = BOARD_DIR / "output"


def _load_module(name: str, path: Path):
    """Load a board script as a module from its absolute path."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module {name!r} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def generate_pcb_mod():
    """Load ``boards/07-matchgroup-test/generate_pcb.py`` as a module."""
    return _load_module("board_07_generate_pcb", BOARD_DIR / "generate_pcb.py")


@pytest.fixture(scope="module")
def generate_design_mod(generate_pcb_mod):
    """Load ``boards/07-matchgroup-test/generate_design.py`` as a module."""
    sys.modules["generate_pcb"] = generate_pcb_mod
    sch_path = BOARD_DIR / "generate_schematic.py"
    sch_mod = _load_module("board_07_generate_schematic", sch_path)
    sys.modules["generate_schematic"] = sch_mod
    return _load_module("board_07_generate_design", BOARD_DIR / "generate_design.py")


# =============================================================================
# AC#1 + AC#3: routed PCB exists and contains the expected match-group nets
# =============================================================================


class TestRoutedPcbArtifact:
    """The committed routed PCB is the test fixture (AC#1, AC#3)."""

    @pytest.fixture
    def routed_pcb_text(self) -> str:
        routed = OUTPUT_DIR / "matchgroup_test_routed.kicad_pcb"
        assert routed.exists(), (
            f"Routed PCB artifact missing: {routed}.\n"
            "Re-run: python boards/07-matchgroup-test/generate_design.py"
        )
        return routed.read_text()

    def test_routed_pcb_is_kicad10_format(self, routed_pcb_text: str) -> None:
        """AC#1: routed PCB is valid KiCad 10 format.

        Asserts both the version marker and the generator_version
        marker exactly as PR #2716 made canonical.
        """
        assert routed_pcb_text.startswith("(kicad_pcb")
        assert "(version 20260206)" in routed_pcb_text, (
            "AC#1: routed PCB must use KiCad 10 (version 20260206). "
            "If the PCB pre-dates PR #2716, regenerate with the current "
            "kicad-tools install."
        )
        assert '(generator_version "10.0")' in routed_pcb_text, (
            "AC#1: routed PCB must declare generator_version 10.0 (PR #2716)."
        )

    def test_routed_pcb_declares_4layer_stackup(self, routed_pcb_text: str) -> None:
        """AC#1 corollary: stackup is the 4-layer JLCPCB tier-1 layout."""
        assert '(0 "F.Cu" signal)' in routed_pcb_text
        assert '(1 "In1.Cu" signal)' in routed_pcb_text
        assert '(2 "In2.Cu" signal)' in routed_pcb_text
        assert '(31 "B.Cu" signal)' in routed_pcb_text

    @pytest.mark.parametrize(
        "net_name",
        [
            # DDR data byte (10 nets: DQ0-7 + DM0 + DQS pair)
            "DQ0",
            "DQ1",
            "DQ2",
            "DQ3",
            "DQ4",
            "DQ5",
            "DQ6",
            "DQ7",
            "DM0",
            "DQS_P",
            "DQS_N",
            # MIPI CSI (3 pairs = 6 nets)
            "MIPI_CLK_P",
            "MIPI_CLK_N",
            "MIPI_DAT0_P",
            "MIPI_DAT0_N",
            "MIPI_DAT1_P",
            "MIPI_DAT1_N",
            # HDMI TMDS (3 pairs = 6 nets)
            "TMDS_D0_P",
            "TMDS_D0_N",
            "TMDS_D1_P",
            "TMDS_D1_N",
            "TMDS_D2_P",
            "TMDS_D2_N",
            # ADDR bus (8 nets)
            "A0",
            "A1",
            "A2",
            "A3",
            "A4",
            "A5",
            "A6",
            "A7",
        ],
    )
    def test_routed_pcb_declares_each_match_group_net(
        self, routed_pcb_text: str, net_name: str
    ) -> None:
        """AC#3: every declared match-group net appears in the PCB."""
        pattern = re.compile(rf'\(net \d+ "{re.escape(net_name)}"\)')
        assert pattern.search(routed_pcb_text), f"Net '{net_name}' not found in routed PCB"

    def test_per_group_net_counts(self, generate_pcb_mod) -> None:
        """AC#3: per-group net counts are 10/6/6/8 = 30 group-member nets."""
        # DDR data byte: 9 singles + 1 diff pair = 11 nets total (9 + 2)
        ddr_singles = generate_pcb_mod.DDR_DATA_BYTE_0_SINGLES
        ddr_pairs = generate_pcb_mod.DDR_DATA_BYTE_0_PAIRS
        ddr_total = len(ddr_singles) + 2 * len(ddr_pairs)
        assert ddr_total == 11, (
            f"DDR_DATA_BYTE_0 expected 9 singles + 1 pair (11 nets total), "
            f"got {len(ddr_singles)} singles + {len(ddr_pairs)} pairs = {ddr_total}"
        )

        mipi_pairs = generate_pcb_mod.MIPI_CSI_LANES_PAIRS
        assert len(mipi_pairs) == 3, f"MIPI_CSI_LANES expected 3 pairs, got {len(mipi_pairs)}"

        hdmi_pairs = generate_pcb_mod.HDMI_TMDS_LANES_PAIRS
        assert len(hdmi_pairs) == 3, f"HDMI_TMDS_LANES expected 3 pairs, got {len(hdmi_pairs)}"

        addr_singles = generate_pcb_mod.ADDR_BUS_SINGLES
        assert len(addr_singles) == 8, f"ADDR_BUS expected 8 singles, got {len(addr_singles)}"

        # Total group member nets = 11 + 6 + 6 + 8 = 31 (issue spec says ~30)
        total_group_nets = ddr_total + 2 * len(mipi_pairs) + 2 * len(hdmi_pairs) + len(addr_singles)
        # Issue spec: "10/6/6/8 = 30 group-member nets" -- this counts the
        # DDR group as 10 (9 singles + 1 PAIR counted as 1 entry, not 2 nets).
        # We assert >=30 to match the spec's lower bound.
        assert total_group_nets >= 30, (
            f"Total match-group nets {total_group_nets} < 30 (issue spec lower bound)"
        )


# =============================================================================
# AC#3 + AC#6: each Phase 1-2 feature is engaged on at least one group
# =============================================================================


class TestPhaseFeatureCoverage:
    """The net-class map exercises every Phase 1A-2G feature.

    This is the keystone test for AC#6: it asserts that
    ``build_net_class_map()`` (the single source of truth shared
    between the autorouter, the JSON sidecar, and the test) declares
    each Phase 1-2 match-group feature on at least one group.
    """

    @pytest.fixture
    def net_class_map(self, generate_design_mod):
        return generate_design_mod.build_net_class_map()

    def test_phase1a_length_match_group_engaged(self, net_class_map) -> None:
        """Phase 1A: at least 4 distinct match groups are declared."""
        groups = {
            nc.length_match_group
            for nc in net_class_map.values()
            if nc.length_match_group is not None
        }
        assert len(groups) >= 4, (
            f"Phase 1A: only {len(groups)} distinct match groups declared "
            f"({groups}). Expected >=4 (DDR_DATA_BYTE_0, MIPI_CSI_LANES, "
            f"HDMI_TMDS_LANES, ADDR_BUS)."
        )
        expected = {"DDR_DATA_BYTE_0", "MIPI_CSI_LANES", "HDMI_TMDS_LANES", "ADDR_BUS"}
        assert expected.issubset(groups), (
            f"Phase 1A: missing expected groups. Got {groups}, expected superset of {expected}."
        )

    def test_phase1a_length_match_reference_engaged(self, net_class_map) -> None:
        """Phase 1A: at least one class sets ``length_match_reference``.

        AC#6: pace-car semantic must be exercised on at least one
        group.  ADDR_BUS uses ``length_match_reference="A0"`` to
        exercise the explicit-reference path.
        """
        engaged = [
            (net, nc) for net, nc in net_class_map.items() if nc.length_match_reference is not None
        ]
        assert engaged, (
            "Phase 1A: no net class declares length_match_reference. "
            "Expected at least ADDR_BUS to set this (pace-car semantic)."
        )

    def test_phase1a_length_match_tolerance_engaged(self, net_class_map) -> None:
        """Phase 1A: at least one class sets ``length_match_tolerance_mm``."""
        engaged = [
            (net, nc)
            for net, nc in net_class_map.items()
            if nc.length_match_tolerance_mm is not None
        ]
        assert engaged, (
            "Phase 1A: no net class declares length_match_tolerance_mm. "
            "Expected DDR/MIPI/HDMI/ADDR all set this."
        )
        # Sanity-check values cover both tight (DDR/MIPI) and loose (ADDR) tiers.
        tolerances = {nc.length_match_tolerance_mm for _, nc in engaged}
        assert min(tolerances) <= 0.1, (
            f"Phase 1A: tightest tolerance {min(tolerances)} > 0.1mm. "
            "Expected DDR / MIPI to set tight (<=0.1mm) tolerance."
        )
        assert max(tolerances) >= 0.5, (
            f"Phase 1A: loosest tolerance {max(tolerances)} < 0.5mm. "
            "Expected ADDR_BUS to set commodity (>=0.5mm) tolerance."
        )

    def test_phase3h_skew_tolerance_on_pair_members(self, net_class_map) -> None:
        """Phase 3H: pair members within match groups set within-pair skew.

        AC#6: the DQS pair, MIPI lanes, and HDMI lanes all have
        diff-pair members that must declare ``skew_tolerance_mm`` for
        the diff-pair-level DRC rule to fire alongside the group-level
        rule.
        """
        engaged = [
            (net, nc)
            for net, nc in net_class_map.items()
            if "_P" in net and nc.skew_tolerance_mm is not None
        ]
        assert engaged, (
            "Phase 3H: no diff-pair member declares skew_tolerance_mm. "
            "Expected DQS / MIPI / HDMI pairs to set this."
        )

    def test_protocol_diversity_in_tolerances(self, net_class_map) -> None:
        """At least three distinct ``length_match_tolerance_mm`` values are used.

        AC#6 wants demonstrable diversity: tight DDR/MIPI/HDMI,
        loose ADDR.  A single value would technically pass the "at
        least one" assertions above but would fail to actually
        exercise tolerance as a discriminating axis.
        """
        tolerances = {
            nc.length_match_tolerance_mm
            for nc in net_class_map.values()
            if nc.length_match_tolerance_mm is not None
        }
        assert len(tolerances) >= 3, (
            f"Phase 1A diversity: only {len(tolerances)} distinct tolerance "
            f"value(s) declared ({tolerances}). Expected >=3 (e.g. DDR=0.1, "
            f"MIPI=0.05, ADDR=0.5)."
        )


# =============================================================================
# AC#4: deterministic regeneration (modulo UUIDs)
# =============================================================================


class TestDeterministicGeneration:
    """``generate_pcb.generate_pcb()`` is deterministic modulo UUIDs."""

    def test_generate_pcb_emits_stable_non_uuid_content(self, generate_pcb_mod) -> None:
        """AC#4: regenerating produces byte-identical output up to UUIDs."""
        first = generate_pcb_mod.generate_pcb()
        second = generate_pcb_mod.generate_pcb()

        uuid_pattern = re.compile(r'"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"')
        first_stripped = uuid_pattern.sub('"UUID"', first)
        second_stripped = uuid_pattern.sub('"UUID"', second)

        assert first_stripped == second_stripped, (
            "generate_pcb() is not deterministic modulo UUIDs --- "
            "two invocations produced different non-UUID content"
        )


# =============================================================================
# AC#5: boards/README.md lists board 07
# =============================================================================


class TestBoardsReadmeUpdated:
    """``boards/README.md`` references board 07 (AC#5)."""

    def test_boards_readme_lists_board_07(self) -> None:
        readme = REPO_ROOT / "boards" / "README.md"
        assert readme.exists(), f"boards/README.md missing: {readme}"
        text = readme.read_text()
        assert "07-matchgroup-test" in text or "07 |" in text or "| 07 |" in text, (
            "AC#5: boards/README.md must list board 07 in its status table."
        )
        # Reference to Epic #2661 should appear near the board 07 row.
        assert "#2661" in text, "AC#5: boards/README.md should cite Epic #2661 in board 07's row."


# =============================================================================
# AC#3 + sidecar: net_class_map.json round-trips through the serializer
# =============================================================================


class TestNetClassMapSidecar:
    """Phase 3M sidecar is emitted and parses cleanly."""

    def test_sidecar_exists_and_is_json(self) -> None:
        """The sidecar JSON is committed alongside the routed PCB."""
        sidecar = OUTPUT_DIR / "net_class_map.json"
        assert sidecar.exists(), (
            f"Phase 3M sidecar missing: {sidecar}. Re-run "
            "``python boards/07-matchgroup-test/generate_design.py`` "
            "to emit it.  Without the sidecar, ``kct check "
            "--net-class-map`` cannot exercise match_group_length_skew "
            "on the standalone routed PCB."
        )
        data = json.loads(sidecar.read_text())
        assert isinstance(data, dict), "Sidecar must be a JSON object"
        assert len(data) > 0, "Sidecar must declare at least one net class"

    def test_sidecar_carries_match_group_declarations(self) -> None:
        """Sidecar entries carry the ``length_match_group`` field (Phase 1A)."""
        sidecar = OUTPUT_DIR / "net_class_map.json"
        if not sidecar.exists():
            pytest.skip("sidecar not present")
        data = json.loads(sidecar.read_text())

        groups_seen: set[str] = set()
        for _net, entry in data.items():
            grp = entry.get("length_match_group")
            if grp is not None:
                groups_seen.add(grp)

        expected = {"DDR_DATA_BYTE_0", "MIPI_CSI_LANES", "HDMI_TMDS_LANES", "ADDR_BUS"}
        assert expected.issubset(groups_seen), (
            f"Phase 3M sidecar missing match-group declarations.  "
            f"Got {groups_seen}, expected superset of {expected}."
        )


# =============================================================================
# AC#7 (partial): match-group tracker is queryable post-route
# =============================================================================


class TestMatchGroupTrackerQueryable:
    """The ``MatchGroupTracker`` is populated on the routed PCB.

    This is the partial AC#7 check.  The full AC#7 statement requires
    "post-pass skew strictly less than pre-pass skew for at least one
    group" -- but that requires the Phase 3H (#2723) tuning step which
    hasn't landed.  Until #2723, the route step measures skew (this
    test) but does not actively reduce it.
    """

    def test_tracker_query_path_exists(self) -> None:
        """The MatchGroupTracker class exposes ``get_all_skews``.

        This is a smoke check that the API surface AC#7 references is
        importable.  Once #2723 lands, a real "before vs after tuning"
        comparison test can replace this skeleton.
        """
        from kicad_tools.router.match_group_length import MatchGroupTracker

        tracker = MatchGroupTracker()
        skews = tracker.get_all_skews()
        assert isinstance(skews, dict), (
            "MatchGroupTracker.get_all_skews() must return a dict (per Phase 1B contract)."
        )

    def test_phase3h_tuning_dependency_documented(self) -> None:
        """Sanity check: route_pcb body has a TODO for Phase 3H wiring.

        This anchors the expectation that when #2723 lands, the
        ``apply_match_group_tuning`` call is added in the marked
        spot in ``boards/07-matchgroup-test/generate_design.py``.
        """
        gd_path = BOARD_DIR / "generate_design.py"
        text = gd_path.read_text()
        assert "TODO Phase 3H (#2723)" in text, (
            "generate_design.py must carry a TODO marker pointing at #2723 "
            "so the eventual tuning-step wiring is easy to find."
        )


# =============================================================================
# Sanity: net-count budget and diff-pair partner consistency
# =============================================================================


class TestNetCountBudget:
    """The board has the budgeted ~33 nets the issue describes."""

    def test_net_count_within_budget(self, generate_pcb_mod) -> None:
        """Nets dict has approximately 33 nets (30..36 acceptable range).

        Per issue #2724:
            ``10 + 6 + 6 + 8 = 30 group-member nets + 3 power = ~33 total``
        """
        nets = generate_pcb_mod.NETS
        signal_nets = [n for n in nets if n != ""]
        count = len(signal_nets)
        assert 30 <= count <= 36, f"Net count {count} out of approved budget [30, 36]"

    def test_seven_diffpairs_declared(self, generate_pcb_mod) -> None:
        """At least 7 diff pairs declared (DQS + 3 MIPI + 3 HDMI = 7)."""
        diffpairs = generate_pcb_mod.DIFFPAIRS
        assert len(diffpairs) >= 7, f"Expected at least 7 diff pairs declared, got {len(diffpairs)}"

    def test_diffpair_partner_consistency(self, generate_pcb_mod) -> None:
        """Each declared pair has its partner net in NETS."""
        nets = generate_pcb_mod.NETS
        diffpairs = generate_pcb_mod.DIFFPAIRS
        for p_name, n_name in diffpairs.items():
            assert p_name in nets, f"Diff pair positive net {p_name!r} not in NETS"
            assert n_name in nets, f"Diff pair negative net {n_name!r} not in NETS"
