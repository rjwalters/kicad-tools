"""The recipe silk clearance pass clears every ``silk_hole_overlap`` finding (#9033).

``kicad_tools.recipes.silk_clearance.clear_silk_over_holes`` is the shared
post-route step the board recipes call once the routed vias exist: it clips
silk graphics off drills, moves the flagged reference designators, and fails
loud when a finding survives.  These tests pin the three behaviours the
recipes rely on -- clear, idempotent re-run, and the fail-loud residual --
using the same non-zero-origin fixtures as ``test_silk_drill_clip_9054.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("shapely")

from kicad_tools.drc.repair_silkscreen import SilkscreenRepairer  # noqa: E402
from kicad_tools.manufacturers.base import DesignRules  # noqa: E402
from kicad_tools.recipes.silk_clearance import SilkClearanceError, clear_silk_over_holes  # noqa: E402
from kicad_tools.schema.pcb import PCB  # noqa: E402
from kicad_tools.validate.rules.silkscreen import check_silk_hole_overlap  # noqa: E402

_HEAD = """(kicad_pcb (version 20240108) (generator "test") (generator_version "9.0")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (36 "B.SilkS" user) (37 "F.SilkS" user)
          (38 "B.Mask" user) (39 "F.Mask" user) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0) (tenting front back))
  (net 0 "")
  (net 1 "N1")
  (gr_rect (start 100 100) (end 160 160) (stroke (width 0.1) (type solid)) (fill no)
    (layer "Edge.Cuts"))
"""

WIDTH = 0.15


def _via(x: float, y: float) -> str:
    return (
        f'  (via (at {x} {y}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") '
        f'(net 1) (uuid "via-{x}-{y}"))\n'
    )


def _fp(body: str, *, ref: str = "U1", at: str = "130 130") -> str:
    return f"""  (footprint "X:Y" (layer "F.Cu") (at {at})
    (property "Reference" "{ref}" (at 0 0) (layer "F.SilkS")
      (effects (font (size 1 1) (thickness 0.15))))
    {body}
  )
"""


def _line(x1, y1, x2, y2, *, uid="l1") -> str:
    return (
        f"(fp_line (start {x1} {y1}) (end {x2} {y2}) (stroke (width {WIDTH}) (type solid)) "
        f'(layer "F.SilkS") (uuid "{uid}"))'
    )


def _arc(*, start="0 0", mid="0.2 -0.2", end="0.4 0", uid="a1") -> str:
    return (
        f"(fp_arc (start {start}) (mid {mid}) (end {end}) "
        f'(stroke (width {WIDTH}) (type solid)) (layer "F.SilkS") (uuid "{uid}"))'
    )


def _write(tmp_path: Path, body: str) -> Path:
    """Write the board, normalised through one no-op save (canonical bytes)."""
    path = tmp_path / "board.kicad_pcb"
    path.write_text(_HEAD + body + ")\n")
    SilkscreenRepairer(path).save()
    return path


def _rules() -> DesignRules:
    return DesignRules(
        min_trace_width_mm=0.0,
        min_clearance_mm=0.0,
        min_via_drill_mm=0.0,
        min_via_diameter_mm=0.0,
        min_annular_ring_mm=0.0,
    )


def _hole_findings(path: Path) -> list[tuple[str, ...]]:
    return [v.items for v in check_silk_hole_overlap(PCB.load(path), _rules()).violations]


def test_clears_graphics_and_references_and_is_idempotent(tmp_path: Path):
    path = _write(
        tmp_path,
        _via(130.0, 130.0)  # under the reference text
        + _via(145.0, 145.0)  # under the outline line
        + _fp(
            # fp-relative: absolute (140..150, 145), crossed by the (145,145) via
            _line(10, 15, 20, 15, uid="over-line"),
            at="130 130",
        ),
    )
    assert _hole_findings(path), "fixture must flag both the reference and the line"

    clipped, moved = clear_silk_over_holes(path)

    assert clipped == 1
    assert moved == 1
    assert _hole_findings(path) == []

    # A re-run on the cleared board is a no-op that still succeeds: the
    # recipes call the pass unconditionally after every route.
    assert clear_silk_over_holes(path) == (0, 0)


def test_unclippable_graphic_fails_loud(tmp_path: Path):
    # An fp_arc over a drill cannot be clipped (the clipper skips arcs), so
    # the pass must refuse to ship silk the fab will remove.  The arc's
    # fp-relative start (0,0) is the footprint origin, i.e. absolute
    # (130,130); the via drill sits 0.2 mm away, inside the 0.15 mm drill
    # radius plus half the stroke width.
    path = _write(
        tmp_path, _via(130.2, 130.0) + _fp(_arc(start="0 0", mid="0.2 -0.2", end="0.4 0"))
    )

    with pytest.raises(SilkClearanceError):
        clear_silk_over_holes(path)


def test_free_silk_text_over_hole_fails_loud(tmp_path: Path):
    # A free ``gr_text`` (e.g. an operator legend) is neither a footprint
    # graphic the clipper owns nor a reference the placer moves, so the
    # pass must refuse to ship it over a drill instead of silently
    # leaving a finding the manufacturing gates count.
    legend = (
        '(gr_text "3V3 SENSE" (at 130 130) (layer "F.SilkS")\n'
        "  (effects (font (size 1 1) (thickness 0.15)) (justify left)))\n"
    )
    path = _write(tmp_path, _via(130.0, 130.0) + legend)

    with pytest.raises(SilkClearanceError, match="3V3 SENSE"):
        clear_silk_over_holes(path)
