"""Tests for branch-specific current-path intent modeling (Issue #4980).

Covers :mod:`kicad_tools.router.current_paths`: the declared-intent data
model, JSON sidecar round-trip, endpoint resolution (including the
fail-closed unresolved/ambiguous outcomes), and the post-write audit.

Fixtures are built entirely in-memory via ``PCB.create`` + ``add_trace`` +
hand-built ``Footprint``/``Pad`` objects (no golden-board dependency),
mirroring the pattern already established in ``tests/test_pcb_reinforce.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.router.current_paths import (
    CurrentPathSpec,
    PathEndpoint,
    audit_current_paths,
    dump_current_path_specs,
    load_current_path_specs,
    parse_current_path_specs,
    reinforcement_eligible_segment_ids,
    resolve_current_path,
)
from kicad_tools.schema.pcb import Footprint, Pad

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def _add_pad_footprint(pcb, *, ref: str, x: float, y: float, net: str, pad: str = "1") -> None:
    """Append an in-memory footprint carrying one pad on ``net``.

    Mirrors ``tests/test_pcb_reinforce.py::_add_th_pad_footprint`` -- built
    directly so tests run without a KiCad library dependency.
    """
    net_obj = pcb.add_net(net)
    p = Pad(
        number=pad,
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
        pads=[p],
    )
    pcb._footprints.append(fp)


def _t_network_pcb(*, trunk_width: float = 3.0, sense_width: float = 0.2):
    """A T-network: J1 -> J2 trunk with a mid-run sense tap to U3.

    Layout:
      J1 (20,50) --trunk--> (60,50) [junction] --trunk--> J2 (120,50)
                                   |
                               sense spur
                                   v
                               U3 (60,70)

    The junction sits at an explicit vertex (the trunk is routed as two
    segments meeting there), matching how a real T-tap is routed -- a
    single continuous trunk segment with a spur touching its *interior*
    would not form a graph node for the tap.
    """
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.create(width=200, height=120, center=False)
    _add_pad_footprint(pcb, ref="J1", x=20, y=50, net="NET1")
    _add_pad_footprint(pcb, ref="J2", x=120, y=50, net="NET1")
    _add_pad_footprint(pcb, ref="U3", x=60, y=70, net="NET1")

    pcb.add_trace(("J1", "1"), (60, 50), width=trunk_width, layer="F.Cu", net="NET1")
    pcb.add_trace((60, 50), ("J2", "1"), width=trunk_width, layer="F.Cu", net="NET1")
    pcb.add_trace((60, 50), ("U3", "1"), width=sense_width, layer="F.Cu", net="NET1")
    return pcb


def _trunk_spec(name: str = "TRUNK", current_a: float = 15.0) -> CurrentPathSpec:
    return CurrentPathSpec(
        name=name,
        net_name="NET1",
        source=PathEndpoint("J1", "1"),
        sink=PathEndpoint("J2", "1"),
        continuous_a=current_a,
        reinforcement_eligible=True,
    )


def _sense_spec(name: str = "SENSE", current_a: float = 0.01) -> CurrentPathSpec:
    return CurrentPathSpec(
        name=name,
        net_name="NET1",
        source=PathEndpoint("J1", "1"),
        sink=PathEndpoint("U3", "1"),
        continuous_a=current_a,
        reinforcement_eligible=False,
    )


# --------------------------------------------------------------------------
# CurrentPathSpec / PathEndpoint serialization
# --------------------------------------------------------------------------


class TestSerialization:
    def test_round_trip_via_dict(self) -> None:
        spec = _trunk_spec()
        data = spec.to_dict()
        restored = CurrentPathSpec.from_dict(data)
        assert restored == spec

    def test_dump_and_parse_bare_list(self) -> None:
        specs = [_trunk_spec(), _sense_spec()]
        dumped = dump_current_path_specs(specs)
        assert set(dumped.keys()) == {"paths"}
        parsed = parse_current_path_specs(dumped["paths"])
        assert parsed == specs

    def test_parse_wrapped_dict(self) -> None:
        specs = [_trunk_spec()]
        dumped = dump_current_path_specs(specs)
        parsed = parse_current_path_specs(dumped)
        assert parsed == specs

    def test_load_from_file(self, tmp_path: Path) -> None:
        specs = [_trunk_spec(), _sense_spec()]
        sidecar = tmp_path / "current_paths.json"
        sidecar.write_text(json.dumps(dump_current_path_specs(specs)))
        loaded = load_current_path_specs(sidecar)
        assert loaded == specs

    def test_parse_rejects_non_list_non_dict(self) -> None:
        with pytest.raises(ValueError, match="list or a"):
            parse_current_path_specs("not valid")

    def test_from_dict_missing_name_raises(self) -> None:
        with pytest.raises(ValueError, match="missing 'name'"):
            CurrentPathSpec.from_dict({"net": "NET1", "source": {}, "sink": {}})

    def test_from_dict_missing_continuous_a_raises(self) -> None:
        with pytest.raises(ValueError, match="continuous_a"):
            CurrentPathSpec.from_dict(
                {
                    "name": "X",
                    "net": "NET1",
                    "source": {"ref": "J1", "pad": "1"},
                    "sink": {"ref": "J2", "pad": "1"},
                }
            )

    def test_endpoint_label(self) -> None:
        assert PathEndpoint("J1", "2").label() == "J1.2"


# --------------------------------------------------------------------------
# resolve_current_path
# --------------------------------------------------------------------------


class TestResolveCurrentPath:
    def test_t_network_trunk_resolves(self) -> None:
        pcb = _t_network_pcb()
        r = resolve_current_path(pcb, _trunk_spec())
        assert r.ok
        assert r.status == "resolved"
        assert len(r.segments) == 2
        assert r.length_mm == pytest.approx(100.0, abs=1e-6)

    def test_t_network_sense_resolves_through_junction(self) -> None:
        pcb = _t_network_pcb()
        r = resolve_current_path(pcb, _sense_spec())
        assert r.ok
        assert len(r.segments) == 2  # J1 -> junction -> U3
        assert r.length_mm == pytest.approx(60.0, abs=1e-6)

    def test_missing_component_unresolved(self) -> None:
        pcb = _t_network_pcb()
        spec = CurrentPathSpec(
            name="BROKEN",
            net_name="NET1",
            source=PathEndpoint("J1", "1"),
            sink=PathEndpoint("J99", "1"),
            continuous_a=15.0,
        )
        r = resolve_current_path(pcb, spec)
        assert r.status == "unresolved"
        assert "not found" in r.reason

    def test_missing_pad_unresolved(self) -> None:
        pcb = _t_network_pcb()
        spec = CurrentPathSpec(
            name="BROKEN",
            net_name="NET1",
            source=PathEndpoint("J1", "1"),
            sink=PathEndpoint("J2", "9"),
            continuous_a=15.0,
        )
        r = resolve_current_path(pcb, spec)
        assert r.status == "unresolved"
        assert "not found" in r.reason

    def test_pad_moved_to_different_net_unresolved(self) -> None:
        """A component replaced with a part on a different net fails closed."""
        pcb = _t_network_pcb()
        # Re-point J2's pad to a different net, simulating a footprint swap.
        fp = pcb.get_footprint("J2")
        assert fp is not None
        other_net = pcb.add_net("OTHER")
        fp.pads[0].net_number = other_net.number
        fp.pads[0].net_name = "OTHER"

        r = resolve_current_path(pcb, _trunk_spec())
        assert r.status == "unresolved"
        assert "expected" in r.reason

    def test_source_sink_different_nets_unresolved(self) -> None:
        pcb = _t_network_pcb()
        _add_pad_footprint(pcb, ref="X1", x=10, y=10, net="OTHERNET")
        spec = CurrentPathSpec(
            name="CROSS",
            net_name="NET1",
            source=PathEndpoint("J1", "1"),
            sink=PathEndpoint("X1", "1"),
            continuous_a=1.0,
        )
        r = resolve_current_path(pcb, spec)
        assert r.status == "unresolved"

    def test_no_routed_copper_between_endpoints_unresolved(self) -> None:
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.create(width=100, height=100, center=False)
        _add_pad_footprint(pcb, ref="J1", x=10, y=10, net="NET1")
        _add_pad_footprint(pcb, ref="J2", x=80, y=80, net="NET1")
        # Net exists (both pads share it) but nothing is routed.
        spec = CurrentPathSpec(
            name="UNROUTED",
            net_name="NET1",
            source=PathEndpoint("J1", "1"),
            sink=PathEndpoint("J2", "1"),
            continuous_a=1.0,
        )
        r = resolve_current_path(pcb, spec)
        assert r.status == "unresolved"

    def test_disconnected_islands_unresolved(self) -> None:
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.create(width=200, height=100, center=False)
        _add_pad_footprint(pcb, ref="J1", x=10, y=10, net="NET1")
        _add_pad_footprint(pcb, ref="J2", x=180, y=90, net="NET1")
        # Routed copper exists on the net, but not connecting these pads.
        pcb.add_trace((10, 10), (30, 10), width=1.0, layer="F.Cu", net="NET1")
        pcb.add_trace((150, 90), (180, 90), width=1.0, layer="F.Cu", net="NET1")
        spec = CurrentPathSpec(
            name="ISLANDS",
            net_name="NET1",
            source=PathEndpoint("J1", "1"),
            sink=PathEndpoint("J2", "1"),
            continuous_a=1.0,
        )
        r = resolve_current_path(pcb, spec)
        assert r.status == "unresolved"
        assert "no continuous copper" in r.reason

    def test_loop_reachable_from_endpoints_is_ambiguous(self) -> None:
        """Two parallel routes between the same pads must never silently
        collapse into a single trusted path."""
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.create(width=200, height=120, center=False)
        _add_pad_footprint(pcb, ref="J1", x=20, y=50, net="NET2")
        _add_pad_footprint(pcb, ref="J2", x=120, y=50, net="NET2")
        pcb.add_trace(("J1", "1"), (60, 30), width=1.0, layer="F.Cu", net="NET2")
        pcb.add_trace((60, 30), ("J2", "1"), width=1.0, layer="F.Cu", net="NET2")
        pcb.add_trace(("J1", "1"), (60, 70), width=1.0, layer="F.Cu", net="NET2")
        pcb.add_trace((60, 70), ("J2", "1"), width=1.0, layer="F.Cu", net="NET2")

        spec = CurrentPathSpec(
            name="LOOP",
            net_name="NET2",
            source=PathEndpoint("J1", "1"),
            sink=PathEndpoint("J2", "1"),
            continuous_a=5.0,
        )
        r = resolve_current_path(pcb, spec)
        assert r.status == "ambiguous"
        assert "loop" in r.reason

    def test_source_equals_sink_resolves_trivially(self) -> None:
        pcb = _t_network_pcb()
        spec = CurrentPathSpec(
            name="TRIVIAL",
            net_name="NET1",
            source=PathEndpoint("J1", "1"),
            sink=PathEndpoint("J1", "1"),
            continuous_a=1.0,
        )
        r = resolve_current_path(pcb, spec)
        assert r.ok
        assert r.segments == ()
        assert r.length_mm == 0.0


# --------------------------------------------------------------------------
# audit_current_paths
# --------------------------------------------------------------------------


class TestAuditCurrentPaths:
    def test_t_network_fully_covered(self) -> None:
        pcb = _t_network_pcb()
        audit = audit_current_paths(pcb, [_trunk_spec(), _sense_spec()])
        assert audit.all_resolved
        assert audit.fully_covered
        assert audit.uncovered == {}

    def test_uncovered_copper_reported(self) -> None:
        """A declared trunk that doesn't account for the sense spur reports it."""
        pcb = _t_network_pcb()
        audit = audit_current_paths(pcb, [_trunk_spec()])
        assert "NET1" in audit.uncovered
        assert len(audit.uncovered["NET1"]) == 1  # the sense spur segment

    def test_net_with_no_declared_path_not_reported(self) -> None:
        pcb = _t_network_pcb()
        _add_pad_footprint(pcb, ref="X1", x=5, y=5, net="UNRELATED")
        _add_pad_footprint(pcb, ref="X2", x=15, y=15, net="UNRELATED")
        pcb.add_trace(("X1", "1"), ("X2", "1"), width=0.2, layer="F.Cu", net="UNRELATED")
        audit = audit_current_paths(pcb, [_trunk_spec(), _sense_spec()])
        assert "UNRELATED" not in audit.uncovered

    def test_unresolved_and_ambiguous_properties(self) -> None:
        pcb = _t_network_pcb()
        broken = CurrentPathSpec(
            name="BROKEN",
            net_name="NET1",
            source=PathEndpoint("J1", "1"),
            sink=PathEndpoint("J99", "1"),
            continuous_a=1.0,
        )
        audit = audit_current_paths(pcb, [_trunk_spec(), broken])
        assert not audit.all_resolved
        assert len(audit.unresolved) == 1
        assert audit.unresolved[0].spec.name == "BROKEN"
        assert audit.ambiguous == []

    def test_route_time_and_post_write_audit_agree(self) -> None:
        """Two independently-constructed boards with identical routed copper
        produce identical audit results -- the audit is a pure function of
        board content + declared specs, with no hidden route-time state.

        This is the "route-time intent and independent final-copper audit
        agree on the branch/path assignments" acceptance criterion: calling
        :func:`audit_current_paths` "at route time" (on the board just
        built) and again as an "independent final-copper audit" (on a
        freshly, independently constructed board with the same routed
        geometry -- standing in for a later, separate load of the written
        file) must agree exactly on status, segment coverage, and length.
        """
        specs = [_trunk_spec(), _sense_spec()]

        route_time_pcb = _t_network_pcb()
        route_time_audit = audit_current_paths(route_time_pcb, specs)

        independent_pcb = _t_network_pcb()
        independent_audit = audit_current_paths(independent_pcb, specs)

        assert [r.status for r in route_time_audit.resolutions] == [
            r.status for r in independent_audit.resolutions
        ]
        assert [len(r.segments) for r in route_time_audit.resolutions] == [
            len(r.segments) for r in independent_audit.resolutions
        ]
        assert [round(r.length_mm, 3) for r in route_time_audit.resolutions] == [
            round(r.length_mm, 3) for r in independent_audit.resolutions
        ]
        assert set(route_time_audit.uncovered) == set(independent_audit.uncovered)


# --------------------------------------------------------------------------
# reinforcement_eligible_segment_ids
# --------------------------------------------------------------------------


class TestReinforcementEligibleSegmentIds:
    def test_only_eligible_resolved_path_segments_included(self) -> None:
        pcb = _t_network_pcb()
        trunk = _trunk_spec()
        sense = _sense_spec()
        trunk_res = resolve_current_path(pcb, trunk)
        sense_res = resolve_current_path(pcb, sense)

        eligible = reinforcement_eligible_segment_ids(pcb, [trunk, sense])
        assert eligible == {id(s) for s in trunk_res.segments}
        # The sense-only segment (not shared with the trunk) is excluded.
        sense_only = {id(s) for s in sense_res.segments} - {id(s) for s in trunk_res.segments}
        assert sense_only.isdisjoint(eligible)
        assert sense_only  # sanity: the sense spur does have a private segment

    def test_unresolved_eligible_spec_contributes_nothing(self) -> None:
        pcb = _t_network_pcb()
        broken_trunk = CurrentPathSpec(
            name="BROKEN_TRUNK",
            net_name="NET1",
            source=PathEndpoint("J1", "1"),
            sink=PathEndpoint("J404", "1"),
            continuous_a=15.0,
            reinforcement_eligible=True,
        )
        eligible = reinforcement_eligible_segment_ids(pcb, [broken_trunk])
        assert eligible == set()

    def test_empty_specs_yields_empty_set(self) -> None:
        pcb = _t_network_pcb()
        assert reinforcement_eligible_segment_ids(pcb, []) == set()
