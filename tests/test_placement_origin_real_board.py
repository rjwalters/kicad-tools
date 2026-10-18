"""Real fleet-board regressions for the placement-optimizer origin frame (#7106).

Board 04 (``stm32_devboard``) has its Edge.Cuts ``gr_rect`` at
``(118.5, 67.5)..(178.5, 107.5)``, so ``pcb.board_origin`` is non-zero.
Before #7106 the optimizer mixed the origin-relative ``Footprint.position``
frame with an absolute outline, and ``kct placement optimize`` saved 16 of 17
parts off the board while reporting success.

Every check here reads the bytes of the *saved* file and compares them against
the absolute Edge.Cuts coordinates, so it cannot be satisfied by a getter that
merely agrees with itself.

Board 05 (``bldc_controller_routed``) carries KiCad keepout zones; the
``--auto-keepout`` path must place them inside the optimizer's board rectangle.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp import parse_file

REPO_ROOT = Path(__file__).resolve().parents[1]
BOARD_04 = REPO_ROOT / "boards" / "04-stm32-devboard" / "output" / "stm32_devboard.kicad_pcb"
BOARD_05 = (
    REPO_ROOT
    / "boards"
    / "05-bldc-motor-controller"
    / "output"
    / "bldc_controller_routed.kicad_pcb"
)

# Absolute Edge.Cuts extents of board 04 (the gr_rect in the source file).
B04_MIN_X, B04_MIN_Y, B04_MAX_X, B04_MAX_Y = 118.5, 67.5, 178.5, 107.5

pytestmark = pytest.mark.skipif(not BOARD_04.exists(), reason="board 04 fixture not present")


def _copy(src: Path, tmp_path: Path) -> Path:
    dst = tmp_path / src.name
    shutil.copyfile(src, dst)
    return dst


def _footprint_at_text(path: Path) -> dict[str, str]:
    """Map reference -> the footprint's own ``(at ...)`` text, read from disk."""
    text = path.read_text(encoding="utf-8")
    starts = [m.start() for m in re.finditer(r"\(footprint\s+\"", text)]
    result: dict[str, str] = {}
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        block = text[start:end]
        at = re.search(r"\(at\s+[^()]*\)", block)
        ref = re.search(r"\(property\s+\"Reference\"\s+\"([^\"]+)\"", block) or re.search(
            r"\(fp_text\s+reference\s+\"?([^\"\s)]+)", block
        )
        assert at is not None and ref is not None, f"unparseable footprint block #{i}"
        result[ref.group(1)] = at.group(0)
    return result


def _saved_positions_and_edge(path: Path) -> tuple[dict[str, tuple[float, float]], tuple]:
    """Read absolute footprint (at) and the absolute Edge.Cuts gr_rect from disk."""
    root = parse_file(path)
    positions: dict[str, tuple[float, float]] = {}
    edge = None
    for child in root.iter_children():
        if child.tag == "gr_rect":
            layer = child.find("layer")
            if layer and layer.get_string(0) == "Edge.Cuts":
                s, e = child.find("start"), child.find("end")
                xs = sorted([s.get_float(0), e.get_float(0)])
                ys = sorted([s.get_float(1), e.get_float(1)])
                edge = (xs[0], ys[0], xs[1], ys[1])
        elif child.tag == "footprint":
            at = child.find("at")
            ref = None
            for prop in child.find_all("property"):
                if prop.get_string(0) == "Reference":
                    ref = prop.get_string(1)
            for fp_text in child.find_all("fp_text"):
                if fp_text.get_string(0) == "reference":
                    ref = fp_text.get_string(1)
            assert at is not None and ref is not None
            positions[ref] = (at.get_float(0), at.get_float(1))
    assert edge is not None, "no Edge.Cuts gr_rect in saved file"
    return positions, edge


def test_board04_noop_round_trip_is_byte_identical(tmp_path: Path) -> None:
    """load -> from_pcb -> no-op write_to_pcb -> save leaves every (at) unchanged."""
    from kicad_tools.optim.placement import PlacementOptimizer

    src = _copy(BOARD_04, tmp_path)
    before = _footprint_at_text(src)
    assert len(before) == 17

    pcb = PCB.load(str(src))
    assert tuple(pcb.board_origin) == (B04_MIN_X, B04_MIN_Y)
    opt = PlacementOptimizer.from_pcb(pcb)
    assert opt.out_of_bounds_components() == []
    opt.write_to_pcb(pcb)

    out = tmp_path / "roundtrip.kicad_pcb"
    pcb.save(str(out))
    assert _footprint_at_text(out) == before


def test_board04_optimize_saves_parts_inside_real_edge(tmp_path: Path) -> None:
    """``kct placement optimize`` write-back lands inside the absolute Edge.Cuts."""
    from kicad_tools.cli.placement_cmd import main

    src = _copy(BOARD_04, tmp_path)
    out = tmp_path / "optimized.kicad_pcb"
    rc = main(
        [
            "optimize",
            str(src),
            "-o",
            str(out),
            "--strategy",
            "force-directed",
            "--iterations",
            "50",
            "--quiet",
        ]
    )
    assert rc == 0
    assert out.exists()

    positions, edge = _saved_positions_and_edge(out)
    assert edge == pytest.approx((B04_MIN_X, B04_MIN_Y, B04_MAX_X, B04_MAX_Y))
    assert len(positions) == 17
    outside = [
        ref
        for ref, (x, y) in positions.items()
        if not (edge[0] - 1e-6 <= x <= edge[2] + 1e-6 and edge[1] - 1e-6 <= y <= edge[3] + 1e-6)
    ]
    assert outside == [], f"parts saved outside the real board edge: {outside}"
    # The optimizer must actually have moved something, or this proves nothing.
    assert _footprint_at_text(out) != _footprint_at_text(src)


def test_board04_align_row_writes_expected_absolute_y(tmp_path: Path) -> None:
    """``kct placement align --axis row`` saves the mean absolute Y, others untouched."""
    from kicad_tools.cli.placement_cmd import main

    src = _copy(BOARD_04, tmp_path)
    before_pos, _ = _saved_positions_and_edge(src)
    before_text = _footprint_at_text(src)
    refs = ["C1", "C2", "C3"]
    expected_y = sum(before_pos[r][1] for r in refs) / len(refs)

    out = tmp_path / "aligned.kicad_pcb"
    rc = main(["align", str(src), "-o", str(out), "-c", ",".join(refs), "--axis", "row", "-q"])
    assert rc == 0

    after_pos, _ = _saved_positions_and_edge(out)
    after_text = _footprint_at_text(out)
    for ref in refs:
        assert after_pos[ref][1] == pytest.approx(expected_y, abs=1e-3)
        assert after_pos[ref][0] == pytest.approx(before_pos[ref][0], abs=1e-3)
    for ref in set(before_text) - set(refs):
        assert after_text[ref] == before_text[ref], ref

    # The align write-back itself never went through the optimizer frame, so
    # the checks above also hold on pre-#7106 code. What did break is that the
    # optimizer, reloading the aligned board, judged every part off-board.
    from kicad_tools.optim.placement import PlacementOptimizer

    assert PlacementOptimizer.from_pcb(PCB.load(str(out))).out_of_bounds_components() == []


@pytest.mark.skipif(not BOARD_05.exists(), reason="board 05 fixture not present")
def test_board05_auto_keepouts_inside_optimizer_board() -> None:
    """KiCad keepout zones land in the optimizer's (origin-relative) board frame."""
    from kicad_tools.optim.keepout import detect_keepout_zones
    from kicad_tools.optim.placement import PlacementOptimizer

    pcb = PCB.load(str(BOARD_05))
    ox, oy = pcb.board_origin
    assert (ox, oy) != (0.0, 0.0)

    opt = PlacementOptimizer.from_pcb(pcb)
    xs = [v.x for v in opt.board_outline.vertices]
    ys = [v.y for v in opt.board_outline.vertices]
    min_x, max_x, min_y, max_y = min(xs), max(xs), min(ys), max(ys)

    zones = [z for z in detect_keepout_zones(pcb) if z.name != "board_edge_clearance"]
    assert zones, "board 05 should yield keepout zones"
    for zone in zones:
        for x, y in zone.polygon:
            assert min_x - 1e-6 <= x <= max_x + 1e-6, (zone.name, x, (min_x, max_x))
            assert min_y - 1e-6 <= y <= max_y + 1e-6, (zone.name, y, (min_y, max_y))


def test_board_edge_keepout_and_edges_are_origin_relative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No-Shapely board-edge keepout and get_board_edges match the optimizer frame."""
    from kicad_tools.optim.edge_placement import get_board_edges
    from kicad_tools.optim.keepout import create_keepout_from_board_edge
    from kicad_tools.pcb import board_geometry

    pcb = PCB.load(str(_copy(BOARD_04, tmp_path)))
    w, h = B04_MAX_X - B04_MIN_X, B04_MAX_Y - B04_MIN_Y

    edges = get_board_edges(pcb)
    ex = [p.x for e in (edges.top, edges.bottom) for p in (e.start, e.end)]
    ey = [p.y for e in (edges.left, edges.right) for p in (e.start, e.end)]
    assert (min(ex), max(ex)) == pytest.approx((0.0, w))
    assert (min(ey), max(ey)) == pytest.approx((0.0, h))

    monkeypatch.setattr(board_geometry, "has_shapely", lambda: False)
    zone = create_keepout_from_board_edge(pcb=pcb, distance_mm=1.0)
    assert zone is not None
    zx = [p[0] for p in zone.polygon]
    zy = [p[1] for p in zone.polygon]
    assert (min(zx), max(zx)) == pytest.approx((1.0, w - 1.0))
    assert (min(zy), max(zy)) == pytest.approx((1.0, h - 1.0))


def test_board04_hierarchical_fallback_outline_is_origin_relative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The no-Shapely fallback in ``place_hierarchical_from_pcb`` shares the footprint frame (#7124)."""
    from kicad_tools.optim.bottom_up_placement import place_hierarchical_from_pcb
    from kicad_tools.pcb.board_geometry import BoardGeometry

    def _boom(*_args, **_kwargs):
        raise RuntimeError("force the raw Edge.Cuts fallback")

    monkeypatch.setattr(BoardGeometry, "from_pcb", _boom)

    pcb = PCB.load(str(_copy(BOARD_04, tmp_path)))
    ox, oy = pcb.board_origin
    assert (ox, oy) == (B04_MIN_X, B04_MIN_Y)

    result = place_hierarchical_from_pcb(pcb)
    assert len(result.positions) == 17

    outside = [
        ref
        for ref, (x, y, _rot) in result.positions.items()
        if not (B04_MIN_X <= x + ox <= B04_MAX_X and B04_MIN_Y <= y + oy <= B04_MAX_Y)
    ]
    assert outside == []
