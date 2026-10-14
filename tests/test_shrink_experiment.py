"""`scripts/research/shrink_experiment.py` inverse map (Issue #6310).

The displaced-end count in ``docs/research/expand-route-shrink-experiment.md``
rests on one piece of arithmetic: copper inside a rigid cluster translates with
the cluster, everything else scales by 1/scale about the 1x outline centre.
Pinned here on a tiny synthetic board, without routing anything.

The script lives under ``scripts/`` (not ``src/``), so it is loaded by path.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "research" / "shrink_experiment.py"


def _load():
    spec = importlib.util.spec_from_file_location("shrink_experiment_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


shrink_mod = _load()


def _fp(
    ref: str, x: float, y: float, pads: list[tuple[str, float, float, int, str]], rot: float = 0
) -> str:
    body = "".join(
        f'\n    (pad "{num}" smd rect (at {dx} {dy}) (size 1 1) '
        f'(layers "F.Cu" "F.Paste" "F.Mask") (net {n} "{name}"))'
        for num, dx, dy, n, name in pads
    )
    return (
        f'\n  (footprint "lib:{ref}" (layer "F.Cu") (at {x} {y} {rot})'
        f'\n    (property "Reference" "{ref}" (at 0 0) (layer "F.SilkS")){body}\n  )'
    )


def _board(outline: tuple[float, float, float, float], fps: str, copper: str = "") -> str:
    x0, y0, x1, y1 = outline
    return (
        '(kicad_pcb (version 20240108) (generator "test")\n'
        '  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))\n'
        '  (net 0 "") (net 1 "A") (net 2 "B")\n'
        f"  (gr_rect (start {x0} {y0}) (end {x1} {y1}) (stroke (width 0.1) (type default))"
        ' (fill none) (layer "Edge.Cuts"))'
        f"{fps}{copper}\n)\n"
    )


def _parts(u1: tuple[float, float], c1: tuple[float, float], r1: tuple[float, float]) -> str:
    return (
        _fp("U1", *u1, [("1", -3, 0, 1, "A"), ("2", 1, 0, 2, "B")])
        + _fp("C1", *c1, [("1", 0, 0, 2, "B")])
        + _fp("R1", *r1, [("1", 0, 0, 1, "A")])
    )


def _seg(a: tuple[float, float], b: tuple[float, float], net: int) -> str:
    return (
        f"\n  (segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width 0.2)"
        f' (layer "F.Cu") (net {net}))'
    )


@pytest.fixture
def boards(tmp_path):
    """1x board, and its rigid 1.5x twin with copper.

    1x outline 40x20 centred on (20, 10). Cluster {U1, C1} has centroid
    (11.5, 10) -> (7.25, 10) at 1.5x, a translation of (-4.25, 0); R1 alone
    scales (30, 10) -> (35, 10).
    """
    src = tmp_path / "b.kicad_pcb"
    src.write_text(_board((0, 0, 40, 20), _parts((10, 10), (13, 10), (30, 10))))
    copper = (
        # In-cluster: U1.2 (6.75, 10) to C1.1 (8.75, 10), net B.
        _seg((6.75, 10), (8.75, 10), 2)
        # Inter-cluster: U1.1 (2.75, 10) to R1.1 (35, 10), net A.
        + _seg((2.75, 10), (35, 10), 1)
    )
    routed = tmp_path / "routed" / "b_routed.kicad_pcb"
    routed.parent.mkdir()
    routed.write_text(_board((-10, -5, 50, 25), _parts((5.75, 10), (8.75, 10), (35, 10)), copper))
    return src, routed


def _segments(path: Path) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    from kicad_tools.core.sexp_file import load_pcb

    out = []
    for node in load_pcb(path).iter_children():
        if node.tag == "segment":
            s, e = node.find_child("start"), node.find_child("end")
            out.append(
                (
                    (float(s.get_value(0)), float(s.get_value(1))),
                    (float(e.get_value(0)), float(e.get_value(1))),
                )
            )
    return out


def test_in_cluster_copper_translates_and_inter_cluster_copper_scales(boards, tmp_path):
    src, routed = boards
    res = shrink_mod.shrink(src, routed, 1.5, tmp_path / "out")

    assert res["origin"] == pytest.approx([20.0, 10.0])
    assert res["footprints_moved"] == 3
    naive = _segments(Path(res["s0_naive"]["board"]))
    # In-cluster segment: exact translation by (+4.25, 0), lands on U1.2 / C1.1.
    assert naive[0][0] == pytest.approx((11.0, 10.0))
    assert naive[0][1] == pytest.approx((13.0, 10.0))
    # Inter-cluster segment: both ends scale by 1/1.5 about (20, 10).
    assert naive[1][0] == pytest.approx((8.5, 10.0))
    assert naive[1][1] == pytest.approx((30.0, 10.0))


def test_naive_map_counts_the_end_it_tears_off_and_snap_reattaches_it(boards, tmp_path):
    src, routed = boards
    res = shrink_mod.shrink(src, routed, 1.5, tmp_path / "out")

    naive, snap = res["s0_naive"], res["s1_snap"]
    assert naive["ends_on_pads_expanded"] == 4
    # Only U1.1's end misses: scaled to x=8.5, pad is at x=7 (1 mm wide).
    assert naive["ends_displaced"] == 1
    assert naive["displaced_detail"][0]["ref"] == "U1"
    assert naive["displaced_detail"][0]["pad"] == "1"
    assert naive["displaced_detail"][0]["miss_mm"] == pytest.approx(1.5)
    assert snap["ends_displaced"] == 0
    assert _segments(Path(snap["board"]))[1][0] == pytest.approx((7.0, 10.0))


def test_a_1x_source_that_already_has_copper_is_refused(boards, tmp_path):
    _src, routed = boards
    dirty = tmp_path / "dirty.kicad_pcb"
    dirty.write_text(
        _board((0, 0, 40, 20), _parts((10, 10), (13, 10), (30, 10)), _seg((0, 0), (1, 1), 1))
    )
    with pytest.raises(SystemExit, match="already carries copper"):
        shrink_mod.shrink(dirty, routed, 1.5, tmp_path / "o")


def test_moved_by_route_ignores_an_equivalent_rotation_and_names_a_real_move(tmp_path):
    def board(name: str, u1: tuple[float, float], rot: float) -> Path:
        path = tmp_path / name
        path.write_text(_board((0, 0, 40, 20), _fp("U1", *u1, [("1", 0, 0, 1, "A")], rot)))
        return path

    before = board("in.kicad_pcb", (10, 10), 270)
    assert shrink_mod.moved_by_route(before, board("same.kicad_pcb", (10, 10), -90)) == []
    assert shrink_mod.moved_by_route(before, board("moved.kicad_pcb", (11, 10), 270)) == ["U1"]
    assert shrink_mod.moved_by_route(before, board("turned.kicad_pcb", (10, 10), 0)) == ["U1"]
