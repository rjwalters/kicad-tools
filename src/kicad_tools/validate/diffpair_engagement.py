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
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from kicad_tools.router.pair_protocols import (
    SKEW_STRETCH_TARGET_MM,
    PairProtocol,
    Sourced,
    protocol_defaults,
    protocol_provenance,
)

if TYPE_CHECKING:
    from kicad_tools.router.rules import NetClassRouting
    from kicad_tools.schema.pcb import PCB


logger = logging.getLogger(__name__)


#: Provenance of a limit no class field and no protocol supplied.
GENERIC_PROVENANCE = "default:generic"


@dataclass(frozen=True)
class PairThresholds:
    """The thresholds ``kct check`` applies to one diff pair (Issue #6442).

    ``continuity`` is a fraction (0.25 == 25 %); ``skew_mm`` is in mm.
    ``source_net`` names the net whose class supplied them (``None`` when
    neither net has a class and the module defaults apply).

    Issue #6443 adds the protocol-aware limits.  Each is resolved as
    ``class field > protocol default > generic default`` and
    :attr:`provenance` records which of the three supplied it, per field::

        {"skew_mm": "protocol:USB2_FS (Renesas, ... Table 2)"}   # table value
        {"skew_mm": "class:USB3"}                                # explicit
        {"skew_mm": "default:generic"}                           # 0.5 mm

    :attr:`skew_severity` follows the source: a cited or explicit limit is an
    ``error``; a limit that is an ``uncited`` table value is a ``warning``.
    The generic 0.5 mm default stays an ``error``, as it was before #6443.
    The uncoupled-length and via limits (piece (b)) resolve the same way and
    carry their own severity: :attr:`uncoupled_severity` and
    :attr:`vias_severity` are ``warning`` when the limit is an ``uncited``
    table value, ``error`` otherwise.  :attr:`max_uncoupled_total_mm` is the
    KiCad-equivalent limit (one net's uncoupled length, see
    ``validate/rules/diffpair_uncoupled_length.py``); when the class sets none
    it is derived per pair as ``max_uncoupled_per_terminal_mm`` x the number
    of components on the net, by the rule that knows the board.
    """

    continuity: float
    skew_mm: float
    source_net: str | None
    protocol: str | None = None
    skew_severity: str = "error"
    skew_stretch_mm: float = SKEW_STRETCH_TARGET_MM
    max_uncoupled_per_terminal_mm: float | None = None
    max_uncoupled_total_mm: float | None = None
    max_vias_per_net: int | None = None
    uncoupled_severity: str = "error"
    vias_severity: str = "error"
    provenance: Mapping[str, str] = field(default_factory=dict)

    @property
    def skew_source(self) -> str:
        """Where the skew limit came from (see :attr:`provenance`)."""
        return self.provenance.get("skew_mm", GENERIC_PROVENANCE)


def _resolve_limit(
    class_value: float | None,
    table_value: Sourced | None,
    class_name: str,
    protocol: PairProtocol | None,
) -> tuple[float | None, str | None, str]:
    """One limit as ``(value, provenance, severity)``: class > protocol > none."""
    if class_value is not None:
        return class_value, f"class:{class_name}", "error"
    if table_value is not None and protocol is not None:
        severity = "error" if table_value.cited else "warning"
        return table_value.value, protocol_provenance(protocol, table_value), severity
    return None, None, "error"


def resolve_pair_thresholds(
    net_class_map: dict[str, NetClassRouting] | None,
    p_name: str,
    n_name: str,
) -> PairThresholds:
    """Resolve a pair's thresholds and their provenance -- the single source.

    The positive net's class wins, then the negative net's, then the rule
    defaults (one-sided-declaration policy, #2558/#2638).  Lookup is by
    NET NAME only: a class-name key is shared by every net of the class
    and, when the class has per-net variants (board 03's ``HighSpeed``),
    would report an arbitrary one of them.

    Within the chosen class each limit resolves as ``class field >
    protocol default > generic default`` (Issue #6443); the protocol
    defaults are :data:`kicad_tools.router.pair_protocols.PROTOCOL_DEFAULTS`.

    Used by :func:`derive_engagement_state`, ``derive_skew_data`` and the
    router's pair report, so the three can never print different numbers
    for the same pair (Issue #6442).
    """
    from kicad_tools.validate.rules.diffpair_length_skew import DEFAULT_SKEW_TOLERANCE_MM
    from kicad_tools.validate.rules.diffpair_routing_continuity import (
        DEFAULT_COUPLED_CONTINUITY_THRESHOLD,
    )

    ncm = net_class_map or {}
    nc_p = ncm.get(p_name)
    nc_n = ncm.get(n_name)
    nc = nc_p if nc_p is not None else nc_n
    source = p_name if nc_p is not None else (n_name if nc_n is not None else None)
    continuity = DEFAULT_COUPLED_CONTINUITY_THRESHOLD
    if nc is not None and hasattr(nc, "effective_coupled_continuity_threshold"):
        continuity = nc.effective_coupled_continuity_threshold(
            default=DEFAULT_COUPLED_CONTINUITY_THRESHOLD
        )

    protocol: PairProtocol | None = None
    row = None
    if nc is not None and hasattr(nc, "effective_pair_protocol"):
        protocol = nc.effective_pair_protocol()
        row = protocol_defaults(protocol)
    class_name = str(getattr(nc, "name", "") or "")

    def limit(field_name: str, table_field: str) -> tuple[float | None, str | None, str]:
        return _resolve_limit(
            getattr(nc, field_name, None) if nc is not None else None,
            getattr(row, table_field) if row is not None else None,
            class_name,
            protocol,
        )

    provenance: dict[str, str] = {}
    skew, skew_src, skew_severity = limit("skew_tolerance_mm", "max_skew_mm")
    if skew is None:
        skew, skew_src, skew_severity = DEFAULT_SKEW_TOLERANCE_MM, GENERIC_PROVENANCE, "error"
    provenance["skew_mm"] = skew_src or GENERIC_PROVENANCE
    per_terminal, src, unc_severity = limit(
        "max_uncoupled_per_terminal_mm", "max_uncoupled_per_terminal_mm"
    )
    if src:
        provenance["max_uncoupled_per_terminal_mm"] = src
    vias, src, vias_severity = limit("max_vias_per_net", "max_vias_per_net")
    if src:
        provenance["max_vias_per_net"] = src
    total = getattr(nc, "max_uncoupled_total_mm", None) if nc is not None else None
    if total is not None:
        provenance["max_uncoupled_total_mm"] = f"class:{class_name}"
        unc_severity = "error"
    return PairThresholds(
        continuity=continuity,
        skew_mm=float(skew),
        source_net=source,
        protocol=protocol.name if protocol is not None else None,
        skew_severity=skew_severity,
        max_uncoupled_per_terminal_mm=per_terminal,
        max_uncoupled_total_mm=total,
        max_vias_per_net=int(vias) if vias is not None else None,
        uncoupled_severity=unc_severity,
        vias_severity=vias_severity,
        provenance=provenance,
    )


def pair_threshold_report(
    net_names: dict[int, str] | None,
    net_class_map: dict[str, NetClassRouting] | None,
) -> list[str]:
    """Report lines naming the thresholds ``kct check`` will apply to each pair.

    Printed by the router after its pair phase so the router log and a
    later ``kct check`` quote the same continuity and skew thresholds for
    every engaged pair (Issue #6442).  Engagement, detection and the
    numbers all come from the validator's own code path
    (:func:`resolve_pair_thresholds`), never a router-local copy.
    """
    if not net_names or not net_class_map:
        return []
    from kicad_tools.router.diffpair import should_engage_coupled
    from kicad_tools.router.diffpair_detection import build_synth_routing, detect_diff_pairs

    synth_routing, net_to_class = build_synth_routing(net_class_map)
    try:
        detected = detect_diff_pairs(
            net_names, net_class_routing=synth_routing, net_to_class=net_to_class
        )
    except Exception:  # reporting must never break routing
        return []
    lines: list[str] = []
    for d in detected:
        p_name = d.pair.positive.net_name
        n_name = d.pair.negative.net_name
        engaged, _ = should_engage_coupled(d.pair, synth_routing, net_to_class)
        if not engaged:
            continue
        t = resolve_pair_thresholds(net_class_map, p_name, n_name)
        lines.append(
            f"    {p_name}/{n_name}: continuity threshold {t.continuity * 100:.1f}%, "
            f"skew tolerance {t.skew_mm:.3f} mm [{t.skew_source}; {t.skew_severity}]"
        )
    return sorted(lines)


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
    from kicad_tools.router.diffpair_detection import build_synth_routing

    synth_routing, net_to_class = build_synth_routing(net_class_map)

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

        # Per-pair threshold: shared resolver (Issue #6442) -- the same one
        # the skew derivation and the router's pair report use.
        threshold_map[key] = resolve_pair_thresholds(
            net_class_map, pair.positive.net_name, pair.negative.net_name
        ).continuity

        logger.info(
            "[diffpair-engagement] %s <-> %s engaged (threshold=%.2f)",
            pair.positive.net_name,
            pair.negative.net_name,
            threshold_map[key],
        )

    return engaged_pairs, threshold_map
