"""Pad-anchored wirelength for the optimizer objective (issue #4831, M1).

The optimizer objective historically measured every net at component
*centres* while the pads needed to measure it properly were computed on every
candidate decode and then discarded (see
``docs/placement-pad-anchoring-audit.md``). These tests pin down the opt-in
pad-anchored path:

* default (``pad_positions=None``) stays byte-identical to the centre score;
* pad anchoring changes the wirelength term, and *only* that term;
* rotation -- invisible to a centre-anchored HPWL -- becomes visible;
* ``Net.weight`` (the ``--anchor-weight`` mechanism) survives the migration;
* pins with no pad fall back to their component centre rather than dropping.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kicad_tools.cli.optimize_placement_cmd import _evaluate
from kicad_tools.placement.cost import (
    BoardOutline,
    ComponentPlacement,
    DesignRuleSet,
    Net,
    PlacementCostConfig,
    compute_wirelength,
    evaluate_placement,
)
from kicad_tools.placement.vector import (
    ComponentDef,
    PadDef,
    PlacedComponent,
    TransformedPad,
    decode,
    encode,
)
from kicad_tools.placement.wirelength import build_pad_position_map, compute_hpwl

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "placement"
BENCHMARK_FILE = FIXTURES_DIR / "benchmark_boards.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _placed(
    reference: str,
    x: float,
    y: float,
    pads: list[tuple[str, float, float]],
    rotation: float = 0.0,
) -> PlacedComponent:
    """A PlacedComponent with pads already in absolute board coordinates."""
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


# Two 10x10 parts 20 mm apart on centres, connected pad-to-pad on their
# *facing* edges: the pads are only 10 mm apart.
#
#   U1 centre (0,0), pad "1" at (+5, 0)      U2 centre (20,0), pad "1" at (15, 0)
FACING_PADS = [
    _placed("U1", 0.0, 0.0, [("1", 5.0, 0.0), ("2", -5.0, 0.0)]),
    _placed("U2", 20.0, 0.0, [("1", 15.0, 0.0), ("2", 25.0, 0.0)]),
]
FACING_NET = Net(name="SIG", pins=[("U1", "1"), ("U2", "1")])


# ---------------------------------------------------------------------------
# build_pad_position_map
# ---------------------------------------------------------------------------


def test_build_pad_position_map_keys_on_reference_and_pad_name() -> None:
    pad_map = build_pad_position_map(FACING_PADS)

    assert pad_map[("U1", "1")] == (5.0, 0.0)
    assert pad_map[("U2", "2")] == (25.0, 0.0)
    assert len(pad_map) == 4


def test_build_pad_position_map_skips_components_without_pads() -> None:
    padless = PlacedComponent(reference="M1", x=1.0, y=2.0, rotation=0.0, side=0, pads=())

    assert build_pad_position_map([padless]) == {}


# ---------------------------------------------------------------------------
# compute_wirelength: default unchanged, pads opt-in
# ---------------------------------------------------------------------------


def test_default_is_centre_anchored_and_unchanged() -> None:
    """No pad map -> the historical centre-to-centre HPWL, to the mm."""
    assert compute_wirelength(_centres(FACING_PADS), [FACING_NET]) == pytest.approx(20.0)
    assert compute_wirelength(_centres(FACING_PADS), [FACING_NET], None) == pytest.approx(20.0)


def test_pad_anchoring_measures_the_pads_not_the_centres() -> None:
    """The facing pads are 10 mm apart though the centres are 20 mm apart."""
    pad_map = build_pad_position_map(FACING_PADS)

    assert compute_wirelength(_centres(FACING_PADS), [FACING_NET], pad_map) == pytest.approx(10.0)


def test_pad_anchoring_can_increase_wirelength() -> None:
    """Pad anchoring is not a uniform discount -- pads can sit further apart."""
    placements = [
        _placed("U1", 0.0, 0.0, [("1", -5.0, 0.0)]),
        _placed("U2", 20.0, 0.0, [("1", 25.0, 0.0)]),
    ]
    net = Net(name="SIG", pins=[("U1", "1"), ("U2", "1")])

    centre = compute_wirelength(_centres(placements), [net])
    pad = compute_wirelength(_centres(placements), [net], build_pad_position_map(placements))

    assert centre == pytest.approx(20.0)
    assert pad == pytest.approx(30.0)


def test_intra_component_net_is_zero_on_centres_but_real_on_pads() -> None:
    """Two pads of one part collapse to a single point in the centre model."""
    placements = [_placed("U1", 0.0, 0.0, [("1", -5.0, 0.0), ("2", 5.0, 2.0)])]
    net = Net(name="LOOP", pins=[("U1", "1"), ("U1", "2")])

    assert compute_wirelength(_centres(placements), [net]) == pytest.approx(0.0)
    assert compute_wirelength(
        _centres(placements), [net], build_pad_position_map(placements)
    ) == pytest.approx(12.0)


def test_net_weight_is_honoured_in_pad_anchored_mode() -> None:
    """The --anchor-weight mechanism survives the migration (audit counter-note)."""
    weighted = Net(name="SIG", pins=[("U1", "1"), ("U2", "1")], weight=3.0)
    pad_map = build_pad_position_map(FACING_PADS)

    assert compute_wirelength(_centres(FACING_PADS), [weighted], pad_map) == pytest.approx(30.0)


def test_zero_weight_net_still_excluded_in_pad_anchored_mode() -> None:
    muted = Net(name="SIG", pins=[("U1", "1"), ("U2", "1")], weight=0.0)
    pad_map = build_pad_position_map(FACING_PADS)

    assert compute_wirelength(_centres(FACING_PADS), [muted], pad_map) == pytest.approx(0.0)


def test_pin_without_a_pad_falls_back_to_its_component_centre() -> None:
    """Partial pad data degrades per pin instead of dropping the net."""
    pad_map = build_pad_position_map(FACING_PADS)
    del pad_map[("U2", "1")]  # U2 loses its pad -> measured at its centre (20, 0)

    assert compute_wirelength(_centres(FACING_PADS), [FACING_NET], pad_map) == pytest.approx(15.0)


def test_pad_map_for_an_unplaced_component_is_ignored_like_before() -> None:
    """A net pin with neither a pad nor a placement contributes nothing."""
    net = Net(name="SIG", pins=[("U1", "1"), ("GHOST", "1")])

    assert compute_wirelength(
        _centres(FACING_PADS), [net], build_pad_position_map(FACING_PADS)
    ) == pytest.approx(0.0)


def test_empty_net_list_is_zero_with_pads() -> None:
    assert compute_wirelength(
        _centres(FACING_PADS), [], build_pad_position_map(FACING_PADS)
    ) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Rotation: invisible to centres, visible to pads
# ---------------------------------------------------------------------------


def test_rotation_is_invisible_to_centres_but_visible_to_pads() -> None:
    """The audit's core argument: the optimizer searches a dimension the
    centre-anchored wirelength term cannot see at all."""
    parts = [
        ComponentDef(
            reference="U1",
            pads=(PadDef(name="1", local_x=5.0, local_y=0.0, size_x=0.5, size_y=0.5),),
            width=10.0,
            height=10.0,
        ),
        ComponentDef(
            reference="U2",
            pads=(PadDef(name="1", local_x=0.0, local_y=0.0, size_x=0.5, size_y=0.5),),
            width=2.0,
            height=2.0,
        ),
    ]
    net = Net(name="SIG", pins=[("U1", "1"), ("U2", "1")])

    def wirelengths(rotation: float) -> tuple[float, float]:
        vector = encode(
            [
                PlacedComponent("U1", 0.0, 0.0, rotation, 0, ()),
                PlacedComponent("U2", 20.0, 0.0, 0.0, 0, ()),
            ]
        )
        placed = decode(vector, parts)
        centres = _centres(placed)
        return (
            compute_wirelength(centres, [net]),
            compute_wirelength(centres, [net], build_pad_position_map(placed)),
        )

    centre_0, pad_0 = wirelengths(0.0)
    centre_180, pad_180 = wirelengths(180.0)

    # Centres: rotating U1 changes nothing.
    assert centre_0 == pytest.approx(centre_180) == pytest.approx(20.0)
    # Pads: U1's pad swings from the near edge to the far edge.
    assert pad_0 == pytest.approx(15.0)
    assert pad_180 == pytest.approx(25.0)


# ---------------------------------------------------------------------------
# evaluate_placement plumbing
# ---------------------------------------------------------------------------


def _score(pad_positions):
    return evaluate_placement(
        _centres(FACING_PADS),
        [FACING_NET],
        DesignRuleSet(),
        BoardOutline(min_x=-20.0, min_y=-20.0, max_x=60.0, max_y=40.0),
        PlacementCostConfig(),
        {"U1": (10.0, 10.0), "U2": (10.0, 10.0)},
        pad_positions=pad_positions,
    )


def test_evaluate_placement_default_is_unchanged() -> None:
    assert _score(None).breakdown.wirelength == pytest.approx(20.0)


def test_evaluate_placement_pad_positions_changes_only_the_wirelength_term() -> None:
    centre = _score(None)
    pad = _score(build_pad_position_map(FACING_PADS))

    assert pad.breakdown.wirelength == pytest.approx(10.0)
    assert pad.total < centre.total
    for field in ("overlap", "boundary", "drc", "area", "block_boundary", "inter_block"):
        assert getattr(pad.breakdown, field) == pytest.approx(getattr(centre.breakdown, field))
    assert pad.is_feasible == centre.is_feasible


# ---------------------------------------------------------------------------
# CLI evaluation path (kct optimize-placement --pad-anchored-wirelength)
# ---------------------------------------------------------------------------


def _cli_parts() -> list[ComponentDef]:
    return [
        ComponentDef(
            reference="U1",
            pads=(PadDef(name="1", local_x=5.0, local_y=0.0, size_x=0.5, size_y=0.5),),
            width=10.0,
            height=10.0,
        ),
        ComponentDef(
            reference="C1",
            pads=(PadDef(name="1", local_x=0.5, local_y=0.0, size_x=0.5, size_y=0.5),),
            width=2.0,
            height=1.0,
        ),
    ]


def test_cli_evaluate_pad_anchored_flag_switches_the_objective() -> None:
    parts = _cli_parts()
    vector = encode(
        [
            PlacedComponent("U1", 0.0, 0.0, 0.0, 0, ()),
            PlacedComponent("C1", 20.0, 0.0, 0.0, 0, ()),
        ]
    )
    nets = [Net(name="VCC", pins=[("U1", "1"), ("C1", "1")])]
    args = (
        vector,
        parts,
        nets,
        DesignRuleSet(),
        BoardOutline(min_x=-20.0, min_y=-20.0, max_x=60.0, max_y=40.0),
        PlacementCostConfig(),
        {"U1": (10.0, 10.0), "C1": (2.0, 1.0)},
    )

    centre = _evaluate(*args)
    pad = _evaluate(*args, pad_anchored=True)

    assert centre.breakdown.wirelength == pytest.approx(20.0)
    assert pad.breakdown.wirelength == pytest.approx(15.5)


def test_cli_evaluate_defaults_to_centre_anchored() -> None:
    """The flag is opt-in: omitting it must not perturb any existing run."""
    parts = _cli_parts()
    vector = encode(
        [
            PlacedComponent("U1", 0.0, 0.0, 0.0, 0, ()),
            PlacedComponent("C1", 20.0, 0.0, 0.0, 0, ()),
        ]
    )
    nets = [Net(name="VCC", pins=[("U1", "1"), ("C1", "1")])]
    args = (
        vector,
        parts,
        nets,
        DesignRuleSet(),
        BoardOutline(min_x=-20.0, min_y=-20.0, max_x=60.0, max_y=40.0),
        PlacementCostConfig(),
        {"U1": (10.0, 10.0), "C1": (2.0, 1.0)},
    )

    assert _evaluate(*args).total == pytest.approx(_evaluate(*args, pad_anchored=False).total)


def test_cli_flag_exists_and_defaults_off() -> None:
    from kicad_tools.cli.parser import create_parser

    args = create_parser().parse_args(["optimize-placement", "board.kicad_pcb"])
    assert args.pad_anchored_wirelength is False

    args = create_parser().parse_args(
        ["optimize-placement", "board.kicad_pcb", "--pad-anchored-wirelength"]
    )
    assert args.pad_anchored_wirelength is True


# ---------------------------------------------------------------------------
# Fixture-board regression: the audit's measured divergence, via the new path
# ---------------------------------------------------------------------------


def _load_fixture_board(name: str) -> tuple[list[PlacedComponent], list[Net]]:
    boards: dict[str, Any] = json.loads(BENCHMARK_FILE.read_text())["boards"]
    board = boards[name]

    defs = {
        c["reference"]: ComponentDef(
            reference=c["reference"],
            pads=tuple(
                PadDef(
                    name=p["name"],
                    local_x=p["local_x"],
                    local_y=p["local_y"],
                    size_x=p.get("size_x", 0.5),
                    size_y=p.get("size_y", 0.5),
                )
                for p in c.get("pads", [])
            ),
            width=c.get("width", 1.0),
            height=c.get("height", 1.0),
        )
        for c in board["components"]
    }
    raw = board.get("known_optimal_placement") or board["reference_placement"]
    order = [defs[r["reference"]] for r in raw]
    vector = encode(
        [
            PlacedComponent(
                reference=r["reference"],
                x=r["x"],
                y=r["y"],
                rotation=r.get("rotation", 0.0),
                side=r.get("side", 0),
                pads=(),
            )
            for r in raw
        ]
    )
    nets = [Net(name=n["name"], pins=[(p[0], p[1]) for p in n["pins"]]) for n in board["nets"]]
    return decode(vector, order), nets


@pytest.mark.parametrize(
    "board_name,centre_mm,pad_mm",
    [
        ("trivial_3_resistors", 12.0, 7.2),
        ("simple_rc_filter", 127.0, 126.69),
        ("medium_mcu_board", 382.0, 339.23),
    ],
)
def test_committed_fixtures_reproduce_the_audit_divergence(
    board_name: str, centre_mm: float, pad_mm: float
) -> None:
    """§4 of docs/placement-pad-anchoring-audit.md, now guarded by a test.

    The two estimators disagree by 0.2%-40% on the *same* committed layout,
    which is the mechanism by which a pad-anchored objective can rank two
    candidates differently. Also asserts the new ``pad_positions`` path agrees
    with the pre-existing ``compute_hpwl`` estimator on unweighted nets.
    """
    placed, nets = _load_fixture_board(board_name)
    centres = _centres(placed)
    pad_map = build_pad_position_map(placed)

    assert compute_wirelength(centres, nets) == pytest.approx(centre_mm, abs=0.01)
    assert compute_wirelength(centres, nets, pad_map) == pytest.approx(pad_mm, abs=0.01)
    assert compute_wirelength(centres, nets, pad_map) == pytest.approx(compute_hpwl(placed, nets))
