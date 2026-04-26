"""Tests for zone fill DRC rule (validate/rules/zone_fill.py)."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import MagicMock

import pytest

from kicad_tools.validate.rules.zone_fill import ZoneFillRule


# ---------------------------------------------------------------------------
# Minimal Zone stub -- mirrors the fields the rule reads
# ---------------------------------------------------------------------------

@dataclass
class _FakeZone:
    net_number: int = 1
    net_name: str = "GND"
    layer: str = "F.Cu"
    polygon: list[tuple[float, float]] = field(default_factory=lambda: [
        (0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0),
    ])
    filled_polygons: list[list[tuple[float, float]]] = field(default_factory=list)
    is_filled: bool = True


# ---------------------------------------------------------------------------
# Helper to build a fake PCB with specific zones
# ---------------------------------------------------------------------------

def _make_pcb(zones: list[_FakeZone]) -> MagicMock:
    pcb = MagicMock()
    pcb.zones = zones
    return pcb


def _make_design_rules() -> MagicMock:
    return MagicMock()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestZoneFillRule:
    """Tests for the ZoneFillRule check."""

    def test_filled_zone_produces_no_violation(self):
        """A zone with is_filled=True and non-empty filled_polygons is OK."""
        zone = _FakeZone(
            is_filled=True,
            filled_polygons=[[(0, 0), (10, 0), (10, 10), (0, 10)]],
        )
        pcb = _make_pcb([zone])
        rule = ZoneFillRule()
        results = rule.check(pcb, _make_design_rules())

        # No zone_unfilled or zone_fill_disabled violations expected
        fill_violations = [
            v for v in results.violations
            if v.rule_id in ("zone_unfilled", "zone_fill_disabled")
        ]
        assert len(fill_violations) == 0

    def test_unfilled_zone_produces_warning(self):
        """A zone with is_filled=True but empty filled_polygons is flagged."""
        zone = _FakeZone(is_filled=True, filled_polygons=[])
        pcb = _make_pcb([zone])
        rule = ZoneFillRule()
        results = rule.check(pcb, _make_design_rules())

        unfilled = [v for v in results.violations if v.rule_id == "zone_unfilled"]
        assert len(unfilled) == 1
        v = unfilled[0]
        assert v.severity == "warning"
        assert "GND" in v.message
        assert "F.Cu" in v.message
        # Bounding box should appear
        assert "0.00" in v.message
        assert "100.00" in v.message

    def test_fill_disabled_produces_warning(self):
        """A zone with is_filled=False is flagged as fill-disabled."""
        zone = _FakeZone(is_filled=False, filled_polygons=[])
        pcb = _make_pcb([zone])
        rule = ZoneFillRule()
        results = rule.check(pcb, _make_design_rules())

        disabled = [v for v in results.violations if v.rule_id == "zone_fill_disabled"]
        assert len(disabled) == 1
        assert disabled[0].severity == "warning"
        assert "disabled" in disabled[0].message.lower()

    def test_unassigned_net_produces_warning(self):
        """A zone with net_number=0 and empty net_name is flagged."""
        zone = _FakeZone(net_number=0, net_name="", is_filled=True, filled_polygons=[
            [(0, 0), (10, 0), (10, 10), (0, 10)],
        ])
        pcb = _make_pcb([zone])
        rule = ZoneFillRule()
        results = rule.check(pcb, _make_design_rules())

        no_net = [v for v in results.violations if v.rule_id == "zone_no_net"]
        assert len(no_net) == 1
        assert "no net assigned" in no_net[0].message.lower()

    def test_assigned_net_no_extra_warning(self):
        """A zone with a valid net does not get a zone_no_net warning."""
        zone = _FakeZone(
            net_number=1,
            net_name="GND",
            is_filled=True,
            filled_polygons=[[(0, 0), (10, 0), (10, 10)]],
        )
        pcb = _make_pcb([zone])
        rule = ZoneFillRule()
        results = rule.check(pcb, _make_design_rules())

        no_net = [v for v in results.violations if v.rule_id == "zone_no_net"]
        assert len(no_net) == 0

    def test_warning_includes_layer(self):
        """Violation layer field matches the zone layer."""
        zone = _FakeZone(layer="In1.Cu", is_filled=True, filled_polygons=[])
        pcb = _make_pcb([zone])
        rule = ZoneFillRule()
        results = rule.check(pcb, _make_design_rules())

        unfilled = [v for v in results.violations if v.rule_id == "zone_unfilled"]
        assert len(unfilled) == 1
        assert unfilled[0].layer == "In1.Cu"

    def test_warning_includes_location(self):
        """Violation location is the centroid of the zone boundary."""
        zone = _FakeZone(
            polygon=[(0, 0), (10, 0), (10, 10), (0, 10)],
            is_filled=True,
            filled_polygons=[],
        )
        pcb = _make_pcb([zone])
        rule = ZoneFillRule()
        results = rule.check(pcb, _make_design_rules())

        unfilled = [v for v in results.violations if v.rule_id == "zone_unfilled"]
        assert len(unfilled) == 1
        loc = unfilled[0].location
        assert loc is not None
        assert abs(loc[0] - 5.0) < 0.01
        assert abs(loc[1] - 5.0) < 0.01

    def test_degenerate_zone_no_polygon(self):
        """A zone with no polygon points still reports correctly."""
        zone = _FakeZone(polygon=[], is_filled=True, filled_polygons=[])
        pcb = _make_pcb([zone])
        rule = ZoneFillRule()
        results = rule.check(pcb, _make_design_rules())

        unfilled = [v for v in results.violations if v.rule_id == "zone_unfilled"]
        assert len(unfilled) == 1
        assert "no boundary" in unfilled[0].message
        assert unfilled[0].location is None

    def test_empty_pcb_no_violations(self):
        """A PCB with no zones produces no violations."""
        pcb = _make_pcb([])
        rule = ZoneFillRule()
        results = rule.check(pcb, _make_design_rules())
        assert len(results.violations) == 0

    def test_rules_checked_count(self):
        """rules_checked is set to 1 regardless of zone count."""
        pcb = _make_pcb([_FakeZone(), _FakeZone()])
        rule = ZoneFillRule()
        results = rule.check(pcb, _make_design_rules())
        assert results.rules_checked == 1

    def test_net_label_unassigned_in_message(self):
        """When net_name is empty, the message uses 'unassigned'."""
        zone = _FakeZone(net_number=0, net_name="", is_filled=True, filled_polygons=[])
        pcb = _make_pcb([zone])
        rule = ZoneFillRule()
        results = rule.check(pcb, _make_design_rules())

        unfilled = [v for v in results.violations if v.rule_id == "zone_unfilled"]
        assert len(unfilled) == 1
        assert "unassigned" in unfilled[0].message

    def test_items_includes_net_label(self):
        """The items tuple contains the net label."""
        zone = _FakeZone(net_name="+3.3V", is_filled=True, filled_polygons=[])
        pcb = _make_pcb([zone])
        rule = ZoneFillRule()
        results = rule.check(pcb, _make_design_rules())

        unfilled = [v for v in results.violations if v.rule_id == "zone_unfilled"]
        assert len(unfilled) == 1
        assert "net:+3.3V" in unfilled[0].items
