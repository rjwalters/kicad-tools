"""Board 04 recipe: fragment-run merge and silk-to-hole clip steps (Issue #9097).

The reviewed zero-warning gate flags the router's sub-0.1 mm jog at a
fine-pitch escape (``redundant_copper_fragments``) and silk over a via drill
(``silk_hole_overlap``).  The recipe clears both from the rule's own verdict;
these tests pin the steps' behaviour on a tiny synthetic board (no routing) and
that ``main()`` wires them in.
"""

from __future__ import annotations

import math
import runpy
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RECIPE = REPO_ROOT / "boards" / "04-stm32-devboard" / "generate_design.py"

_BOARD = """(kicad_pcb (version 20240108) (generator "pcbnew") (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen") (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "") (net 1 "BOOT0")
{items}
)
"""


def _seg(a: tuple[float, float], b: tuple[float, float], width: float = 0.2) -> str:
    return (
        f"  (segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width {width}) "
        f'(layer "F.Cu") (net 1) (uuid "{abs(hash((a, b))) % 10**8:08d}-0000-4000-8000-000000000000"))'
    )


@pytest.fixture(scope="module")
def recipe() -> dict:
    return runpy.run_path(str(RECIPE), run_name="recipe_9097")


def test_line_intersection_extends_both_lines(recipe) -> None:
    hit = recipe["_line_intersection"]((0.0, 0.0), (0.0, 1.0), (1.0, 0.0), (-1.0, 1.0))
    assert hit is not None
    x, y, t, u = hit
    assert (round(x, 9), round(y, 9)) == (0.0, 1.0)
    assert t == pytest.approx(1.0) and u == pytest.approx(1.0)
    assert recipe["_line_intersection"]((0, 0), (1, 0), (0, 1), (2, 0)) is None


def test_merge_fragment_runs_replaces_jog_with_45_corner(recipe, tmp_path: Path) -> None:
    """The BOOT0 shape: vertical run, a 0.05 mm jog made of sub-0.1 mm segments, then a 45 degree leg."""
    from kicad_tools.schema.pcb import PCB

    pts = [
        (30.25, 17.8375),
        (30.25, 17.1375),
        (30.2875, 17.1),
        (30.3, 17.1),
        (30.3, 17.01),
        (31.44, 15.87),
        (39.85, 15.87),
    ]
    pcb_path = tmp_path / "frag.kicad_pcb"
    pcb_path.write_text(
        _BOARD.format(items="\n".join(_seg(a, b) for a, b in zip(pts, pts[1:], strict=False)))
    )

    assert recipe["merge_fragment_runs"](pcb_path) is True

    segments = PCB.load(pcb_path).segments
    ends = {(round(s.start[0], 4), round(s.start[1], 4)) for s in segments} | {
        (round(s.end[0], 4), round(s.end[1], 4)) for s in segments
    }
    assert (30.25, 17.06) in ends  # the corner where the two neighbours meet
    assert (30.3, 17.01) not in ends and (30.3, 17.1) not in ends  # the jog is gone
    assert min(math.dist(s.start, s.end) for s in segments) > 0.1
    for s in segments:  # the 0/45/90/135 census is preserved
        angle = math.degrees(math.atan2(s.end[1] - s.start[1], s.end[0] - s.start[0])) % 45
        assert min(angle, 45 - angle) < 1e-6
    # Both far ends are untouched.
    far_start = (30.25, 17.8375)
    assert any(math.dist(far_start, end) < 1e-2 for end in ends)  # file precision is 1 um
    assert (39.85, 15.87) in ends


def test_main_wires_fragment_merge_and_silk_clip_before_the_refill() -> None:
    """Source-pin: both steps run after the staircase pass and before ``fill_zones``."""
    source = RECIPE.read_text()
    main_body = source[source.index("def main()") :]
    order = [
        main_body.index(call)
        for call in (
            "compress_staircases(routed_path)",
            "merge_fragment_runs(routed_path)",
            "clip_silk_to_holes(routed_path)",
            "fill_zones(routed_path)",
        )
    ]
    assert order == sorted(order)
