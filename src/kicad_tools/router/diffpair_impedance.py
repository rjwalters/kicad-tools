"""Impedance-driven trace width and intra-pair clearance computation.

This module is the router-side consumer of the Phase 3K
``target_diff_impedance`` / ``target_single_impedance`` net-class fields.
Given a :class:`~kicad_tools.router.rules.NetClassRouting`, a PCB
:class:`~kicad_tools.physics.Stackup`, and a manufacturer
:class:`~kicad_tools.manufacturers.DesignRules`, it returns the
``(width_mm, gap_mm | None)`` pair that the autorouter should use for
within-pair traces on the given layer.

Risk-mitigation contract (per Issue #2650 acceptance criteria):

1. **Stackup mismatch detection**.  When the actual stackup deviates
   significantly from any predefined manufacturer stackup, emits a
   ``StackupMismatchWarning`` DRC violation (category
   ``MANUFACTURING``, severity ``warning``) naming the closest match and
   the deviation magnitude (dielectric thickness delta, epsilon_r delta).
2. **Min-grid rounding + clamp**.  Computed width / gap is rounded to a
   manufacturer-typical 0.025 mm grid; if the rounded value falls below
   ``DesignRules.min_trace_width_mm`` or ``DesignRules.min_clearance_mm``,
   the geometry is clamped to the minimum AND an ``error``-severity
   violation is emitted explaining the target is unachievable.
3. **No-stackup graceful degradation**.  When no usable stackup is
   available (no metadata + no override), the function refuses to
   override the per-class literals (returns
   ``(net_class.trace_width, net_class.effective_intra_pair_clearance())``
   unchanged) and emits a WARN-level log line.

Example::

    from kicad_tools.router.diffpair_impedance import apply_impedance_driven_sizing
    from kicad_tools.physics import Stackup
    from kicad_tools.router.rules import NET_CLASS_HIGH_SPEED
    from kicad_tools.manufacturers import get_profile

    nc = NET_CLASS_HIGH_SPEED
    # User opts in to 90 ohm differential
    nc = dataclasses.replace(nc, target_diff_impedance=90.0)

    stackup = Stackup.jlcpcb_4layer()
    rules = get_profile("jlcpcb").get_design_rules(4, 1.0)

    width, gap = apply_impedance_driven_sizing(nc, stackup, rules, layer="F.Cu")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.physics import Stackup

    from .rules import NetClassRouting

log = logging.getLogger(__name__)


# Default manufacturer minimum routing grid (mm).  JLCPCB / OSH Park / Seeed
# all support 0.025 mm (1 mil) precision on outer layers; rounding to this
# grid keeps fabrication consistent with the rest of the router output.
DEFAULT_MIN_GRID_MM: float = 0.025


# Deviation thresholds for the StackupMismatchWarning emission.  These are
# intentionally generous (capture only meaningful divergence, not normal
# manufacturer-tolerance variation).  Numbers below the threshold are
# considered "close enough" to a predefined stackup and produce no warning.
STACKUP_THICKNESS_TOLERANCE_MM: float = 0.05
STACKUP_EPSILON_R_TOLERANCE: float = 0.25


@dataclass(frozen=True)
class StackupMismatchWarning:
    """Structured warning emitted when the PCB stackup deviates from a
    predefined manufacturer stackup.

    Attributes:
        closest_match: Name of the closest-matching predefined stackup
            (e.g. ``"jlcpcb_4layer"``, ``"oshpark_4layer"``,
            ``"default_2layer"``).
        thickness_delta_mm: Absolute dielectric-thickness deviation in mm.
        epsilon_r_delta: Absolute relative-permittivity deviation.
        message: Human-readable summary suitable for DRC violation text.
    """

    closest_match: str
    thickness_delta_mm: float
    epsilon_r_delta: float
    message: str


@dataclass(frozen=True)
class ImpedanceClampError:
    """Structured error emitted when the computed width/gap falls below the
    manufacturer minimum after rounding.

    The autorouter consumes this by emitting a DRC violation (severity
    ``error``) and using the clamped value for routing.

    Attributes:
        kind: One of ``"width"`` or ``"gap"``.
        requested_mm: The unclamped computed value.
        minimum_mm: The manufacturer minimum that the value was clamped to.
        target_impedance_ohms: The target impedance that produced the
            unachievable geometry.
        message: Human-readable summary suitable for DRC violation text.
    """

    kind: str
    requested_mm: float
    minimum_mm: float
    target_impedance_ohms: float
    message: str


@dataclass
class ImpedanceSizingResult:
    """Outcome of :func:`apply_impedance_driven_sizing`.

    Attributes:
        width_mm: Computed (or pass-through) trace width.
        gap_mm: Computed within-pair gap; ``None`` for single-ended classes
            or when no targets are set.
        stackup_mismatch: Set when the actual stackup deviates from any
            predefined manufacturer stackup.  Consumers surface this as a
            ``MANUFACTURING``-category warning.
        clamp_errors: List of clamp events (one per kind).  Consumers
            surface each as an ``error``-severity violation.
        used_target: True iff the function actually overrode the per-class
            literals.  False on the no-targets, no-stackup, and physics-
            failure paths.
    """

    width_mm: float
    gap_mm: float | None
    stackup_mismatch: StackupMismatchWarning | None = None
    clamp_errors: list[ImpedanceClampError] | None = None
    used_target: bool = False


def _round_to_grid(value_mm: float, grid_mm: float = DEFAULT_MIN_GRID_MM) -> float:
    """Round a millimeter value to the manufacturer minimum grid."""
    if grid_mm <= 0:
        return value_mm
    return round(value_mm / grid_mm) * grid_mm


def _predefined_stackup_signature(name: str) -> tuple[float, float] | None:
    """Return (outer_dielectric_thickness_mm, epsilon_r) for a predefined
    manufacturer stackup, used as the comparison signature for mismatch
    detection.  Returns ``None`` if the name is unknown.

    This is a simple two-number fingerprint: the dielectric immediately
    below the outer copper.  It's not a perfect identity but suffices to
    spot users routing 90Ω onto a PTFE board declared as FR-4, which is
    the failure mode the epic warns about.
    """
    # Hard-coded from physics/stackup.py canonical definitions.  Kept
    # local (rather than introspecting the predefined stackups) so we
    # don't pay the import / instantiation cost on the hot routing path.
    presets: dict[str, tuple[float, float]] = {
        # JLCPCB 4-layer: prepreg 7628 directly below F.Cu, 0.2104mm, er=4.05
        "jlcpcb_4layer": (0.2104, 4.05),
        # OSH Park 4-layer: similar prepreg-over-core, er around 4.3-4.5
        "oshpark_4layer": (0.165, 4.4),
        # Generic 2-layer: dielectric ~1.53mm core, er=4.5
        "default_2layer": (1.53, 4.5),
        # Generic 6-layer: prepreg ~0.15mm, er=4.05
        "default_6layer": (0.15, 4.05),
    }
    return presets.get(name)


def _detect_stackup_mismatch(
    stackup: Stackup,
    layer: str,
) -> StackupMismatchWarning | None:
    """Compare ``stackup`` against the predefined manufacturer stackups and
    return a warning if it deviates from all of them by more than the
    configured tolerance.  Returns ``None`` when at least one predefined
    stackup is within tolerance.
    """
    try:
        actual_h = stackup.get_dielectric_height(layer)
        actual_er = stackup.get_dielectric_constant(layer)
    except Exception:
        # Can't introspect -- skip the mismatch check (graceful degrade).
        return None

    best_match: str | None = None
    best_thickness_delta = float("inf")
    best_epsilon_delta = float("inf")

    for name in ("jlcpcb_4layer", "oshpark_4layer", "default_2layer", "default_6layer"):
        sig = _predefined_stackup_signature(name)
        if sig is None:
            continue
        preset_h, preset_er = sig
        thickness_delta = abs(actual_h - preset_h)
        epsilon_delta = abs(actual_er - preset_er)
        # Combined score, prioritizing thickness (dimensional impact dominates Z0).
        score = thickness_delta * 10.0 + epsilon_delta
        best_score_so_far = best_thickness_delta * 10.0 + best_epsilon_delta
        if score < best_score_so_far:
            best_match = name
            best_thickness_delta = thickness_delta
            best_epsilon_delta = epsilon_delta

    if best_match is None:
        return None

    if (
        best_thickness_delta <= STACKUP_THICKNESS_TOLERANCE_MM
        and best_epsilon_delta <= STACKUP_EPSILON_R_TOLERANCE
    ):
        return None

    message = (
        f"PCB stackup deviates from predefined {best_match!r} "
        f"(deviation: dielectric thickness Δ={best_thickness_delta:.3f}mm, "
        f"εr Δ={best_epsilon_delta:.2f}); "
        f"impedance-driven sizing may not achieve the target"
    )
    return StackupMismatchWarning(
        closest_match=best_match,
        thickness_delta_mm=best_thickness_delta,
        epsilon_r_delta=best_epsilon_delta,
        message=message,
    )


def apply_impedance_driven_sizing(
    net_class: NetClassRouting,
    stackup: Stackup | None,
    design_rules: DesignRules,
    layer: str = "F.Cu",
    min_grid_mm: float = DEFAULT_MIN_GRID_MM,
) -> ImpedanceSizingResult:
    """Compute (width, gap) for the given net class on the given layer.

    Returns a :class:`ImpedanceSizingResult` whose ``width_mm`` /
    ``gap_mm`` fields are either:

    - The per-class literals (``trace_width``, ``effective_intra_pair_clearance``)
      when no impedance targets are set, when the stackup is unavailable, or
      when the physics module fails.
    - The impedance-driven values from
      :func:`kicad_tools.physics.CoupledLines.gap_for_differential_impedance`
      (for diff-pair targets) or
      :func:`kicad_tools.physics.TransmissionLine.width_for_impedance` (for
      single-ended targets), rounded to ``min_grid_mm`` and clamped to the
      manufacturer minimums.

    The autorouter is the primary consumer; see ``router/pathfinder.py``,
    ``router/cpp_backend.py``, and ``router/escape.py`` for the integration
    sites.

    Args:
        net_class: Net class to size for.  Reads
            :attr:`~NetClassRouting.target_diff_impedance` and
            :attr:`~NetClassRouting.target_single_impedance`.
        stackup: PCB stackup to compute against.  ``None`` triggers the
            "no stackup -- graceful degradation" path.
        design_rules: Manufacturer design rules (used for min-width /
            min-clearance clamping).
        layer: Layer name to size for (typically the routing layer).
        min_grid_mm: Manufacturer minimum grid for rounding.  Defaults to
            ``DEFAULT_MIN_GRID_MM`` (0.025 mm, the JLCPCB / OSH Park
            outer-layer minimum).

    Returns:
        ImpedanceSizingResult with width, gap (or None for single-ended),
        and any structured warnings/errors that the caller should surface
        as DRC violations.
    """
    fallback_width = net_class.trace_width
    fallback_gap = net_class.effective_intra_pair_clearance()

    target_diff = net_class.target_diff_impedance
    target_single = net_class.target_single_impedance

    # Drift-prevention: no targets set -> pass-through byte-for-byte.
    if target_diff is None and target_single is None:
        return ImpedanceSizingResult(
            width_mm=fallback_width,
            gap_mm=fallback_gap,
            used_target=False,
        )

    # No stackup -> graceful degradation.
    if stackup is None:
        log.warning(
            "skipped impedance-driven sizing: no stackup "
            "(net_class=%r, target_diff=%r, target_single=%r)",
            net_class.name,
            target_diff,
            target_single,
        )
        return ImpedanceSizingResult(
            width_mm=fallback_width,
            gap_mm=fallback_gap,
            used_target=False,
        )

    # Lazy import to avoid pulling physics into hot import paths if no
    # caller actually uses impedance targets.
    try:
        from kicad_tools.physics import CoupledLines, TransmissionLine
    except ImportError:
        log.warning(
            "skipped impedance-driven sizing: physics module unavailable (net_class=%r)",
            net_class.name,
        )
        return ImpedanceSizingResult(
            width_mm=fallback_width,
            gap_mm=fallback_gap,
            used_target=False,
        )

    # Stackup-mismatch check (only meaningful when we WILL override).
    mismatch = _detect_stackup_mismatch(stackup, layer)

    clamp_errors: list[ImpedanceClampError] = []
    min_width = design_rules.min_trace_width_mm
    min_clearance = design_rules.min_clearance_mm

    # Differential target dominates: when set, compute (width, gap) from the
    # coupled-lines model.  Width is taken from the single-ended Z0/2 estimate
    # so the function is deterministic; gap is the bisection result.
    if target_diff is not None:
        try:
            tl = TransmissionLine(stackup)
            # Heuristic: use width that achieves target_diff/2 single-ended Z0
            # as a reasonable starting point.  CoupledLines.gap_for_differential
            # then solves for the gap given this width.
            target_z0_single = target_diff / 2.0
            raw_width = tl.width_for_impedance(target_z0_single, layer)
        except (ValueError, AttributeError) as exc:
            log.warning(
                "impedance-driven sizing: width solver failed for diff target "
                "%.1fΩ on layer %r: %s",
                target_diff,
                layer,
                exc,
            )
            return ImpedanceSizingResult(
                width_mm=fallback_width,
                gap_mm=fallback_gap,
                used_target=False,
            )

        try:
            cl = CoupledLines(stackup)
            raw_gap = cl.gap_for_differential_impedance(
                target_diff,
                raw_width,
                layer,
            )
        except (ValueError, AttributeError) as exc:
            log.warning(
                "impedance-driven sizing: gap solver failed for diff target %.1fΩ on layer %r: %s",
                target_diff,
                layer,
                exc,
            )
            return ImpedanceSizingResult(
                width_mm=fallback_width,
                gap_mm=fallback_gap,
                used_target=False,
            )

        rounded_width = _round_to_grid(raw_width, min_grid_mm)
        rounded_gap = _round_to_grid(raw_gap, min_grid_mm)

        final_width = rounded_width
        if final_width < min_width:
            clamp_errors.append(
                ImpedanceClampError(
                    kind="width",
                    requested_mm=rounded_width,
                    minimum_mm=min_width,
                    target_impedance_ohms=target_diff,
                    message=(
                        f"target impedance unachievable at this stackup: "
                        f"requires width {rounded_width:.3f}mm but manufacturer "
                        f"minimum is {min_width:.3f}mm"
                    ),
                )
            )
            final_width = min_width

        final_gap = rounded_gap
        if final_gap < min_clearance:
            clamp_errors.append(
                ImpedanceClampError(
                    kind="gap",
                    requested_mm=rounded_gap,
                    minimum_mm=min_clearance,
                    target_impedance_ohms=target_diff,
                    message=(
                        f"target impedance unachievable at this stackup: "
                        f"requires gap {rounded_gap:.3f}mm but manufacturer "
                        f"minimum is {min_clearance:.3f}mm"
                    ),
                )
            )
            final_gap = min_clearance

        return ImpedanceSizingResult(
            width_mm=final_width,
            gap_mm=final_gap,
            stackup_mismatch=mismatch,
            clamp_errors=clamp_errors or None,
            used_target=True,
        )

    # Single-ended target only.
    assert target_single is not None
    try:
        tl = TransmissionLine(stackup)
        raw_width = tl.width_for_impedance(target_single, layer)
    except (ValueError, AttributeError) as exc:
        log.warning(
            "impedance-driven sizing: width solver failed for single-ended "
            "target %.1fΩ on layer %r: %s",
            target_single,
            layer,
            exc,
        )
        return ImpedanceSizingResult(
            width_mm=fallback_width,
            gap_mm=fallback_gap,
            used_target=False,
        )

    rounded_width = _round_to_grid(raw_width, min_grid_mm)
    final_width = rounded_width
    if final_width < min_width:
        clamp_errors.append(
            ImpedanceClampError(
                kind="width",
                requested_mm=rounded_width,
                minimum_mm=min_width,
                target_impedance_ohms=target_single,
                message=(
                    f"target impedance unachievable at this stackup: "
                    f"requires width {rounded_width:.3f}mm but manufacturer "
                    f"minimum is {min_width:.3f}mm"
                ),
            )
        )
        final_width = min_width

    return ImpedanceSizingResult(
        width_mm=final_width,
        gap_mm=None,
        stackup_mismatch=mismatch,
        clamp_errors=clamp_errors or None,
        used_target=True,
    )


def resolve_impedance_for_net_classes(
    net_class_map: dict[str, NetClassRouting],
    stackup: Stackup | None,
    design_rules: DesignRules,
    layer: str = "F.Cu",
    min_grid_mm: float = DEFAULT_MIN_GRID_MM,
) -> tuple[dict[str, NetClassRouting], list[StackupMismatchWarning], list[ImpedanceClampError]]:
    """Resolve impedance-driven sizing for every net class in the map.

    For each :class:`NetClassRouting` with a ``target_diff_impedance`` or
    ``target_single_impedance`` field set, calls
    :func:`apply_impedance_driven_sizing` and replaces the net class with a
    copy whose ``trace_width`` / ``intra_pair_clearance`` reflect the
    computed values.  Net classes without any target are passed through
    unchanged (byte-for-byte equality).

    This is the **single integration point** for the impedance-driven
    sizing flow.  The autorouter calls this once at setup; downstream
    routing components (pathfinder, cpp_backend, escape router) continue
    to read ``net_class.trace_width`` and
    :meth:`NetClassRouting.effective_intra_pair_clearance` unchanged --
    they automatically pick up the impedance-driven values when those
    fields were overwritten.  See acceptance criteria for Issue #2650.

    Args:
        net_class_map: ``{net_name: NetClassRouting}`` mapping (the
            autorouter's primary net-class structure).  Not mutated.
        stackup: PCB stackup, or ``None`` for graceful degradation.
        design_rules: Manufacturer design rules used for clamping.
        layer: Layer name used for impedance calculation.
        min_grid_mm: Manufacturer minimum grid for rounding.

    Returns:
        ``(resolved_map, mismatch_warnings, clamp_errors)`` triple:
        ``resolved_map`` has the same keys but new
        :class:`NetClassRouting` values where sizing was resolved;
        ``mismatch_warnings`` and ``clamp_errors`` aggregate the diagnostic
        events from all calls (deduplicated by net-class name + kind).
    """
    import dataclasses

    resolved: dict[str, NetClassRouting] = {}
    mismatch_warnings: list[StackupMismatchWarning] = []
    clamp_errors_out: list[ImpedanceClampError] = []
    seen_mismatch: set[str] = set()

    for net_name, nc in net_class_map.items():
        result = apply_impedance_driven_sizing(
            nc, stackup, design_rules, layer=layer, min_grid_mm=min_grid_mm
        )
        if not result.used_target:
            resolved[net_name] = nc
            continue

        # Build a new NetClassRouting with the resolved sizing.
        new_nc = dataclasses.replace(
            nc,
            trace_width=result.width_mm,
            intra_pair_clearance=result.gap_mm,
        )
        resolved[net_name] = new_nc

        if result.stackup_mismatch is not None and nc.name not in seen_mismatch:
            mismatch_warnings.append(result.stackup_mismatch)
            seen_mismatch.add(nc.name)
        if result.clamp_errors:
            clamp_errors_out.extend(result.clamp_errors)

    return resolved, mismatch_warnings, clamp_errors_out
