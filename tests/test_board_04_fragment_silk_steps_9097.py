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


def test_merge_fragment_runs_skips_a_stray_and_merges_the_later_run(
    recipe, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A lone stray short segment must not hide the flagged run later in the chain (#9133).

    Chain shape ``[long, short, long, long, short, short, short, long]``: the
    rule flags the 3-segment run at indices 4-6, but the merge used to commit
    to the stray at index 1, reject it as too short, and fail the recipe.
    """
    from kicad_tools.schema.pcb import PCB

    pts = [
        (10.0, 10.0),
        (12.0, 10.0),  # long leg
        (12.0, 10.05),  # the stray 0.05 mm segment (a run of 1)
        (14.0, 10.05),  # long leg
        (14.0, 12.0),  # long leg, neighbour before the run
        (14.05, 12.0),  # run segment 1
        (14.1, 12.05),  # run segment 2
        (14.1, 12.1),  # run segment 3
        (12.5, 13.7),  # 135 degree leg, neighbour after the run
    ]
    pcb_path = tmp_path / "frag_stray.kicad_pcb"
    pcb_path.write_text(
        _BOARD.format(items="\n".join(_seg(a, b) for a, b in zip(pts, pts[1:], strict=False)))
    )

    assert recipe["merge_fragment_runs"](pcb_path) is True

    out = capsys.readouterr().out
    assert "no neighbour on both sides" not in out  # the old one-size-fits-all reason
    assert "run of 1 in chain on F.Cu" in out  # the stray names its own length

    segments = PCB.load(pcb_path).segments
    ends = {(round(s.start[0], 4), round(s.start[1], 4)) for s in segments} | {
        (round(s.end[0], 4), round(s.end[1], 4)) for s in segments
    }
    assert (14.0, 12.2) in ends  # the corner where the two neighbours meet
    for gone in ((14.0, 12.0), (14.05, 12.0), (14.1, 12.05), (14.1, 12.1)):
        assert gone not in ends  # the run and its touched neighbours are gone
    assert (12.0, 10.0) in ends and (12.0, 10.05) in ends  # the stray is untouched
    assert (12.5, 13.7) in ends  # the far end is untouched
    for s in segments:  # the 0/45/90/135 census is preserved
        angle = math.degrees(math.atan2(s.end[1] - s.start[1], s.end[0] - s.start[0])) % 45
        assert min(angle, 45 - angle) < 1e-6


def test_merge_fragment_runs_names_the_reason_for_a_boundary_run(
    recipe, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A run touching the chain start has no neighbour before it; the rejection says so (#9133)."""
    pts = [
        (10.0, 10.0),
        (10.05, 10.0),  # run: 0.05 mm, at the chain start
        (10.05, 10.05),  # run: 0.05 mm
        (10.1, 10.05),  # run: 0.05 mm
        (12.0, 10.05),
        (12.0, 12.0),
    ]
    pcb_path = tmp_path / "frag_start.kicad_pcb"
    pcb_path.write_text(
        _BOARD.format(items="\n".join(_seg(a, b) for a, b in zip(pts, pts[1:], strict=False)))
    )

    assert recipe["merge_fragment_runs"](pcb_path) is False

    out = capsys.readouterr().out
    assert "no neighbour before the run (chain start)" in out
    assert "no neighbour on both sides" not in out


def test_main_wires_fragment_merge_and_silk_gate_before_the_refill() -> None:
    """Source-pin: the merge and the silk-vs-hole gate run after the staircase pass
    and before ``fill_zones``; the via slide runs ahead of every copper-tidy pass
    (Issue #9455), and no step edits footprint silk (#5744)."""
    source = RECIPE.read_text()
    main_body = source[source.index("def main()") :]
    order = [
        main_body.index(call)
        for call in (
            "compress_staircases(routed_path)",
            "merge_fragment_runs(routed_path)",
            "check_silk_clear_of_holes(routed_path)",
            "fill_zones(routed_path)",
        )
    ]
    assert order == sorted(order)
    assert main_body.index("slide_vias_off_silk(routed_path)") < main_body.index(
        "quantize_escapes(routed_path)"
    )
    assert "repair_silkscreen import" not in source  # nothing edits footprint silk
