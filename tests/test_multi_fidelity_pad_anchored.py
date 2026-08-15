"""Pad-anchored wirelength at multi-fidelity level 1 (issue #4831, M2).

Fidelity >= 1 already *requires* :class:`PlacedComponent` inputs carrying
transformed pads (it needs them for the pad-to-pad DRC check), then threw them
away for the wirelength term by projecting every part down to its centre. M2
stops that information loss: level 1 measures HPWL at the pads, level 0 keeps
the historical centre-anchored estimate.

Unlike M1 (``kct optimize-placement --pad-anchored-wirelength``) this is
unconditional -- there is no opt-in flag -- because the multi-fidelity module
is library/test surface: nothing under ``src/`` calls
``evaluate_placement_multifidelity``. See
``docs/placement-pad-anchoring-audit.md`` §6 M2.

These tests pin down:

* level 1 measures pads, level 0 measures centres, on the *same* input;
* rotation -- invisible to a centre-anchored HPWL -- becomes visible at level 1;
* only the wirelength term changes (overlap/boundary/area/DRC are untouched);
* ``Net.weight`` survives (``compute_hpwl`` would have dropped it);
* a pin with no pad falls back to its component centre rather than dropping;
* levels 2/3 inherit the level-1 term, and every level stays deterministic.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kicad_tools.placement.cost import (
    BoardOutline,
    ComponentPlacement,
    Net,
    compute_wirelength,
)
from kicad_tools.placement.multi_fidelity import (
    FidelityLevel,
    evaluate_placement_multifidelity,
    make_fixed_fidelity_evaluator,
)
from kicad_tools.placement.vector import (
    ComponentDef,
    PadDef,
    PlacedComponent,
    TransformedPad,
    decode,
    encode,
)
from kicad_tools.placement.wirelength import compute_hpwl

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def board() -> BoardOutline:
    return BoardOutline(min_x=-50.0, min_y=-50.0, max_x=50.0, max_y=50.0)


@pytest.fixture
def design_rules():
    from kicad_tools.router.rules import DesignRules

    return DesignRules(trace_clearance=0.2)


def _placed(
    reference: str,
    x: float,
    y: float,
    pads: list[tuple[str, float, float]],
    rotation: float = 0.0,
) -> PlacedComponent:
    """A PlacedComponent whose pads are already in absolute coordinates."""
    return PlacedComponent(
        reference=reference,
        x=x,
        y=y,
        rotation=rotation,
        side=0,
        pads=tuple(
            TransformedPad(name=n, x=px, y=py, size_x=0.5, size_y=0.5) for n, px, py in pads
        ),
    )


def _centres(placements: list[PlacedComponent]) -> list[ComponentPlacement]:
    return [
        ComponentPlacement(reference=p.reference, x=p.x, y=p.y, rotation=p.rotation)
        for p in placements
    ]


def _defs_for(placements: list[PlacedComponent]) -> list[ComponentDef]:
    """Component defs sized generously so DRC/overlap stay quiet."""
    return [
        ComponentDef(
            reference=p.reference,
            pads=tuple(
                PadDef(
                    name=pad.name, local_x=pad.x - p.x, local_y=pad.y - p.y, size_x=0.5, size_y=0.5
                )
                for pad in p.pads
            ),
            width=10.0,
            height=10.0,
        )
        for p in placements
    ]


# Two 10x10 parts 20 mm apart on centres, wired pad-to-pad on their *facing*
# edges: the copper only has to span 10 mm, which a centre-anchored HPWL
# reports as 20 mm.
#
#   U1 centre (0,0), pad "1" at (+5,0)     U2 centre (20,0), pad "1" at (15,0)
FACING = [
    _placed("U1", 0.0, 0.0, [("1", 5.0, 0.0), ("2", -5.0, 0.0)]),
    _placed("U2", 20.0, 0.0, [("1", 15.0, 0.0), ("2", 25.0, 0.0)]),
]
FACING_NETS = [Net(name="SIG", pins=[("U1", "1"), ("U2", "1")])]


def _evaluate(
    placements,
    nets,
    board,
    fidelity,
    design_rules=None,
    component_defs=None,
    global_router=None,
):
    return evaluate_placement_multifidelity(
        placements=placements,
        nets=nets,
        board=board,
        fidelity=fidelity,
        component_defs=component_defs,
        design_rules=design_rules,
        global_router=global_router,
    )


# ---------------------------------------------------------------------------
# Level 1 measures pads; level 0 does not
# ---------------------------------------------------------------------------


def test_fidelity_0_stays_centre_anchored(board) -> None:
    """Level 0 accepts centre-only placements, so it keeps measuring centres."""
    r0 = _evaluate(FACING, FACING_NETS, board, FidelityLevel.HPWL)

    assert r0.score.breakdown.wirelength == pytest.approx(20.0)
    # Byte-identical to calling the centre-anchored estimator directly.
    assert r0.score.breakdown.wirelength == compute_wirelength(_centres(FACING), FACING_NETS)


def test_fidelity_1_measures_the_pads(board, design_rules) -> None:
    r1 = _evaluate(
        FACING,
        FACING_NETS,
        board,
        FidelityLevel.DRC,
        design_rules=design_rules,
        component_defs=_defs_for(FACING),
    )

    # Pads are 10 mm apart even though the centres are 20 mm apart.
    assert r1.score.breakdown.wirelength == pytest.approx(10.0)


def test_fidelity_1_matches_compute_hpwl_for_unit_weight_nets(board, design_rules) -> None:
    """With every ``Net.weight`` at its 1.0 default the level-1 term is HPWL."""
    r1 = _evaluate(
        FACING,
        FACING_NETS,
        board,
        FidelityLevel.DRC,
        design_rules=design_rules,
        component_defs=_defs_for(FACING),
    )

    assert r1.score.breakdown.wirelength == pytest.approx(compute_hpwl(FACING, FACING_NETS))


def test_pad_anchoring_can_increase_the_estimate(board, design_rules) -> None:
    """Pads are not a uniform discount -- outward-facing pads make it worse."""
    placements = [
        _placed("U1", 0.0, 0.0, [("1", -5.0, 0.0)]),
        _placed("U2", 20.0, 0.0, [("1", 25.0, 0.0)]),
    ]
    nets = [Net(name="SIG", pins=[("U1", "1"), ("U2", "1")])]

    r0 = _evaluate(placements, nets, board, FidelityLevel.HPWL)
    r1 = _evaluate(
        placements,
        nets,
        board,
        FidelityLevel.DRC,
        design_rules=design_rules,
        component_defs=_defs_for(placements),
    )

    assert r0.score.breakdown.wirelength == pytest.approx(20.0)
    assert r1.score.breakdown.wirelength == pytest.approx(30.0)


def test_intra_component_net_is_zero_on_centres_but_real_on_pads(board, design_rules) -> None:
    placements = [_placed("U1", 0.0, 0.0, [("1", -5.0, 0.0), ("2", 5.0, 3.0)])]
    nets = [Net(name="LOOP", pins=[("U1", "1"), ("U1", "2")])]

    r0 = _evaluate(placements, nets, board, FidelityLevel.HPWL)
    r1 = _evaluate(
        placements,
        nets,
        board,
        FidelityLevel.DRC,
        design_rules=design_rules,
        component_defs=_defs_for(placements),
    )

    assert r0.score.breakdown.wirelength == pytest.approx(0.0)
    assert r1.score.breakdown.wirelength == pytest.approx(13.0)  # 10 in x + 3 in y


# ---------------------------------------------------------------------------
# Rotation: invisible to centres, visible to pads
# ---------------------------------------------------------------------------


def test_rotation_is_invisible_at_level_0_but_visible_at_level_1(board, design_rules) -> None:
    """The optimizer searches a rotation axis level 0's wirelength cannot see.

    Pads are produced by the production transform (``encode``/``decode``), not
    hand-placed, so this exercises ``_transform_pad`` rather than a fixture.
    """
    defs = [
        ComponentDef(
            reference="U1",
            pads=(PadDef(name="1", local_x=5.0, local_y=0.0, size_x=0.5, size_y=0.5),),
            width=10.0,
            height=10.0,
        ),
        ComponentDef(
            reference="U2",
            pads=(PadDef(name="1", local_x=-5.0, local_y=0.0, size_x=0.5, size_y=0.5),),
            width=10.0,
            height=10.0,
        ),
    ]
    nets = [Net(name="SIG", pins=[("U1", "1"), ("U2", "1")])]

    def _placements(rotation: float) -> list[PlacedComponent]:
        raw = [
            PlacedComponent("U1", 0.0, 0.0, rotation, 0, ()),
            PlacedComponent("U2", 20.0, 0.0, 0.0, 0, ()),
        ]
        return decode(encode(raw), defs)

    unrotated = _placements(0.0)
    rotated = _placements(180.0)

    l0_unrotated = _evaluate(unrotated, nets, board, FidelityLevel.HPWL)
    l0_rotated = _evaluate(rotated, nets, board, FidelityLevel.HPWL)
    l1_unrotated = _evaluate(
        unrotated, nets, board, FidelityLevel.DRC, design_rules=design_rules, component_defs=defs
    )
    l1_rotated = _evaluate(
        rotated, nets, board, FidelityLevel.DRC, design_rules=design_rules, component_defs=defs
    )

    # Level 0 reads only p.x/p.y, which rotation leaves untouched.
    assert l0_unrotated.score.breakdown.wirelength == pytest.approx(
        l0_rotated.score.breakdown.wirelength
    )

    # Level 1 sees the pad swing from the facing edge to the far edge.
    assert l1_unrotated.score.breakdown.wirelength == pytest.approx(10.0)
    assert l1_rotated.score.breakdown.wirelength == pytest.approx(20.0)
    assert l1_rotated.score.breakdown.wirelength > l1_unrotated.score.breakdown.wirelength


# ---------------------------------------------------------------------------
# Only the wirelength term moves
# ---------------------------------------------------------------------------


def test_only_the_wirelength_term_changes_at_level_1(board, design_rules) -> None:
    """Overlap/boundary/area stay body/centre-based -- §7 of the audit."""
    r0 = _evaluate(FACING, FACING_NETS, board, FidelityLevel.HPWL)
    r1 = _evaluate(
        FACING,
        FACING_NETS,
        board,
        FidelityLevel.DRC,
        design_rules=design_rules,
        component_defs=_defs_for(FACING),
    )

    assert r1.score.breakdown.wirelength != pytest.approx(r0.score.breakdown.wirelength)
    assert r1.score.breakdown.overlap == pytest.approx(r0.score.breakdown.overlap)
    assert r1.score.breakdown.boundary == pytest.approx(r0.score.breakdown.boundary)
    assert r1.score.breakdown.area == pytest.approx(r0.score.breakdown.area)


def test_net_weight_survives_pad_anchoring(board, design_rules) -> None:
    """``compute_hpwl`` drops ``Net.weight``; the M2 path must not."""
    weighted = [Net(name="SIG", pins=[("U1", "1"), ("U2", "1")], weight=3.0)]

    r1 = _evaluate(
        FACING,
        weighted,
        board,
        FidelityLevel.DRC,
        design_rules=design_rules,
        component_defs=_defs_for(FACING),
    )

    assert r1.score.breakdown.wirelength == pytest.approx(30.0)  # 3 x 10 mm
    assert r1.score.breakdown.wirelength != pytest.approx(compute_hpwl(FACING, weighted))


def test_zero_weight_net_is_still_excluded(board, design_rules) -> None:
    muted = [Net(name="SIG", pins=[("U1", "1"), ("U2", "1")], weight=0.0)]

    r1 = _evaluate(
        FACING,
        muted,
        board,
        FidelityLevel.DRC,
        design_rules=design_rules,
        component_defs=_defs_for(FACING),
    )

    assert r1.score.breakdown.wirelength == pytest.approx(0.0)


def test_pin_without_a_pad_falls_back_to_its_component_centre(board, design_rules) -> None:
    """A partially-padded input degrades gracefully instead of dropping nets."""
    placements = [
        _placed("U1", 0.0, 0.0, [("1", 5.0, 0.0)]),
        PlacedComponent("U2", 20.0, 0.0, 0.0, 0, ()),  # no pads at all
    ]
    nets = [Net(name="SIG", pins=[("U1", "1"), ("U2", "1")])]
    defs = [
        ComponentDef(
            reference="U1",
            pads=(PadDef(name="1", local_x=5.0, local_y=0.0, size_x=0.5, size_y=0.5),),
            width=10.0,
            height=10.0,
        ),
        ComponentDef(reference="U2", pads=(), width=10.0, height=10.0),
    ]

    r1 = _evaluate(
        placements,
        nets,
        board,
        FidelityLevel.DRC,
        design_rules=design_rules,
        component_defs=defs,
    )

    # U1 pad at x=5, U2 falls back to its centre at x=20 -> 15 mm.
    assert r1.score.breakdown.wirelength == pytest.approx(15.0)


def test_empty_nets_are_zero_with_pads_present(board, design_rules) -> None:
    r1 = _evaluate(
        FACING,
        [],
        board,
        FidelityLevel.DRC,
        design_rules=design_rules,
        component_defs=_defs_for(FACING),
    )

    assert r1.score.breakdown.wirelength == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Higher levels inherit level 1; determinism
# ---------------------------------------------------------------------------


def test_fidelity_2_inherits_the_level_1_wirelength(board, design_rules) -> None:
    """Levels 2/3 add routability on top of the level-1 breakdown."""
    mock_router = MagicMock()
    mock_result = MagicMock()
    mock_result.failed_nets = []
    mock_router.route_all.return_value = mock_result

    defs = _defs_for(FACING)
    r1 = _evaluate(
        FACING,
        FACING_NETS,
        board,
        FidelityLevel.DRC,
        design_rules=design_rules,
        component_defs=defs,
    )
    r2 = _evaluate(
        FACING,
        FACING_NETS,
        board,
        FidelityLevel.GLOBAL_ROUTE,
        design_rules=design_rules,
        component_defs=defs,
        global_router=mock_router,
    )

    assert r2.score.breakdown.wirelength == pytest.approx(r1.score.breakdown.wirelength)
    assert r2.score.breakdown.wirelength == pytest.approx(10.0)


def test_repeated_evaluation_is_deterministic(board, design_rules) -> None:
    defs = _defs_for(FACING)
    results = [
        _evaluate(
            FACING,
            FACING_NETS,
            board,
            FidelityLevel.DRC,
            design_rules=design_rules,
            component_defs=defs,
        )
        for _ in range(5)
    ]

    wirelengths = {r.score.breakdown.wirelength for r in results}
    totals = {r.score.total for r in results}
    assert len(wirelengths) == 1
    assert len(totals) == 1


def test_pad_order_does_not_change_the_estimate(board, design_rules) -> None:
    """The pad map is keyed by (reference, pad_name), so ordering is irrelevant."""
    shuffled = [
        _placed("U1", 0.0, 0.0, [("2", -5.0, 0.0), ("1", 5.0, 0.0)]),
        _placed("U2", 20.0, 0.0, [("2", 25.0, 0.0), ("1", 15.0, 0.0)]),
    ]

    baseline = _evaluate(
        FACING,
        FACING_NETS,
        board,
        FidelityLevel.DRC,
        design_rules=design_rules,
        component_defs=_defs_for(FACING),
    )
    reordered = _evaluate(
        shuffled,
        FACING_NETS,
        board,
        FidelityLevel.DRC,
        design_rules=design_rules,
        component_defs=_defs_for(shuffled),
    )

    assert reordered.score.breakdown.wirelength == pytest.approx(
        baseline.score.breakdown.wirelength
    )


def test_fixed_fidelity_evaluator_closure_is_pad_anchored(board, design_rules) -> None:
    """The optimizer-facing closure inherits the level-1 anchoring."""
    evaluator = make_fixed_fidelity_evaluator(
        fidelity=FidelityLevel.DRC,
        nets=FACING_NETS,
        board=board,
        component_defs=_defs_for(FACING),
        design_rules=design_rules,
    )

    assert evaluator(FACING).score.breakdown.wirelength == pytest.approx(10.0)
