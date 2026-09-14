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

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

from kicad_tools.manufacturers.fabrication_process import (
    FabricationProcess,
    get_fabrication_process,
)

if TYPE_CHECKING:
    from kicad_tools.router.primitives import Pad


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


@dataclass(frozen=True)
class ComponentHoleContext:
    """The resolved nearest-other-drilled-hole distance for a candidate via.

    Reopened issue #5201: a bare ``nearest_other_hole_distance_mm=None``
    is ambiguous -- it means "skip this check" to
    :func:`via_geometry_eligible` (by design; see that function's
    docstring), but callers that DO have a component-hole census must
    distinguish two very different situations that both used to collapse
    onto that same ``None``:

    * The census is genuinely, verifiably EMPTY -- every other drilled
      hole on the board is accounted for and none is nearby (or there
      are none at all).  ``known=True``, ``nearest_distance_mm=math.inf``.
    * The census is UNKNOWN or INCOMPLETE -- the caller could not
      enumerate every other drilled hole (missing context), or the
      enumeration hit a through-hole pad with a missing/zero/unparseable
      drill diameter (its position is known but its hole radius is not,
      so its true clearance to the candidate cannot be computed).
      ``known=False``, ``nearest_distance_mm=None``.

    Callers MUST fail closed on ``known=False`` (refuse eligibility)
    rather than let it fall through to ``via_geometry_eligible``'s
    "skip the check" behaviour -- see :func:`via_in_pad_candidate_eligible`.
    """

    known: bool
    nearest_distance_mm: float | None


def resolve_component_hole_context(
    via_x: float,
    via_y: float,
    via_drill_mm: float,
    *,
    all_pads: Sequence[Pad] | None,
    exclude: object | None = None,
) -> ComponentHoleContext:
    """Resolve the nearest-other-component-hole distance for a candidate via.

    Issue #5201 (reopened): scans ``all_pads`` -- the COMPLETE physical
    hole census (``Autorouter.all_pads``, which preserves duplicate
    ``(ref, pin)`` holes that the lossy ``Autorouter.pads`` dict and
    net-target maps both drop) -- for through-hole pads other than the
    candidate's own pad, and returns the edge-to-edge distance from the
    candidate via's drill circle to the nearest one, using the same
    ``hypot(...) - via_r - hole_r`` geometry as
    :class:`~kicad_tools.validate.rules.via_in_pad.ViaInPadRule` so the
    router's decision and the downstream DRC pass never disagree.

    Args:
        via_x: Candidate via X position (board mm).
        via_y: Candidate via Y position (board mm).
        via_drill_mm: Candidate via drill diameter in mm.
        all_pads: The complete physical pad list, or ``None`` when no
            census is available to the caller.  ``None`` fails closed
            (``known=False``).
        exclude: The EXACT pad object hosting the candidate via (excluded
            from the "other hole" scan by OBJECT IDENTITY, not logical
            ``(ref, pin)`` key).  Issue #5201's own duplicate-holes
            requirement is the reason a footprint with repeated pad
            numbers (thermal-via arrays / EP paddles) needs
            ``all_pads`` in the first place -- excluding by a
            ``(ref, pin)`` string match would silently discard every
            OTHER physically distinct hole that happens to share that
            same logical name, defeating the exact guarantee this
            parameter exists to preserve.  The candidate pad supplied by
            escape/repair callers is normally an SMD pad (``through_hole``
            False), which the scan below already skips regardless -- this
            identity check only matters for the rare case a through-hole
            pad itself hosts the candidate via.

    Returns:
        A :class:`ComponentHoleContext`.  ``known=False`` when
        ``all_pads`` is ``None``, the candidate via's own position/drill
        is not a finite positive number, OR any OTHER through-hole pad
        has a position or drill diameter that is missing, non-numeric,
        non-finite, or non-positive (its hole radius/location cannot be
        computed, so its true clearance is unknown -- conservatively
        treated as "could be anywhere").  Otherwise ``known=True`` with
        ``nearest_distance_mm`` set to the minimum edge-to-edge distance
        found, or ``math.inf`` when the census contains no other
        through-hole pads at all (a genuinely empty census).
    """
    if all_pads is None:
        return ComponentHoleContext(known=False, nearest_distance_mm=None)
    if (
        not math.isfinite(via_x)
        or not math.isfinite(via_y)
        or not math.isfinite(via_drill_mm)
        or via_drill_mm <= 0.0
    ):
        return ComponentHoleContext(known=False, nearest_distance_mm=None)

    via_r = via_drill_mm / 2.0
    nearest: float | None = None
    for other in all_pads:
        if not getattr(other, "through_hole", False):
            continue
        if exclude is not None and other is exclude:
            continue
        ox, oy, drill = _coerce_finite_hole_geometry(other)
        if ox is None:
            # Unparseable/non-finite/non-positive geometry on a real
            # through-hole pad: its true position or hole radius cannot
            # be computed, so we cannot prove it is far enough away.
            # Fail closed for the WHOLE census rather than silently skip
            # this one hole -- Issue #5201's acceptance criterion treats
            # this the same as a wholly-unknown census.
            return ComponentHoleContext(known=False, nearest_distance_mm=None)
        assert oy is not None and drill is not None
        hole_r = drill / 2.0
        distance = math.hypot(ox - via_x, oy - via_y) - via_r - hole_r
        if not math.isfinite(distance):
            return ComponentHoleContext(known=False, nearest_distance_mm=None)
        if nearest is None or distance < nearest:
            nearest = distance

    if nearest is None:
        # Verified: no other through-hole pads exist anywhere on the
        # board.  A genuinely empty census is eligible-if-otherwise-
        # qualifying, not "unknown".
        return ComponentHoleContext(known=True, nearest_distance_mm=math.inf)
    return ComponentHoleContext(known=True, nearest_distance_mm=nearest)


def _coerce_finite_hole_geometry(
    pad: object,
) -> tuple[float, float, float] | tuple[None, None, None]:
    """Extract ``(x, y, drill)`` from a through-hole pad, validated finite.

    Issue #5201 (reopened): a malformed/unparseable drill (a non-numeric
    string) previously raised an uncaught ``ValueError``, and a ``NaN``
    drill previously produced a ``ComponentHoleContext`` that silently
    compared as eligible (every ``NaN`` comparison is ``False``, so the
    ``min_component_hole_distance_mm`` floor check never fired).  Both
    are "unparseable/unknown geometry" per the acceptance criterion, not
    proof of a safely-distant hole -- returns ``(None, None, None)`` for
    the caller to treat as fail-closed-unknown instead.
    """
    try:
        x = float(getattr(pad, "x", 0.0))
        y = float(getattr(pad, "y", 0.0))
        drill = float(getattr(pad, "drill", 0.0) or 0.0)
    except (TypeError, ValueError):
        return (None, None, None)
    if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(drill)):
        return (None, None, None)
    if drill <= 0.0:
        return (None, None, None)
    return (x, y, drill)


def via_in_pad_candidate_eligible(
    process: FabricationProcess,
    *,
    drill_mm: float,
    annular_ring_mm: float | None,
    hole_context: ComponentHoleContext,
) -> bool:
    """Return True when a candidate via is eligible, INCLUDING hole context.

    Issue #5201 (reopened): the owner's acceptance criterion is that an
    unknown/incomplete component-hole census must REFUSE eligibility
    rather than default to "no constraint" the way a bare
    ``nearest_other_hole_distance_mm=None`` does in
    :func:`via_geometry_eligible` (that function's ``None`` skip-check
    behaviour is intentional and stays pinned by
    ``test_unknown_annular_ring_and_hole_distance_skip_those_checks`` --
    it is the right contract for a caller that never carries hole
    context at all, e.g. the escape router's early pre-rescue gate).
    This wrapper is for callers that DO have a
    :class:`ComponentHoleContext` and must fail closed when it is
    unknown.

    Args:
        process: The resolved (board-level-eligible) fabrication process.
        drill_mm: Candidate via drill diameter in mm.
        annular_ring_mm: Candidate via annular ring width in mm, or
            ``None`` to skip that check (unrelated to hole context).
        hole_context: The resolved :class:`ComponentHoleContext` for this
            candidate (see :func:`resolve_component_hole_context`).

    Returns:
        ``False`` immediately when ``hole_context.known`` is ``False``.
        Otherwise, delegates to :func:`via_geometry_eligible` with the
        resolved ``nearest_distance_mm`` (``math.inf`` for a verified
        empty census correctly clears the process's
        ``min_component_hole_distance_mm`` floor).
    """
    if not hole_context.known:
        return False
    return via_geometry_eligible(
        process,
        drill_mm=drill_mm,
        annular_ring_mm=annular_ring_mm,
        nearest_other_hole_distance_mm=hole_context.nearest_distance_mm,
    )


def component_holes_for_router(router: object) -> list[Pad] | None:
    """Combine the saved physical census with current, possibly newly added pads."""
    pads = getattr(router, "all_pads", None)
    if not hasattr(router, "_loaded_component_holes"):
        return pads
    loaded = router._loaded_component_holes
    if loaded is None or pads is None:
        return None
    return [*loaded, *pads]


def component_holes_from_document(
    pcb: object, *, world_coordinates: bool = False
) -> list[Pad] | None:
    """Read every physical drill without applying routing-target filters.

    These records describe holes only. They must never be installed as copper
    pads on a routing grid. Unsupported drill geometry stays unknown.
    """
    try:
        footprints = getattr(pcb, "footprints", None)
        if footprints is not None:
            from .primitives import Pad as RouterPad

            origin = getattr(pcb, "board_origin", (0.0, 0.0)) if world_coordinates else (0.0, 0.0)
            census: list[Pad] = []
            for fp in footprints:
                ref = getattr(fp, "reference", "") or ""
                fp_x, fp_y = fp.position
                fp_x, fp_y = fp_x + origin[0], fp_y + origin[1]
                rot_rad = math.radians(-(getattr(fp, "rotation", 0.0) or 0.0))
                cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)
                for pad in fp.pads:
                    if getattr(pad, "type", "smd") not in ("thru_hole", "np_thru_hole"):
                        continue
                    px, py = pad.position
                    try:
                        drill = float(getattr(pad, "drill", 0.0) or 0.0)
                    except (TypeError, ValueError):
                        # Unparseable drill: stored as 0.0, which
                        # ``resolve_component_hole_context`` treats as
                        # unknown geometry and fails the WHOLE census
                        # closed -- correct, not a silent skip.
                        drill = 0.0
                    census.append(
                        RouterPad(
                            x=fp_x + px * cos_r - py * sin_r,
                            y=fp_y + px * sin_r + py * cos_r,
                            width=getattr(pad, "size", (0.0, 0.0))[0],
                            height=getattr(pad, "size", (0.0, 0.0))[1],
                            net=getattr(pad, "net_number", 0) or 0,
                            net_name="",
                            ref=ref,
                            pin=str(getattr(pad, "number", "")),
                            through_hole=True,
                            drill=drill,
                        )
                    )
            return census
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None
    return None
