"""Re-derive diff-pair engagement state from a routed PCB + net-class map.

Issue #2652, Epic #2556 Phase 2.5b.

The :class:`~kicad_tools.validate.rules.diffpair_routing_continuity.DiffPairRoutingContinuityRule`
(Phase 2G / Issue #2640) consumes a caller-supplied ``engaged_pairs:
set[tuple[int, int]]`` and per-pair ``threshold_map``.  The rule itself
deliberately does NOT re-derive engagement state to avoid drift between
the router's engagement decision (from #2638's
:func:`should_engage_coupled`) and the validator's continuity check.

This module provides the producer side -- a thin shim that:

1. Detects differential pairs on a routed PCB using the same layered
   detector the router uses (``diffpair_detection.detect_diff_pairs``).
2. Re-runs :func:`should_engage_coupled` against each detected pair.
3. Builds the ``threshold_map`` from each pair's net class
   :meth:`NetClassRouting.effective_coupled_continuity_threshold`.

Why re-derive instead of persist?

The recommended approach per the curator review on #2652 is to recover
the engagement state at validation time rather than persist it as PCB
metadata.  :func:`should_engage_coupled` is a pure function of (pair,
net_class_routing, net_to_class) and the net-class map is already
available to the autorouter consumer, so re-running it on the routed
PCB's detected pairs is idempotent.  PCB-metadata persistence is a
separable change, deferred until a consumer needs more state than can
be re-derived.

Boundary discipline:

This module is the ONLY place the ``validate/`` package depends on the
``router/`` package's diff-pair primitives.  The rule itself remains
router-independent.  Tests live in
``tests/test_validate_diffpair_engagement.py`` and the drift-prevention
test in ``tests/test_validate_diffpair_routing_continuity.py``
(architect spec, AC #5).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.router.rules import NetClassRouting
    from kicad_tools.schema.pcb import PCB


logger = logging.getLogger(__name__)


def derive_engagement_state(
    pcb: PCB,
    net_class_map: dict[str, NetClassRouting] | None,
) -> tuple[set[tuple[int, int]], dict[tuple[int, int], float]]:
    """Re-derive ``(engaged_pairs, threshold_map)`` from a routed PCB.

    Walks the PCB's net table, runs the layered differential-pair
    detector, and re-runs :func:`should_engage_coupled` for each
    detected pair.  Returns the inputs the
    :class:`DiffPairRoutingContinuityRule` expects.

    Args:
        pcb: The routed PCB to inspect.  Only ``pcb.nets`` is consulted
            (net id + net name).  Geometry is NOT used here -- the rule
            walks the routed segments itself.
        net_class_map: Map of ``{net_name: NetClassRouting}`` (the
            autorouter convention used by
            :attr:`~kicad_tools.router.core.AutoRouter.net_class_map`).
            ``None`` or empty returns ``(set(), {})`` so the standalone
            ``kct check`` path degrades gracefully to a no-op.

    Returns:
        ``(engaged_pairs, threshold_map)`` where

        * ``engaged_pairs`` is the set of ``(min_net_id, max_net_id)``
          tuples whose net class has ``coupled_routing == True`` AND
          which passed the engagement-layer single-ended refusal check.
        * ``threshold_map`` is the ``{(min, max): threshold}`` map of
          per-pair coupled-fraction thresholds.  Each engaged pair's
          threshold comes from
          :meth:`NetClassRouting.effective_coupled_continuity_threshold`
          on its net class (with the rule's module-level
          :data:`DEFAULT_COUPLED_CONTINUITY_THRESHOLD` 0.7 fallback).

    Notes:
        Idempotence guarantee (architect AC #5): given the same net
        classes (which are persisted in the PCB schema), this function
        returns the same set as the producer-side
        :meth:`DiffPairRouter._resolve_engagement` for the same pairs.
        This is the drift-prevention property tested in
        ``tests/test_validate_diffpair_routing_continuity.py``.
    """
    if not net_class_map:
        return set(), {}

    # Local imports keep the validate -> router boundary explicit: this
    # is the ONE module in validate/ that depends on router/ diff-pair
    # primitives.  See module docstring.
    from kicad_tools.router.diffpair import should_engage_coupled
    from kicad_tools.router.diffpair_detection import detect_diff_pairs
    from kicad_tools.validate.rules.diffpair_routing_continuity import (
        DEFAULT_COUPLED_CONTINUITY_THRESHOLD,
    )

    # Build the {net_id: net_name} map the detector expects.
    net_names: dict[int, str] = {}
    for net_id, net in pcb.nets.items():
        net_name = getattr(net, "name", None)
        if not net_name:
            continue
        net_names[net_id] = net_name

    if not net_names:
        return set(), {}

    # Synthesise a {net_name: class_name} map so the layered detector
    # can consult ``NetClassRouting.diffpair_partner`` explicit
    # declarations.  Mirrors the autorouter convention -- see
    # ``DiffPairRouter._resolve_detection_inputs`` (router-side).
    net_to_class: dict[str, str] = {}
    synth_routing: dict = dict(net_class_map)
    for net_name, nc in net_class_map.items():
        cls_name = getattr(nc, "name", None)
        if cls_name is None:
            continue
        net_to_class[net_name] = cls_name
        synth_routing.setdefault(cls_name, nc)

    # KiCad-group templates declared in the PCB itself (parsed by the
    # schema layer when the file was loaded).  Optional -- absent on
    # most fixtures.  Mirrors router/orchestrator.py:207.
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
            "[diffpair-engagement] detector raised %s; treating as no engaged pairs",
            exc,
        )
        return set(), {}

    engaged_pairs: set[tuple[int, int]] = set()
    threshold_map: dict[tuple[int, int], float] = {}

    for detected_pair in detected:
        pair = detected_pair.pair
        engaged, reason = should_engage_coupled(
            pair,
            net_class_routing=synth_routing,
            net_to_class=net_to_class,
        )
        if not engaged:
            logger.debug(
                "[diffpair-engagement] %s <-> %s NOT engaged (reason: %s)",
                pair.positive.net_name,
                pair.negative.net_name,
                reason,
            )
            continue

        a = pair.positive.net_id
        b = pair.negative.net_id
        key = (a, b) if a <= b else (b, a)
        engaged_pairs.add(key)

        # Per-pair threshold: prefer the positive side's net class, fall
        # back to the negative side's, then to the default.  Mirrors the
        # one-sided-declaration policy already in use throughout the
        # diff-pair pipeline (#2558 / #2638).
        nc_p = net_class_map.get(pair.positive.net_name)
        nc_n = net_class_map.get(pair.negative.net_name)
        nc = nc_p if nc_p is not None else nc_n
        if nc is not None and hasattr(nc, "effective_coupled_continuity_threshold"):
            threshold_map[key] = nc.effective_coupled_continuity_threshold(
                default=DEFAULT_COUPLED_CONTINUITY_THRESHOLD,
            )
        else:
            threshold_map[key] = DEFAULT_COUPLED_CONTINUITY_THRESHOLD

        logger.info(
            "[diffpair-engagement] %s <-> %s engaged (threshold=%.2f)",
            pair.positive.net_name,
            pair.negative.net_name,
            threshold_map[key],
        )

    return engaged_pairs, threshold_map
