"""Preserved copper of a skipped net keeps its ``--net-class-map`` floor (#8577).

``kct route --preserve-existing`` puts every net whose copper it keeps on the
loader's skip list.  A skipped net's pads become net-0 obstacles, so the net is
absent from ``router.net_names``, which is filled from routable pads.
:func:`refresh_router_class_floors` -- run after the CLI merges
``--net-class-map`` -- resolved class names through ``net_names`` alone and
never touched the neutralised pads, so the kept copper of a wider-clearance
class got no floor at all.  New Signal copper was then held only to the base
clearance against it, and kicad-cli (which needs the larger of the two
classes) failed the output: a Signal via 0.183 mm from a kept Analog (0.2)
track, a Signal track 0.224 mm from a kept Clock (0.25) pad.

The board is fully synthetic: a Signal net ``SIG`` whose straight path runs
0.17 mm past a kept ``ANA`` (Analog, 0.2 mm) track, and a kept ``CLK``
(Clock, 0.25 mm) net whose pads are only ever obstacles.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import kicad_tools.cli.route_cmd as route_cmd
from kicad_tools.router.authored_clearance import authored_violations
from kicad_tools.router.class_clearance_floors import refresh_router_class_floors
from kicad_tools.router.io import load_pcb_for_routing
from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer.pcb import parse_segments, parse_vias
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.rules import DesignRules, NetClassRouting

BASE = 0.15
ANALOG = 0.2
CLOCK = 0.25
SIG_W = 0.15
ANA_W = 0.2
SIG_Y = 108.0
# The kept Analog run's edge sits 0.17 mm below the straight Signal path:
# legal at the 0.15 base, short of the 0.2 Analog class.
ANA_Y = SIG_Y + SIG_W / 2 + 0.17 + ANA_W / 2
NETS = {1: "SIG", 2: "ANA", 3: "CLK"}

CLASS_MAP = {
    "SIG": {"name": "Signal", "trace_width": SIG_W, "clearance": 0.1016},
    "ANA": {"name": "Analog", "trace_width": ANA_W, "clearance": ANALOG},
    "CLK": {"name": "Clock", "trace_width": 0.3, "clearance": CLOCK},
}


def _pad(ref: str, x: float, y: float, net: int, size: float = 0.6) -> str:
    return (
        f'(footprint "Synthetic:P_{ref}" (layer "F.Cu") (at {x} {y})\n'
        f'  (property "Reference" "{ref}" (at 0 -1) (layer "F.SilkS"))\n'
        f'  (pad "1" smd rect (at 0 0) (size {size} {size}) (layers "F.Cu") '
        f'(net {net} "{NETS[net]}")))'
    )


def _seg(x1: float, y1: float, x2: float, y2: float, w: float, net: int, k: int) -> str:
    return (
        f'(segment (start {x1} {y1}) (end {x2} {y2}) (width {w}) (layer "F.Cu") '
        f'(net {net}) (uuid "00000000-0000-4000-8000-{k:012d}"))'
    )


def _board(tmp_path: Path) -> Path:
    """SIG pads 12 mm apart on y=108; kept ANA under them; kept CLK off to one side."""
    parts = [
        _pad("S1", 104, SIG_Y, 1),
        _pad("S2", 116, SIG_Y, 1),
        _pad("A1", 106, 111, 2),
        _pad("A2", 114, 111, 2),
        _pad("C1", 104, 102, 3),
        _pad("C2", 116, 102, 3),
    ]
    kept = [
        _seg(106, 111, 106, ANA_Y, ANA_W, 2, 1),
        _seg(106, ANA_Y, 114, ANA_Y, ANA_W, 2, 2),
        _seg(114, ANA_Y, 114, 111, ANA_W, 2, 3),
        _seg(104, 102, 116, 102, 0.3, 3, 4),
    ]
    table = " ".join(f'(net {i} "{n}")' for i, n in {0: "", **NETS}.items())
    path = tmp_path / "board.kicad_pcb"
    path.write_text(
        '(kicad_pcb (version 20240108) (generator "synthetic-8577")\n'
        "  (general (thickness 1.6))\n"
        '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
        f"  (setup (pad_to_mask_clearance 0)) {table}\n"
        "  (gr_rect (start 100 99) (end 120 114) (stroke (width 0.1) (type default))"
        ' (fill none) (layer "Edge.Cuts"))\n' + "\n".join(parts) + "\n" + "\n".join(kept) + "\n)\n"
    )
    return path


def _class_map(tmp_path: Path) -> Path:
    path = tmp_path / "classes.json"
    path.write_text(json.dumps(CLASS_MAP))
    return path


def _load_skipping_kept(board: Path):
    """The ``--preserve-existing`` load: kept nets skipped, kept copper loaded."""
    rules = DesignRules(trace_clearance=BASE, via_clearance=BASE)
    return load_pcb_for_routing(
        str(board),
        skip_nets=["ANA", "CLK"],
        rules=rules,
        load_existing_routes=True,
        force_python=True,
        validate_drc=False,
        router_class_floors=False,
    )


def _merge_cli_class_map(router) -> None:
    """What ``_apply_net_class_map_sidecar`` does after loading."""
    for name, spec in CLASS_MAP.items():
        router.net_class_map[name] = NetClassRouting(
            name=spec["name"], trace_width=spec["trace_width"], clearance=spec["clearance"]
        )
    refresh_router_class_floors(router)


def test_skipped_kept_nets_get_their_class_floor(tmp_path):
    router, net_map = _load_skipping_kept(_board(tmp_path))
    # The premise: skipped nets are not routable, so net_names lacks them.
    assert net_map["ANA"] not in router.net_names
    assert net_map["CLK"] not in router.net_names

    _merge_cli_class_map(router)

    floors = router.rules.net_clearance_floors
    assert floors.get(net_map["ANA"]) == pytest.approx(ANALOG)
    assert floors.get(net_map["CLK"]) == pytest.approx(CLOCK)
    # Signal (0.1016) is below the base and adds nothing.
    assert net_map["SIG"] not in floors


def test_neutralised_pads_of_skipped_nets_carry_the_class_floor(tmp_path):
    router, _ = _load_skipping_kept(_board(tmp_path))
    _merge_cli_class_map(router)
    by_name = {}
    for pad in router.grid.pads:
        by_name.setdefault(pad.net_name, set()).add((pad.net, round(pad.authored_clearance, 4)))
    assert by_name["CLK"] == {(0, CLOCK)}
    assert by_name["ANA"] == {(0, ANALOG)}
    assert router.grid._authored_item_floors is True


def test_lowered_class_drops_the_neutral_pad_floor(tmp_path):
    router, _ = _load_skipping_kept(_board(tmp_path))
    _merge_cli_class_map(router)
    router.net_class_map["CLK"] = NetClassRouting(name="Clock", clearance=0.1)
    refresh_router_class_floors(router)
    assert {p.authored_clearance for p in router.grid.pads if p.net_name == "CLK"} == {0.0}


@pytest.mark.parametrize(
    "gap,flagged",
    [(0.183, True), (0.21, False)],
)
def test_census_holds_a_signal_via_to_the_kept_analog_track(tmp_path, gap, flagged):
    """The issue's 0.183 mm via-to-kept-Analog-track pair is reported."""
    router, net_map = _load_skipping_kept(_board(tmp_path))
    _merge_cli_class_map(router)
    sig = net_map["SIG"]
    via_d = 0.45
    y = ANA_Y - ANA_W / 2 - gap - via_d / 2
    via = Via(110.0, y, 0.2, via_d, (Layer.F_CU, Layer.B_CU), sig, "SIG")
    new = Route(sig, "SIG", vias=[via])
    hits = authored_violations(
        router.rules.net_clearance_floors, router.grid.pads, [new, *router.existing_routes]
    )
    mine = [h for h in hits if h.net == sig]
    assert bool(mine) is flagged
    if flagged:
        assert mine[0].required == pytest.approx(ANALOG)
        assert mine[0].actual == pytest.approx(gap, abs=1e-6)


def test_census_holds_a_signal_track_to_the_kept_clock_pad(tmp_path):
    """The issue's 0.224 mm track-to-kept-Clock-pad pair is reported."""
    router, net_map = _load_skipping_kept(_board(tmp_path))
    _merge_cli_class_map(router)
    sig = net_map["SIG"]
    y = 102 + 0.3 + 0.224 + SIG_W / 2  # below pad C1 (0.6 mm square at y=102)
    seg = Segment(103.0, y, 105.0, y, SIG_W, Layer.F_CU, net=sig, net_name="SIG")
    hits = authored_violations(
        router.rules.net_clearance_floors,
        router.grid.pads,
        [Route(sig, "SIG", segments=[seg]), *router.existing_routes],
    )
    mine = [h for h in hits if h.net == sig and h.kind == "pad"]
    assert mine and mine[0].required == pytest.approx(CLOCK)


def _min_gap_to_kept_analog(out: Path) -> float:
    from kicad_tools.router.clearance_shapes import copper_gap, segment_shape, via_shape

    segs = parse_segments(out.read_text())
    vias = parse_vias(out.read_text())
    analog = [segment_shape(s) for s in segs.get("ANA", [])]
    assert analog, "the kept Analog copper must survive the route"
    new = [segment_shape(s) for s in segs.get("SIG", [])]
    new += [via_shape(v) for v in vias.get("SIG", [])]
    assert new, "SIG must be routed"
    return min(copper_gap(a, b) for a in new for b in analog)


@pytest.mark.parametrize("backend", ["python", "cpp"])
def test_route_preserve_existing_spaces_new_copper_at_the_wider_class(tmp_path, capsys, backend):
    """End to end: ``kct route --preserve-existing --net-class-map`` (#8577)."""
    if backend == "cpp":
        from kicad_tools.router.cpp_backend import is_cpp_available

        if not is_cpp_available():
            pytest.skip("native build required")
    board = _board(tmp_path)
    out = tmp_path / "out.kicad_pcb"
    rc = route_cmd.main(
        [
            str(board),
            "-o",
            str(out),
            "--preserve-existing",
            "--net-class-map",
            str(_class_map(tmp_path)),
            "--layers",
            "2",
            "--backend",
            backend,
            "--clearance",
            str(BASE),
            "--grid",
            "0.05",
            "--skip-drc",
            "--no-cache",
            "--no-chain-complete",
        ]
    )
    text = capsys.readouterr()
    assert out.is_file(), (rc, text.out[-3000:], text.err[-3000:])
    assert _min_gap_to_kept_analog(out) >= ANALOG - 1e-4
