"""Edge/keepout clearance and width-fallback pins for ``repair_sharp_corners`` (#9239).

Board 04's corner-repair step used to accept a replacement path on foreign-copper
clearance alone: the board outline and keepout rule areas were never consulted,
and a candidate could fall back to a narrower width than the trace it replaced.
Both are latent-only on board 04 (its post-step DRC/LVS gates catch violations),
but the step is copy-paste bait for other boards' recipes. These tests pin:

- a repair path inside the profile's copper-to-edge minimum is refused;
- a repair path entering a track-blocking keepout on the corner's layer is
  refused, while the same keepout on another layer (or with tracks allowed)
  still repairs;
- an unreadable board outline (no Edge.Cuts copper) refuses the repair instead
  of assuming clear;
- the repaired piece keeps the removed segments' widest width -- a path that
  only clears at the removed set's narrower width is refused, where the old
  descending-width loop would have silently thinned the trace;
- a corner with room repairs, including one whose path sits exactly at the
  minimum edge distance.

Each fixture is a minimal 2-layer board: a rectangular Edge.Cuts outline, one
45-degree fold-back (the ``sharp_trace_corner`` bend shape from
``tests/test_validate_sharp_trace_corner.py``) between two same-net vias, and
optionally a keepout rule area or a foreign-net segment. The repair replaces
the fold-back with the straight via-to-via chord, so the checks bite on that
chord.
"""

from __future__ import annotations

import importlib.util
import json
import types
from pathlib import Path

import pytest

from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.rules.sharp_trace_corner import SharpTraceCornerRule

pytest.importorskip("shapely")

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_GEN = REPO_ROOT / "boards" / "04-stm32-devboard" / "generate_design.py"


def _load_step() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("board04_generate_design", BOARD_GEN)
    assert spec is not None and spec.loader is not None
    module = types.ModuleType("board04_generate_design")
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def step() -> types.ModuleType:
    return _load_step()


def _foldback_board(
    via_y: float,
    *,
    outline: bool = True,
    keepout: tuple[str, str] | None = None,  # (layers spec, tracks allowed|not_allowed)
    foreign: tuple[float, float, float, float] | None = None,
) -> str:
    """A board whose only finding is one fold-back between vias at (5, via_y)/(8, via_y).

    The repair chord runs straight along ``y = via_y``; the removed segments'
    widths are {0.2, 0.15}, so a repair must keep 0.2 (#9239 width policy).
    """
    parts = [
        '(kicad_pcb (version 20240108) (generator "test")\n',
        '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n',
        '  (net 0 "") (net 1 "SIG") (net 2 "OTHER")\n',
    ]
    if outline:
        for a, b in (
            ((0, 0), (30, 0)),
            ((30, 0), (30, 20)),
            ((30, 20), (0, 20)),
            ((0, 20), (0, 0)),
        ):
            parts.append(
                f"  (gr_line (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) "
                '(layer "Edge.Cuts") (width 0.1))\n'
            )
    parts.append(
        f'  (segment (start 5 {via_y}) (end 10 {via_y - 1.5}) (width 0.2) (layer "F.Cu") (net 1))\n'
    )
    parts.append(
        f'  (segment (start 10 {via_y - 1.5}) (end 8 {via_y}) (width 0.15) (layer "F.Cu") (net 1))\n'
    )
    parts.append(
        f'  (via (at 5 {via_y}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1 "SIG"))\n'
    )
    parts.append(
        f'  (via (at 8 {via_y}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1 "SIG"))\n'
    )
    if foreign:
        parts.append(
            f"  (segment (start {foreign[0]} {foreign[1]}) (end {foreign[2]} {foreign[3]}) "
            '(width 0.2) (layer "F.Cu") (net 2))\n'
        )
    if keepout:
        layers, tracks = keepout
        # Covers the chord (y = 17, x in [5, 8]) with margin.
        parts.append(
            f'  (zone (net 0) (net_name "") (name "ko") (layers "{layers}") '
            f"(keepout (tracks {tracks}))\n"
            "    (polygon (pts (xy 4 16.4) (xy 9 16.4) (xy 9 17.6) (xy 4 17.6))))\n"
        )
    parts.append(")\n")
    return "".join(parts)


def _write_board(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / f"{name}.kicad_pcb"
    path.write_text(text)
    # _netclass_clearance reads the routed project's net classes; Default 0
    # keeps the foreign-copper check to "no overlap" for these fixtures.
    path.with_suffix(".kicad_pro").write_text(
        json.dumps({"net_settings": {"classes": [{"name": "Default", "clearance": 0.0}]}})
    )
    return path


def _sig_segments(path: Path) -> set[tuple]:
    pcb = PCB.load(str(path))
    return {(s.start, s.end, s.width) for s in pcb.segments if s.net_number == 1}


def test_corner_with_room_repairs_at_widest_removed_width(step, tmp_path):
    path = _write_board(tmp_path, "room", _foldback_board(17.0))
    assert step.repair_sharp_corners(path) is True
    segs = _sig_segments(path)
    # The fold-back is replaced by the via-to-via chord at the removed set's
    # widest width (0.2), never the narrower 0.15 leg.
    assert segs == {((5.0, 17.0), (8.0, 17.0), 0.2)}
    assert SharpTraceCornerRule().check(PCB.load(str(path)), None).violations == []


def test_repair_at_exact_edge_minimum_is_allowed(step, tmp_path):
    # Chord at y=19.6: copper edge (19.6 - 0.1) sits exactly at the profile's
    # 0.3 mm copper-to-edge minimum -> allowed, not refused.
    path = _write_board(tmp_path, "exact", _foldback_board(19.6))
    assert step.repair_sharp_corners(path) is True
    assert _sig_segments(path) == {((5.0, 19.6), (8.0, 19.6), 0.2)}


def test_repair_within_edge_clearance_is_refused(step, tmp_path, capsys):
    # Chord at y=19.75: 0.15 mm of copper to the y=20 edge, under the 0.3 mm
    # minimum. The old check (foreign copper only) accepted it.
    path = _write_board(tmp_path, "edge", _foldback_board(19.75))
    assert step.repair_sharp_corners(path) is False
    assert "no clear repair" in capsys.readouterr().out
    # The fold-back copper is untouched.
    assert len(_sig_segments(path)) == 2


def test_repair_entering_track_keepout_is_refused(step, tmp_path, capsys):
    path = _write_board(tmp_path, "ko", _foldback_board(17.0, keepout=("F.Cu", "not_allowed")))
    assert step.repair_sharp_corners(path) is False
    assert "no clear repair" in capsys.readouterr().out
    assert len(_sig_segments(path)) == 2


def test_keepout_on_other_layer_does_not_block(step, tmp_path):
    path = _write_board(tmp_path, "ko-b", _foldback_board(17.0, keepout=("B.Cu", "not_allowed")))
    assert step.repair_sharp_corners(path) is True
    assert _sig_segments(path) == {((5.0, 17.0), (8.0, 17.0), 0.2)}


def test_keepout_allowing_tracks_does_not_block(step, tmp_path):
    path = _write_board(tmp_path, "ko-ok", _foldback_board(17.0, keepout=("F.Cu", "allowed")))
    assert step.repair_sharp_corners(path) is True
    assert _sig_segments(path) == {((5.0, 17.0), (8.0, 17.0), 0.2)}


def test_missing_outline_refuses_the_repair(step, tmp_path, capsys):
    # No Edge.Cuts copper: the edge check cannot run, so the repair is
    # refused rather than assumed clear (#9239).
    path = _write_board(tmp_path, "no-edge", _foldback_board(17.0, outline=False))
    assert step.repair_sharp_corners(path) is False
    assert "no clear repair" in capsys.readouterr().out
    assert len(_sig_segments(path)) == 2


def test_width_fallback_is_gone(step, tmp_path, capsys):
    # Foreign copper 0.18 mm off the chord: the 0.2 mm chord overlaps it
    # (gap -0.02) but a 0.15 mm chord would clear (gap +0.005). The old
    # descending-width loop thinned the repair to 0.15; now the widest
    # width is the only candidate, so the corner is refused.
    path = _write_board(tmp_path, "narrow", _foldback_board(17.0, foreign=(6.0, 16.82, 7.0, 16.82)))
    assert step.repair_sharp_corners(path) is False
    assert "no clear repair" in capsys.readouterr().out
    assert len(_sig_segments(path)) == 2


@pytest.mark.parametrize(
    ("layers", "layer", "expected"),
    [
        (("F.Cu",), "F.Cu", True),
        (("F.Cu",), "B.Cu", False),
        (("F.Cu", "B.Cu"), "B.Cu", True),
        (("* .Cu".replace(" ", ""),), "B.Cu", True),
        (("* .Cu".replace(" ", ""),), "In1.Cu", True),
        (("F&B.Cu",), "B.Cu", True),
        (("F&B.Cu",), "In1.Cu", False),
        ((), "F.Cu", True),  # empty spec falls back to zone.layer="F.Cu"
        ((), "B.Cu", False),  # ... and does not match other layers
    ],
)
def test_rule_area_layer_spec_wildcards(step, layers, layer, expected):
    class _Zone:
        def __init__(self, layers, layer):
            self.keepout = types.SimpleNamespace(tracks_allowed=False)
            self.layers = list(layers)
            self.layer = layer

    zone = _Zone(layers, "F.Cu" if not layers else "")
    assert step._rule_area_blocks_tracks(zone, layer) is expected


def test_rule_area_with_tracks_allowed_never_blocks(step):
    class _Zone:
        keepout = types.SimpleNamespace(tracks_allowed=True)
        layers = ["F.Cu"]
        layer = "F.Cu"

    assert step._rule_area_blocks_tracks(_Zone(), "F.Cu") is False
