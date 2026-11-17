"""Panel copies keep the source's real Edge.Cuts outline (Issue #6158).

``Panel`` used to drop every source Edge.Cuts graphic and outline each
copy with its bounding-box rectangle, so a board with rounded corners was
panelised -- and routed by the fab -- with square ones.  The panel outline
is now the union of each copy's actual outline with the tabs and rail,
and the union's faceted arcs are folded back into real ``gr_arc``s.
"""

from __future__ import annotations

import json
import math
import subprocess
from collections import Counter
from pathlib import Path

import pytest

pytest.importorskip("shapely", reason="Shapely required for panel tests")

from kicad_tools.panel import CutMethod, PanelConfig  # noqa: E402
from kicad_tools.panel.config import FrameConfig, TabConfig  # noqa: E402
from kicad_tools.panel.outline import (  # noqa: E402
    ArcRegistry,
    OutlinePrimitive,
    arc_geometry,
    parse_edge_cuts,
    render_ring,
)
from kicad_tools.panel.panel import Panel  # noqa: E402
from kicad_tools.sexp.parser import SExp, parse_string  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests" / "fixtures" / "projects" / "test_project.kicad_pcb"

# The fixture's outline is ``(gr_rect (start 95 35) (end 155 65))``.
X0, Y0, X1, Y1 = 95.0, 35.0, 155.0, 65.0
BOARD_W, BOARD_H = X1 - X0, Y1 - Y0
_RECT = """(gr_rect (start 95 35) (end 155 65)
		(stroke (width 0.1) (type default))
		(fill none)
		(layer "Edge.Cuts")
		(uuid "edge-rect-uuid")
	)"""


def _gr(kind: str, **pts: tuple[float, float]) -> str:
    body = " ".join(f"({k} {x:.6f} {y:.6f})" for k, (x, y) in pts.items())
    return f'({kind} {body} (stroke (width 0.1) (type default)) (layer "Edge.Cuts"))'


def _rounded_outline(r: float) -> str:
    """Edge.Cuts of the fixture board with corner radius *r* (lines + arcs)."""
    k = r * (1 - math.sqrt(0.5))
    items = [
        _gr("gr_line", start=(X0 + r, Y0), end=(X1 - r, Y0)),
        _gr("gr_arc", start=(X1 - r, Y0), mid=(X1 - k, Y0 + k), end=(X1, Y0 + r)),
        _gr("gr_line", start=(X1, Y0 + r), end=(X1, Y1 - r)),
        _gr("gr_arc", start=(X1, Y1 - r), mid=(X1 - k, Y1 - k), end=(X1 - r, Y1)),
        _gr("gr_line", start=(X1 - r, Y1), end=(X0 + r, Y1)),
        _gr("gr_arc", start=(X0 + r, Y1), mid=(X0 + k, Y1 - k), end=(X0, Y1 - r)),
        _gr("gr_line", start=(X0, Y1 - r), end=(X0, Y0 + r)),
        _gr("gr_arc", start=(X0, Y0 + r), mid=(X0 + k, Y0 + k), end=(X0 + r, Y0)),
    ]
    return "\n\t".join(items)


def _board(tmp_path: Path, r: float = 2.0, extra: str = "", name: str = "rounded") -> Path:
    text = FIXTURE.read_text()
    assert _RECT in text
    out = tmp_path / f"{name}.kicad_pcb"
    out.write_text(text.replace(_RECT, _rounded_outline(r) + ("\n\t" + extra if extra else "")))
    return out


def _layer(node: SExp) -> str | None:
    layer = node.find_child("layer")
    return layer.get_string(0) if layer is not None else None


def _pt(node: SExp, tag: str) -> tuple[float, float]:
    child = node.find_child(tag)
    return (round(child.get_float(0), 4), round(child.get_float(1), 4))


def _edge(root: SExp, kind: str) -> list[SExp]:
    return [c for c in root.children if c.name == kind and _layer(c) == "Edge.Cuts"]


def _assert_closed(root: SExp) -> None:
    """Every Edge.Cuts endpoint (lines and arcs) is shared by exactly two."""
    degree: Counter[tuple[float, float]] = Counter()
    for node in _edge(root, "gr_line") + _edge(root, "gr_arc"):
        a, b = _pt(node, "start"), _pt(node, "end")
        assert a != b, "zero-length Edge.Cuts graphic"
        degree[a] += 1
        degree[b] += 1
    assert degree, "panel has no Edge.Cuts"
    bad = {p: d for p, d in degree.items() if d != 2}
    assert not bad, f"Edge.Cuts endpoints not at degree 2: {bad}"


def _radius(node: SExp) -> float:
    geo = arc_geometry(_pt(node, "start"), _pt(node, "mid"), _pt(node, "end"))
    assert geo is not None
    return geo.radius


def _panel(source: Path, **kw) -> Panel:
    cfg = PanelConfig(rows=2, cols=2, **kw)
    return Panel.from_config(source, cfg)


# ---------------------------------------------------------------------------
# Core: rounded corners survive as real arcs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("frame", [None, FrameConfig()], ids=["no-frame", "frame"])
def test_rounded_corners_kept_as_arcs(tmp_path: Path, frame) -> None:
    panel = _panel(_board(tmp_path), frame=frame)
    assert panel.tabs
    root = panel.build()
    arcs = _edge(root, "gr_arc")
    assert len(arcs) == 16, "four corner arcs per copy"
    for arc in arcs:
        assert _radius(arc) == pytest.approx(2.0, abs=1e-3)
    _assert_closed(root)
    assert panel.warnings == []


def test_arcs_are_the_source_arcs_moved_rigidly(tmp_path: Path) -> None:
    panel = _panel(_board(tmp_path))
    root = panel.build()
    got = {(_pt(a, "start"), _pt(a, "mid"), _pt(a, "end")) for a in _edge(root, "gr_arc")}
    got = {frozenset((s, e)) | {m} for s, m, e in got}
    want = set()
    for inst in panel.instances:
        for prim in panel._copy_outline(inst):
            if prim.kind == "arc":
                s, m, e = (tuple(round(v, 4) for v in p) for p in prim.points)
                want.add(frozenset((s, e)) | {m})
    assert got == want


def test_copy_region_matches_rounded_area(tmp_path: Path) -> None:
    panel = _panel(_board(tmp_path, r=2.0))
    region = panel.outline_geometry()
    assert region.geom_type == "Polygon" and region.is_valid
    copy_area = BOARD_W * BOARD_H - (4 - math.pi) * 2.0**2
    tab_area = sum((t.max_x - t.min_x) * (t.max_y - t.min_y) for t in panel.tabs)
    # Arcs are faceted to within 1 um for the union, so allow a sliver.
    assert region.area == pytest.approx(4 * copy_area + tab_area, abs=0.05)
    # The bounding-box outline would have been 4 * (4 - pi) * r^2 larger.
    assert region.area < 4 * BOARD_W * BOARD_H + tab_area - 4 * (4 - math.pi) * 4.0 + 0.05


def test_rotated_copies_keep_arcs(tmp_path: Path) -> None:
    panel = _panel(_board(tmp_path), rotation=90)
    root = panel.build()
    assert len(_edge(root, "gr_arc")) == 16
    _assert_closed(root)


def test_rectangular_board_has_no_arcs() -> None:
    root = _panel(FIXTURE).build()
    assert not _edge(root, "gr_arc")
    _assert_closed(root)


def test_cutout_is_kept(tmp_path: Path) -> None:
    """An interior Edge.Cuts cutout (a gr_circle) appears in every copy."""
    hole = '(gr_circle (center 140 50) (end 142 50) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))'
    root = _panel(_board(tmp_path, extra=hole)).build()
    arcs = _edge(root, "gr_arc")
    # 4 corners + a circle as two half arcs, per copy.
    assert len(arcs) == 4 * (4 + 2)
    _assert_closed(root)


# ---------------------------------------------------------------------------
# Tabs stay on straight edges
# ---------------------------------------------------------------------------


def test_tabs_slide_off_curved_corners(tmp_path: Path) -> None:
    """A big corner radius: tabs that would hit the arc move onto the flat."""
    r = 12.0
    cfg = PanelConfig(rows=2, cols=2, tabs=TabConfig(width=3.0, count=3))
    panel = Panel.from_config(_board(tmp_path, r=r), cfg)
    assert panel.tabs
    for tab in panel.tabs:
        if tab.orientation == "horizontal":  # attaches along X
            inst = next(
                i
                for i in panel.instances
                if abs(i.bounds[3] - tab.min_y) < 1e-6 and i.bounds[0] <= tab.x <= i.bounds[2]
            )
            lo, hi = inst.bounds[0] + r, inst.bounds[2] - r
            assert lo - 1e-6 <= tab.min_x and tab.max_x <= hi + 1e-6
        else:
            inst = next(
                i
                for i in panel.instances
                if abs(i.bounds[2] - tab.min_x) < 1e-6 and i.bounds[1] <= tab.y <= i.bounds[3]
            )
            lo, hi = inst.bounds[1] + r, inst.bounds[3] - r
            assert lo - 1e-6 <= tab.min_y and tab.max_y <= hi + 1e-6
    root = panel.build()
    assert len(_edge(root, "gr_arc")) == 16, "no tab clips a corner arc"
    _assert_closed(root)


def test_tab_dropped_when_no_straight_run_fits(tmp_path: Path) -> None:
    # Radius 14 leaves a 2 mm flat on the 30 mm sides: a 3 mm tab cannot fit.
    cfg = PanelConfig(rows=2, cols=2, tabs=TabConfig(width=3.0, count=1))
    panel = Panel.from_config(_board(tmp_path, r=14.0), cfg)
    root = panel.build()
    assert any("Dropped" in w for w in panel.warnings)
    assert all(t.orientation == "horizontal" for t in panel.tabs)
    _assert_closed(root)


# ---------------------------------------------------------------------------
# V-cut: rounded boards cannot be butted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("frame", [None, FrameConfig()], ids=["no-frame", "frame"])
def test_vcut_rounded_falls_back_to_tabs(tmp_path: Path, frame) -> None:
    panel = _panel(_board(tmp_path), cut_method=CutMethod.VCUT, frame=frame)
    insts = {(i.row, i.col): i for i in panel.instances}
    assert insts[(0, 1)].bounds[0] - insts[(0, 0)].bounds[2] == pytest.approx(2.0)
    assert insts[(1, 0)].bounds[1] - insts[(0, 0)].bounds[3] == pytest.approx(2.0)
    assert panel.tabs, "gapped seams are tab-routed"
    root = panel.build()
    assert any("cannot be butted" in w for w in panel.warnings)
    scores = [c for c in root.children if c.name == "gr_line" and _layer(c) == "Cmts.User"]
    assert not scores
    assert len(_edge(root, "gr_arc")) == 16
    _assert_closed(root)


def test_cli_json_reports_the_fallback_gap(tmp_path: Path, capsys) -> None:
    from kicad_tools.cli.commands.panel import run_panel_command
    from kicad_tools.cli.parser import create_parser

    out = tmp_path / "panel.kicad_pcb"
    args = create_parser().parse_args(
        ["panel", str(_board(tmp_path)), "-o", str(out), "--cut", "vcut", "--frame"]
        + ["--format", "json"]
    )
    assert run_panel_command(args) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["grid"]["spacing_mm"] == 2.0
    assert doc["frame_space_mm"] == 2.0
    assert doc["tabs"] > 0
    assert any("cannot be butted" in w for w in doc["warnings"])


def test_vcut_rectangular_still_butted() -> None:
    panel = _panel(FIXTURE, cut_method=CutMethod.VCUT)
    insts = {(i.row, i.col): i for i in panel.instances}
    assert insts[(0, 1)].bounds[0] == pytest.approx(insts[(0, 0)].bounds[2])
    panel.build()
    assert not any("cannot be butted" in w for w in panel.warnings)


# ---------------------------------------------------------------------------
# Outline module units
# ---------------------------------------------------------------------------


def test_parse_rounded_gr_rect() -> None:
    root = parse_string(
        '(kicad_pcb (gr_rect (start 0 0) (end 10 6) (radius 1) (layer "Edge.Cuts")))'
    )
    prims = parse_edge_cuts(root)
    assert Counter(p.kind for p in prims) == {"line": 4, "arc": 4}


def test_parse_gr_poly_with_arc() -> None:
    root = parse_string(
        "(kicad_pcb (gr_poly (pts (xy 0 0) (xy 10 0) (arc (start 10 0) (mid 12 3) (end 10 6))"
        ' (xy 0 6)) (layer "Edge.Cuts")))'
    )
    prims = parse_edge_cuts(root)
    assert Counter(p.kind for p in prims) == {"line": 3, "arc": 1}


def test_clipped_arc_renders_as_sub_arc() -> None:
    """A run covering part of an arc becomes an arc on the same circle."""
    registry = ArcRegistry()
    arc = OutlinePrimitive("arc", ((0.0, 0.0), (5.0, 5.0), (10.0, 0.0)))
    pts = registry.facet(arc)
    ring = [*pts[1:-1], pts[1]]  # drop the first and last facet, close with a chord
    nodes = render_ring(ring, registry)
    kinds = Counter(n.name for n in nodes)
    assert kinds == {"gr_arc": 1, "gr_line": 1}
    (sub,) = [n for n in nodes if n.name == "gr_arc"]
    assert _radius(sub) == pytest.approx(5.0, abs=1e-4)


# ---------------------------------------------------------------------------
# kicad-cli: the rounded outline is valid
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cut,frame",
    [(CutMethod.MOUSEBITE, None), (CutMethod.MOUSEBITE, FrameConfig()), (CutMethod.VCUT, None)],
    ids=["tabs", "tabs-frame", "vcut-fallback"],
)
def test_kicad_cli_accepts_rounded_outline(tmp_path: Path, cut, frame) -> None:
    from kicad_tools.cli.runner import find_kicad_cli

    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("kicad-cli not installed")
    pcb = _panel(_board(tmp_path), cut_method=cut, frame=frame).save(tmp_path / "panel.kicad_pcb")
    report = tmp_path / "drc.json"
    subprocess.run(
        [str(cli), "pcb", "drc", "--refill-zones", "--format", "json", "-o", str(report), str(pcb)],
        check=True,
        capture_output=True,
        timeout=300,
    )
    doc = json.loads(report.read_text())
    types = Counter(v["type"] for v in doc["violations"])
    assert types["invalid_outline"] == 0, types
