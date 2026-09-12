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


@pytest.mark.parametrize("bridge", [False, True])
def test_layer_transition_is_audited_before_ampacity(bridge):
    pcb = _t_network_pcb(trunk_width=6.3)
    pcb.segments[1].layer = "B.Cu"
    if bridge:
        pcb.add_via(60, 50, net="NET1")
    result = PathAmpacityRule(specs=[_trunk_spec(), _sense_spec()]).check(pcb, _design_rules_2oz())
    if bridge:
        assert result.errors == []
    else:
        assert result.errors
        assert any("unresolved" in error.message.lower() for error in result.errors)


# --------------------------------------------------------------------------
# Pulsed / duty-cycled branches (Issue #4980)
# --------------------------------------------------------------------------


def _pulsed_trunk_spec(**overrides) -> CurrentPathSpec:
    kwargs = {
        "name": "TRUNK",
        "net_name": "NET1",
        "source": PathEndpoint("J1", "1"),
        "sink": PathEndpoint("J2", "1"),
        "continuous_a": 3.0,
        "pulsed_a": 18.0,
        "duty_cycle": 0.08,
        "pulse_duration_s": 0.002,
        "reinforcement_eligible": True,
    }
    kwargs.update(overrides)
    return CurrentPathSpec(**kwargs)


class TestPulsedPathAmpacity:
    """A declared pulse drives an RMS thermal width check plus an Onderdonk
    fusing check -- neither of which the continuous-only check could see."""

    def test_rms_width_governs_a_duty_cycled_branch(self) -> None:
        """18A peak at 8% duty over 3A heats like ~5.85A RMS: a trace wide
        enough for 5.85A passes, even though it is far too narrow for 18A
        of *continuous* current."""
        pcb = _t_network_pcb(trunk_width=1.8, sense_width=0.2)
        rule = PathAmpacityRule(specs=[_pulsed_trunk_spec(), _sense_spec(0.01)])
        results = rule.check(pcb, _design_rules_2oz())
        assert results.errors == []

    def test_peak_as_continuous_would_have_failed_the_same_trace(self) -> None:
        """Same board, same peak, duty cycle withheld: the peak is sized as
        continuous, so the trace that passed above now fails. This is the
        whole point of declaring a duty cycle."""
        pcb = _t_network_pcb(trunk_width=1.8, sense_width=0.2)
        rule = PathAmpacityRule(specs=[_pulsed_trunk_spec(duty_cycle=None)])
        results = rule.check(pcb, _design_rules_2oz())
        assert [v for v in results.errors if "too narrow" in v.message]

    def test_peak_as_continuous_assumption_is_disclosed(self) -> None:
        """Sizing at the peak is conservative, so it is not an error -- but
        it is never silent: an info finding names the missing duty cycle."""
        pcb = _t_network_pcb(trunk_width=8.2, sense_width=0.2)
        rule = PathAmpacityRule(specs=[_pulsed_trunk_spec(duty_cycle=None)])
        results = rule.check(pcb, _design_rules_2oz())
        assert results.errors == []
        notices = [v for v in results.infos if "duty_cycle" in v.message]
        assert len(notices) == 1
        assert notices[0].severity == "info"

    def test_declared_duty_cycle_emits_no_assumption_notice(self) -> None:
        pcb = _t_network_pcb(trunk_width=8.0, sense_width=0.2)
        rule = PathAmpacityRule(specs=[_pulsed_trunk_spec()])
        results = rule.check(pcb, _design_rules_2oz())
        assert results.infos == []

    def test_underwidth_message_names_the_rms_basis(self) -> None:
        pcb = _t_network_pcb(trunk_width=0.3, sense_width=0.2)
        rule = PathAmpacityRule(specs=[_pulsed_trunk_spec()])
        results = rule.check(pcb, _design_rules_2oz())
        narrow = [v for v in results.errors if "too narrow" in v.message]
        assert narrow
        assert "RMS" in narrow[0].message
        assert "18" in narrow[0].message  # the peak is named alongside the RMS

    def test_fusing_failure_on_a_thermally_adequate_trace(self) -> None:
        """A trace can be comfortable on RMS heating and still be melted by
        a single pulse. 30A for 2s exceeds the ~25.7A Onderdonk limit of
        1.8mm of 2oz copper, while the ~5.98A RMS needs only ~1.77mm -- so
        the thermal check passes and only the fusing check fires."""
        pcb = _t_network_pcb(trunk_width=1.8, sense_width=0.2)
        spec = _pulsed_trunk_spec(pulsed_a=30.0, duty_cycle=0.03, pulse_duration_s=2.0)
        rule = PathAmpacityRule(specs=[spec])
        results = rule.check(pcb, _design_rules_2oz())
        assert not [v for v in results.errors if "too narrow" in v.message]
        fusing = [v for v in results.errors if "fuse" in v.message]
        assert fusing, [v.message for v in results.errors]
        assert fusing[0].actual_value == pytest.approx(30.0)
        assert fusing[0].required_value == pytest.approx(25.72, abs=0.1)

    def test_short_enough_pulse_survives(self) -> None:
        """The same 30A peak over a 100x shorter pulse does not fuse
        (Onderdonk scales as 1/sqrt(t))."""
        pcb = _t_network_pcb(trunk_width=1.8, sense_width=0.2)
        spec = _pulsed_trunk_spec(pulsed_a=30.0, duty_cycle=0.03, pulse_duration_s=0.02)
        rule = PathAmpacityRule(specs=[spec])
        results = rule.check(pcb, _design_rules_2oz())
        assert not [v for v in results.errors if "fuse" in v.message]

    def test_missing_pulse_duration_reports_the_unchecked_mode(self) -> None:
        """No declared duration -> fusing cannot be evaluated. That is a
        visible warning, never a silent pass."""
        pcb = _t_network_pcb(trunk_width=8.0, sense_width=0.2)
        rule = PathAmpacityRule(specs=[_pulsed_trunk_spec(pulse_duration_s=None)])
        results = rule.check(pcb, _design_rules_2oz())
        unchecked = [v for v in results.warnings if "fusing" in v.message]
        assert len(unchecked) == 1
        assert "NOT checked" in unchecked[0].message

    def test_continuous_only_path_reports_nothing_about_pulses(self) -> None:
        """Regression guard: a purely continuous declaration behaves exactly
        as it did before pulsed support existed."""
        pcb = _t_network_pcb(trunk_width=6.3, sense_width=0.2)
        rule = PathAmpacityRule(specs=[_trunk_spec(15.0), _sense_spec(0.01)])
        results = rule.check(pcb, _design_rules_2oz())
        assert results.errors == []
        assert results.warnings == []
        assert results.infos == []

    def test_unresolved_pulsed_path_still_fails_closed_without_extra_noise(self) -> None:
        """A broken endpoint mapping short-circuits to the unresolved error
        -- no waveform findings are emitted for copper nobody could find."""
        pcb = _t_network_pcb(trunk_width=1.6)
        fp = pcb.get_footprint("J2")
        assert fp is not None
        other_net = pcb.add_net("OTHER")
        fp.pads[0].net_number = other_net.number
        fp.pads[0].net_name = "OTHER"

        rule = PathAmpacityRule(specs=[_pulsed_trunk_spec(duty_cycle=None)])
        results = rule.check(pcb, _design_rules_2oz())
        assert len(results.errors) == 1
        assert "unresolved" in results.errors[0].message
        assert results.infos == []
