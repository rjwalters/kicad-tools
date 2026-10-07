"""V-cut panels ship their score lines in the Gerber set (Issue #6156).

#6143 moved ``kct panel --cut vcut`` score lines off Edge.Cuts (where an open
line breaks the outline) onto ``Cmts.User``.  ``GerberExporter`` never plotted
that layer, so ``kct export`` of a V-cut panel sent the fab no V-score data at
all.  The exporter now detects user-layer lines that span the board outline
and plots their layer; ``kct panel --vcut-layer`` picks the layer.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytest.importorskip("shapely", reason="Shapely required for panel tests")

from kicad_tools.cli.parser import create_parser  # noqa: E402
from kicad_tools.export.gerber import (  # noqa: E402
    GerberConfig,
    GerberExporter,
    pcb_vscore_layers,
)
from kicad_tools.panel import CutMethod, PanelConfig  # noqa: E402
from kicad_tools.panel.config import FrameConfig, MousebiteConfig, VCutConfig  # noqa: E402
from kicad_tools.panel.cuts import generate_mousebite_holes  # noqa: E402
from kicad_tools.panel.panel import Panel  # noqa: E402
from kicad_tools.panel.tabs import Tab  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests" / "fixtures" / "projects" / "test_project.kicad_pcb"


def _save_panel(tmp_path: Path, *, cut: CutMethod, layer: str = "Cmts.User", frame=False) -> Path:
    cfg = PanelConfig(
        rows=2,
        cols=2,
        cut_method=cut,
        vcut=VCutConfig(layer=layer),
        frame=FrameConfig() if frame else None,
    )
    return Panel.from_config(FIXTURE, cfg).save(tmp_path / "panel.kicad_pcb")


def _make_exporter(pcb_path: Path) -> GerberExporter:
    with patch.object(GerberExporter, "__init__", lambda self, path: None):
        exporter = GerberExporter.__new__(GerberExporter)
        exporter.pcb_path = pcb_path
        exporter.kicad_cli = Path("/usr/bin/kicad-cli")
        return exporter


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("frame", [False, True], ids=["no-frame", "frame"])
def test_vcut_panel_reports_its_score_layer(tmp_path: Path, frame: bool) -> None:
    pcb = _save_panel(tmp_path, cut=CutMethod.VCUT, frame=frame)
    assert pcb_vscore_layers(pcb) == ["Cmts.User"]


def test_mousebite_panel_has_no_score_layer(tmp_path: Path) -> None:
    pcb = _save_panel(tmp_path, cut=CutMethod.MOUSEBITE)
    assert pcb_vscore_layers(pcb) == []


def test_source_board_has_no_score_layer() -> None:
    assert pcb_vscore_layers(FIXTURE) == []


_NOTE_BOARD = """(kicad_pcb
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user)
    (19 "Cmts.User" user "User.Comments"))
  (gr_rect (start 0 0) (end 40 30) (layer "Edge.Cuts"))
  (gr_line (start 5 5) (end 20 5) (layer "Cmts.User"))
  (gr_line (start 0 10) (end 40 12) (layer "Cmts.User"))
  {extra}
)
"""


def test_comment_lines_that_do_not_span_the_outline_are_ignored(tmp_path: Path) -> None:
    """A note or a slanted line on Cmts.User does not add a Gerber."""
    pcb = tmp_path / "notes.kicad_pcb"
    pcb.write_text(_NOTE_BOARD.format(extra=""))
    assert pcb_vscore_layers(pcb) == []


def test_full_span_line_on_user_layer_is_a_score(tmp_path: Path) -> None:
    pcb = tmp_path / "scored.kicad_pcb"
    pcb.write_text(
        _NOTE_BOARD.format(extra='(gr_line (start 20 30) (end 20 0) (layer "Dwgs.User"))')
    )
    assert pcb_vscore_layers(pcb) == ["Dwgs.User"]


def test_edge_cuts_lines_are_never_a_score_layer(tmp_path: Path) -> None:
    pcb = tmp_path / "edge.kicad_pcb"
    pcb.write_text(
        _NOTE_BOARD.format(extra='(gr_line (start 0 15) (end 40 15) (layer "Edge.Cuts"))')
    )
    assert pcb_vscore_layers(pcb) == []


def test_missing_file_has_no_score_layer(tmp_path: Path) -> None:
    assert pcb_vscore_layers(tmp_path / "missing.kicad_pcb") == []


# ---------------------------------------------------------------------------
# Layer selection
# ---------------------------------------------------------------------------


def test_default_layers_include_score_layer_for_vcut_panel(tmp_path: Path) -> None:
    pcb = _save_panel(tmp_path, cut=CutMethod.VCUT)
    layers = _make_exporter(pcb)._get_default_layers(GerberConfig())
    assert "Edge.Cuts" in layers
    assert layers[-1] == "Cmts.User"


def test_default_layers_unchanged_for_plain_board() -> None:
    layers = _make_exporter(FIXTURE)._get_default_layers(GerberConfig())
    assert "Cmts.User" not in layers


def test_include_vscore_false_opts_out(tmp_path: Path) -> None:
    pcb = _save_panel(tmp_path, cut=CutMethod.VCUT)
    layers = _make_exporter(pcb)._get_default_layers(GerberConfig(include_vscore=False))
    assert "Cmts.User" not in layers


# ---------------------------------------------------------------------------
# kct panel --vcut-layer
# ---------------------------------------------------------------------------


def test_parser_vcut_layer_default_and_choice() -> None:
    parser = create_parser()
    assert parser.parse_args(["panel", str(FIXTURE)]).panel_vcut_layer == "Cmts.User"
    args = parser.parse_args(["panel", str(FIXTURE), "--cut", "vcut", "--vcut-layer", "Dwgs.User"])
    assert args.panel_vcut_layer == "Dwgs.User"
    with pytest.raises(SystemExit):
        parser.parse_args(["panel", str(FIXTURE), "--vcut-layer", "Edge.Cuts"])


@pytest.mark.parametrize("layer", ["Dwgs.User", "Eco1.User", "Eco2.User"])
def test_cli_vcut_layer_reaches_the_board(tmp_path: Path, layer: str, capsys) -> None:
    from kicad_tools.cli.commands.panel import run_panel_command

    out = tmp_path / "nested" / "panel.kicad_pcb"
    args = create_parser().parse_args(
        [
            "panel",
            str(FIXTURE),
            "-o",
            str(out),
            "--cut",
            "vcut",
            "--vcut-layer",
            layer,
            "--format",
            "json",
        ]
    )
    assert run_panel_command(args) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["vcut_layer"] == layer

    text = out.read_text()
    assert f'"{layer}"' in text  # declared in the layer table
    assert pcb_vscore_layers(out) == [layer]


# ---------------------------------------------------------------------------
# Mousebite end holes stay inside the tab
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("orientation", ["horizontal", "vertical"])
@pytest.mark.parametrize("offset", [0.0, 0.25])
def test_mousebite_end_holes_do_not_overlap_the_slot(orientation: str, offset: float) -> None:
    tab = Tab(x=10, y=10, width=3.0, height=2.0, orientation=orientation)
    cfg = MousebiteConfig(diameter=0.5, spacing=0.8, offset=offset)
    holes = generate_mousebite_holes(tab, cfg)
    assert len(holes) >= 2
    r = cfg.diameter / 2.0
    if orientation == "horizontal":
        lo, hi = tab.min_x, tab.max_x
        pos = [h.x for h in holes]
    else:
        lo, hi = tab.min_y, tab.max_y
        pos = [h.y for h in holes]
    assert min(pos) - r == pytest.approx(lo + offset)
    assert max(pos) + r == pytest.approx(hi - offset)


def test_mousebite_tab_exactly_one_hole_wide() -> None:
    tab = Tab(x=0, y=0, width=0.5, height=2.0, orientation="horizontal")
    holes = generate_mousebite_holes(tab, MousebiteConfig(diameter=0.5))
    assert [(h.x, h.y) for h in holes] == [(0.0, 0.0)]


def test_mousebite_tab_narrower_than_a_hole_gets_none() -> None:
    tab = Tab(x=0, y=0, width=0.4, height=2.0, orientation="horizontal")
    assert generate_mousebite_holes(tab, MousebiteConfig(diameter=0.5)) == []


# ---------------------------------------------------------------------------
# End to end with kicad-cli
# ---------------------------------------------------------------------------


def _kicad_cli() -> Path | None:
    from kicad_tools.cli.runner import find_kicad_cli

    return find_kicad_cli()


@pytest.mark.skipif(_kicad_cli() is None, reason="kicad-cli not installed")
def test_exported_gerbers_carry_the_score_lines(tmp_path: Path) -> None:
    pcb = _save_panel(tmp_path, cut=CutMethod.VCUT)
    out = tmp_path / "gerbers"
    config = GerberConfig(create_zip=False, generate_drill=False, verify_zone_fill=False)
    GerberExporter(pcb).export(config, out)

    comments = list(out.glob("*-User_Comments.gbr"))
    assert len(comments) == 1, sorted(p.name for p in out.iterdir())
    gbr = comments[0].read_text()
    assert "%TF.FileFunction,Other,Comment*%" in gbr
    # One horizontal and one vertical score for a 2x2 grid: two strokes.
    assert gbr.count("D01*") == 2

    job = json.loads(next(out.glob("*.gbrjob")).read_text())
    assert comments[0].name in {f["Path"] for f in job["FilesAttributes"]}


def test_namespace_without_vcut_layer_uses_default(tmp_path: Path) -> None:
    """Callers building args by hand (no --vcut-layer attr) keep Cmts.User."""
    from kicad_tools.cli.commands.panel import run_panel_command

    out = tmp_path / "panel.kicad_pcb"
    args = SimpleNamespace(panel_input=str(FIXTURE), panel_output=str(out), panel_cut="vcut")
    assert run_panel_command(args) == 0
    assert pcb_vscore_layers(out) == ["Cmts.User"]
