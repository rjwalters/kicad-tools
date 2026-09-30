"""Routing must not destroy footprint silkscreen geometry (Issue #5744).

Background
----------
Board 03's committed *routed* artifact shipped with **zero** footprint
silkscreen graphics while its *unrouted* sibling carried 139 of them across
all 38 footprints -- every body outline and, critically, every pin-1 marker
was gone.  Only board 03 was affected, because only board 03 replays a saved
routing plan instead of routing live:
``boards/03-usb-joystick/generate_design.py:route_pcb()`` defaults to
``use_saved_plan=True`` and delegates to
``boards/03-usb-joystick/routing_plan.py:apply_plan()``.

``apply_plan()`` rewrote each footprint's silk-layer children as
"delete everything on ``F.SilkS``/``B.SilkS``, then restore whatever the plan
holds".  The plan's ``"silkscreen"`` section only ever held ``property``
(reviewed reference-designator text placement) nodes -- never graphic
primitives -- so the graphics were deleted and never restored.

Two independent checks below, because the bug had two observable faces:

* the **replay** face -- ``apply_plan()`` itself dropping the geometry, and
* the **artifact** face -- the committed routed ``.kicad_pcb`` bytes that a
  reviewer, the gallery, and CI's routed-DRC job actually consume.

The artifact check is deliberately parametrized over *every* demo board with a
routed sibling, not just board 03, so any future board that grows a
footprint-rewriting step is covered by construction.
"""

from __future__ import annotations

import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from kicad_tools.schema.pcb import PCB

REPO_ROOT = Path(__file__).resolve().parents[1]
BOARD_03 = REPO_ROOT / "boards/03-usb-joystick"
UNROUTED_03 = BOARD_03 / "output/usb_joystick.kicad_pcb"

SILK_LAYERS = frozenset({"F.SilkS", "B.SilkS"})


def silk_graphics_per_footprint(pcb_path: Path) -> dict[str, int]:
    """Count each footprint's silkscreen *graphic* items, keyed by reference.

    Footprint text (reference designators, hidden generator properties) is
    excluded on purpose: the reviewed plan legitimately overrides silk *text*
    placement, so only the geometry is expected to survive byte-for-byte.
    """
    return {
        footprint.reference: sum(1 for g in footprint.graphics if g.layer in SILK_LAYERS)
        for footprint in PCB.load(pcb_path).footprints
    }


def routed_artifact_pairs() -> list[tuple[Path, Path]]:
    """Every ``(unrouted, routed)`` committed demo-board artifact pair."""
    pairs = []
    for routed in sorted(REPO_ROOT.glob("boards/*/output/*_routed.kicad_pcb")):
        unrouted = routed.with_name(routed.name.replace("_routed.kicad_pcb", ".kicad_pcb"))
        if unrouted.is_file():
            pairs.append((unrouted, routed))
    return pairs


@pytest.fixture
def routing():
    return runpy.run_path(str(BOARD_03 / "routing_plan.py"))


def test_saved_plan_replay_preserves_footprint_silk_graphics(routing, tmp_path) -> None:
    """``apply_plan()`` must leave every footprint's silk geometry untouched."""
    before = silk_graphics_per_footprint(UNROUTED_03)
    assert sum(before.values()) > 0, "fixture board carries no footprint silk graphics"

    replayed = tmp_path / "replayed.kicad_pcb"
    assert routing["apply_plan"](UNROUTED_03, replayed)

    assert silk_graphics_per_footprint(replayed) == before


def test_saved_plan_replay_still_applies_reviewed_silk_text_placement(routing, tmp_path) -> None:
    """Preserving geometry must not stop the plan overriding silk *text*.

    The reviewed revision-B silk text placement (reference designators nudged
    by hand) is the whole reason the plan carries a ``"silkscreen"`` section,
    so the fix for #5744 must not throw it away along with the delete-all.
    """
    replayed = tmp_path / "replayed.kicad_pcb"
    assert routing["apply_plan"](UNROUTED_03, replayed)

    def reference_text_offset(pcb_path: Path, reference: str) -> tuple[float, float]:
        footprint = next(f for f in PCB.load(pcb_path).footprints if f.reference == reference)
        text = next(t for t in footprint.texts if t.text_type == "reference")
        return (round(text.position[0], 5), round(text.position[1], 5))

    # U1's reviewed designator sits left of the body, not above it.
    assert reference_text_offset(UNROUTED_03, "U1") == (0.0, -7.35)
    assert reference_text_offset(replayed, "U1") == (-1.5, 0.0)


@pytest.mark.parametrize(
    ("unrouted", "routed"),
    routed_artifact_pairs(),
    ids=lambda path: path.parent.parent.name if isinstance(path, Path) else path,
)
def test_committed_routed_artifact_keeps_footprint_silk_graphics(
    unrouted: Path, routed: Path
) -> None:
    """Routing changes copper, never footprint silkscreen geometry."""
    assert silk_graphics_per_footprint(routed) == silk_graphics_per_footprint(unrouted)


# ---------------------------------------------------------------------------
# The restored silk has to be manufacturable, not merely present
# ---------------------------------------------------------------------------


@pytest.fixture
def silk_repair():
    return runpy.run_path(str(BOARD_03 / "silk_repair.py"))


def test_committed_artifacts_meet_the_reviewed_silk_to_pad_floor(silk_repair) -> None:
    """Both board-03 artifacts clear ``jlcpcb-tier1``'s 0.15 mm silk-to-pad floor.

    The 21 ``silk_pad_clearance`` errors this board's library footprints carry
    (``J3`` at 0.1150 mm, ``R3``/``R4`` at 0.1002 mm) were invisible while the
    routed artifact had no silk at all.  They are repaired at the placement
    stage now, so assert the floor on the committed bytes of *both* artifacts
    -- the routed one is what CI's routed-DRC gate reads.
    """
    floor = silk_repair["silk_to_pad_floor"]("jlcpcb-tier1")
    assert floor == 0.15
    for artifact in (UNROUTED_03, BOARD_03 / "output/usb_joystick_routed.kicad_pcb"):
        assert silk_repair["residual_violations"](artifact, minimum_mm=floor) == []


def test_bare_generator_entry_point_also_emits_repaired_silk(
    routing, silk_repair, tmp_path
) -> None:
    """``generate_pcb.py`` itself must repair the silk, not only the full recipe.

    The silk-to-pad repair originally lived in ``generate_design.py:main()``.
    That is NOT sufficient: ``route_demo.py``,
    ``tests/test_board_03_regression.py``'s regeneration fixture and a bare
    ``python generate_pcb.py <dir>`` all produce a board and hand it straight
    to :func:`routing_plan.apply_plan`, which (post-#5744) carries footprint
    silk through untouched.  With the repair in the recipe only, those paths
    replayed a board carrying all 21 ``silk_pad_clearance`` errors while the
    committed artifact looked clean -- exactly the "latent because nothing
    measured it" shape of the original bug.  The repair therefore lives in
    ``generate_pcb.write_pcb()``, and this test pins it to the *entry point*.
    """
    generated = tmp_path / "usb_joystick.kicad_pcb"
    proc = subprocess.run(
        [sys.executable, str(BOARD_03 / "generate_pcb.py"), str(generated)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        cwd=str(BOARD_03),
    )
    if proc.returncode != 0 and "Required real KiCad footprint unavailable" in (
        proc.stdout + proc.stderr
    ):
        pytest.skip("KiCad footprint libraries not installed; generator cannot run")
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]

    floor = silk_repair["silk_to_pad_floor"]("jlcpcb-tier1")
    assert silk_repair["residual_violations"](generated, minimum_mm=floor) == []

    # ...and the replay of that freshly generated board stays inside the floor.
    replayed = tmp_path / "replayed.kicad_pcb"
    assert routing["apply_plan"](generated, replayed)
    assert silk_repair["residual_violations"](replayed, minimum_mm=floor) == []
    assert silk_graphics_per_footprint(replayed) == silk_graphics_per_footprint(generated)


_SYNTHETIC_BOARD = """(kicad_pcb (version 20240108) (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (37 "F.SilkS" user) (39 "F.Mask" user)
    (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (gr_rect (start 0 0) (end 20 20) (stroke (width 0.1) (type default)) (fill none)
    (layer "Edge.Cuts") (uuid "aaaa0000-0000-4000-8000-000000000001"))
  (footprint "Test:Chip"
    (at 10 10 0)
    (layer "F.Cu")
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS")
      (uuid "bbbb0000-0000-4000-8000-000000000001")
      (effects (font (size 1 1) (thickness 0.15))))
    (attr smd)
    (fp_line (start -1 -0.4) (end 1 -0.4) (stroke (width 0.12) (type solid))
      (layer "F.SilkS") (uuid "cccc0000-0000-4000-8000-000000000001"))
    (fp_line (start -1.25 -0.55) (end -0.35 -0.55) (stroke (width 0.12) (type solid))
      (layer "F.SilkS") (uuid "cccc0000-0000-4000-8000-000000000002"))
    (pad "1" smd rect (at -0.8 0) (size 0.9 0.9) (layers "F.Cu" "F.Paste" "F.Mask")
      (uuid "dddd0000-0000-4000-8000-000000000001"))
    (pad "2" smd rect (at 0.8 0) (size 0.9 0.9) (layers "F.Cu" "F.Paste" "F.Mask")
      (uuid "dddd0000-0000-4000-8000-000000000002"))
  )
)
"""


def test_repair_clips_a_crossing_line_and_nudges_a_parallel_one(silk_repair, tmp_path) -> None:
    """Both repair strategies, on a board built to need exactly one of each.

    ``(-1,-0.4)..(1,-0.4)`` runs *across* both pads, so clipping leaves the
    span between them.  ``(-1.25,-0.55)..(-0.35,-0.55)`` runs *parallel* to
    pad 1 along its whole length -- clipping would consume it entirely, so it
    must be nudged outward instead.  Neither graphic may be lost.
    """
    board = tmp_path / "synthetic.kicad_pcb"
    board.write_text(_SYNTHETIC_BOARD)
    assert silk_repair["residual_violations"](board, minimum_mm=0.15)

    result = silk_repair["trim_silk_to_pad_clearance"](board, minimum_mm=0.15)

    assert (result.lines_trimmed, result.lines_nudged, result.lines_removed) == (1, 1, 0)
    assert result.residual == []
    graphics = [
        (*g.start, *g.end) for g in PCB.load(board).footprints[0].graphics if g.layer in SILK_LAYERS
    ]
    assert len(graphics) == 2
    # Clipped back to the gap between the two pads.
    assert graphics[0] == pytest.approx((-0.13, -0.4, 0.13, -0.4), abs=1e-3)
    # Nudged perpendicular, away from pad 1; length unchanged.
    assert graphics[1] == pytest.approx((-1.25, -0.67, -0.35, -0.67), abs=1e-3)


def test_repair_pass_clears_violations_without_losing_graphics(silk_repair, tmp_path) -> None:
    """The repair is idempotent and never silently deletes a silk graphic.

    Re-running it on the already-repaired committed board must find nothing
    left to do (no drift on every regeneration), and the graphic count must
    stay at the unrouted artifact's 139 -- J3's pin-1 marker is *nudged*
    0.045 mm outward rather than clipped away.
    """
    board = tmp_path / "board.kicad_pcb"
    board.write_text(UNROUTED_03.read_text())
    before = silk_graphics_per_footprint(board)

    result = silk_repair["trim_silk_to_pad_clearance"](board, minimum_mm=0.15)

    assert result.residual == []
    assert not result.changed, "committed artifact should already be repaired"
    assert silk_graphics_per_footprint(board) == before
    assert sum(before.values()) == 139
