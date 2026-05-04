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
    """PCB containing GND and +3.3V nets."""
    p = tmp_path / "board.kicad_pcb"
    p.write_text(MINIMAL_PCB)
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
