"""Unit tests for the branch current-path ampacity DRC rule (Issue #4980).

Exercises :class:`kicad_tools.validate.rules.path_ampacity.PathAmpacityRule`
against synthetic in-memory PCBs -- no golden-board dependency. The golden
IPC-2221 widths mirror ``tests/test_ampacity_check.py``.
"""

from __future__ import annotations

import pytest

from kicad_tools.manufacturers import DesignRules
from kicad_tools.router.current_paths import CurrentPathSpec, PathEndpoint
from kicad_tools.schema.pcb import PCB, Footprint, Pad
from kicad_tools.validate.rules.path_ampacity import PathAmpacityRule

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def _add_pad_footprint(pcb: PCB, *, ref: str, x: float, y: float, net: str) -> None:
    net_obj = pcb.add_net(net)
    pad = Pad(
        number="1",
        type="thru_hole",
        shape="circle",
        position=(0.0, 0.0),
        size=(1.0, 1.0),
        layers=["*.Cu"],
        net_number=net_obj.number,
        net_name=net,
        drill=0.5,
    )
    fp = Footprint(
        name="TH:Pad",
        layer="F.Cu",
        position=(x, y),
        rotation=0.0,
        reference=ref,
        value="TP",
        pads=[pad],
    )
    pcb._footprints.append(fp)


def _t_network_pcb(*, trunk_width: float, sense_width: float = 0.2) -> PCB:
    pcb = PCB.create(width=200, height=120, center=False)
    _add_pad_footprint(pcb, ref="J1", x=20, y=50, net="NET1")
    _add_pad_footprint(pcb, ref="J2", x=120, y=50, net="NET1")
    _add_pad_footprint(pcb, ref="U3", x=60, y=70, net="NET1")
    pcb.add_trace(("J1", "1"), (60, 50), width=trunk_width, layer="F.Cu", net="NET1")
    pcb.add_trace((60, 50), ("J2", "1"), width=trunk_width, layer="F.Cu", net="NET1")
    pcb.add_trace((60, 50), ("U3", "1"), width=sense_width, layer="F.Cu", net="NET1")
    return pcb


def _design_rules_2oz() -> DesignRules:
    return DesignRules(
        min_trace_width_mm=0.127,
        min_clearance_mm=0.127,
        min_via_drill_mm=0.2,
        min_via_diameter_mm=0.45,
        min_annular_ring_mm=0.13,
        outer_copper_oz=2.0,
        inner_copper_oz=2.0,
    )


def _trunk_spec(current_a: float = 15.0) -> CurrentPathSpec:
    return CurrentPathSpec(
        name="TRUNK",
        net_name="NET1",
        source=PathEndpoint("J1", "1"),
        sink=PathEndpoint("J2", "1"),
        continuous_a=current_a,
        reinforcement_eligible=True,
    )


def _sense_spec(current_a: float = 0.01) -> CurrentPathSpec:
    return CurrentPathSpec(
        name="SENSE",
        net_name="NET1",
        source=PathEndpoint("J1", "1"),
        sink=PathEndpoint("U3", "1"),
        continuous_a=current_a,
        reinforcement_eligible=False,
    )


# --------------------------------------------------------------------------
# PathAmpacityRule
# --------------------------------------------------------------------------


class TestPathAmpacityRule:
    def test_no_specs_is_clean_noop(self) -> None:
        pcb = _t_network_pcb(trunk_width=0.1)
        rule = PathAmpacityRule(specs=None)
        results = rule.check(pcb, _design_rules_2oz())
        assert results.errors == []
        assert results.warnings == []

    def test_t_network_adequate_trunk_and_narrow_sense_passes(self) -> None:
        """A 15A trunk routed at >= required width, sense spur declared its
        own tiny current -- both pass, no findings at all."""
        pcb = _t_network_pcb(trunk_width=6.3, sense_width=0.2)
        rule = PathAmpacityRule(specs=[_trunk_spec(15.0), _sense_spec(0.01)])
        results = rule.check(pcb, _design_rules_2oz())
        assert results.errors == []
        assert results.warnings == []

    def test_t_network_narrow_trunk_fails(self) -> None:
        """Narrowing the trunk below the IPC-2221 floor for its declared
        15A -- while the sense spur's own tiny requirement is still met --
        fails on the trunk only."""
        pcb = _t_network_pcb(trunk_width=3.0, sense_width=0.2)
        rule = PathAmpacityRule(specs=[_trunk_spec(15.0), _sense_spec(0.01)])
        results = rule.check(pcb, _design_rules_2oz())

        trunk_errors = [v for v in results.errors if "TRUNK" in v.items]
        assert len(trunk_errors) == 2  # both trunk segments under-width
        for v in trunk_errors:
            assert v.severity == "error"
            assert v.required_value == pytest.approx(6.29, abs=0.05)
        # The sense spur's own declared current is tiny -- its segment is
        # never flagged even though it is physically narrower than the
        # trunk's requirement.
        assert not any("SENSE" in v.items for v in results.errors)

    def test_whole_net_current_would_have_failed_sense_spur(self) -> None:
        """Sanity check that the branch-scoped current -- not the trunk's --
        governs the sense spur: at the trunk's 15A the spur's 0.2mm width
        would fail, but the sense spec declares 0.01A so it does not."""
        pcb = _t_network_pcb(trunk_width=6.3, sense_width=0.2)
        rule = PathAmpacityRule(specs=[_trunk_spec(15.0), _sense_spec(0.01)])
        results = rule.check(pcb, _design_rules_2oz())
        assert results.errors == []

    def test_moved_pad_endpoint_fails_closed(self) -> None:
        """A declared trunk endpoint that no longer resolves is a visible
        error, never a silent fallback to whole-net ampacity."""
        pcb = _t_network_pcb(trunk_width=6.3)
        fp = pcb.get_footprint("J2")
        assert fp is not None
        other_net = pcb.add_net("OTHER")
        fp.pads[0].net_number = other_net.number
        fp.pads[0].net_name = "OTHER"

        rule = PathAmpacityRule(specs=[_trunk_spec(15.0)])
        results = rule.check(pcb, _design_rules_2oz())
        assert len(results.errors) == 1
        assert "unresolved" in results.errors[0].message

    def test_ambiguous_loop_is_error(self) -> None:
        pcb = PCB.create(width=200, height=120, center=False)
        _add_pad_footprint(pcb, ref="J1", x=20, y=50, net="NET2")
        _add_pad_footprint(pcb, ref="J2", x=120, y=50, net="NET2")
        pcb.add_trace(("J1", "1"), (60, 30), width=6.3, layer="F.Cu", net="NET2")
        pcb.add_trace((60, 30), ("J2", "1"), width=6.3, layer="F.Cu", net="NET2")
        pcb.add_trace(("J1", "1"), (60, 70), width=6.3, layer="F.Cu", net="NET2")
        pcb.add_trace((60, 70), ("J2", "1"), width=6.3, layer="F.Cu", net="NET2")

        spec = CurrentPathSpec(
            name="LOOP",
            net_name="NET2",
            source=PathEndpoint("J1", "1"),
            sink=PathEndpoint("J2", "1"),
            continuous_a=5.0,
        )
        rule = PathAmpacityRule(specs=[spec])
        results = rule.check(pcb, _design_rules_2oz())
        assert len(results.errors) == 1
        assert "ambiguous" in results.errors[0].message

    def test_uncovered_copper_is_warning_not_error(self) -> None:
        """A net with a declared trunk but no declared sense spec reports
        the spur as uncovered -- visible (warning), not silently waived,
        and NOT an error (declaring a path is opt-in, not mandatory)."""
        pcb = _t_network_pcb(trunk_width=6.3, sense_width=0.2)
        rule = PathAmpacityRule(specs=[_trunk_spec(15.0)])  # no sense spec declared
        results = rule.check(pcb, _design_rules_2oz())
        assert results.errors == []
        assert len(results.warnings) == 1
        assert "uncovered" in results.warnings[0].message.lower() or (
            "not covered" in results.warnings[0].message.lower()
        )

    def test_kelvin_force_and_sense_declared_currents_independent(self) -> None:
        """A four-terminal-shunt-style fixture: a force path across the
        shunt and a Kelvin sense path sharing the electrical net, but
        carrying wildly different declared currents. Both must pass
        independently at their own declared current."""
        pcb = PCB.create(width=200, height=120, center=False)
        _add_pad_footprint(pcb, ref="RSH1", x=60, y=50, net="PGND")  # force- side
        _add_pad_footprint(pcb, ref="J2", x=120, y=50, net="PGND")  # force+ side
        _add_pad_footprint(pcb, ref="U3", x=60, y=80, net="PGND")  # INA181 sense pad
        pcb.add_trace(("RSH1", "1"), ("J2", "1"), width=6.3, layer="F.Cu", net="PGND")
        pcb.add_trace(("RSH1", "1"), ("U3", "1"), width=0.2, layer="F.Cu", net="PGND")

        force = CurrentPathSpec(
            name="FORCE",
            net_name="PGND",
            source=PathEndpoint("RSH1", "1"),
            sink=PathEndpoint("J2", "1"),
            continuous_a=15.0,
            reinforcement_eligible=True,
        )
        sense = CurrentPathSpec(
            name="KELVIN_SENSE",
            net_name="PGND",
            source=PathEndpoint("RSH1", "1"),
            sink=PathEndpoint("U3", "1"),
            continuous_a=0.001,
            reinforcement_eligible=False,
        )
        rule = PathAmpacityRule(specs=[force, sense])
        results = rule.check(pcb, _design_rules_2oz())
        assert results.errors == []
        assert results.warnings == []
