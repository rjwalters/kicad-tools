"""Panel Edge.Cuts form closed loops; tabs splice into them (Issue #6143).

``Panel._render_tab`` used to draw only the two side lines of each tab and
never open the board rectangles they joined, so every tab side dangled as a
T-junction and ``kicad-cli pcb drc`` reported ``invalid_outline``.  Side-by-
side tabs also had their width and height swapped, so they reached 0.5 mm
into both boards, and V-score lines were drawn on Edge.Cuts as open segments.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

pytest.importorskip("shapely", reason="Shapely required for panel tests")

from kicad_tools.panel import CutMethod, PanelConfig  # noqa: E402
from kicad_tools.panel.config import FrameConfig, TabConfig  # noqa: E402
from kicad_tools.panel.panel import Panel  # noqa: E402
from kicad_tools.panel.tabs import (  # noqa: E402
    compute_tabs_between_boards,
    compute_tabs_to_frame,
)
from kicad_tools.sexp.parser import SExp  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests" / "fixtures" / "projects" / "test_project.kicad_pcb"
BOARD01 = REPO / "boards" / "01-voltage-divider" / "output" / "voltage_divider_routed.kicad_pcb"

_KEY_DIGITS = 4  # 0.1 um -- far below any real gap, far above float noise


def _layer(node: SExp) -> str | None:
    layer = node.find_child("layer")
    return layer.get_string(0) if layer is not None else None


def _edge_segments(root: SExp) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    segs = []
    for child in root.children:
        if child.name != "gr_line" or _layer(child) != "Edge.Cuts":
            continue
        start, end = child.find_child("start"), child.find_child("end")
        segs.append(
            (
                (round(start.get_float(0), _KEY_DIGITS), round(start.get_float(1), _KEY_DIGITS)),
                (round(end.get_float(0), _KEY_DIGITS), round(end.get_float(1), _KEY_DIGITS)),
            )
        )
    return segs


def _assert_closed_loops(root: SExp) -> None:
    """Every Edge.Cuts endpoint is shared by exactly two segments."""
    segs = _edge_segments(root)
    assert segs, "panel has no Edge.Cuts"
    degree: Counter[tuple[float, float]] = Counter()
    for a, b in segs:
        assert a != b, "zero-length Edge.Cuts segment"
        degree[a] += 1
        degree[b] += 1
    bad = {pt: d for pt, d in degree.items() if d != 2}
    assert not bad, f"Edge.Cuts endpoints not at degree 2: {bad}"


def _overlaps_interior(a: tuple[float, ...], b: tuple[float, ...], eps: float = 1e-6) -> bool:
    return a[0] < b[2] - eps and a[2] > b[0] + eps and a[1] < b[3] - eps and a[3] > b[1] + eps


def _panel(source: Path, *, frame: bool = False, cut: CutMethod = CutMethod.MOUSEBITE) -> Panel:
    cfg = PanelConfig(
        rows=2,
        cols=2,
        cut_method=cut,
        frame=FrameConfig() if frame else None,
    )
    return Panel.from_config(source, cfg)


SOURCES = [pytest.param(FIXTURE, id="fixture")]
if BOARD01.exists():
    SOURCES.append(pytest.param(BOARD01, id="board01"))


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize(
    "frame,cut",
    [
        (False, CutMethod.MOUSEBITE),
        (True, CutMethod.MOUSEBITE),
        (False, CutMethod.VCUT),
        (True, CutMethod.VCUT),
    ],
    ids=["default", "frame", "vcut", "frame-vcut"],
)
def test_edge_cuts_form_closed_loops(source: Path, frame: bool, cut: CutMethod) -> None:
    panel = _panel(source, frame=frame, cut=cut)
    assert panel.tabs, "expected tabs on a 2x2 panel"
    _assert_closed_loops(panel.build())


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("frame", [False, True], ids=["default", "frame"])
def test_tabs_never_enter_a_board(source: Path, frame: bool) -> None:
    panel = _panel(source, frame=frame)
    for tab in panel.tabs:
        rect = (tab.min_x, tab.min_y, tab.max_x, tab.max_y)
        for inst in panel.instances:
            assert not _overlaps_interior(rect, inst.bounds), (
                f"tab {rect} intrudes into board {inst.index} {inst.bounds}"
            )


@pytest.mark.parametrize("frame", [False, True], ids=["default", "frame"])
def test_every_tab_bridges_two_solids(frame: bool) -> None:
    """Each tab's two ends lie on a board edge or the rail -- no floating tabs."""
    panel = _panel(FIXTURE, frame=frame)
    inner = panel._get_frame_inner_bounds() if frame else None
    for tab in panel.tabs:
        if tab.orientation == "horizontal":  # spans the gap along Y
            ends = (tab.min_y, tab.max_y)
            touch = [
                any(
                    abs(inst.bounds[3] - y) < 1e-6 or abs(inst.bounds[1] - y) < 1e-6
                    for inst in panel.instances
                )
                or (inner is not None and (abs(inner[1] - y) < 1e-6 or abs(inner[3] - y) < 1e-6))
                for y in ends
            ]
        else:
            ends = (tab.min_x, tab.max_x)
            touch = [
                any(
                    abs(inst.bounds[2] - x) < 1e-6 or abs(inst.bounds[0] - x) < 1e-6
                    for inst in panel.instances
                )
                or (inner is not None and (abs(inner[0] - x) < 1e-6 or abs(inner[2] - x) < 1e-6))
                for x in ends
            ]
        assert all(touch), f"tab {tab} has a free end"


def test_outline_is_one_region_with_frame() -> None:
    """With a frame, every board hangs off the rail: one connected polygon."""
    region = _panel(FIXTURE, frame=True).outline_geometry()
    assert region.geom_type == "Polygon"
    assert region.is_valid


def test_outline_is_one_region_without_frame() -> None:
    """Tabs join the 2x2 copies into one connected polygon."""
    region = _panel(FIXTURE).outline_geometry()
    assert region.geom_type == "Polygon"
    assert region.is_valid


def test_vcut_lines_not_on_edge_cuts() -> None:
    panel = _panel(FIXTURE, cut=CutMethod.VCUT)
    root = panel.build()
    scores = [c for c in root.children if c.name == "gr_line" and _layer(c) == "Cmts.User"]
    assert len(scores) == 2
    layers = root.find_child("layers")
    names = {c.get_string(0) for c in layers.children if not c.is_atom}
    assert "Cmts.User" in names


class TestTabExtents:
    def test_side_by_side_tab_spans_exactly_the_gap(self) -> None:
        tabs = compute_tabs_between_boards(
            (0, 0, 20, 30), (22, 0, 42, 30), TabConfig(width=3.0, count=1), "horizontal"
        )
        (tab,) = tabs
        assert (tab.min_x, tab.max_x) == pytest.approx((20.0, 22.0))
        assert tab.max_y - tab.min_y == pytest.approx(3.0)

    def test_stacked_tab_spans_exactly_the_gap(self) -> None:
        tabs = compute_tabs_between_boards(
            (0, 0, 20, 30), (0, 32, 20, 62), TabConfig(width=3.0, count=1), "vertical"
        )
        (tab,) = tabs
        assert (tab.min_y, tab.max_y) == pytest.approx((30.0, 32.0))
        assert tab.max_x - tab.min_x == pytest.approx(3.0)

    def test_frame_side_tabs_span_exactly_the_gap(self) -> None:
        tabs = compute_tabs_to_frame((10, 10, 30, 40), (5, 5, 35, 45), TabConfig(count=1))
        left = [t for t in tabs if t.orientation == "vertical" and t.x < 10]
        right = [t for t in tabs if t.orientation == "vertical" and t.x > 30]
        assert (left[0].min_x, left[0].max_x) == pytest.approx((5.0, 10.0))
        assert (right[0].min_x, right[0].max_x) == pytest.approx((30.0, 35.0))
        assert left[0].max_y - left[0].min_y == pytest.approx(3.0)
