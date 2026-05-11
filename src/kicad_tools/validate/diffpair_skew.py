"""Re-derive diff-pair length-skew data from a routed PCB + net-class map.

Issue #2675, Epic #2556 Phase 2.5c.

The :class:`~kicad_tools.validate.rules.diffpair_length_skew.DiffPairLengthSkewRule`
(Phase 3J / Issue #2649) consumes caller-supplied ``skew_data:
dict[tuple[str, str], float]`` and per-pair ``threshold_map``.  The rule
itself deliberately does NOT re-derive skew data to avoid drift between
the router's per-pair length tracker
(:class:`~kicad_tools.router.diffpair_length.DiffPairLengthTracker`) and
the validator's skew check.

This module provides the producer side -- a thin shim that:

1. Detects differential pairs on a routed PCB using the same layered
   detector the router uses (``diffpair_detection.detect_diff_pairs``).
2. Sums per-net length from PCB-side segments + vias (via
   :meth:`DiffPairLengthTracker.measure_net_from_pcb`) for each detected
   pair's two halves.
3. Builds the ``skew_data`` and per-pair ``threshold_map`` from each
   pair's net class :meth:`NetClassRouting.effective_skew_tolerance`.

Mirrors the shape of :mod:`kicad_tools.validate.diffpair_engagement`
(Phase 2.5b, PR #2653) exactly -- this is the sister producer-side wiring
for the skew rule.

Why re-derive instead of persist?

The recommended approach per the curator review on #2652 / #2675 is to
recover skew at validation time rather than persist it as PCB metadata.
PCB segment/via geometry IS the source of truth for routed length; the
skew is a pure function of (net_id, geometry, net_class_map.skew_tolerance).
This avoids needing to round-trip Phase 3H's
:class:`DiffPairLengthTracker` state through the PCB schema and keeps
the validate->router boundary thin.

Boundary discipline:

This module is one of TWO places the ``validate/`` package depends on
the ``router/`` package's diff-pair primitives (the other being
:mod:`kicad_tools.validate.diffpair_engagement`).  The rule itself
remains router-independent.  Tests live in
``tests/test_validate_diffpair_skew.py`` and the drift-prevention test
mirrors PR #2653's pattern.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.router.rules import NetClassRouting
    from kicad_tools.schema.pcb import PCB


logger = logging.getLogger(__name__)


def derive_skew_data(
    pcb: PCB,
    net_class_map: dict[str, NetClassRouting] | None,
    board_thickness_mm: float | None = None,
    num_copper_layers: int = 2,
) -> tuple[dict[tuple[str, str], float], dict[tuple[int, int], float]]:
    """Re-derive ``(skew_data, threshold_map)`` from a routed PCB.

    Walks the PCB's net table, runs the layered differential-pair
    detector, and sums per-net length from PCB-side segments + vias for
    each detected pair.  Returns the inputs the
    :class:`~kicad_tools.validate.rules.diffpair_length_skew.DiffPairLengthSkewRule`
    expects.

    Args:
        pcb: The routed PCB to inspect.  ``pcb.nets`` is consulted for
            the detector; ``pcb.segments_in_net`` and ``pcb.vias_in_net``
            are consulted for length measurement.
        net_class_map: Map of ``{net_name: NetClassRouting}`` (the
            autorouter convention used by
            :attr:`~kicad_tools.router.core.AutoRouter.net_class_map`).
            ``None`` or empty returns ``({}, {})`` so the standalone
            ``kct check`` path degrades gracefully to a no-op.
        board_thickness_mm: Total stackup thickness in mm.  When
            ``None`` (the default), vias contribute ``0.0`` to the
            length.  Mirrors
            :meth:`~kicad_tools.router.diffpair_length.DiffPairLengthTracker.record_routes`
            and the curator's documented policy on #2647.
        num_copper_layers: Number of copper layers in the stack (used
            to compute per-via drilled length when ``board_thickness_mm``
            is supplied).  Defaults to ``2``.

    Returns:
        ``(skew_data, threshold_map)`` where

        * ``skew_data`` is a ``{(p_net_name, n_net_name) -> skew_mm}``
          dict matching :meth:`DiffPairLengthTracker.get_all_skews`
          shape.  Pairs with neither half routed are omitted (graceful
          degradation, matching the producer-side tracker's behaviour).
        * ``threshold_map`` is a ``{(min_net_id, max_net_id) ->
          tolerance_mm}`` map of per-pair skew tolerance overrides.
          Each pair's tolerance comes from
          :meth:`NetClassRouting.effective_skew_tolerance` on its net
          class (with the rule's module-level
          :data:`DEFAULT_SKEW_TOLERANCE_MM` 0.5 mm fallback).

    Notes:
        Idempotence guarantee (drift-prevention AC): given the same
        physical routing and the same net classes, this function returns
        the same ``skew_data`` as the producer-side
        :meth:`DiffPairLengthTracker.get_all_skews` for routes recorded
        via :meth:`DiffPairLengthTracker.record_routes`.  This is the
        property tested in ``tests/test_validate_diffpair_skew.py``.
    """
    if not net_class_map:
        return {}, {}

    # Local imports keep the validate -> router boundary explicit: this
    # is one of two modules in validate/ that depend on router/ diff-pair
    # primitives.  See module docstring.
    from kicad_tools.router.diffpair_detection import detect_diff_pairs
    from kicad_tools.router.diffpair_length import DiffPairLengthTracker
    from kicad_tools.validate.rules.diffpair_length_skew import (
        DEFAULT_SKEW_TOLERANCE_MM,
    )

    # Build the {net_id: net_name} map the detector expects.
    net_names: dict[int, str] = {}
    for net_id, net in pcb.nets.items():
        net_name = getattr(net, "name", None)
        if not net_name:
            continue
        net_names[net_id] = net_name

    if not net_names:
        return {}, {}

    # Synthesise a {net_name: class_name} map so the layered detector
    # can consult ``NetClassRouting.diffpair_partner`` explicit
    # declarations.  Mirrors the autorouter convention -- see
    # ``DiffPairRouter._resolve_detection_inputs`` (router-side) and the
    # sister :func:`derive_engagement_state` (validate-side).
    net_to_class: dict[str, str] = {}
    synth_routing: dict = dict(net_class_map)
    for net_name, nc in net_class_map.items():
        cls_name = getattr(nc, "name", None)
        if cls_name is None:
            continue
        net_to_class[net_name] = cls_name
        synth_routing.setdefault(cls_name, nc)

    # KiCad-group templates declared in the PCB itself (optional).
    # Mirrors derive_engagement_state.
    kicad_groups = getattr(pcb, "kicad_diff_pair_groups", None)

    try:
        detected = detect_diff_pairs(
            net_names,
            net_class_routing=synth_routing,
            net_to_class=net_to_class,
            kicad_groups=kicad_groups,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[diffpair-skew] detector raised %s; treating as no skew data",
            exc,
        )
        return {}, {}

    skew_data: dict[tuple[str, str], float] = {}
    threshold_map: dict[tuple[int, int], float] = {}

    for detected_pair in detected:
        pair = detected_pair.pair
        p_id = pair.positive.net_id
        n_id = pair.negative.net_id
        p_name = pair.positive.net_name
        n_name = pair.negative.net_name

        # Measure each side.  ``measure_net_from_pcb`` returns 0.0 for
        # unrouted nets (no segments + no vias = 0.0), which matches the
        # producer-side tracker's "missing half is omitted" behaviour
        # ONLY if we also gate-out pairs where neither side has any
        # geometry.  We mirror the tracker semantics exactly: skip pairs
        # where at least one side has zero recorded geometry.
        p_has_geom = _net_has_geometry(pcb, p_id)
        n_has_geom = _net_has_geometry(pcb, n_id)
        if not p_has_geom or not n_has_geom:
            logger.debug(
                "[diffpair-skew] %s <-> %s skipped (P routed=%s, N routed=%s)",
                p_name,
                n_name,
                p_has_geom,
                n_has_geom,
            )
            continue

        l_p = DiffPairLengthTracker.measure_net_from_pcb(
            pcb, p_id, board_thickness_mm, num_copper_layers
        )
        l_n = DiffPairLengthTracker.measure_net_from_pcb(
            pcb, n_id, board_thickness_mm, num_copper_layers
        )
        skew_mm = abs(l_p - l_n)
        skew_data[(p_name, n_name)] = skew_mm

        # Per-pair threshold: prefer the positive side's net class, fall
        # back to the negative side's, then to the module-level default.
        # Mirrors the one-sided-declaration policy in
        # :func:`derive_engagement_state` (diffpair_engagement.py:179-187).
        key = (p_id, n_id) if p_id <= n_id else (n_id, p_id)
        nc_p = net_class_map.get(p_name)
        nc_n = net_class_map.get(n_name)
        nc = nc_p if nc_p is not None else nc_n
        if nc is not None and hasattr(nc, "effective_skew_tolerance"):
            threshold_map[key] = nc.effective_skew_tolerance(
                default=DEFAULT_SKEW_TOLERANCE_MM,
            )
        else:
            threshold_map[key] = DEFAULT_SKEW_TOLERANCE_MM

        logger.info(
            "[diffpair-skew] %s <-> %s skew=%.3f mm (tolerance=%.3f)",
            p_name,
            n_name,
            skew_mm,
            threshold_map[key],
        )

    return skew_data, threshold_map


def _net_has_geometry(pcb: PCB, net_id: int) -> bool:
    """Return True if ``net_id`` has at least one segment or via on ``pcb``.

    Used to mirror :meth:`DiffPairLengthTracker.get_all_skews` semantics:
    pairs where one half is entirely unrouted are omitted from the
    skew_data dict.  Without this gate, an unrouted pair would emit a
    spurious ``skew_mm = |L_p - 0| = L_p`` entry that would fire a
    bogus DRC violation.
    """
    for _seg in pcb.segments_in_net(net_id):
        return True
    for _via in pcb.vias_in_net(net_id):
        return True
    return False
