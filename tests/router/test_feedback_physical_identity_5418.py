"""Physical feedback targets cannot alias through authored reference labels."""

from types import SimpleNamespace

import pytest

from kicad_tools.recovery import StrategyApplicator
from kicad_tools.recovery.types import Action, Difficulty, ResolutionStrategy, StrategyType
from kicad_tools.router.placement_delta import PlacementDelta, endpoint_align_deltas
from kicad_tools.router.placement_feedback import PlacementDeltaFeedbackLoop, _delta_key
from kicad_tools.router.primitives import Pad
from kicad_tools.schema.physical_identity import footprint_keys
from tests.router.test_placement_delta import _diag, _endpoint_board, _load
from tests.router.test_placement_delta_feedback import MockFootprint, MockPad, MockPCB


def _loop(ref, *, uuid=False):
    footprints = [
        MockFootprint(
            ref,
            x,
            5,
            layer="F.Cu",
            pads=[MockPad(20, (0, 0), ["F.Cu"]), MockPad(40, (1, 0), ["F.Cu"])],
        )
        for x in (5, 15)
    ]
    if uuid:
        for index, footprint in enumerate(footprints):
            footprint.uuid = f"physical-{index}"
    pcb = MockPCB(footprints)
    keys = footprint_keys(footprints)
    pads = [
        Pad(x + offset, 5, 0.5, 0.5, index + 1, "NET", ref=ref, pin="1", component_id=keys[index])
        for index, x in enumerate((5, 15))
        for offset in (0, 1)
    ]
    router = SimpleNamespace(all_pads=pads, pads={pad.key: pad for pad in pads}, router=None)
    return PlacementDeltaFeedbackLoop(router, pcb, verbose=False), keys


@pytest.mark.parametrize("ref", ["", "DUP", 'DUP\\return"pin'])
@pytest.mark.parametrize("uuid", [False, True])
def test_snapshot_restores_each_physical_footprint_and_all_pad_shapes(ref, uuid):
    loop, keys = _loop(ref, uuid=uuid)
    snapshot = loop._snapshot_placement()
    assert set(snapshot) == set(keys)
    for index, footprint in enumerate(loop.pcb.footprints):
        footprint.position = (50 + index, 60 + index)
        footprint.rotation = 90
        footprint.layer = "B.Cu"
        for pad in footprint.pads:
            pad.position = (8, 9)
            pad.rotation = 120
            pad.layers = ["B.Cu"]
    if uuid:
        loop.pcb.footprints.reverse()
    loop._restore_placement(snapshot)
    assert loop._snapshot_placement() == snapshot
    assert [fp.reference for fp in loop.pcb.footprints] == [ref, ref]


@pytest.mark.parametrize("ref", ["", "DUP"])
@pytest.mark.parametrize("kind", ["translate", "rotate_180", "rotate_align", "mirror"])
def test_physical_delta_moves_only_target_and_retains_authored_labels(ref, kind):
    loop, keys = _loop(ref)
    delta = PlacementDelta(
        "NET",
        ref,
        kind,
        dx=1,
        rotation_delta=90 if kind == "rotate_align" else 180,
        component_id=keys[0],
    )
    assert PlacementDelta.from_dict(delta.to_dict()).target_key == keys[0]
    assert delta.to_dict()["target_ref"] == ref
    other = PlacementDelta("NET", ref, kind, component_id=keys[1])
    assert _delta_key(delta) != _delta_key(other)
    loop._snapshot_positions([delta.target_key])
    pcb_before = loop._snapshot_placement()
    pads_before = loop._snapshot_router_pads()
    strategy = loop._strategy_from_delta(delta)
    result = StrategyApplicator().apply_strategy(loop.pcb, strategy)
    assert result.success
    loop._apply_delta_to_router_pads(delta)
    assert loop._snapshot_placement()[keys[1]] == pcb_before[keys[1]]
    assert [(p.x, p.y, p.layer) for p in loop.router.all_pads[2:]] == [
        (p[1], p[2], p[3]) for p in pads_before[2:]
    ]
    assert all(p.ref == ref for p in loop.router.all_pads)
    assert any((p.x, p.y, p.layer) != (x, y, layer) for p, x, y, layer in pads_before[:2])
    diff = loop._build_placement_diff()
    assert len(diff) == 1
    assert diff[0].ref == ref and diff[0].component_id == keys[0]
    loop._restore_router_pads(pads_before)
    loop._restore_placement(pcb_before)
    assert loop._snapshot_placement() == pcb_before


@pytest.mark.parametrize("ref", ["", "DUP"])
def test_ambiguous_authored_selector_rejected_and_fixed_physical_target_respected(ref):
    loop, keys = _loop(ref)
    before = loop._snapshot_placement()
    with pytest.raises(ValueError, match="Ambiguous footprint reference"):
        loop._find_footprint(ref)
    with pytest.raises(ValueError, match="Ambiguous footprint reference"):
        StrategyApplicator()._find_footprint(loop.pcb, ref)
    with pytest.raises(ValueError, match="Ambiguous footprint reference"):
        loop._apply_delta_to_router_pads(PlacementDelta("NET", ref, "translate", dx=1))
    assert loop._snapshot_placement() == before
    loop.fixed_refs = {keys[0]}
    assert loop._target_is_fixed(keys[0]) and not loop._target_is_fixed(keys[1])
    loop.fixed_refs = {ref}
    assert all(loop._target_is_fixed(key) for key in keys)


@pytest.mark.parametrize("ref", ["", "DUP"])
def test_endpoint_proposer_groups_complete_physical_footprints(tmp_path, ref):
    original = _load(tmp_path, _endpoint_board())
    diagnosis = _diag(original, "TGT")
    for footprint in original.footprints[:2]:
        footprint.reference = ref
    keys = footprint_keys(original.footprints)[:2]
    deltas = endpoint_align_deltas(original, diagnosis)
    assert {delta.target_key for delta in deltas} == set(keys)
    assert all(delta.target_ref == ref for delta in deltas)
    assert all(delta.component_id for delta in deltas)
    assert endpoint_align_deltas(original, diagnosis, fixed_refs={ref}) == []


@pytest.mark.parametrize("ref", ["", "DUP"])
@pytest.mark.parametrize("ambiguous_action", ["move", "rotate", "mirror"])
def test_strategy_rejects_late_ambiguous_selector_before_any_mutation(ref, ambiguous_action):
    loop, keys = _loop(ref)
    before = loop._snapshot_placement()
    strategy = ResolutionStrategy(
        type=StrategyType.MOVE_MULTIPLE,
        difficulty=Difficulty.MEDIUM,
        confidence=1.0,
        actions=[
            Action("move", keys[0], {"x": 8, "y": 9}),
            Action(ambiguous_action, ref, {"x": 18, "y": 19, "rotation_delta": 90}),
        ],
    )
    with pytest.raises(ValueError, match="Ambiguous footprint reference"):
        StrategyApplicator().apply_strategy(loop.pcb, strategy)
    assert loop._snapshot_placement() == before

    strategy.actions[1] = Action("move", keys[1], {"x": 18, "y": 19})
    result = StrategyApplicator().apply_strategy(loop.pcb, strategy)
    assert result.success
    assert [fp.position for fp in loop.pcb.footprints] == [(8, 9), (18, 19)]
