"""Shared via-in-pad process-eligibility resolution for router decisions.

Issue #5201: the escape router's opportunistic in-pad via placement
(:mod:`kicad_tools.router.escape`) and the post-route auto-fix repair
sweep (:mod:`kicad_tools.router.drc_nudge`) each independently reasoned
about via-in-pad eligibility from the bare
``MfrLimits.via_in_pad_supported`` capability boolean -- the same gap
issue #5009 closed on the validate side by attaching a real, orderable
:class:`~kicad_tools.manufacturers.fabrication_process.FabricationProcess`
to specific layer/copper configurations
(``DesignRules.via_in_pad_process_id``). A manufacturer tier's bare
capability flag only says via-in-pad is offered *somewhere* in the
catalog -- the canonical example is JLCPCB Capability Plus
(``jlcpcb-tier1``): every layer configuration sets
``via_in_pad_supported: true``, but its via-in-pad-specific POFV process
publishes a 4-layer minimum, so a 2-layer board at that tier has no
orderable process.

This module is the single place router decision points resolve:

1. Whether via-in-pad is board-level eligible at all -- the manufacturer
   supports it AND declares a process whose layer-count floor this
   board's ACTUAL copper-layer count satisfies (:func:`resolve_process`).
2. Whether a SPECIFIC candidate via's geometry (drill, annular ring,
   distance to the nearest other drilled hole) satisfies that process's
   published envelope (:func:`via_geometry_eligible`), delegating to
   :meth:`~kicad_tools.manufacturers.fabrication_process.FabricationProcess.eligibility_reasons`
   -- the exact predicate
   :class:`~kicad_tools.validate.rules.via_in_pad.ViaInPadRule` applies
   downstream, so the router never disagrees with the DRC pass that
   follows it.

Fails closed throughout: an unresolvable manufacturer, profile, or
process, or an unknown board layer count, never grants eligibility
(Issue #5201 acceptance criteria: "missing or unknown context must not
silently grant eligibility").
"""

from __future__ import annotations

from kicad_tools.manufacturers.fabrication_process import (
    FabricationProcess,
    get_fabrication_process,
)


def resolve_process(
    manufacturer: str | None,
    layer_count: int | None,
) -> FabricationProcess | None:
    """Resolve the eligible :class:`FabricationProcess` for a board, if any.

    Returns ``None`` (fail closed) when:

    * ``manufacturer`` is unset or unrecognized.
    * The manufacturer's :class:`~kicad_tools.router.mfr_limits.MfrLimits`
      does not advertise ``via_in_pad_supported``.
    * ``layer_count`` is ``None`` -- an unknown board layer count must
      never silently grant eligibility.
    * The resolved profile carries no ``via_in_pad_process_id`` for this
      layer count, or the id does not name a known process.
    * The declared process's ``min_layer_count`` exceeds ``layer_count``.

    Args:
        manufacturer: Manufacturer/tier identifier (e.g. ``"jlcpcb-tier1"``).
        layer_count: The board's actual copper-layer count, or ``None``
            when unknown.

    Returns:
        The eligible :class:`FabricationProcess`, or ``None``.
    """
    if not manufacturer or layer_count is None:
        return None

    try:
        from kicad_tools.router.mfr_limits import get_mfr_limits

        limits = get_mfr_limits(manufacturer)
    except (ValueError, ImportError):
        return None
    if not bool(getattr(limits, "via_in_pad_supported", False)):
        return None

    try:
        from kicad_tools.manufacturers import get_profile

        design_rules = get_profile(manufacturer).get_design_rules(layers=layer_count)
    except (ValueError, KeyError, ImportError, IndexError):
        return None

    process = get_fabrication_process(getattr(design_rules, "via_in_pad_process_id", None))
    if process is None:
        return None
    if layer_count < process.min_layer_count:
        return None
    return process


def via_geometry_eligible(
    process: FabricationProcess,
    *,
    drill_mm: float,
    annular_ring_mm: float | None = None,
    nearest_other_hole_distance_mm: float | None = None,
) -> bool:
    """Return True when a candidate via's geometry satisfies ``process``.

    Layer count is intentionally excluded here -- :func:`resolve_process`
    already folded the board-level layer-count floor into whether
    ``process`` was resolved at all, so a caller holding a non-``None``
    ``process`` has already cleared that gate. This function checks only
    the PER-VIA geometric envelope (drill range, annular ring,
    component-hole distance) via
    :meth:`~kicad_tools.manufacturers.fabrication_process.FabricationProcess.eligibility_reasons`
    -- the exact predicate the validate-side ``via_in_pad`` DRC rule
    applies, so a via the router treats as eligible is never one DRC then
    rejects.

    Args:
        process: The resolved (board-level-eligible) fabrication process.
        drill_mm: Candidate via drill diameter in mm.
        annular_ring_mm: Candidate via annular ring width in mm
            (``(via.size - via.drill) / 2``), or ``None`` when the via's
            total diameter is not yet known (the check is skipped).
        nearest_other_hole_distance_mm: Distance from the candidate via to
            the nearest OTHER component's drilled hole, or ``None`` when
            not computed (the check is skipped).

    Returns:
        ``True`` when no eligibility reason fires.
    """
    return not process.eligibility_reasons(
        layer_count=None,
        drill_mm=drill_mm,
        annular_ring_mm=annular_ring_mm,
        nearest_other_hole_distance_mm=nearest_other_hole_distance_mm,
    )
