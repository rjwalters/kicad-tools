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


# ---------------------------------------------------------------------------
# Issue #6165: overshoot, unquoted layers, edge lines, explicit tagging
# ---------------------------------------------------------------------------


def _board(tmp_path: Path, extra: str, name: str = "b.kicad_pcb") -> Path:
    pcb = tmp_path / name
    pcb.write_text(_NOTE_BOARD.format(extra=extra))
    return pcb


def test_overshooting_score_line_is_detected(tmp_path: Path) -> None:
    """KiKit draws score lines ~3 mm past the frame."""
    pcb = _board(tmp_path, '(gr_line (start -3 15) (end 43 15) (layer "Eco1.User"))')
    assert pcb_vscore_layers(pcb) == ["Eco1.User"]
    pcb = _board(tmp_path, '(gr_line (start 20 -3) (end 20 33) (layer "Eco1.User"))', "v.kicad_pcb")
    assert pcb_vscore_layers(pcb) == ["Eco1.User"]


def test_unquoted_layer_token_is_detected(tmp_path: Path) -> None:
    pcb = tmp_path / "old.kicad_pcb"
    pcb.write_text(
        "(kicad_pcb\n  (gr_rect (start 0 0) (end 40 30) (layer Edge.Cuts))\n"
        "  (gr_line (start 20 -3) (end 20 33) (layer Cmts.User))\n)\n"
    )
    assert pcb_vscore_layers(pcb) == ["Cmts.User"]


@pytest.mark.parametrize(
    "line",
    [
        "(start 0 0) (end 40 0)",
        "(start -2 30) (end 42 30)",
        "(start 0 -1) (end 0 31)",
        "(start 40 0) (end 40 30)",
    ],
)
def test_untagged_line_on_outline_edge_is_not_a_score(tmp_path: Path, line: str) -> None:
    pcb = _board(tmp_path, f'(gr_line {line} (layer "Dwgs.User"))')
    assert pcb_vscore_layers(pcb) == []


@pytest.mark.parametrize(
    "layer,line",
    [
        ("Dwgs.User", "(start -5 -5) (end 45 -5)"),
        ("Dwgs.User", "(start -5 35) (end 45 35)"),
        ("Dwgs.User", "(start -5 -5) (end -5 35)"),
        ("Dwgs.User", "(start 45 -5) (end 45 35)"),
        ("Cmts.User", "(start -2 -4) (end 42 -4)"),
    ],
)
def test_untagged_line_outside_outline_is_not_a_score(
    tmp_path: Path, layer: str, line: str
) -> None:
    pcb = _board(tmp_path, f'(gr_line {line} (layer "{layer}"))')
    assert pcb_vscore_layers(pcb) == []


def test_tagged_line_on_edge_is_still_a_score(tmp_path: Path) -> None:
    from kicad_tools.sexp.vscore import make_vscore_uuid

    pcb = _board(
        tmp_path,
        f'(gr_line (start 0 0) (end 40 0) (layer "Dwgs.User") (uuid "{make_vscore_uuid()}"))',
    )
    assert pcb_vscore_layers(pcb) == ["Dwgs.User"]


def test_tagged_partial_line_is_detected_untagged_is_not(tmp_path: Path) -> None:
    from kicad_tools.sexp.vscore import make_vscore_uuid

    partial = "(start 0 15) (end 25 15)"
    assert pcb_vscore_layers(_board(tmp_path, f'(gr_line {partial} (layer "Eco2.User"))')) == []
    tagged = _board(
        tmp_path,
        f'(gr_line {partial} (layer "Eco2.User") (uuid "{make_vscore_uuid()}"))',
        "t.kicad_pcb",
    )
    assert pcb_vscore_layers(tagged) == ["Eco2.User"]


def _cfg(rows: int, cols: int, frame, spacing: float = 0.0) -> PanelConfig:
    kwargs = {"spacing_x": spacing, "spacing_y": spacing} if spacing else {}
    return PanelConfig(
        rows=rows,
        cols=cols,
        cut_method=CutMethod.VCUT,
        frame=frame,
        **kwargs,
    )


@pytest.mark.parametrize(
    "rows,cols,frame",
    [(2, 2, None), (1, 3, None), (2, 2, FrameConfig())],
    ids=["butted", "butted-row", "framed"],
)
def test_kct_panel_score_lines_are_tagged(tmp_path: Path, rows, cols, frame) -> None:
    from kicad_tools.sexp.vscore import VSCORE_UUID_MARKER

    pcb = Panel.from_config(FIXTURE, _cfg(rows, cols, frame)).save(tmp_path / "p.kicad_pcb")
    text = pcb.read_text()
    assert f"-{VSCORE_UUID_MARKER}-" in text
    assert pcb_vscore_layers(pcb) == ["Cmts.User"]


def _vscore_lines(pcb: Path) -> list[tuple[float, float, float, float]]:
    from kicad_tools.sexp import parse_string

    out = []
    for c in parse_string(pcb.read_text()).children:
        if c.is_atom or c.name != "gr_line":
            continue
        layer = c.find_child("layer").get_string(0)
        if layer != "Cmts.User":
            continue
        s, e = c.find_child("start"), c.find_child("end")
        out.append(
            (
                float(s.get_float(0)),
                float(s.get_float(1)),
                float(e.get_float(0)),
                float(e.get_float(1)),
            )
        )
    return out


@pytest.mark.parametrize("frame", [FrameConfig(), None], ids=["framed", "bare"])
def test_mixed_panel_gapped_seams_still_detects_scored_seams(tmp_path: Path, frame) -> None:
    """Gapped columns (spacing_x=2) are routed, butted rows (spacing_y=0) are scored."""
    cfg = PanelConfig(
        rows=2,
        cols=3,
        spacing_x=2.0,
        spacing_y=0.0,
        cut_method=CutMethod.VCUT,
        frame=frame,
    )
    pcb = Panel.from_config(FIXTURE, cfg).save(tmp_path / "m.kicad_pcb")
    assert pcb_vscore_layers(pcb) == ["Cmts.User"]
    lines = _vscore_lines(pcb)
    if frame is None:
        # Only the butted row seam is scored; gapped column seams are not.
        assert len(lines) == 1
        x0, y0, x1, y1 = lines[0]
        assert y0 == y1 and x0 != x1
    else:
        assert len(lines) == 5


# ---------------------------------------------------------------------------
# Partial and jump scores on third-party panels (Issue #6193)
# ---------------------------------------------------------------------------


_SLOT_H = '(gr_rect (start 14 12) (end 18 18) (layer "Edge.Cuts"))'
_SLOTS_V = (
    '(gr_rect (start 18 8) (end 22 11) (layer "Edge.Cuts"))'
    '(gr_rect (start 18 19) (end 22 22) (layer "Edge.Cuts"))'
)


@pytest.mark.parametrize(
    "extra",
    [
        # jump score: two pieces handing off to an interior slot
        _SLOT_H + '(gr_line (start -3 15) (end 14 15) (layer "Eco2.User"))'
        '(gr_line (start 18 15) (end 43 15) (layer "Eco2.User"))',
        # jump score, vertical, three pieces, slightly off-axis coordinates
        _SLOTS_V + '(gr_line (start 20 0) (end 20 8) (layer "Eco2.User"))'
        '(gr_line (start 20.005 11) (end 20.005 19) (layer "Eco2.User"))'
        '(gr_line (start 20 22) (end 20 30) (layer "Eco2.User"))',
    ],
)
def test_jump_scores_over_interior_cutouts_are_detected(tmp_path: Path, extra: str) -> None:
    assert pcb_vscore_layers(_board(tmp_path, extra)) == ["Eco2.User"]


def _line(x0: float, y0: float, x1: float, y1: float, layer: str = "Eco2.User") -> str:
    return f'(gr_line (start {x0} {y0}) (end {x1} {y1}) (layer "{layer}"))'


@pytest.mark.parametrize("layer", ["Dwgs.User", "Cmts.User", "User.1", "Eco1.User"])
@pytest.mark.parametrize(
    "extra",
    [
        # section divider, left edge to 55%
        _line(0, 15, 22, 15),
        # fold/keepout line, top edge to 60%, vertical
        _line(20, 0, 20, 18),
        # title-block rule, right edge to centre
        _line(40, 15, 20, 15),
        # connector centre line, edge to 70%
        _line(0, 15, 28, 15),
        # dashed line 3-on/2-off, edge to edge over solid board
        "".join(_line(x, 15, x + 3, 15) for x in range(0, 40, 5)),
        # dashed line 2-on/2-off (50% duty), edge to edge
        "".join(_line(x, 15, x + 2, 15) for x in range(0, 40, 4)),
    ],
)
def test_documentation_drawings_on_single_boards_are_not_scores(
    tmp_path: Path, layer: str, extra: str
) -> None:
    extra = extra.replace("Eco2.User", layer)
    assert pcb_vscore_layers(_board(tmp_path, extra)) == []


@pytest.mark.parametrize(
    "extra",
    [
        # short stub from one edge
        _line(0, 15, 10, 15),
        # long collinear pieces floating mid-board, touching no edge
        _SLOT_H + _line(5, 15, 12, 15) + _line(16, 15, 36, 15),
        # gapped pieces that reach only one edge, gap over an interior slot
        _SLOT_H + _line(0, 15, 12, 15) + _line(16, 15, 30, 15),
        # collinear pieces on the outline itself or outside it
        _line(-3, 0, 12, 0) + _line(16, 0, 43, 0),
        _line(-3, -5, 12, -5) + _line(16, -5, 43, -5),
        # dimension: extension lines and a dimension line above the board
        _line(0, -6, 0, -2, "Dwgs.User")
        + _line(40, -6, 40, -2, "Dwgs.User")
        + _line(0, -5, 40, -5, "Dwgs.User"),
    ],
)
def test_short_or_off_outline_collinear_drawings_are_not_scores(tmp_path: Path, extra: str) -> None:
    assert pcb_vscore_layers(_board(tmp_path, extra)) == []


def test_pieces_on_different_layers_or_lines_are_not_combined(tmp_path: Path) -> None:
    extra = _SLOT_H + (
        _line(-3, 15, 12, 15, "Eco2.User")
        + _line(16, 15, 43, 15, "Eco1.User")
        + _line(-3, 10, 12, 10, "Eco2.User")
        + _line(16, 12, 43, 12, "Eco2.User")
    )
    assert pcb_vscore_layers(_board(tmp_path, extra)) == []


def test_layers_are_returned_in_file_order(tmp_path: Path) -> None:
    from kicad_tools.sexp.vscore import make_vscore_uuid

    untagged = _line(-3, 15, 43, 15, "Dwgs.User")
    tagged = (
        f'(gr_line (start 20 -3) (end 20 33) (layer "Cmts.User") (uuid "{make_vscore_uuid()}"))'
    )
    assert pcb_vscore_layers(_board(tmp_path, untagged + tagged)) == ["Dwgs.User", "Cmts.User"]


# Partial scores, more probes and the explicit opt-in (Issue #6193)

# A 100 x 80 mm single board.  ``{edge}`` adds interior Edge.Cuts geometry.
_PROBE_BOARD = """(kicad_pcb
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user)
    (19 "Cmts.User" user "User.Comments") (17 "Dwgs.User" user "User.Drawings")
    (21 "Eco1.User" user "User.Eco1") (22 "Eco2.User" user "User.Eco2")
    (31 "User.1" user))
  (gr_rect (start 0 0) (end 100 80) (layer "Edge.Cuts"))
  {edge}
  {lines}
)
"""

_PROBE_LAYERS = ["Dwgs.User", "Cmts.User", "User.1", "Eco1.User"]

# A 10 x 10 mm cutout centred on (50, 40): y=40 crosses it at x=45 and x=55.
_CUTOUT = '(gr_rect (start 45 35) (end 55 45) (layer "Edge.Cuts"))'


def _probe(tmp_path: Path, lines: list[tuple[float, float, float, float]], layer: str, edge=""):
    body = "\n  ".join(
        f'(gr_line (start {x0} {y0}) (end {x1} {y1}) (layer "{layer}"))' for x0, y0, x1, y1 in lines
    )
    pcb = tmp_path / "probe.kicad_pcb"
    pcb.write_text(_PROBE_BOARD.format(edge=edge, lines=body))
    return pcb


def _dashed(on: float, off: float, y: float = 30.0, end: float = 100.0):
    out, x = [], 0.0
    while x < end:
        out.append((x, y, min(x + on, end), y))
        x += on + off
    return out


# The judge's probe drawings on PR #6203: ordinary single-board documentation
# that must not add a V-score Gerber to the fab package.
_DOCUMENTATION_DRAWINGS = {
    "divider-left-edge-to-55pct": [(0, 40, 55, 40)],
    "fold-line-top-edge-to-60pct": [(50, 0, 50, 48)],
    "title-block-rule-right-edge-to-centre": [(100, 70, 50, 70)],
    "connector-centre-line-edge-to-70pct": [(0, 20, 70, 20)],
    "dashed-3-on-2-off-edge-to-edge": _dashed(3, 2),
    "dashed-2-on-2-off-edge-to-edge": _dashed(2, 2),
    "dashed-dnp-line-edge-to-60pct": _dashed(3, 2, y=60, end=60),
    "assembly-inner-frame-inset-5mm": [
        (5, 5, 95, 5),
        (95, 5, 95, 75),
        (95, 75, 5, 75),
        (5, 75, 5, 5),
    ],
}


@pytest.mark.parametrize("layer", _PROBE_LAYERS)
@pytest.mark.parametrize(
    "lines", list(_DOCUMENTATION_DRAWINGS.values()), ids=list(_DOCUMENTATION_DRAWINGS)
)
def test_documentation_drawings_are_not_scores(tmp_path: Path, lines, layer: str) -> None:
    assert pcb_vscore_layers(_probe(tmp_path, lines, layer)) == []


@pytest.mark.parametrize("layer", _PROBE_LAYERS)
def test_solid_edge_to_edge_line_is_still_a_score(tmp_path: Path, layer: str) -> None:
    assert pcb_vscore_layers(_probe(tmp_path, [(0, 40, 100, 40)], layer)) == [layer]


@pytest.mark.parametrize(
    "lines",
    [
        # dashed line across a cutout: one gap skips it, the others skip nothing
        _dashed(3, 2, y=40),
        # divider stopping 1 mm short of the cutout
        [(0, 40, 44, 40)],
        # jump over the cutout, plus a second gap over plain board
        [(-3, 40, 44, 40), (56, 40, 70, 40), (75, 40, 103, 40)],
        # pieces reaching neither outline edge, even though the gap is a cutout
        [(10, 40, 44, 40), (56, 40, 90, 40)],
        # partial line from the cutout that reaches no outline edge
        [(55, 40, 80, 40)],
        # jump over the cutout that stops 1 mm short on each side: the
        # material between score and cutout is left uncut
        [(-3, 40, 44, 40), (56, 40, 103, 40)],
        [(50, -3, 50, 34), (50, 46, 50, 83)],
        # partial lines ending on the isolated cutout, solid board beyond it
        [(0, 40, 45, 40)],
        [(100, 40, 55.05, 40)],
        [(50, 80, 50, 45)],
    ],
    ids=[
        "dashed-over-cutout",
        "short-of-cutout",
        "extra-gap",
        "no-outline-edge",
        "floating",
        "jump-short",
        "jump-vertical-short",
        "partial-to-cutout",
        "partial-to-cutout-reversed",
        "partial-to-cutout-vertical",
    ],
)
def test_lines_the_cutout_does_not_explain_are_not_scores(tmp_path: Path, lines) -> None:
    assert pcb_vscore_layers(_probe(tmp_path, lines, "Cmts.User", edge=_CUTOUT)) == []


@pytest.mark.parametrize("layer", _PROBE_LAYERS)
@pytest.mark.parametrize(
    "lines",
    [
        # jump score handing off to the cutout, overshooting both edges like KiKit
        [(-3, 40, 45, 40), (55, 40, 103, 40)],
        # jump score whose pieces stop exactly at the cutout
        [(0, 40, 45, 40), (55, 40, 100, 40)],
        # pieces ending within 0.1 mm of the cutout, drawn reversed
        [(100, 40, 55.05, 40), (44.95, 40, 0, 40)],
        # vertical jump score over the cutout (x=50 crosses it at y=35, 45)
        [(50, -3, 50, 35), (50, 45, 50, 83)],
    ],
    ids=["jump", "jump-flush", "jump-within-tolerance", "jump-vertical"],
)
def test_scores_explained_by_a_cutout_are_detected(tmp_path: Path, lines, layer: str) -> None:
    assert pcb_vscore_layers(_probe(tmp_path, lines, layer, edge=_CUTOUT)) == [layer]


def test_jump_score_over_a_rounded_slot_is_detected(tmp_path: Path) -> None:
    """A routed slot drawn as two lines and two arcs (y=40 meets the arcs)."""
    slot = (
        '(gr_line (start 45 38) (end 55 38) (layer "Edge.Cuts"))'
        '(gr_line (start 45 42) (end 55 42) (layer "Edge.Cuts"))'
        '(gr_arc (start 45 38) (mid 43 40) (end 45 42) (layer "Edge.Cuts"))'
        '(gr_arc (start 55 42) (mid 57 40) (end 55 38) (layer "Edge.Cuts"))'
    )
    jump = [(-3, 40, 43, 40), (57, 40, 103, 40)]
    assert pcb_vscore_layers(_probe(tmp_path, jump, "Cmts.User", edge=slot)) == ["Cmts.User"]
    # ending on the slot with board beyond it, or stopping short of it
    for lines in ([(0, 40, 43, 40)], [(0, 40, 30, 40)], [(-3, 40, 42.5, 40), (57.5, 40, 103, 40)]):
        assert pcb_vscore_layers(_probe(tmp_path, lines, "Cmts.User", edge=slot)) == []


# A 100 x 80 board whose outline has a 4 mm routed slot cut in from the right
# edge along y=40, ending at x=55.
_SLOTTED_OUTLINE = (
    "(gr_poly (pts (xy 0 0) (xy 100 0) (xy 100 38) (xy 55 38) (xy 55 42) (xy 100 42)"
    ' (xy 100 80) (xy 0 80)) (layer "Edge.Cuts"))'
)


@pytest.mark.parametrize("layer", _PROBE_LAYERS)
def test_partial_score_handing_off_to_a_routed_slot_is_detected(tmp_path: Path, layer: str) -> None:
    """The score cuts x=0..55 and the slot separates the rest: a full seam."""
    for lines in ([(-3, 40, 55, 40)], [(0, 40, 54.95, 40)]):
        pcb = tmp_path / "slotted.kicad_pcb"
        body = f'(gr_line (start {lines[0][0]} 40) (end {lines[0][2]} 40) (layer "{layer}"))'
        pcb.write_text(f"(kicad_pcb\n  {_SLOTTED_OUTLINE}\n  {body}\n)\n")
        assert pcb_vscore_layers(pcb) == [layer]
    # stopping 2 mm before the slot leaves material uncut
    pcb.write_text(
        f'(kicad_pcb\n  {_SLOTTED_OUTLINE}\n  (gr_line (start 0 40) (end 53 40) (layer "{layer}"))\n)\n'
    )
    assert pcb_vscore_layers(pcb) == []


# The judge's second-round probes on PR #6203: an untagged drafting line snapped
# from the board edge to a hole, cutout or slot on an ordinary single board.
_M3_HOLE = '(gr_circle (center 30 40) (end 31.6 40) (layer "Edge.Cuts"))'
_WIDE_CUTOUT = '(gr_rect (start 60 30) (end 80 40) (layer "Edge.Cuts"))'
# 1.2 mm wide oblong USB-C shell slot, x=20, y=5..8, drawn as lines and arcs.
_USB_SLOT = (
    '(gr_line (start 19.4 5.6) (end 19.4 7.4) (layer "Edge.Cuts"))'
    '(gr_line (start 20.6 5.6) (end 20.6 7.4) (layer "Edge.Cuts"))'
    '(gr_arc (start 19.4 5.6) (mid 20 5) (end 20.6 5.6) (layer "Edge.Cuts"))'
    '(gr_arc (start 20.6 7.4) (mid 20 8) (end 19.4 7.4) (layer "Edge.Cuts"))'
)
_SNAPPED_DRAFTING_LINES = {
    "left-edge-to-m3-hole-rim": (_M3_HOLE, [(0, 40, 28.4, 40)]),
    "top-edge-to-m3-hole-rim": (_M3_HOLE, [(30, 0, 30, 38.4)]),
    "left-edge-to-m3-hole-centre": (_M3_HOLE, [(0, 40, 30, 40)]),
    "dashed-edge-to-edge-past-the-hole": (_M3_HOLE, _dashed(3, 2, y=40)),
    "left-edge-to-cutout-side": (_WIDE_CUTOUT, [(0, 35, 60, 35)]),
    "top-edge-to-cutout-top": (_WIDE_CUTOUT, [(70, 0, 70, 30)]),
    "bottom-edge-to-cutout-bottom": (_WIDE_CUTOUT, [(70, 80, 70, 40)]),
    "top-edge-to-usb-c-slot": (_USB_SLOT, [(20, 0, 20, 5)]),
}


@pytest.mark.parametrize("layer", _PROBE_LAYERS)
@pytest.mark.parametrize(
    "edge,lines", list(_SNAPPED_DRAFTING_LINES.values()), ids=list(_SNAPPED_DRAFTING_LINES)
)
def test_lines_snapped_to_holes_and_slots_are_not_scores(
    tmp_path: Path, edge: str, lines, layer: str
) -> None:
    assert pcb_vscore_layers(_probe(tmp_path, lines, layer, edge=edge)) == []


def test_line_broken_around_a_hole_crosses_all_material(tmp_path: Path) -> None:
    """Both pieces plus the hole cover the line, as a full-span line would.

    This carries the same risk as the solid edge-to-edge line #6156 accepts:
    every bit of board material on the line is drawn over.
    """
    hole = '(gr_circle (center 50 40) (end 54 40) (layer "Edge.Cuts"))'
    broken = [(0, 40, 46, 40), (54, 40, 100, 40)]
    assert pcb_vscore_layers(_probe(tmp_path, broken, "Eco1.User", edge=hole)) == ["Eco1.User"]
    assert pcb_vscore_layers(_probe(tmp_path, [(0, 40, 46, 40)], "Eco1.User", edge=hole)) == []


# The judge's third-round probes on PR #6203: an overlapping or duplicated
# Edge.Cuts segment (DXF imports, a ``gr_rect`` later traced with ``gr_line``)
# adds a spurious crossing that flips the crossing parity and turns the hole
# interior into "material".  A hole mark must not become a V-score.
_MOUNT_HOLE = '(gr_circle (center 20 40) (end 21 40) (layer "Edge.Cuts"))'
_DUP_LEFT_FULL = '(gr_line (start 0 0) (end 0 80) (layer "Edge.Cuts"))'
_DUP_LEFT_PART = '(gr_line (start 0 10) (end 0 30) (layer "Edge.Cuts"))'
_HOLE_CROSSHAIR = [(18.5, 40, 21.5, 40), (20, 38.5, 20, 41.5)]
_DUPLICATED_EDGE_CASES = {
    "full-duplicate-with-crosshair": (_DUP_LEFT_FULL, _HOLE_CROSSHAIR),
    "full-duplicate-with-horizontal-bar": (_DUP_LEFT_FULL, [(17, 40, 23, 40)]),
    "full-duplicate-with-line-edge-to-past-hole": (_DUP_LEFT_FULL, [(0, 40, 22, 40)]),
    "partial-overlap-with-crosshair": (_DUP_LEFT_PART, _HOLE_CROSSHAIR),
}


@pytest.mark.parametrize("layer", _PROBE_LAYERS)
@pytest.mark.parametrize(
    "dup,lines",
    list(_DUPLICATED_EDGE_CASES.values()),
    ids=list(_DUPLICATED_EDGE_CASES),
)
def test_duplicated_outline_edge_does_not_turn_a_hole_mark_into_a_score(
    tmp_path: Path, dup: str, lines, layer: str
) -> None:
    clean = _probe(tmp_path, lines, layer, edge=_MOUNT_HOLE)
    assert pcb_vscore_layers(clean) == []
    dirty = _probe(tmp_path, lines, layer, edge=_MOUNT_HOLE + dup)
    assert pcb_vscore_layers(dirty) == []


def test_duplicated_outline_edge_keeps_the_full_span_shortcut(tmp_path: Path) -> None:
    """An ambiguous outline fails closed, but a solid edge-to-edge line still counts."""
    pcb = _probe(tmp_path, [(-3, 40, 103, 40)], "Eco1.User", edge=_MOUNT_HOLE + _DUP_LEFT_FULL)
    assert pcb_vscore_layers(pcb) == ["Eco1.User"]


def test_l_board_divider_to_the_notch_wall_matches_full_span_acceptance(
    tmp_path: Path,
) -> None:
    """Divider from the left edge to the notch wall of an L-shaped board.

    The judge's L-notch probe.  Beyond the notch wall there is no board at
    all, so the divider crosses every bit of material on its line: it is
    accepted, the same risk as the full-span solid line main already
    accepts.  A divider stopping short of the wall is not.
    """
    pcb = tmp_path / "l.kicad_pcb"
    outline = (
        "(gr_poly (pts (xy 0 0) (xy 100 0) (xy 100 40) (xy 60 40) (xy 60 80) (xy 0 80))"
        ' (layer "Edge.Cuts"))'
    )
    for end, expected in ((60, ["Dwgs.User"]), (50, [])):
        line = f'(gr_line (start 0 60) (end {end} 60) (layer "Dwgs.User"))'
        pcb.write_text(f"(kicad_pcb\n  {outline}\n  {line}\n)\n")
        assert pcb_vscore_layers(pcb) == expected


def test_dashed_line_across_a_two_board_panel_is_not_a_score(tmp_path: Path) -> None:
    """Edge to edge, but most gaps lie on solid board inside a sub-board outline.

    Each gap must lie off the board; overlapping the bounding box of an
    interior outline is not enough.
    """
    dashes = "".join(
        f'(gr_line (start {x} 15) (end {x + 3} 15) (layer "Cmts.User"))' for x in range(-3, 88, 5)
    )
    pcb = tmp_path / "pair.kicad_pcb"
    pcb.write_text(
        "(kicad_pcb\n"
        '  (gr_rect (start 0 0) (end 40 30) (layer "Edge.Cuts"))\n'
        '  (gr_rect (start 45 0) (end 85 30) (layer "Edge.Cuts"))\n'
        f"  {dashes}\n"
        ")\n"
    )
    assert pcb_vscore_layers(pcb) == []


def test_jump_score_between_separate_board_outlines_is_detected(tmp_path: Path) -> None:
    """Two boards 5 mm apart: the gap is the space between their outlines."""
    pcb = tmp_path / "pair.kicad_pcb"
    pcb.write_text(
        "(kicad_pcb\n"
        '  (gr_rect (start 0 0) (end 40 30) (layer "Edge.Cuts"))\n'
        '  (gr_rect (start 45 0) (end 85 30) (layer "Edge.Cuts"))\n'
        '  (gr_line (start -3 15) (end 40 15) (layer "Cmts.User"))\n'
        '  (gr_line (start 45 15) (end 88 15) (layer "Cmts.User"))\n'
        ")\n"
    )
    assert pcb_vscore_layers(pcb) == ["Cmts.User"]


@pytest.mark.parametrize(
    "extra",
    [
        # long collinear pieces floating mid-board, touching no edge
        '(gr_line (start 5 15) (end 20 15) (layer "Eco2.User"))'
        '(gr_line (start 22 15) (end 36 15) (layer "Eco2.User"))',
        # collinear pieces on the outline itself or outside it
        '(gr_line (start -3 0) (end 12 0) (layer "Eco2.User"))'
        '(gr_line (start 16 0) (end 43 0) (layer "Eco2.User"))',
        '(gr_line (start -3 -5) (end 12 -5) (layer "Eco2.User"))'
        '(gr_line (start 16 -5) (end 43 -5) (layer "Eco2.User"))',
        # dimension: extension lines and a dimension line above the board
        '(gr_line (start 0 -6) (end 0 -2) (layer "Dwgs.User"))'
        '(gr_line (start 40 -6) (end 40 -2) (layer "Dwgs.User"))'
        '(gr_line (start 0 -5) (end 40 -5) (layer "Dwgs.User"))',
        # pieces split across layers do not combine into one line
        '(gr_line (start 0 15) (end 20 15) (layer "Eco2.User"))'
        '(gr_line (start 20 15) (end 40 15) (layer "Eco1.User"))',
    ],
)
def test_collinear_drawings_without_geometry_are_not_scores(tmp_path: Path, extra: str) -> None:
    assert pcb_vscore_layers(_board(tmp_path, extra)) == []


def test_layers_come_back_in_file_order(tmp_path: Path) -> None:
    from kicad_tools.sexp.vscore import make_vscore_uuid

    extra = (
        '(gr_line (start 0 15) (end 40 15) (layer "Dwgs.User"))'
        f'(gr_line (start 0 5) (end 10 5) (layer "Cmts.User") (uuid "{make_vscore_uuid()}"))'
        '(gr_line (start 20 0) (end 20 30) (layer "Eco1.User"))'
    )
    assert pcb_vscore_layers(_board(tmp_path, extra)) == ["Dwgs.User", "Cmts.User", "Eco1.User"]


# ---------------------------------------------------------------------------
# Explicit opt-in: GerberConfig.vscore_layers / --vscore-layer (Issue #6193)
# ---------------------------------------------------------------------------


def test_explicit_vscore_layer_is_plotted_without_detection() -> None:
    exporter = _make_exporter(FIXTURE)
    layers = exporter._get_default_layers(GerberConfig(vscore_layers=["User.2"]))
    assert layers[-1] == "User.2"
    layers = exporter._get_default_layers(
        GerberConfig(vscore_layers=["User.2"], include_vscore=False)
    )
    assert layers[-1] == "User.2"


def test_explicit_vscore_layer_is_not_duplicated(tmp_path: Path) -> None:
    pcb = _save_panel(tmp_path, cut=CutMethod.VCUT)
    layers = _make_exporter(pcb)._get_default_layers(
        GerberConfig(vscore_layers=["Cmts.User", "Edge.Cuts"])
    )
    assert layers.count("Cmts.User") == 1
    assert layers.count("Edge.Cuts") == 1


def test_export_for_manufacturer_applies_explicit_vscore_layers() -> None:
    exporter = _make_exporter(FIXTURE)
    with patch.object(GerberExporter, "export", autospec=True) as export:
        exporter.export_for_manufacturer("jlcpcb", "out", vscore_layers=["User.3"])
    config = export.call_args.args[1]
    assert config.vscore_layers == ["User.3"]
    from kicad_tools.export.gerber import MANUFACTURER_PRESETS

    assert MANUFACTURER_PRESETS["jlcpcb"].config.vscore_layers == []


def test_explicit_vscore_layer_missing_from_layer_table_warns(tmp_path: Path, caplog) -> None:
    import logging

    from kicad_tools.export.gerber import missing_vscore_layers

    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(_NOTE_BOARD.format(extra=""))  # defines Cmts.User only
    assert missing_vscore_layers(pcb, ["User.2", "Cmts.User", "User.Comments"]) == ["User.2"]
    with caplog.at_level(logging.WARNING, logger="kicad_tools.export.gerber"):
        layers = _make_exporter(pcb)._get_default_layers(
            GerberConfig(vscore_layers=["User.2", "Cmts.User"])
        )
    assert "User.2" in layers
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("User.2" in m and "layer table" in m for m in warned)
    assert not any("Cmts.User is not" in m for m in warned)


def test_no_layer_table_means_no_missing_layer_warning(tmp_path: Path) -> None:
    from kicad_tools.export.gerber import missing_vscore_layers

    pcb = tmp_path / "bare.kicad_pcb"
    pcb.write_text('(kicad_pcb (gr_rect (start 0 0) (end 4 4) (layer "Edge.Cuts")))')
    assert missing_vscore_layers(pcb, ["User.2"]) == []
    assert missing_vscore_layers(tmp_path / "missing.kicad_pcb", ["User.2"]) == []


def test_export_gerbers_script_warns_on_stderr(tmp_path: Path, capsys) -> None:
    from kicad_tools.cli import export_gerbers as script

    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(_NOTE_BOARD.format(extra=""))
    with patch.object(script.subprocess, "run") as run:
        script.export_gerbers(pcb, tmp_path / "out", Path("kicad-cli"), vscore_layers=["User.2"])
    err = capsys.readouterr().err
    assert "User.2" in err and "layer table" in err
    assert (
        "User.2"
        in run.call_args_list[0].args[0][run.call_args_list[0].args[0].index("--layers") + 1]
    )


def test_kct_export_vscore_layer_reaches_gerber_config(tmp_path: Path, monkeypatch) -> None:
    from kicad_tools.cli import main as kct_main
    from kicad_tools.export.manufacturing import ManufacturingPackage, ManufacturingResult

    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    captured = {}
    original_init = ManufacturingPackage.__init__

    def spy_init(self, pcb_path, schematic_path=None, manufacturer="jlcpcb", config=None):
        captured["gerber_config"] = config.gerber_config
        original_init(self, pcb_path, schematic_path, manufacturer, config)

    def fake_export(self, output_dir=None, *, dry_run=False):
        return ManufacturingResult(output_dir=Path(output_dir or tmp_path))

    monkeypatch.setattr(ManufacturingPackage, "__init__", spy_init)
    monkeypatch.setattr(ManufacturingPackage, "export", fake_export)
    argv = ["export", str(pcb), "--vscore-layer", "User.2", "--vscore-layer", "Eco1.User"]
    argv += ["--no-report", "--skip-preflight", "-o", str(tmp_path / "out")]
    assert kct_main(argv) == 0
    config = captured["gerber_config"]
    assert config.vscore_layers == ["User.2", "Eco1.User"]
    assert config.clean_after_zip is True


def test_fleet_boards_detect_no_score_layer() -> None:
    import subprocess

    files = subprocess.run(
        ["git", "ls-files", "*.kicad_pcb"], cwd=REPO, capture_output=True, text=True
    ).stdout.split()
    assert len(files) >= 50
    offenders = [f for f in files if pcb_vscore_layers(REPO / f)]
    assert offenders == []
