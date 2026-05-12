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
        - routed_pcb is byte-identical (zero segments, zero vias),
          simulating the silent-success path.
        """
        # Minimal PCB with two footprints sharing 3 nets via pads.
        # Each net has 2 pads => 3 multi-pad signal nets to route.
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
        input_pcb = tmp_path / "input.kicad_pcb"
        input_pcb.write_text(pcb_text)
        routed_pcb = tmp_path / "input_routed.kicad_pcb"
        routed_pcb.write_text(pcb_text)  # Same content == zero segments produced.
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
