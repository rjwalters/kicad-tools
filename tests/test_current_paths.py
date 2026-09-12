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
    single continuous trunk segment also connects an exact same-layer
    spur endpoint touching its interior.
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


def _seg_len(seg) -> float:
    import math

    return math.dist(seg.start, seg.end)


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


class TestPadExtentEndpointBinding:
    """Endpoints attach by pad *extent*, not by an exact center hit (#4980).

    A router may terminate a trace anywhere inside a pad's copper, and a
    wide power pad is routinely entered by several stubs at once. Resolving
    only on an exact center match reported such boards as ``unresolved`` --
    a FALSE fail-closed, which teaches users to delete declarations and is
    every bit as unsafe as the silent pass this model exists to prevent.
    Regression fixture is board09's 2.29 x 2.03 mm shunt pad ``RSH1.4``,
    entered by three stubs 0.015 / 0.785 / 0.815 mm off center.
    """

    @staticmethod
    def _wide_pad_pcb(*, offsets: tuple[float, ...], pad_size=(2.29, 2.03)):
        """J1 -> J2, where the J1 end fans into ``offsets`` stubs on its pad."""
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.create(width=200, height=120, center=False)
        _add_pad_footprint(pcb, ref="J1", x=20, y=50, net="NET1")
        _add_pad_footprint(pcb, ref="J2", x=120, y=50, net="NET1")
        j1 = pcb.get_footprint("J1")
        assert j1 is not None
        j1.pads[0].shape = "roundrect"
        j1.pads[0].size = pad_size

        # Each stub starts ON the pad (dx from its center) and drops to the
        # y=60 rail, which then runs to J2. The pad's own copper is what
        # joins the stubs -- there is no trace between them.
        for dx in offsets:
            pcb.add_trace((20 + dx, 50), (20 + dx, 60), width=0.6, layer="F.Cu", net="NET1")
        for i in range(len(offsets) - 1):
            pcb.add_trace(
                (20 + offsets[i], 60),
                (20 + offsets[i + 1], 60),
                width=2.0,
                layer="F.Cu",
                net="NET1",
            )
        pcb.add_trace((20 + offsets[-1], 60), ("J2", "1"), width=2.0, layer="F.Cu", net="NET1")
        return pcb

    def test_offcenter_stub_on_pad_resolves(self) -> None:
        """A single stub 0.015 mm off center is on the pad, not unresolved."""
        pcb = self._wide_pad_pcb(offsets=(0.015,))
        r = resolve_current_path(pcb, _trunk_spec())
        assert r.status == "resolved", r.reason

    def test_stub_outside_pad_still_fails_closed(self) -> None:
        """The fix must not degrade into "near enough" -- 5 mm away is not on it."""
        pcb = self._wide_pad_pcb(offsets=(5.0,))
        r = resolve_current_path(pcb, _trunk_spec())
        assert r.status == "unresolved"
        assert "no routed copper touching it" in r.reason

    def test_pad_edge_is_inside_and_just_beyond_is_not(self) -> None:
        """Containment is the pad's real extent: half-width 1.145 mm here."""
        assert resolve_current_path(self._wide_pad_pcb(offsets=(1.14,)), _trunk_spec()).ok
        assert not resolve_current_path(self._wide_pad_pcb(offsets=(1.16,)), _trunk_spec()).ok

    def test_multiple_stubs_on_one_pad_are_shorted_by_it(self) -> None:
        """board09's case: three stubs into one shunt pad, none at its center.

        Only the pad connects them, so without pad-extent binding the trunk
        is split across three graph components and never resolves.
        """
        pcb = self._wide_pad_pcb(offsets=(-0.785, 0.015, 0.815))
        r = resolve_current_path(pcb, _trunk_spec())
        # The three stubs plus the y=60 rail form a genuine parallel loop
        # through the pad, which the model reports rather than collapsing.
        assert r.status == "ambiguous", r.reason
        assert "loop" in r.reason

    def test_pad_short_never_appears_as_a_covered_segment(self) -> None:
        """Pad shorts are pseudo-edges: nothing can check their width."""
        pcb = self._wide_pad_pcb(offsets=(0.015,))
        r = resolve_current_path(pcb, _trunk_spec())
        assert r.ok
        assert all(hasattr(s, "width") for s in r.segments)
        assert r.length_mm == pytest.approx(sum(_seg_len(s) for s in r.segments))

    def test_rotated_pad_extent_follows_the_pad(self) -> None:
        """A 90-degree pad rotation swaps which offsets land on the copper."""
        # 0.9 mm off center along x: inside a 2.29-wide pad, outside the
        # 2.03-tall one once that extent is rotated onto the x axis.
        upright = self._wide_pad_pcb(offsets=(1.1,))
        assert resolve_current_path(upright, _trunk_spec()).ok

        rotated = self._wide_pad_pcb(offsets=(1.1,))
        fp = rotated.get_footprint("J1")
        assert fp is not None
        fp.pads[0].size = (2.29, 1.0)
        fp.pads[0].rotation = 90.0
        r = resolve_current_path(rotated, _trunk_spec())
        assert r.status == "unresolved", "the 1.0 mm-wide axis now faces x"


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


class TestPhysicalLayerGraph:
    @pytest.mark.parametrize("bridge", ["missing", "wrong-net", "blind", "malformed", "valid"])
    def test_cross_layer_t_requires_real_bridge(self, bridge):
        pcb = _t_network_pcb()
        pcb.segments[1].layer = "B.Cu"
        if bridge != "missing":
            via = pcb.add_via(60, 50, net="OTHER" if bridge == "wrong-net" else "NET1")
            if bridge == "blind":
                via.layers = ["F.Cu", "In1.Cu"]
            if bridge == "malformed":
                via.layers = ["F.Cu", "garbage", "B.Cu"]
        resolution = resolve_current_path(pcb, _trunk_spec())
        assert resolution.status == ("resolved" if bridge == "valid" else "unresolved")
        if bridge == "valid":
            assert {id(s) for s in resolution.segments} == {id(s) for s in pcb.segments[:2]}
            assert resolution.length_mm == pytest.approx(100)
        else:
            assert not reinforcement_eligible_segment_ids(pcb, [_trunk_spec()])

    @staticmethod
    def _interior_pcb(*, split=False, reverse=False, receiving_layer="B.Cu"):
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.create(layers=4)
        _add_pad_footprint(pcb, ref="J1", x=20, y=50, net="NET1")
        _add_pad_footprint(pcb, ref="J2", x=120, y=50, net="NET1")
        pcb.add_trace((20, 50), (60, 50), width=3, layer="F.Cu", net="NET1")
        pieces = [((40, 50), (60, 50)), ((60, 50), (120, 50))] if split else [((40, 50), (120, 50))]
        for start, end in pieces:
            pcb.add_trace(start, end, width=3, layer=receiving_layer, net="NET1")
        pcb.add_via(60, 50, net="NET1")
        if reverse:
            pcb._segments.reverse()
            for seg in pcb.segments:
                seg.start, seg.end = seg.end, seg.start
        return pcb

    @pytest.mark.parametrize("split", [False, True])
    @pytest.mark.parametrize("reverse", [False, True])
    def test_interior_via_original_evidence(self, split, reverse):
        pcb = self._interior_pcb(split=split, reverse=reverse)
        original_ids = {id(seg) for seg in pcb.segments}
        result = resolve_current_path(pcb, _trunk_spec())
        assert result.ok
        expected = [
            s
            for s in pcb.segments
            if not (split and max(s.start[0], s.end[0]) == 60 and s.layer == "B.Cu")
        ]
        assert {id(s) for s in result.segments} == {id(s) for s in expected}
        assert result.length_mm == pytest.approx(sum(_seg_len(s) for s in expected))
        assert reinforcement_eligible_segment_ids(pcb, [_trunk_spec()]) == {id(s) for s in expected}
        audit = audit_current_paths(pcb, [_trunk_spec()])
        assert {id(s) for s in audit.uncovered.get("NET1", [])} == original_ids - {
            id(s) for s in expected
        }

    @pytest.mark.parametrize(
        "receiving_layer,span,expected",
        [
            ("In1.Cu", ["F.Cu", "B.Cu"], "resolved"),
            ("In1.Cu", ["F.Cu", "In1.Cu"], "resolved"),
            ("B.Cu", ["F.Cu", "In1.Cu"], "unresolved"),
            ("In1.Cu", ["In2.Cu", "B.Cu"], "unresolved"),
        ],
    )
    def test_interior_via_span(self, receiving_layer, span, expected):
        pcb = self._interior_pcb(receiving_layer=receiving_layer)
        pcb.vias[0].layers = span
        assert resolve_current_path(pcb, _trunk_spec()).status == expected

    def test_split_edges_count_as_distinct_cycle_edges(self):
        pcb = self._interior_pcb()
        # Three via contacts split the SAME B.Cu segment. Counting original
        # segment IDs undercounts graph edges and misses this physical cycle.
        pcb.add_trace((60, 50), (100, 50), width=3, layer="F.Cu", net="NET1")
        pcb.add_via(80, 50, net="NET1")
        pcb.add_via(100, 50, net="NET1")
        assert resolve_current_path(pcb, _trunk_spec()).status == "ambiguous"
        pcb._segments.reverse()
        assert resolve_current_path(pcb, _trunk_spec()).status == "ambiguous"

    def test_unused_vias_split_evidence_without_duplicate_accounting(self):
        pcb = _t_network_pcb()
        for x in (30, 40, 50):
            pcb.add_via(x, 50, net="NET1")
        result = resolve_current_path(pcb, _trunk_spec())
        assert result.ok
        assert len(result.segments) == 2
        assert result.length_mm == pytest.approx(100)
        assert audit_current_paths(pcb, [_trunk_spec(), _sense_spec()]).fully_covered

    def test_interior_via_nearby_is_not_contact(self):
        pcb = self._interior_pcb()
        pcb.vias[0].position = (60, 50.01)
        assert not resolve_current_path(pcb, _trunk_spec()).ok

    @pytest.mark.parametrize(
        "pad_type,layers,expected",
        [
            ("thru_hole", ["*.Cu"], "resolved"),
            ("smd", ["F.Cu"], "unresolved"),
            ("np_thru_hole", ["*.Cu"], "unresolved"),
        ],
    )
    def test_actual_intermediate_pad_copper_bridges_layers(self, pad_type, layers, expected):
        pcb = _t_network_pcb()
        pcb.segments[1].layer = "B.Cu"
        _add_pad_footprint(pcb, ref="TP1", x=60, y=50, net="NET1")
        pad = pcb.get_footprint("TP1").pads[0]
        pad.type, pad.layers = pad_type, layers
        assert resolve_current_path(pcb, _trunk_spec()).status == expected

    def test_distinct_pads_at_same_xy_do_not_make_self_path(self):
        pcb = _t_network_pcb()
        _add_pad_footprint(pcb, ref="BACK", x=20, y=50, net="NET1")
        for ref, layer in (("J1", "F.Cu"), ("BACK", "B.Cu")):
            pad = pcb.get_footprint(ref).pads[0]
            pad.type, pad.layers = "smd", [layer]
        pcb.add_trace((20, 50), (20, 40), width=1, layer="B.Cu", net="NET1")
        spec = CurrentPathSpec(
            name="opposite",
            net_name="NET1",
            source=PathEndpoint("J1", "1"),
            sink=PathEndpoint("BACK", "1"),
            continuous_a=1,
        )
        assert resolve_current_path(pcb, spec).status == "unresolved"

    def test_pad_internal_star_does_not_manufacture_cycle(self):
        pcb = _t_network_pcb()
        # Both endpoints are within J1's copper. The pad already shorts
        # these points, so this internal stub is not a physical return path.
        pcb.add_trace((20, 50), (20.2, 50), width=1, layer="F.Cu", net="NET1")
        pcb.add_trace((20.2, 50), (20.2, 40), width=1, layer="F.Cu", net="NET1")
        result = resolve_current_path(pcb, _trunk_spec())
        assert result.ok
        assert len({id(s) for s in result.segments}) == len(result.segments)
        # Rejoining outside the pad is a real parallel route and stays ambiguous.
        pcb.add_trace((20.2, 40), (60, 50), width=1, layer="F.Cu", net="NET1")
        assert resolve_current_path(pcb, _trunk_spec()).status == "ambiguous"

    @pytest.mark.parametrize("shape", ["custom", "trapezoid"])
    def test_unsupported_pad_shapes_fail_closed(self, shape):
        pcb = _t_network_pcb()
        pcb.get_footprint("J1").pads[0].shape = shape
        assert resolve_current_path(pcb, _trunk_spec()).status == "unresolved"

    def test_roundrect_clipped_corner_not_copper(self):
        pcb = _t_network_pcb()
        pad = pcb.get_footprint("J1").pads[0]
        pad.shape, pad.size, pad.roundrect_rratio = "roundrect", (2, 2), 0.5
        pcb.segments[0].start = (20.95, 50.95)
        assert not resolve_current_path(pcb, _trunk_spec()).ok

    def test_duplicate_physical_pad_number_is_not_guessed(self):
        from copy import deepcopy

        pcb = _t_network_pcb()
        fp = pcb.get_footprint("J1")
        fp.pads.append(deepcopy(fp.pads[0]))
        assert resolve_current_path(pcb, _trunk_spec()).status == "unresolved"

    def test_board09_via_array_resolves_and_is_read_only(self):
        """Issue #5197: a benign parallel via array must not force 'ambiguous'.

        ``RSH1.4`` fans into three parallel vias reunited by a wide ``B.Cu``
        trace -- standard high-current practice, not an alternate operating
        mode. Before #5197 this made the WHOLE net ambiguous, including
        unrelated low-current taps that never touch the array.
        """
        from pathlib import Path

        from kicad_tools.schema.pcb import PCB

        board = (
            Path(__file__).resolve().parents[1]
            / "boards/09-usbc-pd-power/output/usbc_pd_power.kicad_pcb"
        )
        before = board.read_bytes()
        pcb = PCB.load(board)
        trunk = CurrentPathSpec(
            name="output",
            net_name="+5V_OUT",
            source=PathEndpoint("RSH1", "4"),
            sink=PathEndpoint("J2", "1"),
            continuous_a=3,
        )
        result = resolve_current_path(pcb, trunk)
        assert result.status == "resolved"
        assert result.segments
        assert result.length_mm > 0.0

        # Unrelated low-current taps sharing the same array-tainted source
        # pad must resolve too -- the array is not "the whole net".
        for sink_ref, sink_pad in (("R16", "1"), ("U3", "8")):
            tap = CurrentPathSpec(
                name=f"tap-{sink_ref}",
                net_name="+5V_OUT",
                source=PathEndpoint("RSH1", "4"),
                sink=PathEndpoint(sink_ref, sink_pad),
                continuous_a=0.02,
            )
            assert resolve_current_path(pcb, tap).status == "resolved"

        assert board.read_bytes() == before

    def test_transitive_pad_union_does_not_erase_external_return(self):
        pcb = _t_network_pcb()
        pcb.get_footprint("J1").pads[0].size = (1.6, 1.6)
        for ref, x, y in (("TP1", 21, 51), ("TP2", 22, 50)):
            _add_pad_footprint(pcb, ref=ref, x=x, y=y, net="NET1")
            pcb.get_footprint(ref).pads[0].size = (1.6, 1.6)
        for start, end in [
            ((20, 50), (20.5, 50.5)),
            ((20.5, 50.5), (21.5, 50.5)),
            ((21.5, 50.5), (22, 50)),
        ]:
            pcb.add_trace(start, end, width=0.1, layer="F.Cu", net="NET1")
        # The existing trunk crosses outside the joined pad copper at (21,50).
        # Its endpoints collapse transitively but it must remain a self-loop.
        assert resolve_current_path(pcb, _trunk_spec()).status == "ambiguous"

    def test_disabled_copper_layer_is_not_bridged_by_wildcard_pad(self):
        pcb = _t_network_pcb()
        pcb.segments[0].layer = "In1.Cu"  # absent on this two-layer board
        pcb.segments[1].layer = "In1.Cu"
        assert resolve_current_path(pcb, _trunk_spec()).status == "unresolved"

    @pytest.mark.parametrize("layer,expected", [("F.Cu", "ambiguous"), ("B.Cu", "resolved")])
    def test_interior_crossing_cannot_hide_parallel_route(self, layer, expected):
        pcb = _t_network_pcb()
        # This return crosses the trunk interior at (40,50). The source
        # through-hole pad connects either layer, but only F.Cu rejoins it.
        pcb.add_trace((20, 50), (40, 40), width=1, layer=layer, net="NET1")
        pcb.add_trace((40, 40), (40, 60), width=1, layer=layer, net="NET1")
        assert resolve_current_path(pcb, _trunk_spec()).status == expected

    @pytest.mark.parametrize("split_inside_pad", [False, True])
    def test_pad_internal_piece_does_not_cover_external_spur(self, split_inside_pad):
        pcb = _t_network_pcb()
        pcb.add_trace((20, 50), (20, 40), width=0.1, layer="F.Cu", net="NET1")
        spur = pcb.segments[-1]
        if split_inside_pad:
            pcb.add_via(20, 49.8, net="NET1")
        result = resolve_current_path(pcb, _trunk_spec())
        assert result.ok
        assert result.length_mm == pytest.approx(100)
        assert {id(s) for s in result.segments} == {id(s) for s in pcb.segments[:2]}
        audit = audit_current_paths(pcb, [_trunk_spec(), _sense_spec()])
        assert [id(s) for s in audit.uncovered["NET1"]] == [id(spur)]
        assert id(spur) not in reinforcement_eligible_segment_ids(pcb, [_trunk_spec()])
