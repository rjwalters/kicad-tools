"""Unit tests for the branch current-path ampacity DRC rule (Issue #4980).

Exercises :class:`kicad_tools.validate.rules.path_ampacity.PathAmpacityRule`
against synthetic in-memory PCBs -- no golden-board dependency. The golden
IPC-2221 widths mirror ``tests/test_ampacity_check.py``.
"""

from __future__ import annotations

import dataclasses

import pytest

from kicad_tools.manufacturers import DesignRules
from kicad_tools.pcb.reinforce import reinforce_net
from kicad_tools.router.current_paths import CurrentPathSpec, PathEndpoint
from kicad_tools.schema.pcb import PCB, Footprint, Pad
from kicad_tools.validate.rules.path_ampacity import PathAmpacityRule
from kicad_tools.validate.rules.reinforcement_incomplete import WiredLeg
from tests.test_pcb_reinforce import _add_th_pad_footprint

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

    def test_test_point_stub_resolves_with_one_uncovered_warning(self) -> None:
        """Issue #9492: a 0.4 mm test-point stub off a two-layer power bar is
        a dead-end branch, not a loop -- no ambiguity error, the bars still
        sum as a parallel group, and the stub is reported once as uncovered
        copper (warning)."""
        pcb = PCB.create(width=100, height=100, center=False)
        _add_pad_footprint(pcb, ref="J1", x=20, y=50, net="NET1")
        _add_pad_footprint(pcb, ref="J2", x=32, y=50, net="NET1")
        _add_pad_footprint(pcb, ref="TP1", x=26, y=61, net="NET1")
        for layer in ("F.Cu", "B.Cu"):
            pcb.add_trace((20, 50), (32, 50), width=6.3, layer=layer, net="NET1")
        pcb.add_trace((26, 50), (26, 61), width=0.4, layer="F.Cu", net="NET1")
        rule = PathAmpacityRule(specs=[_trunk_spec(15.0)])
        results = rule.check(pcb, _design_rules_2oz())
        assert results.errors == []
        assert len(results.warnings) == 1
        assert "uncovered" in results.warnings[0].message.lower() or (
            "not covered" in results.warnings[0].message.lower()
        )

    def test_unmodeled_arc_is_warning_plus_ambiguous_error(self) -> None:
        """A same-net routed arc (#5273) makes the declared trunk ambiguous
        (error) AND is separately surfaced by name (warning) -- the warning
        names the specific object responsible, on top of the status error."""
        from kicad_tools.sexp import parse_string

        pcb = _t_network_pcb(trunk_width=6.3, sense_width=0.2)
        net = pcb.get_net_by_name("NET1")
        assert net is not None
        pcb._sexp.append(
            parse_string(
                f"(arc (start 20 50) (mid 70 20) (end 120 50) (width 0.2) "
                f'(layer "F.Cu") (net {net.number}))'
            )
        )
        rule = PathAmpacityRule(specs=[_trunk_spec(15.0), _sense_spec(0.01)])
        results = rule.check(pcb, _design_rules_2oz())

        ambiguous_errors = [v for v in results.errors if "ambiguous" in v.message]
        assert len(ambiguous_errors) == 2  # both TRUNK and SENSE resolve ambiguous

        unmodeled_warnings = [v for v in results.warnings if "arc" in v.message]
        assert len(unmodeled_warnings) == 1
        assert unmodeled_warnings[0].severity == "warning"

    def test_unmodeled_pour_is_warning(self) -> None:
        """A same-net non-keepout zone (#5273) is surfaced the same way."""
        from kicad_tools.schema.pcb import Zone

        pcb = _t_network_pcb(trunk_width=6.3, sense_width=0.2)
        net = pcb.get_net_by_name("NET1")
        assert net is not None
        pcb._zones.append(
            Zone(
                net.number,
                "NET1",
                "F.Cu",
                polygon=[(0, 0), (200, 0), (200, 120), (0, 120)],
            )
        )
        rule = PathAmpacityRule(specs=[_trunk_spec(15.0)])
        results = rule.check(pcb, _design_rules_2oz())

        unmodeled_warnings = [v for v in results.warnings if "zone" in v.message]
        assert len(unmodeled_warnings) == 1

    def test_keepout_zone_is_not_flagged_as_unmodeled(self) -> None:
        """A keepout rule area carries no copper -- excluded entirely."""
        from kicad_tools.schema.pcb import Zone, ZoneKeepout

        pcb = _t_network_pcb(trunk_width=6.3, sense_width=0.2)
        net = pcb.get_net_by_name("NET1")
        assert net is not None
        pcb._zones.append(
            Zone(
                net.number,
                "NET1",
                "F.Cu",
                polygon=[(0, 0), (200, 0), (200, 120), (0, 120)],
                keepout=ZoneKeepout(),
            )
        )
        rule = PathAmpacityRule(specs=[_trunk_spec(15.0), _sense_spec(0.01)])
        results = rule.check(pcb, _design_rules_2oz())
        assert results.errors == []
        assert results.warnings == []

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
        # Issue #8745: the 15 A path now crosses layers through one 0.3 mm
        # via -- a true positive -- but it is no longer "unresolved".
        assert result.errors
        assert not any("unresolved" in error.message.lower() for error in result.errors)
        assert all("changes layer" in error.message for error in result.errors)
    else:
        assert result.errors
        assert any("unresolved" in error.message.lower() for error in result.errors)


# --------------------------------------------------------------------------
# Layer-change via capacity (Issue #8745)
# --------------------------------------------------------------------------


def _layer_change_pcb(*, drill: float = 0.3) -> PCB:
    pcb = _t_network_pcb(trunk_width=6.3)
    pcb.segments[1].layer = "B.Cu"
    pcb.add_via(60, 50, size=drill + 0.3, drill=drill, net="NET1")
    return pcb


class TestLayerChangeVia:
    def test_undersized_via_is_error_for_force_path(self):
        res = PathAmpacityRule(specs=[_trunk_spec(), _sense_spec()]).check(
            _layer_change_pcb(), _design_rules_2oz()
        )
        assert len(res.errors) == 1
        assert "changes layer" in res.errors[0].message
        assert res.errors[0].required_value == 15.0

    def test_warning_when_not_reinforcement_eligible(self):
        import dataclasses

        spec = dataclasses.replace(_trunk_spec(), reinforcement_eligible=False)
        res = PathAmpacityRule(specs=[spec, _sense_spec()]).check(
            _layer_change_pcb(), _design_rules_2oz()
        )
        assert res.errors == []
        assert any("changes layer" in w.message for w in res.warnings)

    def test_low_current_does_not_fail(self):
        res = PathAmpacityRule(specs=[_trunk_spec(0.05), _sense_spec()]).check(
            _layer_change_pcb(), _design_rules_2oz()
        )
        assert res.errors == []
        assert not any("changes layer" in w.message for w in res.warnings)

    def test_adequate_via_does_not_newly_fail(self):
        res = PathAmpacityRule(specs=[_trunk_spec(), _sense_spec()]).check(
            _layer_change_pcb(drill=8.0), _design_rules_2oz()
        )
        assert res.errors == []

    def test_layer_changes_recorded_on_resolution(self):
        from kicad_tools.router.current_paths import resolve_current_path

        res = resolve_current_path(_layer_change_pcb(), _trunk_spec())
        assert res.ok and len(res.layer_changes) == 1
        change = res.layer_changes[0]
        assert change.count == 1 and change.drill == 0.3 and change.pad_diameter == 0.6
        assert change.position == (60.0, 50.0)

    def test_single_layer_path_has_no_layer_changes(self):
        from kicad_tools.router.current_paths import resolve_current_path

        res = resolve_current_path(_t_network_pcb(trunk_width=6.3), _trunk_spec())
        assert res.ok and res.layer_changes == ()


def _series_hop_pcb(*, gap: float) -> PCB:
    """J1 -F.Cu-> via -B.Cu-> via -F.Cu-> J2: two vias in SERIES."""
    pcb = PCB.create(width=200, height=120, center=False)
    _add_pad_footprint(pcb, ref="J1", x=20, y=50, net="NET1")
    _add_pad_footprint(pcb, ref="J2", x=120, y=50, net="NET1")
    pcb.add_trace(("J1", "1"), (60, 50), width=6.3, layer="F.Cu", net="NET1")
    pcb.add_trace((60, 50), (60 + gap, 50), width=6.3, layer="B.Cu", net="NET1")
    pcb.add_trace((60 + gap, 50), ("J2", "1"), width=6.3, layer="F.Cu", net="NET1")
    pcb.add_via(60, 50, size=0.6, drill=0.3, net="NET1")
    pcb.add_via(60 + gap, 50, size=0.6, drill=0.3, net="NET1")
    return pcb


class TestSeriesVersusParallelVias:
    @pytest.mark.parametrize("gap", [1.0, 10.0])
    def test_series_vias_are_separate_changes_at_any_spacing(self, gap):
        from kicad_tools.router.current_paths import resolve_current_path

        pcb = _series_hop_pcb(gap=gap)
        res = resolve_current_path(pcb, _trunk_spec(3.0))
        assert res.ok, res.reason
        assert [c.count for c in res.layer_changes] == [1, 1]
        out = PathAmpacityRule(specs=[_trunk_spec(3.0)]).check(pcb, _design_rules_2oz())
        assert len([e for e in out.errors if "changes layer" in e.message]) == 2

    def test_proved_via_array_meeting_capacity_is_not_flagged(self):
        from kicad_tools.router.current_paths import resolve_current_path
        from tests.test_current_paths_via_array import fixture, spec

        pcb = fixture()
        res = resolve_current_path(pcb, spec())  # 3 A through a 3-via array
        assert res.ok, res.reason
        arrays = [c for c in res.layer_changes if c.count == 3]
        assert len(arrays) == 1
        assert arrays[0].current_capacity_a() >= 3.0
        out = PathAmpacityRule(specs=[spec()]).check(pcb, _design_rules_2oz())
        assert not any("changes layer" in m.message for m in [*out.errors, *out.warnings])

    def test_audit_json_reports_layer_changes(self, tmp_path, capsys, monkeypatch):
        import argparse
        import json

        from kicad_tools.cli.commands.pcb import run_pcb_command
        from kicad_tools.router.current_paths import dump_current_path_specs

        # Programmatic footprints do not survive a save/load round trip, so
        # hand the in-memory board to the CLI.
        pcb_path = tmp_path / "series.kicad_pcb"
        pcb_path.write_text("")
        board = _series_hop_pcb(gap=1.0)
        monkeypatch.setattr(PCB, "load", classmethod(lambda cls, *_a, **_k: board))
        sidecar = tmp_path / "current_paths.json"
        sidecar.write_text(json.dumps(dump_current_path_specs([_trunk_spec(3.0)])))
        args = argparse.Namespace(
            pcb_command="current-paths-audit",
            pcb=str(pcb_path),
            current_paths=str(sidecar),
            format="json",
        )
        assert run_pcb_command(args) == 0
        changes = json.loads(capsys.readouterr().out)["resolutions"][0]["layer_changes"]
        assert len(changes) == 2
        assert changes[0]["via_count"] == 1 and changes[0]["drill_mm"] == 0.3
        assert changes[0]["from_layer"] == "F.Cu" and changes[0]["to_layer"] == "B.Cu"
        assert changes[0]["capacity_a"] == pytest.approx(1.6, abs=0.01)


def test_via_barrel_capacity_grows_with_drill():
    from kicad_tools.physics.ampacity import via_barrel_current_capacity

    assert 1.0 < via_barrel_current_capacity(0.3) < 3.0
    assert via_barrel_current_capacity(0.6) > via_barrel_current_capacity(0.3)


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


# --------------------------------------------------------------------------
# Doubled copper judged on summed cross-section (Issue #8790)
# --------------------------------------------------------------------------


def _doubled_ampacity_pcb(width_b: float, width_f: float) -> PCB:
    pcb = PCB.create(width=100, height=100, center=False)
    _add_pad_footprint(pcb, ref="J1", x=20, y=50, net="NET1")
    _add_pad_footprint(pcb, ref="J2", x=32, y=50, net="NET1")
    pcb.add_trace(("J1", "1"), ("J2", "1"), width=width_b, layer="B.Cu", net="NET1")
    pcb.add_trace(("J1", "1"), ("J2", "1"), width=width_f, layer="F.Cu", net="NET1")
    return pcb


def _same_layer_doubled_ampacity_pcb(width: float, offset: float) -> PCB:
    """The issue #9542 shape: one straight F.Cu track plus a stepped F.Cu
    copy ``offset`` mm sideways, overlapping it along the whole run."""
    pcb = PCB.create(width=100, height=100, center=False)
    _add_pad_footprint(pcb, ref="J1", x=20, y=50, net="NET1")
    _add_pad_footprint(pcb, ref="J2", x=32, y=50, net="NET1")
    y = 50 - offset
    pcb.add_trace(("J1", "1"), ("J2", "1"), width=width, layer="F.Cu", net="NET1")
    pcb.add_trace(("J1", "1"), (22, y), width=width, layer="F.Cu", net="NET1")
    pcb.add_trace((22, y), (30, y), width=width, layer="F.Cu", net="NET1")
    pcb.add_trace((30, y), ("J2", "1"), width=width, layer="F.Cu", net="NET1")
    return pcb


class TestDoubledCopper:
    def test_doubled_pair_meeting_summed_requirement_passes(self) -> None:
        # 15 A needs ~6.29 mm at 2 oz; 2 x 3.5 mm carries it, 1 x 3.5 mm would not.
        rule = PathAmpacityRule(specs=[_trunk_spec(15.0)])
        results = rule.check(_doubled_ampacity_pcb(3.5, 3.5), _design_rules_2oz())
        assert results.errors == []

    def test_under_width_doubled_pair_still_fails(self) -> None:
        rule = PathAmpacityRule(specs=[_trunk_spec(15.0)])
        results = rule.check(_doubled_ampacity_pcb(2.0, 2.0), _design_rules_2oz())
        assert len(results.errors) == 1
        assert "parallel group" in results.errors[0].message

    def test_under_width_stitched_pair_still_fails(self) -> None:
        pcb = _doubled_ampacity_pcb(2.0, 2.0)
        pcb.add_via(26, 50, net="NET1")
        rule = PathAmpacityRule(specs=[_trunk_spec(15.0)])
        results = rule.check(pcb, _design_rules_2oz())
        messages = [e.message for e in results.errors]
        assert not any("ambiguous" in m for m in messages)
        assert any("parallel group" in m for m in messages)

    def test_single_run_same_width_still_fails(self) -> None:
        pcb = PCB.create(width=100, height=100, center=False)
        _add_pad_footprint(pcb, ref="J1", x=20, y=50, net="NET1")
        _add_pad_footprint(pcb, ref="J2", x=32, y=50, net="NET1")
        pcb.add_trace(("J1", "1"), ("J2", "1"), width=3.5, layer="B.Cu", net="NET1")
        rule = PathAmpacityRule(specs=[_trunk_spec(15.0)])
        assert rule.check(pcb, _design_rules_2oz()).errors


class TestSameLayerDoubledCopper:
    """Overlapping same-layer tracks are one conductor of merged width (#9542)."""

    def test_overlapping_pair_passes_at_merged_width(self) -> None:
        # 15 A needs ~6.29 mm at 2 oz; the merged bar is 3.0 + 2 x 1.75 = 6.5 mm.
        rule = PathAmpacityRule(specs=[_trunk_spec(15.0)])
        results = rule.check(_same_layer_doubled_ampacity_pcb(3.5, 3.0), _design_rules_2oz())
        assert results.errors == []

    def test_overlapping_pair_is_judged_on_merged_not_summed_width(self) -> None:
        # Merged 1.0 + 2 x 1.75 = 4.5 mm is under the ~6.29 mm requirement,
        # even though the summed-width reading (2 x 3.5 mm) would pass.
        rule = PathAmpacityRule(specs=[_trunk_spec(15.0)])
        results = rule.check(_same_layer_doubled_ampacity_pcb(3.5, 1.0), _design_rules_2oz())
        assert len(results.errors) == 1
        message = results.errors[0].message
        assert "parallel group" in message
        assert "merged width 4.500mm" in message
        assert "summed width" not in message

    def test_mixed_group_names_merged_and_summed(self) -> None:
        """Issue #9560: merged within F.Cu, summed with B.Cu -- say both.

        0.6 + 2 x 1.0 = 2.6 mm of merged F.Cu copper plus 2.0 mm on B.Cu;
        plain "merged" would read the 4.6 mm figure as one contiguous bar.
        """
        pcb = _same_layer_doubled_ampacity_pcb(2.0, 0.6)
        pcb.add_trace(("J1", "1"), ("J2", "1"), width=2.0, layer="B.Cu", net="NET1")
        rule = PathAmpacityRule(specs=[_trunk_spec(15.0)])
        results = rule.check(pcb, _design_rules_2oz())
        assert len(results.errors) == 1
        message = results.errors[0].message
        assert "merged+summed width 4.600mm" in message
        # Not the plain merged noun: "merged width" is absent (the mixed
        # wording embeds "summed width", so it cannot double as the negative).
        assert "merged width" not in message


# --------------------------------------------------------------------------
# A width-swap via between doubled stretches (Issue #8950 part b)
# --------------------------------------------------------------------------


def _width_swap_pcb(*, swap: bool) -> PCB:
    """Two doubled stretches meeting at one shared via (Issue #8950 (b)).

    Both sides of the via are doubled copper (wide on one layer, narrow on
    the other). With ``swap`` the split flips across the via -- wide F.Cu /
    narrow B.Cu, then narrow F.Cu / wide B.Cu -- so current must cross
    layers at the via to follow the wider copper. Without it the same split
    continues through the via, which is the benign stitching shape.
    """
    wide, narrow = 5.0, 2.0
    f_second, b_second = (narrow, wide) if swap else (wide, narrow)
    pcb = PCB.create(width=200, height=100, center=False)
    _add_pad_footprint(pcb, ref="J1", x=20, y=50, net="NET1")
    _add_pad_footprint(pcb, ref="J2", x=44, y=50, net="NET1")
    pcb.add_via(32, 50, size=0.6, drill=0.3, net="NET1")
    pcb.add_trace((20, 50), (32, 50), width=wide, layer="F.Cu", net="NET1")
    pcb.add_trace((32, 50), (44, 50), width=f_second, layer="F.Cu", net="NET1")
    pcb.add_trace((20, 50), (32, 50), width=narrow, layer="B.Cu", net="NET1")
    pcb.add_trace((32, 50), (44, 50), width=b_second, layer="B.Cu", net="NET1")
    return pcb


class TestWidthSwapVia:
    def test_width_swap_at_a_shared_via_is_judged(self) -> None:
        """The swap via carries real redistribution current, so it stays a
        series layer change and is judged on the path's current."""
        from kicad_tools.router.current_paths import resolve_current_path

        pcb = _width_swap_pcb(swap=True)
        resolution = resolve_current_path(pcb, _trunk_spec(15.0))
        (change,) = resolution.series_layer_changes
        assert change.count == 1 and change.position == pytest.approx((32.0, 50.0))
        errors = PathAmpacityRule(specs=[_trunk_spec(15.0)]).check(pcb, _design_rules_2oz()).errors
        assert len(errors) == 1
        assert "changes layer" in errors[0].message and "1 via(s)" in errors[0].message

    def test_matching_split_through_the_shared_via_stays_silent(self) -> None:
        """The control: same split on both sides, so the via only equalises
        the two branches -- no series layer change, no finding."""
        from kicad_tools.router.current_paths import resolve_current_path

        pcb = _width_swap_pcb(swap=False)
        resolution = resolve_current_path(pcb, _trunk_spec(15.0))
        assert resolution.series_layer_changes == ()
        assert (
            PathAmpacityRule(specs=[_trunk_spec(15.0)]).check(pcb, _design_rules_2oz()).errors == []
        )


# --------------------------------------------------------------------------
# Off-centreline tie vias in a layer-change bar (Issue #9561)
# --------------------------------------------------------------------------


def _tie_bar_pcb(*, ties: int = 7, width: float = 5.5, layers: int = 2) -> PCB:
    """The reporter's shape: a wide two-layer bar changing layer once.

    F.Cu carries the bar from J1 to the change at x=60, B.Cu from there to
    J2. One via sits exactly on the centreline junction; ``ties`` extra
    same-net vias are hand-placed inside the bar's copper, off the
    centreline, spreading to ~2.8 mm from the junction via (the reporter's
    own board stays within 3.2 mm).
    """
    pcb = PCB.create(width=200, height=120, center=False, layers=layers)
    _add_pad_footprint(pcb, ref="J1", x=20, y=50, net="NET1")
    _add_pad_footprint(pcb, ref="J2", x=120, y=50, net="NET1")
    pcb.add_trace(("J1", "1"), (60, 50), width=width, layer="F.Cu", net="NET1")
    pcb.add_trace((60, 50), ("J2", "1"), width=width, layer="B.Cu", net="NET1")
    pcb.add_via(60, 50, size=0.6, drill=0.3, net="NET1")
    offsets = [
        (0.9, 0.9),
        (-0.9, 0.9),
        (0.9, -0.9),
        (-0.9, -0.9),
        (1.8, 0.9),
        (-1.8, -0.9),
        (2.4, 1.5),
    ]
    for dx, dy in offsets[:ties]:
        pcb.add_via(60 + dx, 50 + dy, size=0.6, drill=0.3, net="NET1")
    return pcb


def _branch_via_pcb() -> PCB:
    """A same-net via on a thin branch teed off the junction, hanging off
    the bar's copper on the B.Cu side -- near the change, never parallel."""
    pcb = _tie_bar_pcb(ties=0)
    pcb.add_trace((60, 50), (61.2, 53.2), width=0.3, layer="F.Cu", net="NET1")
    pcb.add_via(61.2, 53.2, size=0.6, drill=0.3, net="NET1")
    return pcb


def _wide_bar_pcb() -> PCB:
    """An 8 mm bar with a same-net via on both layers' copper but 4.12 mm
    from the junction via -- past the tie bound, so never counted."""
    pcb = _tie_bar_pcb(ties=0, width=8.0)
    pcb.add_via(64.0, 51.0, size=0.6, drill=0.3, net="NET1")
    return pcb


def _blind_via_pcb() -> PCB:
    """A 4-layer bar with a blind via (F.Cu -> In1.Cu) inside the bar's
    F.Cu copper: it never reaches the change's B.Cu side."""
    pcb = _tie_bar_pcb(ties=0, layers=4)
    pcb.add_via(60.9, 50.9, size=0.6, drill=0.3, layers=("F.Cu", "In1.Cu"), net="NET1")
    return pcb


def _two_hop_pcb() -> PCB:
    """Two series layer changes 3 mm apart with one off-centre via inside
    the copper of both hops -- it must join the first change only."""
    pcb = PCB.create(width=200, height=120, center=False)
    _add_pad_footprint(pcb, ref="J1", x=20, y=50, net="NET1")
    _add_pad_footprint(pcb, ref="J2", x=120, y=50, net="NET1")
    pcb.add_trace(("J1", "1"), (50, 50), width=5.5, layer="F.Cu", net="NET1")
    pcb.add_trace((50, 50), (53, 50), width=5.5, layer="B.Cu", net="NET1")
    pcb.add_trace((53, 50), ("J2", "1"), width=5.5, layer="F.Cu", net="NET1")
    pcb.add_via(50, 50, size=0.6, drill=0.3, net="NET1")
    pcb.add_via(53, 50, size=0.6, drill=0.3, net="NET1")
    pcb.add_via(51.5, 50.8, size=0.6, drill=0.3, net="NET1")
    return pcb


class TestTieViasInLayerChangeBar:
    """Off-centreline tie vias inside the bar's copper count in parallel.

    A same-net via whose pad overlaps the change's copper on both layers,
    chained to a centreline via within the tie bound, is provably parallel
    to it, so its barrel capacity joins the change (Issue #9561).
    """

    def test_tie_vias_join_the_layer_change(self) -> None:
        from kicad_tools.physics.ampacity import via_barrel_current_capacity
        from kicad_tools.router.current_paths import resolve_current_path

        res = resolve_current_path(_tie_bar_pcb(ties=7), _trunk_spec())
        assert res.ok, res.reason
        (change,) = res.layer_changes
        assert change.count == 8
        assert len(change.vias) == 1 and len(change.tie_vias) == 7
        assert change.current_capacity_a() == pytest.approx(8 * via_barrel_current_capacity(0.3))

    def test_tie_vias_clear_the_warning_at_a_suitable_current(self) -> None:
        """One 0.3 mm drill barrel carries ~1.6 A: 8 A needs the tie vias.
        With them the finding is gone; without them it fires."""
        with_ties = PathAmpacityRule(specs=[_trunk_spec(8.0)]).check(
            _tie_bar_pcb(ties=7), _design_rules_2oz()
        )
        assert with_ties.errors == []
        assert with_ties.warnings == []
        without = PathAmpacityRule(specs=[_trunk_spec(8.0)]).check(
            _tie_bar_pcb(ties=0), _design_rules_2oz()
        )
        assert len([e for e in without.errors if "changes layer" in e.message]) == 1

    def test_message_breaks_the_count_down(self) -> None:
        out = PathAmpacityRule(specs=[_trunk_spec(8.0)]).check(
            _tie_bar_pcb(ties=2), _design_rules_2oz()
        )
        (message,) = [e.message for e in out.errors if "changes layer" in e.message]
        assert "through 3 via(s) (1 on the centreline, 2 tie vias in the bar)" in message
        assert "not counted" not in message

    def test_branch_via_off_the_bar_copper_is_excluded_with_reason(self) -> None:
        out = PathAmpacityRule(specs=[_trunk_spec(8.0)]).check(
            _branch_via_pcb(), _design_rules_2oz()
        )
        (message,) = [e.message for e in out.errors if "changes layer" in e.message]
        assert "through 1 via(s)" in message
        assert "1 same-net via(s) not counted: 1 off the bar copper" in message

    def test_on_copper_via_beyond_the_tie_bound_is_excluded_with_reason(self) -> None:
        out = PathAmpacityRule(specs=[_trunk_spec(8.0)]).check(_wide_bar_pcb(), _design_rules_2oz())
        (message,) = [e.message for e in out.errors if "changes layer" in e.message]
        assert "through 1 via(s)" in message
        assert "1 same-net via(s) not counted: 1 beyond the 3.5mm tie bound" in message

    def test_blind_via_on_one_layer_of_the_pair_is_excluded_with_reason(self) -> None:
        out = PathAmpacityRule(specs=[_trunk_spec(8.0)]).check(
            _blind_via_pcb(), _design_rules_2oz()
        )
        (message,) = [e.message for e in out.errors if "changes layer" in e.message]
        assert "through 1 via(s)" in message
        assert "1 same-net via(s) not counted: 1 not spanning both layers" in message

    def test_via_within_range_of_two_changes_joins_the_first(self) -> None:
        from kicad_tools.router.current_paths import resolve_current_path

        res = resolve_current_path(_two_hop_pcb(), _trunk_spec())
        assert res.ok, res.reason
        assert [c.count for c in res.layer_changes] == [2, 1]
        assert len(res.layer_changes[0].tie_vias) == 1
        assert res.layer_changes[1].tie_vias == ()

    def test_off_centre_stitch_beside_an_interior_stitch_stays_benign(self) -> None:
        """#8916 guard: an off-centre hand via that chains to a benign
        interior stitch inside doubled copper must not turn that stitch
        into a series layer change."""
        from kicad_tools.router.current_paths import resolve_current_path

        pcb = _doubled_ampacity_pcb(3.5, 3.5)
        pcb.add_via(26, 50, size=0.6, drill=0.3, net="NET1")
        pcb.add_via(26.9, 50.8, size=0.6, drill=0.3, net="NET1")
        res = resolve_current_path(pcb, _trunk_spec(15.0))
        assert res.ok, res.reason
        assert res.series_layer_changes == ()
        assert (
            PathAmpacityRule(specs=[_trunk_spec(15.0)]).check(pcb, _design_rules_2oz()).errors == []
        )


# --------------------------------------------------------------------------
# Buttress-wire credit on fully wired legs (Issue #9648)
# --------------------------------------------------------------------------

_WIRE_NET = "FUSED_LINE"


def _force_spec(
    continuous_a: float = 15.0,
    *,
    pulsed_a: float | None = None,
    duty_cycle: float | None = None,
    pulse_duration_s: float | None = None,
) -> CurrentPathSpec:
    return CurrentPathSpec(
        name="FORCE",
        net_name=_WIRE_NET,
        source=PathEndpoint("J1", "1"),
        sink=PathEndpoint("J2", "1"),
        continuous_a=continuous_a,
        pulsed_a=pulsed_a,
        duty_cycle=duty_cycle,
        pulse_duration_s=pulse_duration_s,
        reinforcement_eligible=True,
    )


def _wired_trunk(
    trace_width: float = 2.0, *, reinforced: bool = True, end_width: float | None = None
) -> tuple[PCB, list[dict]]:
    """A straight reinforcement-eligible trunk, with a real placed 16 AWG wire.

    Built with the placer itself (``reinforce_net``) so the credit is judged
    against what ``kct pcb reinforce`` actually writes: two end anchors, a
    continuous mask opening and a sidecar wire record. ``end_width`` splits
    the run into wide end stretches at that width flanking a ``trace_width``
    middle -- the shape a board the wire actually rescues has, since the
    hole-to-pad stubs outside the anchors are always judged at bare copper
    (Issue #9654).
    """
    pcb = PCB.create(width=200, height=100, center=False)
    if end_width is None or end_width == trace_width:
        pcb.add_trace((20, 50), (120, 50), width=trace_width, layer="F.Cu", net=_WIRE_NET)
    else:
        pcb.add_trace((20, 50), (30, 50), width=end_width, layer="F.Cu", net=_WIRE_NET)
        pcb.add_trace((30, 50), (110, 50), width=trace_width, layer="F.Cu", net=_WIRE_NET)
        pcb.add_trace((110, 50), (120, 50), width=end_width, layer="F.Cu", net=_WIRE_NET)
    _add_th_pad_footprint(pcb, ref="J1", x=20.0, y=50.0, net=_WIRE_NET, drill=1.0, size=2.0)
    _add_th_pad_footprint(pcb, ref="J2", x=120.0, y=50.0, net=_WIRE_NET, drill=1.0, size=2.0)
    wires: list[dict] = []
    if reinforced:
        from kicad_tools.pcb.reinforcement_sidecar import build_wire_records

        outcome = reinforce_net(pcb, _WIRE_NET)
        wires = build_wire_records(outcome, 0.0, 0.0)
    return pcb, wires


def _wired_legs(pcb: PCB, wires: list[dict], spec: CurrentPathSpec) -> list[WiredLeg]:
    from kicad_tools.validate.rules.reinforcement_incomplete import ReinforcementIncompleteRule

    return ReinforcementIncompleteRule(specs=[spec], wires=wires).fully_wired_legs(
        pcb, _design_rules_2oz()
    )


def _t_junction(specs: list[CurrentPathSpec]) -> tuple[PCB, list[WiredLeg]]:
    """A reinforced T-junction, wired by the placer itself (Issue #9654).

    A 6.5 mm trunk from J1 at (20, 50) through the junction at (85, 50) to
    J2 at (150, 50), with a perpendicular branch dropping from the junction
    through y = 56 and 70 to J3 at (85, 80) at 2, 2 and 6.5 mm. Every leg
    of every spec is declared reinforcement-eligible, so ``reinforce_net``
    wires both the trunk and the branch -- the first branch segment sits
    inside the trunk's corridor with both endpoints projecting to the same
    trunk point, the shape that must be matched to the leg it follows.
    """
    pcb = PCB.create(width=200, height=120, center=False)
    pcb.add_trace((20, 50), (85, 50), width=6.5, layer="F.Cu", net=_WIRE_NET)
    pcb.add_trace((85, 50), (150, 50), width=6.5, layer="F.Cu", net=_WIRE_NET)
    pcb.add_trace((85, 50), (85, 56), width=2.0, layer="F.Cu", net=_WIRE_NET)
    pcb.add_trace((85, 56), (85, 70), width=2.0, layer="F.Cu", net=_WIRE_NET)
    pcb.add_trace((85, 70), (85, 80), width=6.5, layer="F.Cu", net=_WIRE_NET)
    _add_th_pad_footprint(pcb, ref="J1", x=20.0, y=50.0, net=_WIRE_NET, drill=1.0, size=2.0)
    _add_th_pad_footprint(pcb, ref="J2", x=150.0, y=50.0, net=_WIRE_NET, drill=1.0, size=2.0)
    _add_th_pad_footprint(pcb, ref="J3", x=85.0, y=80.0, net=_WIRE_NET, drill=1.0, size=2.0)
    from kicad_tools.pcb.reinforcement_sidecar import build_wire_records
    from kicad_tools.validate.rules.reinforcement_incomplete import ReinforcementIncompleteRule

    outcome = reinforce_net(pcb, _WIRE_NET, current_paths=specs, all_runs=True)
    wires = build_wire_records(outcome, 0.0, 0.0)
    legs = ReinforcementIncompleteRule(specs=specs, wires=wires).fully_wired_legs(
        pcb, _design_rules_2oz()
    )
    return pcb, legs


class TestButtressWireCredit:
    def test_hole_to_pad_stub_is_judged_at_copper_width(self) -> None:
        # Issue #9654: the wire is soldered between the two anchors, so the
        # ~2.96 mm hole-to-pad stub at each end of the run is bare copper
        # and must fail at its own width (15 A at 2 oz needs ~6.29 mm),
        # while the anchored span between them stays clean.
        pcb, wires = _wired_trunk(trace_width=2.0)
        spec = _force_spec(15.0)
        rule = PathAmpacityRule(specs=[spec], wired_legs=_wired_legs(pcb, wires, spec))
        results = rule.check(pcb, _design_rules_2oz())
        underwidth = [v for v in results.errors if "too narrow" in v.message]
        assert len(underwidth) == 2
        for v in underwidth:
            assert "width 2.000mm; this stretch is not covered by a wire leg" in v.message
            assert "wire =" not in v.message
        xs = sorted(v.location[0] for v in underwidth)
        assert xs[0] == pytest.approx(21.5, abs=0.1)
        assert xs[1] == pytest.approx(118.5, abs=0.1)

    def test_stub_across_a_bend_is_still_bare_copper(self) -> None:
        # The run bends 2 mm in from its end, inside the start stub: each
        # stretch of the polyline before the anchor is judged at its own
        # copper width, the span between the anchors stays clean.
        pcb = PCB.create(width=200, height=100, center=False)
        pcb.add_trace((20, 50), (22, 50), width=2.0, layer="F.Cu", net=_WIRE_NET)
        pcb.add_trace((22, 50), (120, 68), width=2.0, layer="F.Cu", net=_WIRE_NET)
        _add_th_pad_footprint(pcb, ref="J1", x=20.0, y=50.0, net=_WIRE_NET, drill=1.0, size=2.0)
        _add_th_pad_footprint(pcb, ref="J2", x=120.0, y=68.0, net=_WIRE_NET, drill=1.0, size=2.0)
        from kicad_tools.pcb.reinforcement_sidecar import build_wire_records

        outcome = reinforce_net(pcb, _WIRE_NET)
        wires = build_wire_records(outcome, 0.0, 0.0)
        spec = _force_spec(15.0)
        rule = PathAmpacityRule(specs=[spec], wired_legs=_wired_legs(pcb, wires, spec))
        results = rule.check(pcb, _design_rules_2oz())
        underwidth = [v for v in results.errors if "too narrow" in v.message]
        assert len(underwidth) == 3  # first segment, bent stub, end stub
        for v in underwidth:
            assert "width 2.000mm; this stretch is not covered by a wire leg" in v.message
        # the whole first segment sits in the start stub, at its midpoint
        assert underwidth[0].location == pytest.approx((21.0, 50.0))

    def test_segment_straddling_one_anchor_splits_at_it(self) -> None:
        # Two collinear segments sharing a leg whose anchors sit at arc
        # 10 and 90: each segment is credited only past the anchor it
        # straddles, and both bare stubs fail at their own width.
        pcb = PCB.create(width=200, height=100, center=False)
        pcb.add_trace((20, 50), (70, 50), width=2.0, layer="F.Cu", net=_WIRE_NET)
        pcb.add_trace((70, 50), (120, 50), width=2.0, layer="F.Cu", net=_WIRE_NET)
        _add_th_pad_footprint(pcb, ref="J1", x=20.0, y=50.0, net=_WIRE_NET, drill=1.0, size=2.0)
        _add_th_pad_footprint(pcb, ref="J2", x=120.0, y=50.0, net=_WIRE_NET, drill=1.0, size=2.0)
        leg = WiredLeg(
            net=_WIRE_NET,
            layer="F.Cu",
            points=((20.0, 50.0), (70.0, 50.0), (120.0, 50.0)),
            width=2.0,
            gauge=16,
            anchor_arc=(10.0, 90.0),
        )
        rule = PathAmpacityRule(specs=[_force_spec(15.0)], wired_legs=[leg])
        results = rule.check(pcb, _design_rules_2oz())
        underwidth = [v for v in results.errors if "too narrow" in v.message]
        assert [v.location for v in underwidth] == [(25.0, 50.0), (115.0, 50.0)]
        for v in underwidth:
            assert "width 2.000mm; this stretch is not covered by a wire leg" in v.message

    def test_fully_wired_leg_clears_the_underwidth_errors(self) -> None:
        # The unwired stubs are wide enough on their own (6.5 mm > 6.29 mm
        # at 15 A) and the 2.0 mm middle stretch rides the 16 AWG wire
        # (~18.7 mm of extra width at 2 oz): nothing fails.
        pcb, wires = _wired_trunk(trace_width=2.0, end_width=6.5)
        spec = _force_spec(15.0)
        rule = PathAmpacityRule(specs=[spec], wired_legs=_wired_legs(pcb, wires, spec))
        results = rule.check(pcb, _design_rules_2oz())
        assert results.errors == []

    def test_same_path_without_a_wire_still_fails_identically(self) -> None:
        pcb, _ = _wired_trunk(trace_width=2.0, reinforced=False)
        spec = _force_spec(15.0)
        with_legs = PathAmpacityRule(specs=[spec], wired_legs=_wired_legs(pcb, [], spec)).check(
            pcb, _design_rules_2oz()
        )
        baseline = PathAmpacityRule(specs=[spec]).check(pcb, _design_rules_2oz())
        assert [v.message for v in with_legs.errors] == [v.message for v in baseline.errors]
        assert len(with_legs.errors) == 1
        assert "too narrow" in with_legs.errors[0].message

    def test_incomplete_leg_gets_no_credit(self) -> None:
        # Remove one end anchor: reinforcement_incomplete rejects the leg, so
        # path_ampacity must not credit the wire it no longer trusts.
        pcb, wires = _wired_trunk(trace_width=2.0)
        anchor = min((v for v in pcb.vias if v.net_name == _WIRE_NET), key=lambda v: v.position[0])
        pcb.vias.remove(anchor)
        spec = _force_spec(15.0)
        rule = PathAmpacityRule(specs=[spec], wired_legs=_wired_legs(pcb, wires, spec))
        results = rule.check(pcb, _design_rules_2oz())
        assert len(results.errors) == 1
        assert "too narrow" in results.errors[0].message

    def test_unwired_neck_on_the_same_path_still_fails(self) -> None:
        # The wire leg covers only the first stretch; the 0.5 mm neck past
        # its end is judged at its bare copper width and says so.
        pcb = PCB.create(width=200, height=100, center=False)
        pcb.add_trace((20, 50), (70, 50), width=2.4, layer="F.Cu", net=_WIRE_NET)
        pcb.add_trace((70, 50), (120, 50), width=0.5, layer="F.Cu", net=_WIRE_NET)
        _add_th_pad_footprint(pcb, ref="J1", x=20.0, y=50.0, net=_WIRE_NET, drill=1.0, size=2.0)
        _add_th_pad_footprint(pcb, ref="J2", x=120.0, y=50.0, net=_WIRE_NET, drill=1.0, size=2.0)
        leg = WiredLeg(
            net=_WIRE_NET,
            layer="F.Cu",
            points=((20.0, 50.0), (70.0, 50.0)),
            width=2.4,
            gauge=16,
        )
        rule = PathAmpacityRule(specs=[_force_spec(13.42)], wired_legs=[leg])
        results = rule.check(pcb, _design_rules_2oz())
        underwidth = [v for v in results.errors if "too narrow" in v.message]
        assert len(underwidth) == 1
        assert "not covered by a wire leg" in underwidth[0].message
        assert "width 0.500mm" in underwidth[0].message

    def test_credited_segment_that_still_fails_states_the_wire(self) -> None:
        # ~224 mm is required at 200 A: even the credited 2.0 + ~18.7 mm
        # effective width fails and must say what was counted -- while the
        # two bare hole-to-pad stubs fail at their own copper width.
        pcb, wires = _wired_trunk(trace_width=2.0)
        spec = _force_spec(200.0)
        rule = PathAmpacityRule(specs=[spec], wired_legs=_wired_legs(pcb, wires, spec))
        results = rule.check(pcb, _design_rules_2oz())
        underwidth = [v for v in results.errors if "too narrow" in v.message]
        assert len(underwidth) == 3
        (with_wire,) = [v for v in underwidth if "wire =" in v.message]
        assert "width 2.000mm + 16 AWG wire = 20.700mm effective" in with_wire.message
        assert "not covered" not in with_wire.message
        assert with_wire.actual_value == pytest.approx(20.7, abs=0.01)
        assert all(
            "not covered by a wire leg" in v.message for v in underwidth if v is not with_wire
        )

    def test_wire_leg_on_another_layer_does_not_credit(self) -> None:
        pcb, _ = _wired_trunk(trace_width=2.0, reinforced=False)
        spec = _force_spec(15.0)
        leg = WiredLeg(
            net=_WIRE_NET,
            layer="B.Cu",
            points=((20.0, 50.0), (120.0, 50.0)),
            width=2.0,
            gauge=16,
        )
        results = PathAmpacityRule(specs=[spec], wired_legs=[leg]).check(pcb, _design_rules_2oz())
        assert len(results.errors) == 1
        assert "width 2.000mm" in results.errors[0].message

    def test_unsupported_gauge_falls_back_to_no_credit(self) -> None:
        pcb, _ = _wired_trunk(trace_width=2.0, reinforced=False)
        spec = _force_spec(15.0)
        leg = WiredLeg(
            net=_WIRE_NET,
            layer="F.Cu",
            points=((20.0, 50.0), (120.0, 50.0)),
            width=2.0,
            gauge=18,
        )
        with_leg = PathAmpacityRule(specs=[spec], wired_legs=[leg]).check(pcb, _design_rules_2oz())
        baseline = PathAmpacityRule(specs=[spec]).check(pcb, _design_rules_2oz())
        assert [v.message for v in with_leg.errors] == [v.message for v in baseline.errors]

    def test_credit_scales_with_the_layer_copper_thickness(self) -> None:
        # The same wire over 1 oz foil is worth twice the width (same area,
        # half the thickness): the credited stretch that still fails at
        # 200 A reports ~39.4 mm effective at 1 oz versus ~20.7 mm at 2 oz.
        pcb, wires = _wired_trunk(trace_width=2.0)
        spec = _force_spec(200.0)
        rules_1oz = dataclasses.replace(_design_rules_2oz(), outer_copper_oz=1.0)
        from kicad_tools.validate.rules.reinforcement_incomplete import ReinforcementIncompleteRule

        def credited_effective(rules: DesignRules) -> float:
            legs = ReinforcementIncompleteRule(specs=[spec], wires=wires).fully_wired_legs(
                pcb, rules
            )
            results = PathAmpacityRule(specs=[spec], wired_legs=legs).check(pcb, rules)
            (with_wire,) = [v for v in results.errors if "wire =" in v.message]
            return float(with_wire.actual_value)

        assert credited_effective(_design_rules_2oz()) == pytest.approx(20.7, abs=0.01)
        assert credited_effective(rules_1oz) == pytest.approx(39.4, abs=0.05)


class TestButtressWireFusing:
    """Onderdonk fusing is judged on the combined section (#9648)."""

    # 6.0 mm of 2 oz copper fuses at ~121 A for a 1 s pulse; + 16 AWG wire
    # (~18.7 mm credit) raises that to ~499 A. The RMS floor of a 300 A peak
    # at 0.2% duty (~13.45 A -> ~5.4 mm) stays met by the bare 6.0 mm copper,
    # so the fusing check is the only thing that moves.
    _SPEC = {"continuous_a": 1.0, "pulsed_a": 300.0, "duty_cycle": 0.002, "pulse_duration_s": 1.0}

    def test_the_stubs_fuse_but_the_wired_span_carries_the_pulse(self) -> None:
        pcb, wires = _wired_trunk(trace_width=6.0)
        spec = _force_spec(**self._SPEC)
        bare = PathAmpacityRule(specs=[spec]).check(pcb, _design_rules_2oz())
        assert any("would fuse" in v.message for v in bare.errors)
        credited = PathAmpacityRule(specs=[spec], wired_legs=_wired_legs(pcb, wires, spec)).check(
            pcb, _design_rules_2oz()
        )
        # The bare ~2.96 mm hole-to-pad stubs still fuse at their own
        # 6.0 mm copper (Issue #9654); the stretch between the anchors
        # rides the wire (~499 A) and survives.
        fused = [v for v in credited.errors if "would fuse" in v.message]
        assert len(fused) == 2
        for v in fused:
            assert "6.000mm of 2.0oz copper" in v.message
            assert "wire" not in v.message

    def test_pulse_above_the_combined_section_names_the_wire(self) -> None:
        # 600 A for 1 s fuses even the combined ~24.7 mm section; the bare
        # stubs fuse at 6.0 mm copper too, but only the credited stretch
        # names the wire.
        pcb, wires = _wired_trunk(trace_width=6.0)
        spec = _force_spec(
            continuous_a=1.0, pulsed_a=600.0, duty_cycle=0.0005, pulse_duration_s=1.0
        )
        rule = PathAmpacityRule(specs=[spec], wired_legs=_wired_legs(pcb, wires, spec))
        results = rule.check(pcb, _design_rules_2oz())
        fused = [v for v in results.errors if "would fuse" in v.message]
        assert len(fused) == 3
        (with_wire,) = [v for v in fused if "wire" in v.message]
        assert "24.700mm effective" in with_wire.message
        assert "16 AWG wire" in with_wire.message


class TestButtressWireTJunction:
    """A wired trunk must not hide an undersized stub on its branch (#9654).

    The first branch segment passes the trunk's midpoint-distance test and
    both of its endpoints project to the same trunk point: it must be
    matched to the leg whose polyline it follows (the branch wire), so the
    2 mm stretch between the junction and the first branch anchor is
    judged at bare copper -- in either leg order, for both the width and
    the fusing checks that share the credit spans.
    """

    @staticmethod
    def _specs(
        *, pulsed_a: float | None = None, duty_cycle: float | None = None
    ) -> list[CurrentPathSpec]:
        trunk = CurrentPathSpec(
            name="TRUNK",
            net_name=_WIRE_NET,
            source=PathEndpoint("J1", "1"),
            sink=PathEndpoint("J2", "1"),
            continuous_a=15.0,
            reinforcement_eligible=True,
        )
        branch = CurrentPathSpec(
            name="BRANCH",
            net_name=_WIRE_NET,
            source=PathEndpoint("J1", "1"),
            sink=PathEndpoint("J3", "1"),
            continuous_a=15.0,
            pulsed_a=pulsed_a,
            duty_cycle=duty_cycle,
            pulse_duration_s=1.0 if pulsed_a is not None else None,
            reinforcement_eligible=True,
        )
        return [trunk, branch]

    def test_branch_stub_fails_at_bare_copper_in_either_leg_order(self) -> None:
        specs = self._specs()
        pcb, legs = _t_junction(specs)
        assert len(legs) == 2  # the trunk wire and the branch wire
        for wired in (legs, legs[::-1]):
            results = PathAmpacityRule(specs=specs, wired_legs=wired).check(
                pcb, _design_rules_2oz()
            )
            underwidth = [v for v in results.errors if "too narrow" in v.message]
            assert [v.location for v in underwidth] == [pytest.approx((85.0, 52.604))]
            assert "width 2.000mm; this stretch is not covered by a wire leg" in (
                underwidth[0].message
            )
            assert "wire =" not in underwidth[0].message

    def test_branch_stub_pulse_fuses_at_bare_copper_in_either_leg_order(self) -> None:
        specs = self._specs(pulsed_a=100.0, duty_cycle=0.001)
        pcb, legs = _t_junction(specs)
        assert len(legs) == 2
        for wired in (legs, legs[::-1]):
            results = PathAmpacityRule(specs=specs, wired_legs=wired).check(
                pcb, _design_rules_2oz()
            )
            underwidth = [v for v in results.errors if "too narrow" in v.message]
            fused = [v for v in results.errors if "would fuse" in v.message]
            assert [v.location for v in underwidth] == [pytest.approx((85.0, 52.604))]
            assert [v.location for v in fused] == [pytest.approx((85.0, 52.604))]
            for v in fused:
                assert "2.000mm of 2.0oz copper" in v.message
                assert "wire" not in v.message


class TestButtressWireCreditThroughChecker:
    def test_check_path_ampacity_credits_placed_wires(self) -> None:
        from kicad_tools.validate.checker import DRCChecker

        # The 14 mm end stretches carry 15 A on bare copper past the
        # anchors even at the checker's default foil weight; the 2.0 mm
        # middle is rescued by the wire.
        pcb, wires = _wired_trunk(trace_width=2.0, end_width=14.0)
        checker = DRCChecker(pcb, current_path_specs=[_force_spec(15.0)], reinforcement_wires=wires)
        assert checker.check_path_ampacity().errors == []

        # Break the wire (one end anchor gone): the credit disappears and
        # the bare-width error comes back on the middle stretch.
        anchor = min((v for v in pcb.vias if v.net_name == _WIRE_NET), key=lambda v: v.position[0])
        pcb.vias.remove(anchor)
        broken = DRCChecker(pcb, current_path_specs=[_force_spec(15.0)], reinforcement_wires=wires)
        errors = broken.check_path_ampacity().errors
        assert errors and "too narrow" in errors[0].message
        assert "width 2.000mm" in errors[0].message

    def test_unreinforced_board_output_is_unchanged(self) -> None:
        from kicad_tools.validate.checker import DRCChecker

        pcb, _ = _wired_trunk(trace_width=2.0, reinforced=False)
        checker = DRCChecker(pcb, current_path_specs=[_force_spec(15.0)])
        errors = checker.check_path_ampacity().errors
        assert len(errors) == 1
        assert "too narrow" in errors[0].message


class TestButtressWireCreditFromReinforceNet:
    """The uncovered-stretch verdict on legs the placer itself derived (#9666).

    The uncovered-stretch cases above feed ``PathAmpacityRule`` hand-built
    ``WiredLeg`` geometry; this one builds a layer-change board, lets
    ``reinforce_net`` anchor only the F.Cu leg, and judges the credit through
    the real leg derivation (frame, per-layer splitting, run chaining).
    """

    def test_bcu_stretch_outside_a_reinforce_net_leg_still_fails(self) -> None:
        from kicad_tools.pcb.reinforcement_sidecar import build_wire_records

        # F.Cu (20,50)->(70,50) 2.4 mm, via at (70,50), B.Cu
        # (70,50)->(120,50) 2.4 mm, one declared 13.42 A eligible path.
        # reinforce_net anchors the F.Cu leg only, so path_ampacity credits
        # F.Cu and must judge the B.Cu stretch at bare copper width.
        pcb = PCB.create(width=200, height=100, center=False)
        pcb.add_trace((20, 50), (70, 50), width=2.4, layer="F.Cu", net=_WIRE_NET)
        pcb.add_via(70, 50, size=0.6, drill=0.3, net=_WIRE_NET)
        pcb.add_trace((70, 50), (120, 50), width=2.4, layer="B.Cu", net=_WIRE_NET)
        _add_th_pad_footprint(pcb, ref="J1", x=20.0, y=50.0, net=_WIRE_NET, drill=1.0, size=2.0)
        _add_th_pad_footprint(pcb, ref="J2", x=120.0, y=50.0, net=_WIRE_NET, drill=1.0, size=2.0)
        spec = _force_spec(13.42)
        outcome = reinforce_net(pcb, _WIRE_NET, current_paths=[spec], layer="F.Cu")
        wires = build_wire_records(outcome, 0.0, 0.0)
        legs = _wired_legs(pcb, wires, spec)

        # The placer derived exactly the F.Cu leg: the B.Cu stretch of the
        # same path is anchored on no layer and must not be credited.
        assert [(leg.layer, leg.points, leg.width, leg.gauge) for leg in legs] == [
            ("F.Cu", ((20.0, 50.0), (70.0, 50.0)), 2.4, 16)
        ]
        results = PathAmpacityRule(specs=[spec], wired_legs=legs).check(pcb, _design_rules_2oz())
        underwidth = [v for v in results.errors if "too narrow" in v.message]
        assert len(underwidth) == 1
        assert "trace on B.Cu too narrow" in underwidth[0].message
        assert "not covered by a wire leg" in underwidth[0].message
        assert "width 2.400mm" in underwidth[0].message
        assert not any("trace on F.Cu" in v.message for v in results.errors)
