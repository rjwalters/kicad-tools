"""Mid-path via groups, through-hole pairs and partial reinforcement (Issue #9725).

A tripled layer change -- three same-net vias in a row joined by a bar on
both layers -- used to read as a loop whenever a bar was a sub-span of a
longer track (the branch running on over the row, or the lane passing under
it): the parallel-group test judges whole original segments, so the bars
never qualified and the whole net was ``ambiguous``. These fixtures pin:

* the group resolves as ONE layer change whose capacity is the sum of its
  barrels, under the pitch / both-bar contact rule, and nothing looser;
* a through-hole pair drawn on both outer layers resolves as one
  ``ParallelGroup`` only along the same route;
* a genuine loop stays ambiguous, its witness naming both branches, and
  reinforcement anchors only the loop-free lane between the endpoints.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from kicad_tools.physics.ampacity import via_barrel_current_capacity
from kicad_tools.router.current_paths import (
    CurrentPathSpec,
    PathEndpoint,
    describe_witness,
    reinforcement_eligible_segment_ids,
    reinforcement_path_resolutions,
    resolve_current_path,
)
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.rules.path_ampacity import PathAmpacityRule
from tests.test_current_paths import _add_pad_footprint
from tests.test_path_ampacity_check import _design_rules_2oz

DRILL = 1.4
PAD = 2.2


def _spec(current_a: float = 3.0, *, source: str = "J1", sink: str = "J2") -> CurrentPathSpec:
    return CurrentPathSpec(
        name="force",
        net_name="P",
        source=PathEndpoint(source, "1"),
        sink=PathEndpoint(sink, "1"),
        continuous_a=current_a,
        reinforcement_eligible=True,
    )


def tripled(
    *,
    pitch: float = 2.4,
    count: int = 3,
    layers: int = 4,
    lane_under: bool = False,
) -> PCB:
    """A top-layer branch changing layer through ``count`` barrels in a row.

    The branch (F.Cu, 3 mm) runs from J1 straight over the whole row and ends
    on the far barrel, so its tail IS the top bar. A separate bottom bar of
    the branch's width spans the row, and a long bottom lane leaves from the
    far barrel to J2. With ``lane_under`` the bottom lane instead passes
    under the whole row from a second feeder J3, so the bottom bar is a
    sub-span of the lane.
    """
    pcb = PCB.create(layers=layers)
    _add_pad_footprint(pcb, ref="J1", x=30, y=10, net="P")
    ys = [40 + i * pitch for i in range(count)]
    far = ys[-1]
    _add_pad_footprint(pcb, ref="J2", x=200, y=far, net="P")
    for y in ys:
        pcb.add_via(30, y, size=PAD, drill=DRILL, net="P")
    pcb.add_trace((30, 10), (30, far), width=3, layer="F.Cu", net="P")
    if lane_under:
        _add_pad_footprint(pcb, ref="J3", x=30, y=far + 60, net="P")
        pcb.add_trace((30, far + 60), (30, ys[0]), width=3, layer="B.Cu", net="P")
        pcb.add_trace((30, far), (200, far), width=4, layer="B.Cu", net="P")
    else:
        pcb.add_trace((30, ys[0]), (30, far), width=3, layer="B.Cu", net="P")
        pcb.add_trace((30, far), (200, far), width=4, layer="B.Cu", net="P")
    return pcb


# --------------------------------------------------------------- AC1 / AC5


@pytest.mark.parametrize("layers", [2, 4])
@pytest.mark.parametrize("reverse", [False, True])
def test_tripled_layer_change_resolves_as_one_summed_change(layers, reverse):
    """(a) 3 vias, 1.4 mm drill, 2.2 mm pad, 2.4 mm pitch: one change, 3 barrels."""
    pcb = tripled(layers=layers)
    spec = _spec(source="J2", sink="J1") if reverse else _spec()
    result = resolve_current_path(pcb, spec)
    assert result.ok, result.reason + describe_witness(result.witness)
    assert len(result.layer_changes) == 1
    (change,) = result.layer_changes
    assert change.count == 3
    assert {change.from_layer, change.to_layer} == {"F.Cu", "B.Cu"}
    assert change.current_capacity_a() == pytest.approx(3 * via_barrel_current_capacity(DRILL))
    # It is a real series layer change: the ampacity check judges it.
    assert result.series_layer_changes == result.layer_changes
    # Every bar and lane segment is evidence checked at the full current.
    assert {id(s) for s in result.segments} == {id(s) for s in pcb.segments}


def test_summed_capacity_clears_a_current_one_barrel_cannot():
    one = via_barrel_current_capacity(DRILL)
    spec = _spec(current_a=2 * one)  # above one barrel, below three
    result = PathAmpacityRule([spec]).check(tripled(), _design_rules_2oz())
    assert not [v for v in result.violations if "changes layer" in v.message]


def test_two_barrels_too_few_for_the_current_still_fail_the_check():
    one = via_barrel_current_capacity(DRILL)
    spec = _spec(current_a=2.5 * one)
    result = PathAmpacityRule([spec]).check(tripled(count=2), _design_rules_2oz())
    found = [v for v in result.violations if "changes layer" in v.message]
    assert len(found) == 1
    assert "through 2 via(s)" in found[0].message
    assert found[0].actual_value == pytest.approx(2 * one)


@pytest.mark.parametrize("exit_y", [42.4, 44.8])
def test_inner_layer_exit_cannot_claim_outer_bar_capacity(exit_y):
    pcb = tripled()
    # Keep the bottom lane as an exit from the proved pair, but route the
    # declared path to a separate sink through just one inner-layer ring.
    _add_pad_footprint(pcb, ref="J3", x=80, y=exit_y, net="P")
    pcb.add_trace((30, exit_y), (80, exit_y), width=2, layer="In1.Cu", net="P")
    spec = _spec(current_a=2.5 * via_barrel_current_capacity(DRILL), sink="J3")
    resolution = resolve_current_path(pcb, spec)
    assert resolution.status == "ambiguous"
    assert not resolution.layer_changes
    findings = PathAmpacityRule([spec]).check(pcb, _design_rules_2oz())
    assert any(v.severity == "error" and "is ambiguous" in v.message for v in findings.violations)


def test_offset_inner_layer_track_touching_a_ring_rejects_group():
    pcb = tripled()
    pcb.add_trace((31, 42.4), (80, 42.4), width=0.4, layer="In1.Cu", net="P")
    assert resolve_current_path(pcb, _spec()).status == "ambiguous"


def test_two_vias_only_resolve_with_two_barrels():
    """(d) two vias."""
    result = resolve_current_path(tripled(count=2), _spec())
    assert result.ok, result.reason
    (change,) = result.layer_changes
    assert change.count == 2


def test_lane_passing_under_the_row_resolves():
    """The bottom bar is a sub-span of a lane fed from both sides."""
    pcb = tripled(lane_under=True)
    result = resolve_current_path(pcb, _spec())
    assert result.ok, result.reason + describe_witness(result.witness)
    (change,) = result.layer_changes
    assert change.count == 3


@pytest.mark.parametrize("pitch,expected", [(4.4, "resolved"), (4.5, "ambiguous")])
def test_pitch_bound_is_two_pad_diameters(pitch, expected):
    assert resolve_current_path(tripled(pitch=pitch), _spec()).status == expected


def test_pitch_just_outside_stays_ambiguous_with_witness():
    """(b) 5.0 mm pitch is beyond 2 x 2.2 mm: not one structure."""
    pcb = tripled(pitch=5.0)
    result = resolve_current_path(pcb, _spec())
    assert result.status == "ambiguous"
    assert result.witness
    # Only the bottom lane, which every route crosses, stays anchorable: the
    # branch (partly over the loop) and the bottom bar (in it) do not.
    lane = pcb.segments[-1]
    assert (lane.start, lane.end) == ((30, 50.0), (200, 50.0))
    assert reinforcement_eligible_segment_ids(pcb, [_spec()]) == {id(lane)}


def test_barrel_missing_a_bar_contact_stays_ambiguous():
    """(c) the middle barrel misses the bottom bar: no group is proved."""
    pcb = PCB.create(layers=4)
    _add_pad_footprint(pcb, ref="J1", x=30, y=10, net="P")
    _add_pad_footprint(pcb, ref="J2", x=200, y=44, net="P")
    for y in (40, 42, 44):
        pcb.add_via(30, y, size=PAD, drill=DRILL, net="P")
    pcb.add_trace((30, 10), (30, 44), width=3, layer="F.Cu", net="P")
    # Bottom bar jogs round the middle barrel: it joins the outer two only.
    pcb.add_trace((30, 40), (34, 40), width=1, layer="B.Cu", net="P")
    pcb.add_trace((34, 40), (34, 44), width=1, layer="B.Cu", net="P")
    pcb.add_trace((34, 44), (30, 44), width=1, layer="B.Cu", net="P")
    pcb.add_trace((30, 44), (200, 44), width=4, layer="B.Cu", net="P")
    result = resolve_current_path(pcb, _spec())
    assert result.status == "ambiguous"


def test_unmodeled_via_pad_on_the_bar_rejects_the_group():
    """A same-net barrel whose pad touches the bar without a modeled contact."""
    pcb = tripled(count=2, pitch=4.0)
    pcb.add_via(30.5, 42, size=PAD, drill=DRILL, net="P")  # off both centrelines
    result = resolve_current_path(pcb, _spec())
    assert not result.ok


def test_bars_on_both_layers_continuing_are_a_doubled_run_not_a_change():
    """Both layers run on past both ends: stitching, never a credited change."""
    pcb = PCB.create(layers=2)
    _add_pad_footprint(pcb, ref="J1", x=10, y=50, net="P")
    _add_pad_footprint(pcb, ref="J2", x=100, y=50, net="P")
    for x in (40, 42.4, 44.8):
        pcb.add_via(x, 50, size=PAD, drill=DRILL, net="P")
    pcb.add_trace((10, 50), (100, 50), width=2, layer="F.Cu", net="P")
    pcb.add_trace((10, 50), (100, 50), width=2, layer="B.Cu", net="P")
    result = resolve_current_path(pcb, _spec())
    # Coincident doubled copper resolves as a parallel group, as before, and
    # no via group is credited as a layer change.
    assert result.ok, result.reason
    assert result.parallel_groups
    assert not result.series_layer_changes


def _with_loop(pcb: PCB) -> PCB:
    """Add a genuine same-layer loop on the lane near J2."""
    pcb.add_trace((150, 44.8), (160, 54.8), width=0.5, layer="B.Cu", net="P")
    pcb.add_trace((160, 54.8), (170, 44.8), width=0.5, layer="B.Cu", net="P")
    return pcb


def test_genuine_loop_elsewhere_stays_ambiguous_and_names_both_branches():
    """(e) the via group is fine; the detour off the lane is a real loop."""
    pcb = _with_loop(tripled())
    result = resolve_current_path(pcb, _spec())
    assert result.status == "ambiguous"
    branches = result.witness["loop_branches"]
    assert len(branches) == 2
    text = describe_witness(result.witness)
    assert "[loop branches: " in text
    # One branch is the lane span, the other the two-segment detour.
    spans = sorted(b["segments"] for b in branches)
    assert spans == [1, 2]
    for branch in branches:
        assert sorted([tuple(branch["start"]), tuple(branch["end"])]) == [
            (150.0, 44.8),
            (170.0, 44.8),
        ]


def test_loop_at_a_declared_endpoint_splits_at_that_pad():
    """The endpoint pad is a junction of the loop even with only two arms."""
    result = resolve_current_path(_lane_with_loop_and_spur(), _spec())
    branches = result.witness["loop_branches"]
    assert sorted(b["segments"] for b in branches) == [1, 3]
    for branch in branches:
        assert branch["layers"] == ["F.Cu"]
        assert {tuple(branch["start"]), tuple(branch["end"])} == {(10.0, 50.0), (30.0, 50.0)}
    assert branches[0]["start"] == branches[1]["start"]


def test_loop_branches_take_the_origin():
    pcb = _with_loop(tripled())
    witness = resolve_current_path(pcb, _spec()).witness
    shifted = describe_witness(witness, (100.0, 50.0))
    assert "(250.000, 94.800)" in shifted
    assert "(270.000, 94.800)" in shifted


# ----------------------------------------------------------------------- AC2


def _th_pair(*, detour: float = 0.0) -> PCB:
    pcb = PCB.create(layers=2)
    _add_pad_footprint(pcb, ref="J1", x=10, y=50, net="P")
    _add_pad_footprint(pcb, ref="J2", x=40, y=50, net="P")
    pcb.add_trace((10, 50), (40, 50), width=2, layer="F.Cu", net="P")
    if detour:
        pcb.add_trace((10, 50), (10, 50 + detour), width=2, layer="B.Cu", net="P")
        pcb.add_trace((10, 50 + detour), (40, 50 + detour), width=2, layer="B.Cu", net="P")
        pcb.add_trace((40, 50 + detour), (40, 50), width=2, layer="B.Cu", net="P")
    else:
        pcb.add_trace((10, 50), (40, 50), width=2, layer="B.Cu", net="P")
    return pcb


def test_through_hole_pair_on_both_layers_is_one_parallel_group():
    result = resolve_current_path(_th_pair(), _spec())
    assert result.ok, result.reason
    assert len(result.parallel_groups) == 1
    assert len(result.parallel_groups[0].branches) == 2


def test_through_hole_pair_with_a_detour_stays_ambiguous():
    """A detour of more than half the narrowest width is not credited."""
    result = resolve_current_path(_th_pair(detour=1.5), _spec())
    assert result.status == "ambiguous"
    assert not result.parallel_groups


# ----------------------------------------------------------------------- AC3


def _lane_with_loop_and_spur() -> PCB:
    """J1 -> loop -> straight two-segment lane -> J2, plus an off-path spur.

    The loop is a same-layer detour beside the first 20 mm (never a
    qualifying group); the spur leaves the lane at x=100 for a test point.
    """
    pcb = PCB.create(layers=2)
    _add_pad_footprint(pcb, ref="J1", x=10, y=50, net="P")
    _add_pad_footprint(pcb, ref="J2", x=200, y=50, net="P")
    _add_pad_footprint(pcb, ref="TP1", x=100, y=80, net="P")
    pcb.add_trace((10, 50), (30, 50), width=3, layer="F.Cu", net="P")  # loop a
    pcb.add_trace((10, 50), (10, 58), width=3, layer="F.Cu", net="P")  # loop b
    pcb.add_trace((10, 58), (30, 58), width=3, layer="F.Cu", net="P")
    pcb.add_trace((30, 58), (30, 50), width=3, layer="F.Cu", net="P")
    pcb.add_trace((30, 50), (100, 50), width=3, layer="F.Cu", net="P")  # lane 1
    pcb.add_trace((100, 50), (200, 50), width=3, layer="F.Cu", net="P")  # lane 2
    pcb.add_trace((100, 50), (100, 80), width=1, layer="F.Cu", net="P")  # spur
    pcb.add_trace((100, 80), (110, 90), width=1, layer="F.Cu", net="P")  # spur tail
    return pcb


@pytest.mark.parametrize("reverse", [False, True])
def test_partial_reinforcement_takes_only_the_loop_free_lane(reverse):
    pcb = _lane_with_loop_and_spur()
    spec = _spec(source="J2", sink="J1") if reverse else _spec()
    lane = {id(pcb.segments[4]), id(pcb.segments[5])}

    result = resolve_current_path(pcb, spec)
    assert result.status == "ambiguous"
    assert {id(s) for s in result.loop_free_segments} == lane
    assert result.segments == ()  # never a resolved path

    partition = reinforcement_path_resolutions(pcb, [spec])
    assert partition.eligible == ()
    assert [r.spec.name for r in partition.unresolved] == ["force"]
    assert [r.spec.name for r in partition.partial] == ["force"]
    assert reinforcement_eligible_segment_ids(pcb, [spec]) == lane


def test_partial_reinforcement_respects_declared_ineligibility():
    pcb = _lane_with_loop_and_spur()
    spec = replace(_spec(), reinforcement_eligible=False)
    assert reinforcement_path_resolutions(pcb, [spec]).partial == ()
    assert not reinforcement_eligible_segment_ids(pcb, [spec])


def test_unmodeled_copper_withholds_partial_reinforcement():
    """A same-net pour may bypass any 'bridge': nothing is loop-free then."""
    pcb = _lane_with_loop_and_spur()
    from kicad_tools.schema.pcb import Zone

    net = pcb.get_net_by_name("P")
    pcb._zones.append(
        Zone(net.number, "P", "F.Cu", polygon=[(50, 40), (150, 40), (150, 45), (50, 45)])
    )
    result = resolve_current_path(pcb, _spec())
    assert result.status == "ambiguous"
    assert result.loop_free_segments == ()
    assert not reinforcement_eligible_segment_ids(pcb, [_spec()])


def test_reinforce_ends_anchors_the_lane_and_reports_the_loop():
    from kicad_tools.pcb.reinforce import reinforce_net

    pcb = _lane_with_loop_and_spur()
    outcome = reinforce_net(pcb, "P", current_paths=[_spec()], dry_run=True, anchor_mode="ends")
    (unresolved,) = outcome.unresolved_paths
    assert unresolved.status == "ambiguous"
    assert unresolved.anchored_segments == 2
    anchored = [leg for leg in outcome.legs if leg.anchors]
    assert anchored, [(leg.status, leg.reason) for leg in outcome.legs]
    # Only the lane (y = 50, x from 30 to 200) receives anchors: never the
    # loop beside J1 (x <= 30) nor the spur to TP1.
    for leg in anchored:
        for anchor in leg.anchors:
            assert anchor.y == pytest.approx(50.0)
            assert 30.0 <= anchor.x <= 200.0


def test_cli_audit_quotes_loop_branches_in_the_sheet_frame(tmp_path, capsys):
    import argparse
    import json

    from kicad_tools.cli.commands.pcb import run_pcb_command
    from tests.test_witness_frame_8928 import _loop_board

    pcb_path, sidecar = _loop_board(tmp_path)
    args = argparse.Namespace(
        pcb_command="current-paths-audit",
        pcb=str(pcb_path),
        current_paths=str(sidecar),
        format="json",
    )
    assert run_pcb_command(args) == 0
    (res,) = json.loads(capsys.readouterr().out)["resolutions"]
    branches = res["witness"]["loop_branches"]
    assert len(branches) == 2
    # Board-relative junctions (50, 50) and (70, 50), shifted by (48.5, 55).
    for b in branches:
        assert sorted([tuple(b["start"]), tuple(b["end"])]) == [(98.5, 105.0), (118.5, 105.0)]
    assert "[loop branches: " in res["reason"]


def test_cli_reinforce_json_reports_partial_anchoring(tmp_path, capsys):
    import json

    from kicad_tools.cli.commands.pcb import run_pcb_command
    from tests.test_pcb_reinforce import _reinforce_args
    from tests.test_witness_frame_8928 import _loop_board

    pcb_path, sidecar = _loop_board(tmp_path)
    rc = run_pcb_command(
        _reinforce_args(pcb_path, current_paths=str(sidecar), format="json", dry_run=True)
    )
    assert rc == 0
    captured = capsys.readouterr()
    (entry,) = json.loads(captured.out)["unresolved_paths"]
    # The lane J1 -> J2 is ONE segment the loop rides for 20 mm: no part of
    # it is loop-free, so the whole path still gets no anchors.
    assert entry["anchored_segments"] == 0
    assert "it gets no anchors" in captured.err
