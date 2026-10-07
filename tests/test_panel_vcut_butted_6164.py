"""V-cut panels are butted, not gapped and tabbed (Issue #6164).

``kct panel --cut vcut`` used to lay copies out 2 mm apart with tabs in
every gap, then draw each score line down the middle of the empty routed
slot.  Fabs V-score only boards that are butted edge to edge, so the score
crossed mostly air and the fab was asked to both rout and score the same
seam.  V-cut panels now default to 0 mm spacing (and a butted rail) and
get no tabs.  The score lines lie exactly on the shared edges.  A seam
that keeps a gap is tab-routed with mousebites and reported.
"""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from pathlib import Path

import pytest

pytest.importorskip("shapely", reason="Shapely required for panel tests")

from kicad_tools.cli.commands.panel import run_panel_command  # noqa: E402
from kicad_tools.cli.parser import create_parser  # noqa: E402
from kicad_tools.export.gerber import pcb_vscore_layers  # noqa: E402
from kicad_tools.panel import CutMethod, PanelConfig  # noqa: E402
from kicad_tools.panel.config import FrameConfig, VCutConfig  # noqa: E402
from kicad_tools.panel.panel import Panel  # noqa: E402
from kicad_tools.panel.vscore import rotate_side, source_side_for  # noqa: E402
from kicad_tools.sexp.parser import SExp  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests" / "fixtures" / "projects" / "test_project.kicad_pcb"
BOARD01 = REPO / "boards" / "01-voltage-divider" / "output" / "voltage_divider_routed.kicad_pcb"

needs_board01 = pytest.mark.skipif(not BOARD01.exists(), reason="board 01 output missing")


def _layer(node: SExp) -> str | None:
    layer = node.find_child("layer")
    return layer.get_string(0) if layer is not None else None


def _lines(root: SExp, layer: str) -> list[tuple[float, float, float, float]]:
    out = []
    for child in root.children:
        if child.name == "gr_line" and _layer(child) == layer:
            s, e = child.find_child("start"), child.find_child("end")
            out.append((s.get_float(0), s.get_float(1), e.get_float(0), e.get_float(1)))
    return out


def _keepouts(root: SExp) -> list[SExp]:
    return [c for c in root.children if c.name == "zone" and c.find_child("keepout")]


def _mousebites(root: SExp) -> int:
    return sum(
        1
        for c in root.children
        if c.name == "footprint" and c.children and c.children[0].value == "Panel:Mousebite"
    )


def _panel(source: Path = FIXTURE, **kw) -> Panel:
    frame = kw.pop("frame", None)
    cfg = PanelConfig(rows=2, cols=2, cut_method=CutMethod.VCUT, frame=frame, **kw)
    return Panel.from_config(source, cfg)


def _assert_closed_loops(root: SExp) -> None:
    degree: Counter[tuple[float, float]] = Counter()
    for x0, y0, x1, y1 in _lines(root, "Edge.Cuts"):
        degree[(round(x0, 4), round(y0, 4))] += 1
        degree[(round(x1, 4), round(y1, 4))] += 1
    assert degree
    assert all(d == 2 for d in degree.values()), degree


def _board_size(panel: Panel) -> tuple[float, float]:
    x0, y0, x1, y1 = panel.instances[0].bounds
    return x1 - x0, y1 - y0


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------


def test_vcut_spacing_defaults_to_zero() -> None:
    assert PanelConfig(cut_method=CutMethod.VCUT).resolved_spacing() == (0.0, 0.0)
    assert PanelConfig().resolved_spacing() == (2.0, 2.0)
    assert PanelConfig(cut_method=CutMethod.VCUT, spacing=1.5).resolved_spacing() == (1.5, 1.5)
    mixed = PanelConfig(cut_method=CutMethod.VCUT, spacing_x=2.0)
    assert mixed.resolved_spacing() == (2.0, 0.0)


def test_frame_space_defaults_follow_cut_method() -> None:
    assert FrameConfig().resolved_space(CutMethod.VCUT) == 0.0
    assert FrameConfig().resolved_space(CutMethod.MOUSEBITE) == 2.0
    assert FrameConfig(space=3.0).resolved_space(CutMethod.VCUT) == 3.0


# ---------------------------------------------------------------------------
# Butted 2x2
# ---------------------------------------------------------------------------


def test_butted_2x2_has_no_tabs_slots_or_mousebites() -> None:
    panel = _panel()
    root = panel.build()
    assert panel.tabs == []
    assert _mousebites(root) == 0
    region = panel.outline_geometry()
    assert region.geom_type == "Polygon"
    assert region.is_valid
    assert not list(region.interiors), "a butted panel has no internal routed slot"
    # One rectangle: four Edge.Cuts segments.
    assert len(_lines(root, "Edge.Cuts")) == 4
    _assert_closed_loops(root)


def test_butted_2x2_scores_lie_on_shared_edges() -> None:
    panel = _panel()
    root = panel.build()
    w, h = _board_size(panel)
    scores = _lines(root, "Cmts.User")
    assert len(scores) == 2
    horizontal = [s for s in scores if s[1] == s[3]]
    vertical = [s for s in scores if s[0] == s[2]]
    assert [s[1] for s in horizontal] == pytest.approx([h])
    assert [s[0] for s in vertical] == pytest.approx([w])
    # Each score is an edge shared by two copies.
    for inst in panel.instances:
        x0, y0, x1, y1 = inst.bounds
        assert any(abs(y - h) < 1e-6 for y in (y0, y1))
        assert any(abs(x - w) < 1e-6 for x in (x0, x1))
    assert panel.warnings == [] or all("V-score clearance" in w for w in panel.warnings)


def test_butted_frame_is_scored_off_the_rails() -> None:
    panel = _panel(frame=FrameConfig())
    root = panel.build()
    w, h = _board_size(panel)
    assert panel.tabs == []
    region = panel.outline_geometry()
    assert region.geom_type == "Polygon" and not list(region.interiors)
    assert len(_lines(root, "Edge.Cuts")) == 4
    scores = _lines(root, "Cmts.User")
    ys = sorted(s[1] for s in scores if s[1] == s[3])
    xs = sorted(s[0] for s in scores if s[0] == s[2])
    assert ys == pytest.approx([0.0, h, 2 * h])
    assert xs == pytest.approx([0.0, w, 2 * w])
    # Scores run edge to edge across the rails.
    px0, py0, px1, py1 = panel.panel_bounds
    for x0, y0, x1, y1 in scores:
        if y0 == y1:
            assert (min(x0, x1), max(x0, x1)) == pytest.approx((px0, px1))
        else:
            assert (min(y0, y1), max(y0, y1)) == pytest.approx((py0, py1))


def test_explicit_frame_space_tabs_the_rails() -> None:
    panel = _panel(frame=FrameConfig(space=2.0))
    root = panel.build()
    assert panel.tabs, "a gapped rail needs tabs"
    assert _mousebites(root) > 0
    assert len(_lines(root, "Cmts.User")) == 2  # board seams only
    assert any("tab-routed" in w for w in panel.warnings)
    _assert_closed_loops(root)


# ---------------------------------------------------------------------------
# Gapped override and mixed mode
# ---------------------------------------------------------------------------


def test_spacing_override_is_tab_routed_and_warns() -> None:
    panel = _panel(spacing=2.0)
    root = panel.build()
    assert panel.tabs
    assert _mousebites(root) > 0
    assert _lines(root, "Cmts.User") == [], "no score across an empty slot"
    assert any("tab-routed" in w for w in panel.warnings)
    assert any("no butted seams" in w for w in panel.warnings)


def test_mixed_panel_scores_rows_and_tabs_columns() -> None:
    panel = _panel(spacing_x=2.0)  # columns gapped, rows butted
    root = panel.build()
    _, h = _board_size(panel)
    # Every tab bridges the column gap (it spans along X).
    assert panel.tabs
    assert all(t.orientation == "vertical" for t in panel.tabs)
    assert _mousebites(root) > 0
    scores = _lines(root, "Cmts.User")
    assert len(scores) == 1
    (x0, y0, x1, y1) = scores[0]
    assert y0 == y1 == pytest.approx(h)
    region = panel.outline_geometry()
    assert region.geom_type == "Polygon" and region.is_valid
    _assert_closed_loops(root)


def test_low_level_vcuts_skip_gapped_seams() -> None:
    panel = Panel().append_board(FIXTURE, rows=2, cols=2, spacing=2.0)
    panel.make_tabs().make_vcuts()
    root = panel.build()
    assert _lines(root, "Cmts.User") == []


# ---------------------------------------------------------------------------
# Copper clearance to the score
# ---------------------------------------------------------------------------


def test_keepout_flanks_every_score() -> None:
    root = _panel().build()
    scores = _lines(root, "Cmts.User")
    keepouts = _keepouts(root)
    assert len(keepouts) == len(scores)
    for zone in keepouts:
        assert zone.find_child("layers").get_string(0) == "*.Cu"
        rule = zone.find_child("keepout")
        assert rule.find_child("copperpour").get_string(0) == "not_allowed"
        assert rule.find_child("tracks").get_string(0) == "allowed"


def test_zero_clearance_disables_keepout_and_check() -> None:
    panel = _panel(vcut=VCutConfig(clearance=0.0))
    root = panel.build()
    assert _keepouts(root) == []
    assert panel.vscore_findings == []


@needs_board01
def test_board01_zone_fill_too_close_to_score_is_reported() -> None:
    panel = _panel(BOARD01)
    panel.build()
    findings = panel.vscore_findings
    assert {f.source_side for f in findings} == {"left", "top", "right", "bottom"}
    for f in findings:
        assert f.gap == pytest.approx(0.3, abs=1e-3)
        assert f.required == 0.4
        assert f.item.startswith("zone fill")
    # Interior seams only: each source edge is scored on two of the four copies.
    assert all(len(f.copies) == 2 for f in findings)


@needs_board01
def test_board01_meets_a_looser_clearance() -> None:
    panel = _panel(BOARD01, vcut=VCutConfig(clearance=0.3))
    panel.build()
    assert panel.vscore_findings == []


_EDGE_TRACK_BOARD = """(kicad_pcb (version 20240108) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "")
  (net 1 "SIG")
  (gr_rect (start 0 0) (end 20 10) (layer "Edge.Cuts") (width 0.1))
  (segment (start 2 9.8) (end 18 9.8) (width 0.2) (layer "F.Cu") (net 1))
)
"""


def test_rotation_maps_scored_edge_back_to_source(tmp_path: Path) -> None:
    """Copper near the source *bottom* edge lands on the copy's right under 90 deg."""
    board = tmp_path / "edge.kicad_pcb"
    board.write_text(_EDGE_TRACK_BOARD)
    cfg = PanelConfig(rows=1, cols=2, cut_method=CutMethod.VCUT, rotation=90)
    panel = Panel.from_config(board, cfg)
    panel.build()
    # Only the vertical seam is scored: copy 0's right edge, copy 1's left.
    (finding,) = panel.vscore_findings
    assert finding.source_side == "bottom"
    assert finding.copies == (0,)
    assert finding.gap == pytest.approx(0.1, abs=1e-3)
    assert "move the copper" in finding.message()


@pytest.mark.parametrize("rotation", [0.0, 90.0, 180.0, 270.0])
def test_rotate_side_round_trips(rotation: float) -> None:
    for side in ("left", "top", "right", "bottom"):
        assert source_side_for(rotate_side(side, rotation), rotation) == side
    assert rotate_side("left", 90.0) == "bottom"  # CCW on screen
    assert rotate_side("bottom", 90.0) == "right"


# ---------------------------------------------------------------------------
# Export and CLI
# ---------------------------------------------------------------------------


def test_butted_panel_still_exports_score_layer(tmp_path: Path) -> None:
    out = _panel().save(tmp_path / "panel.kicad_pcb")
    assert pcb_vscore_layers(out) == ["Cmts.User"]


def test_cli_spacing_defaults() -> None:
    parser = create_parser()
    args = parser.parse_args(["panel", str(FIXTURE)])
    assert args.panel_spacing is None
    assert args.panel_frame_space is None
    assert args.panel_vscore_clearance == 0.4
    args = parser.parse_args(["panel", str(FIXTURE), "--spacing-x", "2", "--spacing-y", "0"])
    assert (args.panel_spacing_x, args.panel_spacing_y) == (2.0, 0.0)


@pytest.mark.parametrize(
    "extra,spacing",
    [([], 0.0), (["--spacing", "1.5"], 1.5)],
    ids=["default", "override"],
)
def test_cli_vcut_json(tmp_path: Path, capsys, extra: list[str], spacing: float) -> None:
    out = tmp_path / "panel.kicad_pcb"
    parser = create_parser()
    args = parser.parse_args(
        ["panel", str(FIXTURE), "-o", str(out), "--cut", "vcut", "--format", "json", *extra]
    )
    assert run_panel_command(args) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["grid"]["spacing_mm"] == spacing
    assert doc["vscore_clearance_mm"] == 0.4
    assert isinstance(doc["warnings"], list)
    if spacing:
        assert doc["tabs"] > 0
        assert any("tab-routed" in w for w in doc["warnings"])
    else:
        assert doc["tabs"] == 0


def test_cli_mousebite_keeps_2mm_gap(tmp_path: Path, capsys) -> None:
    out = tmp_path / "panel.kicad_pcb"
    args = create_parser().parse_args(["panel", str(FIXTURE), "-o", str(out), "--format", "json"])
    assert run_panel_command(args) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["grid"]["spacing_mm"] == 2.0
    assert doc["vscore_clearance_mm"] is None
    assert doc["warnings"] == []


# ---------------------------------------------------------------------------
# kicad-cli: the butted outline is valid
# ---------------------------------------------------------------------------


@needs_board01
@pytest.mark.parametrize("frame", [None, FrameConfig()], ids=["no-frame", "frame"])
def test_kicad_cli_accepts_butted_outline(tmp_path: Path, frame) -> None:
    from kicad_tools.cli.runner import find_kicad_cli

    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("kicad-cli not installed")
    pcb = _panel(BOARD01, frame=frame).save(tmp_path / "panel.kicad_pcb")
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
    assert not doc.get("unconnected_items"), "butting copies must not break nets"
