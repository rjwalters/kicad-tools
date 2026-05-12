"""Tests for the build pipeline zones step and auto_create_zones_for_pour_nets."""

from pathlib import Path

import pytest
from rich.console import Console

from kicad_tools.cli.build_cmd import BuildContext, BuildStep, _run_step_zones
from kicad_tools.router.net_class import NetClass
from kicad_tools.schema.pcb import PCB
from kicad_tools.zones.generator import auto_create_zones_for_pour_nets

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

MINIMAL_PCB = """\
(kicad_pcb
  (version 20240108)
  (generator "kicad")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3.3V")
  (net 3 "SDA")
  (gr_rect
    (start 0 0)
    (end 50 50)
    (stroke (width 0.15) (type solid))
    (fill none)
    (layer "Edge.Cuts")
    (uuid "edge-uuid")
  )
)
"""

# Board where every net classifies as POWER or GROUND (issue #2740).
# The build pipeline must SKIP zone creation entirely on such boards so
# the router can route VIN/VOUT/GND as ordinary signal traces.  Without
# this guard, _auto_skip_pour_nets in route_cmd would skip every net,
# nets_to_route would drop to 0, and the router would silently emit a
# PCB with zero copper.
MINIMAL_PCB_ALL_POWER = """\
(kicad_pcb
  (version 20240108)
  (generator "kicad")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "VIN")
  (net 2 "VOUT")
  (net 3 "GND")
  (gr_rect
    (start 0 0)
    (end 50 50)
    (stroke (width 0.15) (type solid))
    (fill none)
    (layer "Edge.Cuts")
    (uuid "edge-uuid")
  )
)
"""

MINIMAL_PCB_NO_POWER = """\
(kicad_pcb
  (version 20240108)
  (generator "kicad")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "SDA")
  (net 2 "SCL")
  (gr_rect
    (start 0 0)
    (end 50 50)
    (stroke (width 0.15) (type solid))
    (fill none)
    (layer "Edge.Cuts")
    (uuid "edge-uuid")
  )
)
"""

MINIMAL_PCB_AGND = """\
(kicad_pcb
  (version 20240108)
  (generator "kicad")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "AGND")
  (net 2 "+3.3V")
  (net 3 "SDA")
  (gr_rect
    (start 0 0)
    (end 50 50)
    (stroke (width 0.15) (type solid))
    (fill none)
    (layer "Edge.Cuts")
    (uuid "edge-uuid")
  )
)
"""

MINIMAL_PCB_GND_ONLY = """\
(kicad_pcb
  (version 20240108)
  (generator "kicad")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "SDA")
  (gr_rect
    (start 0 0)
    (end 50 50)
    (stroke (width 0.15) (type solid))
    (fill none)
    (layer "Edge.Cuts")
    (uuid "edge-uuid")
  )
)
"""


@pytest.fixture
def pcb_with_power(tmp_path: Path) -> Path:
    """PCB containing GND, +3.3V, and one signal net (SDA)."""
    p = tmp_path / "board.kicad_pcb"
    p.write_text(MINIMAL_PCB)
    return p


@pytest.fixture
def pcb_all_power(tmp_path: Path) -> Path:
    """PCB whose only nets are VIN/VOUT/GND (all-power board, issue #2740)."""
    p = tmp_path / "board.kicad_pcb"
    p.write_text(MINIMAL_PCB_ALL_POWER)
    return p


@pytest.fixture
def pcb_no_power(tmp_path: Path) -> Path:
    """PCB with only signal nets (no power/ground)."""
    p = tmp_path / "board.kicad_pcb"
    p.write_text(MINIMAL_PCB_NO_POWER)
    return p


@pytest.fixture
def pcb_agnd(tmp_path: Path) -> Path:
    """PCB with AGND (non-standard ground net name) and +3.3V."""
    p = tmp_path / "board.kicad_pcb"
    p.write_text(MINIMAL_PCB_AGND)
    return p


@pytest.fixture
def pcb_gnd_only(tmp_path: Path) -> Path:
    """PCB with only a GND net."""
    p = tmp_path / "board.kicad_pcb"
    p.write_text(MINIMAL_PCB_GND_ONLY)
    return p


def _make_ctx(pcb_file: Path | None, **kwargs) -> BuildContext:
    """Build a minimal BuildContext for testing."""
    return BuildContext(
        project_dir=pcb_file.parent if pcb_file else Path("/tmp"),
        spec_file=None,
        pcb_file=pcb_file,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# BuildStep enum
# ---------------------------------------------------------------------------


class TestBuildStepEnum:
    """Verify ZONES is part of the enum."""

    def test_zones_value(self):
        assert BuildStep.ZONES.value == "zones"

    def test_step_ordering_in_all(self):
        """ZONES should appear between PLACEMENT and ROUTE in the enum definition."""
        members = list(BuildStep)
        outline_idx = members.index(BuildStep.OUTLINE)
        placement_idx = members.index(BuildStep.PLACEMENT)
        zones_idx = members.index(BuildStep.ZONES)
        route_idx = members.index(BuildStep.ROUTE)
        assert outline_idx < placement_idx < zones_idx < route_idx


# ---------------------------------------------------------------------------
# auto_create_zones_for_pour_nets
# ---------------------------------------------------------------------------


class TestAutoCreateZonesForPourNets:
    """Tests for the generator helper."""

    def test_creates_gnd_and_power_zones(self, pcb_with_power: Path):
        pour_nets = [
            ("GND", NetClass.GROUND),
            ("+3.3V", NetClass.POWER),
        ]
        count = auto_create_zones_for_pour_nets(pcb_with_power, pour_nets)
        assert count == 2

        # Verify zones are in the saved file
        pcb = PCB.load(str(pcb_with_power))
        zone_nets = {z.net_name for z in pcb.zones}
        assert "GND" in zone_nets
        assert "+3.3V" in zone_nets

    def test_gnd_zone_on_bcu(self, pcb_with_power: Path):
        pour_nets = [("GND", NetClass.GROUND)]
        auto_create_zones_for_pour_nets(pcb_with_power, pour_nets)

        pcb = PCB.load(str(pcb_with_power))
        gnd_zones = [z for z in pcb.zones if z.net_name == "GND"]
        assert len(gnd_zones) == 1
        assert gnd_zones[0].layer == "B.Cu"

    def test_power_zone_on_fcu(self, pcb_with_power: Path):
        pour_nets = [("+3.3V", NetClass.POWER)]
        auto_create_zones_for_pour_nets(pcb_with_power, pour_nets)

        pcb = PCB.load(str(pcb_with_power))
        pwr_zones = [z for z in pcb.zones if z.net_name == "+3.3V"]
        assert len(pwr_zones) == 1
        assert pwr_zones[0].layer == "F.Cu"

    def test_gnd_only(self, pcb_gnd_only: Path):
        pour_nets = [("GND", NetClass.GROUND)]
        count = auto_create_zones_for_pour_nets(pcb_gnd_only, pour_nets)
        assert count == 1

        pcb = PCB.load(str(pcb_gnd_only))
        assert len(pcb.zones) == 1
        assert pcb.zones[0].net_name == "GND"

    def test_non_gnd_ground_net_uses_correct_name(self, pcb_agnd: Path):
        """Ground nets with non-standard names (e.g. AGND) must use actual net name."""
        pour_nets = [
            ("AGND", NetClass.GROUND),
            ("+3.3V", NetClass.POWER),
        ]
        count = auto_create_zones_for_pour_nets(pcb_agnd, pour_nets)
        assert count == 2

        pcb = PCB.load(str(pcb_agnd))
        zone_nets = {z.net_name for z in pcb.zones}
        # Zone must be created for "AGND", not hardcoded "GND"
        assert "AGND" in zone_nets, f"Expected 'AGND' in zone nets, got {zone_nets}"
        assert "GND" not in zone_nets, f"'GND' should not appear - got {zone_nets}"
        assert "+3.3V" in zone_nets

        # Verify AGND is on B.Cu (ground layer)
        agnd_zones = [z for z in pcb.zones if z.net_name == "AGND"]
        assert len(agnd_zones) == 1
        assert agnd_zones[0].layer == "B.Cu"

    def test_empty_pour_nets_no_save(self, pcb_with_power: Path):
        original = pcb_with_power.read_text()
        count = auto_create_zones_for_pour_nets(pcb_with_power, [])
        assert count == 0
        # File should not have been modified
        assert pcb_with_power.read_text() == original


# ---------------------------------------------------------------------------
# _run_step_zones
# ---------------------------------------------------------------------------


class TestRunStepZones:
    """Tests for the build pipeline step function."""

    def test_skip_when_no_pcb(self):
        ctx = _make_ctx(pcb_file=None)
        result = _run_step_zones(ctx, Console())
        assert result.success is True
        assert "skipping" in result.message.lower()

    def test_skip_when_pcb_missing(self, tmp_path: Path):
        ctx = _make_ctx(pcb_file=tmp_path / "nonexistent.kicad_pcb")
        result = _run_step_zones(ctx, Console())
        assert result.success is True
        assert "skipping" in result.message.lower()

    def test_skip_when_no_power_nets(self, pcb_no_power: Path):
        ctx = _make_ctx(pcb_file=pcb_no_power)
        result = _run_step_zones(ctx, Console())
        assert result.success is True
        assert "no power" in result.message.lower() or "skipping" in result.message.lower()

    def test_creates_zones(self, pcb_with_power: Path):
        ctx = _make_ctx(pcb_file=pcb_with_power)
        result = _run_step_zones(ctx, Console())
        assert result.success is True
        assert "created" in result.message.lower()
        assert result.output_file == pcb_with_power

        # Verify zones actually exist in the file
        pcb = PCB.load(str(pcb_with_power))
        assert len(pcb.zones) >= 1

    def test_dry_run(self, pcb_with_power: Path):
        ctx = _make_ctx(pcb_file=pcb_with_power, dry_run=True)
        result = _run_step_zones(ctx, Console())
        assert result.success is True
        assert "dry-run" in result.message.lower()

        # File should not have zones
        pcb = PCB.load(str(pcb_with_power))
        assert len(pcb.zones) == 0

    def test_idempotent(self, pcb_with_power: Path):
        """Running zones step twice should not duplicate zones."""
        ctx = _make_ctx(pcb_file=pcb_with_power)
        console = Console()

        # First run creates zones
        result1 = _run_step_zones(ctx, console)
        assert result1.success is True
        assert "created" in result1.message.lower()

        pcb1 = PCB.load(str(pcb_with_power))
        zone_count_1 = len(pcb1.zones)

        # Second run should skip (zones already exist)
        result2 = _run_step_zones(ctx, console)
        assert result2.success is True
        assert "already exist" in result2.message.lower()

        pcb2 = PCB.load(str(pcb_with_power))
        assert len(pcb2.zones) == zone_count_1


# ---------------------------------------------------------------------------
# Edge clearance regression (issue #2496)
# ---------------------------------------------------------------------------


class TestRunStepZonesEdgeClearance:
    """Verify _run_step_zones threads ``edge_clearance`` from manufacturer.

    Regression for issue #2496: ``kct build`` was producing zones whose
    polygon vertices sat exactly on the board outline (0.000 mm clearance),
    causing ``edge_clearance_zone`` DRC violations on JLCPCB-targeted boards.
    The fix looks up ``MfrLimits.min_edge_clearance`` from ``ctx.mfr`` and
    forwards it to :func:`auto_create_zones_for_pour_nets`.
    """

    @staticmethod
    def _bbox(points: list[tuple[float, float]]) -> tuple[float, float, float, float]:
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        return min(xs), min(ys), max(xs), max(ys)

    @staticmethod
    def _assert_inset(
        polygon: list[tuple[float, float]],
        outline_bbox: tuple[float, float, float, float],
        clearance: float,
        epsilon: float = 1e-3,
    ) -> None:
        """Every vertex must be at least ``clearance - epsilon`` from outline."""
        x_min, y_min, x_max, y_max = outline_bbox
        for x, y in polygon:
            assert x - x_min >= clearance - epsilon, (
                f"vertex x={x} too close to left edge x_min={x_min} "
                f"(needs >= {clearance - epsilon}mm)"
            )
            assert x_max - x >= clearance - epsilon, (
                f"vertex x={x} too close to right edge x_max={x_max} "
                f"(needs >= {clearance - epsilon}mm)"
            )
            assert y - y_min >= clearance - epsilon, (
                f"vertex y={y} too close to top edge y_min={y_min} "
                f"(needs >= {clearance - epsilon}mm)"
            )
            assert y_max - y >= clearance - epsilon, (
                f"vertex y={y} too close to bottom edge y_max={y_max} "
                f"(needs >= {clearance - epsilon}mm)"
            )

    def test_jlcpcb_inset_to_min_edge_clearance(self, pcb_with_power: Path):
        """With mfr=jlcpcb, zone polygons must be inset >= 0.3 mm from outline."""
        ctx = _make_ctx(pcb_file=pcb_with_power, mfr="jlcpcb")
        result = _run_step_zones(ctx, Console())
        assert result.success is True

        pcb = PCB.load(str(pcb_with_power))
        assert len(pcb.zones) >= 1, "expected at least one zone to be created"

        # Outline in MINIMAL_PCB is the rect (0,0)-(50,50).
        outline_bbox = (0.0, 0.0, 50.0, 50.0)

        for zone in pcb.zones:
            assert zone.polygon, f"zone {zone.net_name} has no boundary polygon"
            self._assert_inset(zone.polygon, outline_bbox, clearance=0.3)

    def test_oshpark_uses_larger_clearance(self, pcb_with_power: Path):
        """With mfr=oshpark (0.381 mm), zone polygons inset by oshpark value.

        Confirms the value is plumbed from the manufacturer profile, not
        hard-coded to JLCPCB's 0.3 mm.
        """
        ctx = _make_ctx(pcb_file=pcb_with_power, mfr="oshpark")
        result = _run_step_zones(ctx, Console())
        assert result.success is True

        pcb = PCB.load(str(pcb_with_power))
        assert len(pcb.zones) >= 1

        outline_bbox = (0.0, 0.0, 50.0, 50.0)
        for zone in pcb.zones:
            assert zone.polygon, f"zone {zone.net_name} has no boundary polygon"
            self._assert_inset(zone.polygon, outline_bbox, clearance=0.381)

    def test_unknown_manufacturer_falls_back_to_no_inset(self, pcb_with_power: Path) -> None:
        """Unknown manufacturer should not crash; zones still get created.

        ``get_mfr_limits`` raises ``ValueError`` for unknown names; the
        build step swallows that and proceeds with ``edge_clearance=None``,
        matching the existing behaviour before this fix.
        """
        ctx = _make_ctx(pcb_file=pcb_with_power, mfr="not-a-real-manufacturer")
        result = _run_step_zones(ctx, Console())
        assert result.success is True

        pcb = PCB.load(str(pcb_with_power))
        assert len(pcb.zones) >= 1
        # Vertices should sit on the outline (no inset applied)
        for zone in pcb.zones:
            xs = [p[0] for p in zone.polygon]
            ys = [p[1] for p in zone.polygon]
            assert min(xs) <= 0.01
            assert max(xs) >= 49.99
            assert min(ys) <= 0.01
            assert max(ys) >= 49.99


# ---------------------------------------------------------------------------
# All-power-board guard (issue #2740)
# ---------------------------------------------------------------------------


class TestAllPowerBoardGuard:
    """Regression test for issue #2740 — board 01 silent empty-route.

    On boards where every net classifies as POWER or GROUND (e.g.
    01-voltage-divider with VIN/VOUT/GND), ``_run_step_zones`` MUST
    skip zone creation so the router routes those nets as signal
    traces.  Without this guard, ``kct route``'s ``_auto_skip_pour_nets``
    skips every net (each has a zone), ``nets_to_route`` drops to 0,
    the router hard-codes completion = 1.0 and exits 0, and the build
    silently ships a PCB with zero copper segments.

    The guard mirrors the one in
    :func:`kicad_tools.router.auto_pour.auto_pour_if_missing` and both
    call sites share :func:`classify_pour_candidates`.
    """

    def test_all_power_board_skips_zone_creation(self, pcb_all_power: Path):
        """VIN/VOUT/GND-only board must not get zones from kct build."""
        ctx = _make_ctx(pcb_file=pcb_all_power)
        result = _run_step_zones(ctx, Console())

        assert result.success is True
        assert "skipping zone creation" in result.message.lower()
        assert "#2740" in result.message or "power/ground" in result.message.lower()

        # Critical postcondition: NO zones on disk.
        pcb = PCB.load(str(pcb_all_power))
        assert len(pcb.zones) == 0, (
            "All-power board must have zero zones so the router can route "
            "every net as a signal (see issue #2740)."
        )

    def test_all_power_guard_mirrors_auto_pour(self, pcb_all_power: Path):
        """Both call sites must agree the board is all-power.

        Drift between :func:`_run_step_zones` and
        :func:`auto_pour_if_missing` was the root cause of issue #2740.
        Both now share ``classify_pour_candidates`` so they cannot drift.
        """
        from kicad_tools.router.auto_pour import (
            auto_pour_if_missing,
            classify_pour_candidates,
        )

        # Shared helper agrees this is an all-power board.
        net_names = {1: "VIN", 2: "VOUT", 3: "GND"}
        _pour, _signal_count, is_all_power = classify_pour_candidates(net_names)
        assert is_all_power is True

        # auto_pour_if_missing also skips this board.
        count, names = auto_pour_if_missing(pcb_all_power)
        assert count == 0
        assert names == []
        # And the file is unchanged.
        pcb = PCB.load(str(pcb_all_power))
        assert len(pcb.zones) == 0

    def test_mixed_board_still_creates_zones(self, pcb_with_power: Path):
        """A board with power/ground + signal nets must still get zones.

        Guards against over-correction: the all-power guard must only
        fire when every classified net is POWER or GROUND.  MINIMAL_PCB
        contains GND + +3.3V + SDA, so the guard must NOT fire.
        """
        ctx = _make_ctx(pcb_file=pcb_with_power)
        result = _run_step_zones(ctx, Console())

        assert result.success is True
        assert "created" in result.message.lower()

        pcb = PCB.load(str(pcb_with_power))
        # 2 zones expected: GND and +3.3V.  SDA has no zone (it's signal).
        zone_nets = {z.net_name for z in pcb.zones}
        assert "GND" in zone_nets
        assert "+3.3V" in zone_nets
        assert "SDA" not in zone_nets


# ---------------------------------------------------------------------------
# Route-step postcondition (issue #2740 defense-in-depth)
# ---------------------------------------------------------------------------


class TestRouteStepPostcondition:
    """Tests for :func:`build_cmd._check_route_postcondition`.

    This postcondition catches the "silent green empty PCB" failure mode
    where ``kct route`` exits 0 with ``completion = 1.0`` because every
    routable net was auto-skipped (yielding ``nets_to_route == 0``).
    See issue #2740.
    """

    @staticmethod
    def _pcb_with_pads_no_traces(tmp_path: Path) -> tuple[Path, Path]:
        """Build an input/output pair representing the empty-route regression.

        Returns (input_pcb, routed_pcb) where:
        - input_pcb has 3 multi-pad signal nets and zero zones
          (so the postcondition expects segments to be produced).
        - routed_pcb has zero segments and zero vias, but a different
          generator string so it is **not** byte-identical to the input
          (the byte-identical check from issue #2782 is exercised
          separately in :class:`TestRouteStepWritePath`).
        """
        # Minimal PCB with two footprints sharing 3 nets via pads.
        # Each net has 2 pads => 3 multi-pad signal nets to route.
        # The two files share the same structure but differ in the
        # generator string -- this models a router that processed the
        # PCB (rewrote it) but produced zero copper segments.
        pcb_text_input = """\
(kicad_pcb
  (version 20240108)
  (generator "kicad")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "VIN")
  (net 2 "VOUT")
  (net 3 "GND")
  (gr_rect
    (start 0 0)
    (end 50 50)
    (stroke (width 0.15) (type solid))
    (fill none)
    (layer "Edge.Cuts")
    (uuid "edge-uuid")
  )
  (footprint "Test:U1"
    (layer "F.Cu")
    (at 10 10)
    (uuid "fp1-uuid")
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "VIN"))
    (pad "2" smd rect (at 2 0) (size 1 1) (layers "F.Cu") (net 2 "VOUT"))
    (pad "3" smd rect (at 4 0) (size 1 1) (layers "F.Cu") (net 3 "GND"))
  )
  (footprint "Test:U2"
    (layer "F.Cu")
    (at 30 10)
    (uuid "fp2-uuid")
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "VIN"))
    (pad "2" smd rect (at 2 0) (size 1 1) (layers "F.Cu") (net 2 "VOUT"))
    (pad "3" smd rect (at 4 0) (size 1 1) (layers "F.Cu") (net 3 "GND"))
  )
)
"""
        pcb_text_routed = pcb_text_input.replace(
            '(generator "kicad")', '(generator "kicad-tools-router")'
        )
        input_pcb = tmp_path / "input.kicad_pcb"
        input_pcb.write_text(pcb_text_input)
        routed_pcb = tmp_path / "input_routed.kicad_pcb"
        routed_pcb.write_text(pcb_text_routed)
        return input_pcb, routed_pcb

    def test_postcondition_fails_when_zero_segments_for_routable_nets(self, tmp_path: Path):
        """Routed PCB with 0 segments + 0 vias for >=1 signal net must fail."""
        from kicad_tools.cli.build_cmd import _check_route_postcondition

        input_pcb, routed_pcb = self._pcb_with_pads_no_traces(tmp_path)
        result = _check_route_postcondition(input_pcb=input_pcb, routed_pcb=routed_pcb)

        assert result is not None, "Postcondition must fail on empty routed PCB"
        assert result.success is False
        assert "0 segments" in result.message
        assert "#2740" in result.message

    def test_postcondition_ok_when_no_routable_signal_nets(self, tmp_path: Path):
        """No multi-pad signal nets in the input => zero segments is fine."""
        from kicad_tools.cli.build_cmd import _check_route_postcondition

        # Single-pad-per-net PCB: no multi-pad nets => nothing to route.
        pcb_text = """\
(kicad_pcb
  (version 20240108)
  (generator "kicad")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "VCC")
  (gr_rect
    (start 0 0)
    (end 10 10)
    (stroke (width 0.15) (type solid))
    (fill none)
    (layer "Edge.Cuts")
    (uuid "e")
  )
  (footprint "Test:U1"
    (layer "F.Cu")
    (at 1 1)
    (uuid "fp1")
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "VCC"))
  )
)
"""
        input_pcb = tmp_path / "input.kicad_pcb"
        input_pcb.write_text(pcb_text)
        routed_pcb = tmp_path / "input_routed.kicad_pcb"
        routed_pcb.write_text(pcb_text)

        result = _check_route_postcondition(input_pcb=input_pcb, routed_pcb=routed_pcb)
        # No multi-pad signal nets => postcondition cannot fire.
        assert result is None

    def test_postcondition_ok_when_all_nets_have_zones(self, tmp_path: Path):
        """All multi-pad nets are pour-handled with zones => 0 segments is fine."""
        from kicad_tools.cli.build_cmd import _check_route_postcondition

        pcb_text = """\
(kicad_pcb
  (version 20240108)
  (generator "kicad")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "GND")
  (gr_rect
    (start 0 0)
    (end 50 50)
    (stroke (width 0.15) (type solid))
    (fill none)
    (layer "Edge.Cuts")
    (uuid "e")
  )
  (footprint "Test:U1"
    (layer "F.Cu")
    (at 10 10)
    (uuid "fp1")
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "GND"))
  )
  (footprint "Test:U2"
    (layer "F.Cu")
    (at 30 10)
    (uuid "fp2")
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "GND"))
  )
  (zone
    (net 1)
    (net_name "GND")
    (layer "B.Cu")
    (uuid "z1")
    (hatch edge 0.5)
    (connect_pads (clearance 0.25))
    (min_thickness 0.25)
    (fill yes)
    (polygon
      (pts
        (xy 0 0) (xy 50 0) (xy 50 50) (xy 0 50)
      )
    )
  )
)
"""
        input_pcb = tmp_path / "input.kicad_pcb"
        input_pcb.write_text(pcb_text)
        routed_pcb = tmp_path / "input_routed.kicad_pcb"
        routed_pcb.write_text(pcb_text)

        result = _check_route_postcondition(input_pcb=input_pcb, routed_pcb=routed_pcb)
        # GND has a zone => pour-handled => postcondition does not fire.
        assert result is None

    def test_postcondition_ok_with_missing_files(self, tmp_path: Path):
        """Missing files => postcondition returns None (best-effort)."""
        from kicad_tools.cli.build_cmd import _check_route_postcondition

        result = _check_route_postcondition(input_pcb=None, routed_pcb=None)
        assert result is None

        nonexistent = tmp_path / "ghost.kicad_pcb"
        result = _check_route_postcondition(input_pcb=nonexistent, routed_pcb=nonexistent)
        assert result is None


# ---------------------------------------------------------------------------
# Geometric outline allocator (issue #2771)
# ---------------------------------------------------------------------------


def _aabb(points: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    """Return axis-aligned bounding box (x_min, y_min, x_max, y_max)."""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def _aabbs_overlap(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> bool:
    """Return True iff the two AABBs overlap (touching edges allowed)."""
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


class TestAutoPourGeometricOutlines:
    """Tests for the outline allocator added in issue #2771.

    When two or more pour zones land on the same layer, they must use
    *disjoint per-net bounding outlines* rather than the full board
    outline.  Otherwise KiCad's fill resolver awards the entire shared
    region to the highest-priority zone and the siblings receive zero
    copper -- the exact failure mode on board 05 documented in #2771.

    These tests cover the three scenarios called out in the issue's
    acceptance criteria:
      * 2-layer board with 4 power nets on F.Cu + GND on B.Cu
      * 4-layer board with the same pour set (GND on In1.Cu full-outline)
      * Edge case: power net with a single pad (fall back to a small
        default square, not a zero-area polygon)
    """

    # ----- fixtures -----------------------------------------------------

    @staticmethod
    def _make_pcb(tmp_path: Path, layers_block: str, footprints_block: str) -> Path:
        """Build a minimal PCB with the given layers and footprints.

        The board 05 pour set has 5 nets (VMOTOR / +5V / +3.3V / GND /
        PWR_LED).  Footprints are laid out in disjoint clusters along the
        x axis so each net's bounding box is geometrically separated --
        this mirrors the way real boards arrange their power
        sub-circuits and lets the test assert that the bbox allocator
        produces disjoint outlines.
        """
        pcb_text = f"""\
(kicad_pcb
  (version 20240108)
  (generator "kicad")
  (general (thickness 1.6))
  (layers
{layers_block}
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "VMOTOR")
  (net 3 "+5V")
  (net 4 "+3.3V")
  (net 5 "PWR_LED")
  (gr_rect
    (start 0 0)
    (end 100 50)
    (stroke (width 0.15) (type solid))
    (fill none)
    (layer "Edge.Cuts")
    (uuid "edge-uuid")
  )
{footprints_block}
)
"""
        p = tmp_path / "board.kicad_pcb"
        p.write_text(pcb_text)
        return p

    @staticmethod
    def _footprints_for_board05_layout() -> str:
        """Footprints arranged so each power net has a disjoint pad cluster.

        Layout (x positions, all on F.Cu):
          VMOTOR  cluster: x = 10, 12      (with U1, U2)
          +5V     cluster: x = 30, 32      (U3, U4)
          +3.3V   cluster: x = 50, 52      (U5, U6)
          PWR_LED cluster: x = 70, 72      (U7, U8)
          GND     pads at every footprint (return-path style)
        """
        rows = []

        def fp(ref: str, x: float, pwr_net: str) -> str:
            return f"""  (footprint "Test:{ref}"
    (layer "F.Cu")
    (at {x} 25)
    (uuid "fp-{ref}-uuid")
    (property "Reference" "{ref}"
      (at 0 -2 0)
      (layer "F.SilkS")
      (uuid "prop-{ref}-ref-uuid")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (property "Value" "TEST"
      (at 0 2 0)
      (layer "F.Fab")
      (uuid "prop-{ref}-val-uuid")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "GND"))
    (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net {pwr_net} ))
  )"""

        # The board 05 pour set: 4 distinct power nets + GND.
        rows.append(fp("U1", 10, '2 "VMOTOR"'))
        rows.append(fp("U2", 12, '2 "VMOTOR"'))
        rows.append(fp("U3", 30, '3 "+5V"'))
        rows.append(fp("U4", 32, '3 "+5V"'))
        rows.append(fp("U5", 50, '4 "+3.3V"'))
        rows.append(fp("U6", 52, '4 "+3.3V"'))
        rows.append(fp("U7", 70, '5 "PWR_LED"'))
        rows.append(fp("U8", 72, '5 "PWR_LED"'))
        return "\n".join(rows)

    @pytest.fixture
    def two_layer_board05_pcb(self, tmp_path: Path) -> Path:
        layers = '    (0 "F.Cu" signal)\n    (31 "B.Cu" signal)'
        fps = self._footprints_for_board05_layout()
        return self._make_pcb(tmp_path, layers, fps)

    @pytest.fixture
    def four_layer_board05_pcb(self, tmp_path: Path) -> Path:
        layers = (
            '    (0 "F.Cu" signal)\n'
            '    (1 "In1.Cu" signal)\n'
            '    (2 "In2.Cu" signal)\n'
            '    (31 "B.Cu" signal)'
        )
        fps = self._footprints_for_board05_layout()
        return self._make_pcb(tmp_path, layers, fps)

    @pytest.fixture
    def single_pad_power_pcb(self, tmp_path: Path) -> Path:
        """PCB where +3.3V has exactly one pad on the whole board.

        Exercises the single-pad fallback in ``_bbox_polygon`` (a
        zero-extent pad cluster must produce a non-degenerate square,
        not a zero-area polygon).
        """
        layers = '    (0 "F.Cu" signal)\n    (31 "B.Cu" signal)'
        fps = """  (footprint "Test:U1"
    (layer "F.Cu")
    (at 10 10)
    (uuid "fp-u1-uuid")
    (property "Reference" "U1"
      (at 0 -2 0) (layer "F.SilkS") (uuid "prop-u1-ref-uuid")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (property "Value" "TEST"
      (at 0 2 0) (layer "F.Fab") (uuid "prop-u1-val-uuid")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "GND"))
    (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net 4 "+3.3V"))
    (pad "3" smd rect (at 2 0) (size 1 1) (layers "F.Cu") (net 3 "+5V"))
  )
  (footprint "Test:U2"
    (layer "F.Cu")
    (at 40 10)
    (uuid "fp-u2-uuid")
    (property "Reference" "U2"
      (at 0 -2 0) (layer "F.SilkS") (uuid "prop-u2-ref-uuid")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (property "Value" "TEST"
      (at 0 2 0) (layer "F.Fab") (uuid "prop-u2-val-uuid")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "GND"))
    (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net 3 "+5V"))
  )"""
        return self._make_pcb(tmp_path, layers, fps)

    # ----- tests --------------------------------------------------------

    def test_2layer_power_nets_have_disjoint_bboxes(
        self, two_layer_board05_pcb: Path, capsys: pytest.CaptureFixture
    ):
        """Board 05 pour set on 2-layer: 4 power F.Cu zones must be disjoint."""
        from kicad_tools.zones.generator import auto_create_zones_for_pour_nets

        pour_nets = [
            ("VMOTOR", NetClass.POWER),
            ("+5V", NetClass.POWER),
            ("+3.3V", NetClass.POWER),
            ("GND", NetClass.GROUND),
            ("PWR_LED", NetClass.POWER),
        ]
        count = auto_create_zones_for_pour_nets(two_layer_board05_pcb, pour_nets)
        assert count == 5

        # Every power-net zone must have a distinct AABB on F.Cu.
        pcb = PCB.load(str(two_layer_board05_pcb))
        f_cu_zones = [z for z in pcb.zones if z.layer == "F.Cu"]
        assert len(f_cu_zones) == 4, (
            f"Expected 4 power-net zones on F.Cu, got {[z.net_name for z in f_cu_zones]}"
        )

        boxes = {z.net_name: _aabb(z.polygon) for z in f_cu_zones}

        # All 4 AABBs must be pairwise distinct (no two nets share an outline).
        unique_boxes = {tuple(round(v, 3) for v in box) for box in boxes.values()}
        assert len(unique_boxes) == 4, f"AABBs not unique: {boxes}"

        # No two AABBs may overlap (this is the fix's core invariant).
        names = list(boxes)
        for i, name_a in enumerate(names):
            for name_b in names[i + 1 :]:
                assert not _aabbs_overlap(boxes[name_a], boxes[name_b]), (
                    f"AABBs for {name_a} and {name_b} overlap: {boxes[name_a]} vs {boxes[name_b]}"
                )

        # No overlap warnings should have been emitted to stderr by
        # ``_check_overlap`` -- the whole point of the geometric
        # partition is to silence them on legitimate multi-power boards.
        captured = capsys.readouterr()
        assert "WARNING" not in captured.err, (
            f"Unexpected overlap warning(s) from _check_overlap:\n{captured.err}"
        )

    def test_2layer_gnd_retains_full_outline(self, two_layer_board05_pcb: Path):
        """GND on B.Cu is the only zone on its layer -> full board outline."""
        from kicad_tools.zones.generator import auto_create_zones_for_pour_nets

        pour_nets = [
            ("VMOTOR", NetClass.POWER),
            ("+5V", NetClass.POWER),
            ("+3.3V", NetClass.POWER),
            ("GND", NetClass.GROUND),
            ("PWR_LED", NetClass.POWER),
        ]
        auto_create_zones_for_pour_nets(two_layer_board05_pcb, pour_nets)

        pcb = PCB.load(str(two_layer_board05_pcb))
        gnd_zones = [z for z in pcb.zones if z.net_name == "GND"]
        assert len(gnd_zones) == 1
        assert gnd_zones[0].layer == "B.Cu"

        # GND must span the full 100x50 board (allow 1mm slop for rounding).
        x_min, y_min, x_max, y_max = _aabb(gnd_zones[0].polygon)
        assert x_min <= 1.0
        assert y_min <= 1.0
        assert x_max >= 99.0
        assert y_max >= 49.0

    def test_4layer_gnd_keeps_full_inner_plane(
        self, four_layer_board05_pcb: Path, capsys: pytest.CaptureFixture
    ):
        """4-layer: GND on In1.Cu sole-zone -> full board outline preserved.

        On 4-layer boards the GROUND return-path plane *must* span the
        full board so signals can cross over it without gaps.  This is
        the core of the "hybrid" strategy: only zones that share a layer
        get per-net bounding regions; sole-layer zones (GND) keep the
        full outline.
        """
        from kicad_tools.zones.generator import auto_create_zones_for_pour_nets

        pour_nets = [
            ("VMOTOR", NetClass.POWER),
            ("+5V", NetClass.POWER),
            ("+3.3V", NetClass.POWER),
            ("GND", NetClass.GROUND),
            ("PWR_LED", NetClass.POWER),
        ]
        count = auto_create_zones_for_pour_nets(four_layer_board05_pcb, pour_nets)
        assert count == 5

        pcb = PCB.load(str(four_layer_board05_pcb))

        # GND should be on In1.Cu and span the full board.
        gnd = next(z for z in pcb.zones if z.net_name == "GND")
        assert gnd.layer == "In1.Cu"
        x_min, y_min, x_max, y_max = _aabb(gnd.polygon)
        assert x_min <= 1.0 and y_min <= 1.0
        assert x_max >= 99.0 and y_max >= 49.0, (
            f"GND on In1.Cu should be full-outline, got AABB ({x_min}, {y_min})-({x_max}, {y_max})"
        )

        # The first-priority power net (VMOTOR) is sole on In2.Cu --
        # should also keep the full outline so it acts as a power plane.
        vmotor = next(z for z in pcb.zones if z.net_name == "VMOTOR")
        assert vmotor.layer == "In2.Cu"
        x_min, y_min, x_max, y_max = _aabb(vmotor.polygon)
        assert x_max - x_min > 50.0  # spans most of the board
        assert y_max - y_min > 25.0

        # The remaining three power nets share F.Cu -- they must use
        # disjoint per-net bounding outlines.
        f_cu_zones = [z for z in pcb.zones if z.layer == "F.Cu"]
        assert len(f_cu_zones) == 3, (
            f"Expected 3 F.Cu zones (+5V/+3.3V/PWR_LED), got {[z.net_name for z in f_cu_zones]}"
        )
        boxes = {z.net_name: _aabb(z.polygon) for z in f_cu_zones}
        names = list(boxes)
        for i, name_a in enumerate(names):
            for name_b in names[i + 1 :]:
                assert not _aabbs_overlap(boxes[name_a], boxes[name_b]), (
                    f"F.Cu zones {name_a}/{name_b} overlap: {boxes[name_a]} vs {boxes[name_b]}"
                )

        captured = capsys.readouterr()
        assert "WARNING" not in captured.err, f"Unexpected overlap warning(s):\n{captured.err}"

    def test_single_pad_net_uses_fallback_square(self, single_pad_power_pcb: Path):
        """Power net with one pad: outline is a small square, not zero-area.

        ``+3.3V`` has exactly one pad on the board.  The bbox allocator
        must produce a non-degenerate polygon (default 4 mm square)
        rather than a zero-extent point that KiCad would silently drop.
        """
        from kicad_tools.zones.generator import (
            SINGLE_PAD_FALLBACK_SIDE_MM,
            auto_create_zones_for_pour_nets,
        )

        pour_nets = [
            ("+5V", NetClass.POWER),
            ("+3.3V", NetClass.POWER),
            ("GND", NetClass.GROUND),
        ]
        count = auto_create_zones_for_pour_nets(single_pad_power_pcb, pour_nets)
        assert count == 3

        pcb = PCB.load(str(single_pad_power_pcb))
        # Both power zones land on F.Cu (shared) so both must get per-net
        # bounding outlines.
        threev_zones = [z for z in pcb.zones if z.net_name == "+3.3V"]
        assert len(threev_zones) == 1
        polygon = threev_zones[0].polygon
        x_min, y_min, x_max, y_max = _aabb(polygon)
        width = x_max - x_min
        height = y_max - y_min

        # The polygon must have non-zero area (the failure mode the test
        # guards against is a zero-area polygon that KiCad drops).
        assert width > 0.5, f"+3.3V polygon collapsed: width={width}"
        assert height > 0.5, f"+3.3V polygon collapsed: height={height}"

        # The polygon must be smaller than the full board (the whole
        # point is to NOT use the full outline when sharing a layer).
        assert width < 90.0
        assert height < 40.0

        # And the fallback square is at least roughly the configured size.
        # (Bbox allocator with one pad emits the default square, possibly
        # clipped to the board outline.)
        assert width <= SINGLE_PAD_FALLBACK_SIDE_MM + 0.1
        assert height <= SINGLE_PAD_FALLBACK_SIDE_MM + 0.1

    def test_compute_pour_outlines_returns_none_for_sole_layer(self, two_layer_board05_pcb: Path):
        """Direct unit test: sole-layer zones (GND) get ``None`` outline."""
        from kicad_tools.zones import ZoneGenerator
        from kicad_tools.zones.generator import (
            _assign_layers_for_pour_nets,
            _compute_pour_outlines,
        )

        pour_nets = [
            ("VMOTOR", NetClass.POWER),
            ("+5V", NetClass.POWER),
            ("+3.3V", NetClass.POWER),
            ("GND", NetClass.GROUND),
            ("PWR_LED", NetClass.POWER),
        ]
        gen = ZoneGenerator.from_pcb(two_layer_board05_pcb)
        pcb = gen.pcb

        assignments = _assign_layers_for_pour_nets(2, pour_nets)
        outlines = _compute_pour_outlines(pcb, assignments, gen.board_outline)

        # GND is the only zone on B.Cu -> None (use full board outline)
        assert outlines["GND"] is None
        # Every shared-layer zone (F.Cu) -> concrete polygon
        for net in ("VMOTOR", "+5V", "+3.3V", "PWR_LED"):
            assert outlines[net] is not None, f"{net} should have a per-net outline"
            assert len(outlines[net]) >= 3, f"{net} polygon must have >=3 vertices"


# ---------------------------------------------------------------------------
# Rotation-sign drift-prevention (issue #2778)
# ---------------------------------------------------------------------------


class TestNetPadPositionsRotationConvention:
    """Regression guard for #2778.

    ``_net_pad_positions_absolute`` must use the same rotation-sign
    convention as the canonical :meth:`PCB.get_pad_position` (positive
    ``math.radians(fp.rotation)``).  A previous implementation used the
    negative sign, which silently produced mirrored pad positions for
    rotated footprints.  Existing fixtures in
    :class:`TestAutoPourGeometricOutlines` all use ``rotation=0`` and
    therefore did not catch the drift.

    These tests parametrize a rotation set that includes 45/90/270 --
    angles where the sign of ``math.radians(...)`` actually matters
    (0 and 180 alone would be insufficient because the rotation matrix
    is sign-symmetric there).
    """

    @staticmethod
    def _make_rotated_pcb(tmp_path: Path, rotation: float) -> Path:
        """Build a minimal one-footprint PCB at the given rotation.

        The pad is placed at a non-origin offset ``(1, 0)`` so the
        rotation transform is visible in the resulting absolute pad
        position.  The board outline starts at ``(0, 0)`` which means
        ``PCB.board_origin`` will be ``(0, 0)`` after detection -- this
        is critical because :func:`_net_pad_positions_absolute` adds
        ``board_origin`` to its output while :meth:`PCB.get_pad_position`
        does not, so they only agree directly when ``board_origin`` is
        the origin.  See the test below for the explicit assertion.
        """
        pcb_text = f"""\
(kicad_pcb
  (version 20240108)
  (generator "kicad")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "SIG")
  (gr_rect
    (start 0 0)
    (end 100 50)
    (stroke (width 0.15) (type solid))
    (fill none)
    (layer "Edge.Cuts")
    (uuid "edge-uuid")
  )
  (footprint "Test:U1"
    (layer "F.Cu")
    (at 20 30 {rotation})
    (uuid "fp-u1-uuid")
    (property "Reference" "U1"
      (at 0 -2 0)
      (layer "F.SilkS")
      (uuid "prop-u1-ref-uuid")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (property "Value" "T"
      (at 0 2 0)
      (layer "F.Fab")
      (uuid "prop-u1-val-uuid")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net 1 "SIG"))
  )
)
"""
        p = tmp_path / "rot.kicad_pcb"
        p.write_text(pcb_text)
        return p

    @pytest.mark.parametrize("rotation", [0.0, 45.0, 90.0, 180.0, 270.0])
    def test_agrees_with_get_pad_position(self, tmp_path: Path, rotation: float):
        """Drift guard: zone-pad positions must equal ``PCB.get_pad_position``.

        Must include at least one rotation in {45, 90, 270}.  Pure 0/180
        cases are sign-symmetric and would pass even with the buggy
        ``math.radians(-fp.rotation)`` implementation.
        """
        import math

        from kicad_tools.zones.generator import _net_pad_positions_absolute

        pcb_file = self._make_rotated_pcb(tmp_path, rotation)
        pcb = PCB.load(pcb_file)

        # Fixture invariant: board_origin must be (0, 0) so that the two
        # functions can be compared without subtracting the origin.
        # _net_pad_positions_absolute adds board_origin to its result;
        # get_pad_position does not.
        assert pcb.board_origin == (0.0, 0.0), (
            f"fixture invariant broken: board_origin={pcb.board_origin} "
            f"(test compares pad positions in the same frame and requires (0, 0))"
        )

        canonical = pcb.get_pad_position("U1", "1")
        assert canonical is not None, "U1 pad 1 not found via get_pad_position"

        zone_positions = _net_pad_positions_absolute(pcb, "SIG")
        assert len(zone_positions) == 1, (
            f"expected exactly 1 pad on SIG, got {len(zone_positions)}"
        )

        zx, zy = zone_positions[0]
        cx, cy = canonical
        assert math.isclose(zx, cx, abs_tol=1e-9), (
            f"x mismatch at rotation={rotation}°: zone={zx} canonical={cx}"
        )
        assert math.isclose(zy, cy, abs_tol=1e-9), (
            f"y mismatch at rotation={rotation}°: zone={zy} canonical={cy}"
        )

    def test_negative_control_buggy_sign_disagrees_at_90deg(self, tmp_path: Path):
        """Negative control: confirm the 90° fixture is sensitive to the sign.

        If a future refactor reintroduces the inverted sign, the
        ``test_agrees_with_get_pad_position[90.0]`` case must fail.  This
        test documents that by computing what the buggy expression
        would produce and asserting it differs from the canonical result.
        It is *not* asserting on the production code path -- it's an
        invariant about the test fixture itself.
        """
        import math

        pcb_file = self._make_rotated_pcb(tmp_path, 90.0)
        pcb = PCB.load(pcb_file)

        canonical = pcb.get_pad_position("U1", "1")
        assert canonical is not None

        # Manually compute what the pre-fix code (negative sign) would have
        # produced for the single rotated pad.
        fp = pcb.get_footprint("U1")
        assert fp is not None
        fp_x, fp_y = fp.position
        ox, oy = pcb.board_origin
        rot_rad_buggy = math.radians(-fp.rotation)
        cos_b, sin_b = math.cos(rot_rad_buggy), math.sin(rot_rad_buggy)
        pad = fp.pads[0]
        px, py = pad.position
        buggy_x = fp_x + (px * cos_b - py * sin_b) + ox
        buggy_y = fp_y + (px * sin_b + py * cos_b) + oy

        # Sanity: at 90°, the buggy and canonical results must disagree.
        # If this assertion ever passes (buggy ~ canonical) the fixture has
        # lost its discriminating power and the parametrized test above
        # would no longer protect against the bug.
        cx, cy = canonical
        disagrees = (
            not math.isclose(buggy_x, cx, abs_tol=1e-6)
            or not math.isclose(buggy_y, cy, abs_tol=1e-6)
        )
        assert disagrees, (
            "fixture failed to discriminate: buggy and canonical 90° transforms "
            "produced the same result -- the regression test would not catch "
            "a reintroduction of the inverted-sign bug"
        )
