"""Recipe post-route silkscreen clearance pass (Issue #9033).

``silk_hole_overlap`` grades as an **error** under the JLCPCB profiles
(Issue #9033's ``silk_to_hole_danger_mm`` band), so a routed board whose
reference designators or footprint silk land on a drill now fails the
manufacturing gates instead of carrying a warning.  The findings are a
side effect of *routing* -- vias land where the router puts them, over
silk the generator authored before any copper existed -- so the recipes
clear them after the last via-producing step, reading the rule's own
findings so the pass tracks whatever via population this run produced.

The two halves are the CLI tools' backing passes, applied in the order
their help text prescribes (``fix-silkscreen --clip-to-holes`` first:
"text is left for place-silk-refs --over-holes"):

* :meth:`kicad_tools.drc.repair_silkscreen.SilkscreenRepairer.repair_hole_clearance`
  clips footprint silk *graphics* (``fp_line``/``fp_poly``/``fp_rect``)
  off every drilled hole;
* :meth:`kicad_tools.silkscreen.place_refs.SilkRefPlacer.plan` with
  ``only=references_over_holes(pcb)`` moves exactly the reference
  designators the rule flags (Issue #9054).

The pass is deliberately **fail-loud**: whatever the rule still flags
after both halves -- a graphic that cannot be clipped (an arc/circle over
a drill), a reference with no legal spot inside the placer's search
budget, or silk neither half owns (free ``gr_text`` such as an operator
legend) -- raises :class:`SilkClearanceError` instead of shipping silk
the fab will remove, the same posture as ``kct fix-silkscreen
--clip-to-holes``' residual exit.  A recipe that hits it should widen its
generator's silk, not silence the error.

The router never calls this (#5744: silk repair is a recipe step, not a
routing concern); it runs after routing so it sees the routed vias.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path


class SilkClearanceError(RuntimeError):
    """A silk-over-hole finding survived the clearance pass."""


def _hole_findings(pcb) -> list:
    """Every ``silk_hole_overlap`` finding on *pcb* at the rule's 0 mm band.

    Bare :class:`DesignRules` keep the rule's own overlap threshold (the
    same 0 mm the jlcpcb profiles' ``silk_to_hole_danger_mm: 0.0`` grades),
    so the residual verdict matches what the manufacturing gates count.
    """
    from kicad_tools.manufacturers.base import DesignRules
    from kicad_tools.validate.rules.silkscreen import check_silk_hole_overlap

    rules = DesignRules(
        min_trace_width_mm=0.0,
        min_clearance_mm=0.0,
        min_via_drill_mm=0.0,
        min_via_diameter_mm=0.0,
        min_annular_ring_mm=0.0,
    )
    return check_silk_hole_overlap(pcb, rules).violations


def clear_silk_over_holes(
    pcb_path: str | Path,
    *,
    mask_clearance_mm: float = 0.05,
    log: Callable[[str], None] | None = None,
) -> tuple[int, int]:
    """Clear every ``silk_hole_overlap`` finding on a routed PCB.

    Clips silk graphics off drilled holes, then moves the reference
    designators the rule still flags.  ``pcb_path`` is re-saved in place
    only when a half actually changed something.

    Args:
        pcb_path: Routed ``.kicad_pcb`` to clear; mutated and re-saved
            in place.
        mask_clearance_mm: Solder-mask margin used to size pad/via
            apertures while planning reference moves (matches
            ``DesignRules.min_solder_mask_clearance_mm``; the placer's
            own default).
        log: Optional sink for per-half progress lines (recipes pass
            ``print``).

    Returns:
        ``(graphics_clipped, references_moved)``.

    Raises:
        SilkClearanceError: the rule still reports a ``silk_hole_overlap``
            finding after both halves ran -- an unclippable graphic, an
            unplaceable reference, or silk neither half moves (free text).
    """
    from kicad_tools.drc.repair_silkscreen import SilkscreenRepairer
    from kicad_tools.silkscreen.place_refs import SilkRefPlacer, references_over_holes

    def _say(message: str) -> None:
        if log is not None:
            log(message)

    path = Path(pcb_path)

    repairer = SilkscreenRepairer(path)
    hole_result = repairer.repair_hole_clearance()
    if hole_result.total_fixed:
        repairer.save(path)
    _say(
        f"   silk hole clip: {hole_result.total_fixed} graphic(s) clipped, "
        f"{len(hole_result.skipped)} skipped (arc/circle)"
    )

    placer = SilkRefPlacer(path)
    over = references_over_holes(placer.pcb)
    moved = 0
    if over:
        plan = placer.plan(only=over, mask_clearance_mm=mask_clearance_mm)
        moved = placer.apply(plan)
        if moved:
            placer.save(path)
    _say(f"   silk ref placement: {moved} reference(s) moved off holes")

    # Fail loud on survivors: re-parse the saved file and re-run the rule's
    # own finding set, so anything the two halves leave behind -- a clipped-
    # but-residual graphic, a reference the placer could not place, or silk
    # neither half owns (free ``gr_text`` such as an operator legend) --
    # fails the recipe here, not a CI gate later.  (``apply`` mutates raw
    # S-exp nodes; the in-memory ``PCB`` footprint positions are stale, so
    # the verdict is read from a fresh parse.  Bare rules keep the rule's
    # 0 mm overlap threshold, the same band the jlcpcb profiles grade.)
    survivors = sorted(" / ".join(v.items) for v in _hole_findings(SilkRefPlacer(path).pcb))
    if survivors:
        raise SilkClearanceError(
            f"silk still overlaps drilled hole(s) after the clearance pass: {', '.join(survivors)}"
        )
    return hole_result.total_fixed, moved
