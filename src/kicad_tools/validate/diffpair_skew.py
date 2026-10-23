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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kicad_tools.router.rules import NetClassRouting
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.diffpair_engagement import PairSkewLimits, PairThresholds


logger = logging.getLogger(__name__)


def derive_skew_data(
    pcb: PCB,
    net_class_map: dict[str, NetClassRouting] | None,
    board_thickness_mm: float | None = None,
    num_copper_layers: int = 2,
) -> tuple[dict[tuple[str, str], float], dict[tuple[int, int], float]]:
    """``(skew_data, threshold_map)`` of :func:`derive_skew_limits`."""
    skew_data, limits = derive_skew_limits(
        pcb, net_class_map, board_thickness_mm, num_copper_layers
    )
    return skew_data, {key: t.skew_mm for key, t in limits.items()}


def derive_skew_limits(
    pcb: PCB,
    net_class_map: dict[str, NetClassRouting] | None,
    board_thickness_mm: float | None = None,
    num_copper_layers: int = 2,
) -> tuple[dict[tuple[str, str], float], dict[tuple[int, int], PairThresholds]]:
    """Re-derive ``(skew_data, limits)`` from a routed PCB.

    ``limits`` maps ``(min_net_id, max_net_id)`` to the pair's resolved
    :class:`~kicad_tools.validate.diffpair_engagement.PairThresholds` --
    limit, provenance and severity (Issue #6443).  :func:`derive_skew_data`
    flattens it to the ``threshold_map`` described under Returns.

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
    from kicad_tools.router.diffpair_detection import build_synth_routing

    synth_routing, net_to_class = build_synth_routing(net_class_map)

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
    threshold_map: dict[tuple[int, int], PairThresholds] = {}

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

        # Per-pair threshold: shared resolver (Issue #6442).
        from kicad_tools.validate.diffpair_engagement import resolve_pair_thresholds

        key = (p_id, n_id) if p_id <= n_id else (n_id, p_id)
        threshold_map[key] = resolve_pair_thresholds(net_class_map, p_name, n_name)

        logger.info(
            "[diffpair-skew] %s <-> %s skew=%.3f mm (tolerance=%.3f)",
            p_name,
            n_name,
            skew_mm,
            threshold_map[key].skew_mm,
        )

    return skew_data, threshold_map


@dataclass(frozen=True)
class PairSkewMeasurement:
    """One routed pair measured in length and in time (Issue #7892).

    ``skew_mm`` is the kicad-cli length difference (as before #7892).
    ``skew_ps`` is the difference of the two legs' propagation delays: each
    trimmed track run at its layer's delay plus each via barrel at the via
    delay (:class:`~kicad_tools.physics.skew_budget.LayerDelays`); ``None``
    without per-layer delays.  ``layers`` maps each net to its track length
    per copper layer (mm).
    """

    p_id: int
    n_id: int
    p_name: str
    n_name: str
    skew_mm: float
    skew_ps: float | None
    limits: PairSkewLimits
    layers: dict[str, dict[str, float]]

    @property
    def key(self) -> tuple[int, int]:
        return (self.p_id, self.n_id) if self.p_id <= self.n_id else (self.n_id, self.p_id)


def derive_pair_skews(
    pcb: PCB,
    net_class_map: dict[str, NetClassRouting] | None,
    num_copper_layers: int = 2,
    stackup: Any | None = None,
) -> list[PairSkewMeasurement]:
    """Measure every routed, detected pair in mm and ps (Issue #7892).

    Detection and the routed-both-halves gate are those of
    :func:`derive_skew_limits`; lengths are the same kicad-cli measurement
    (:func:`kicad_tools.router.kicad_length.measure_pcb_net_parts`, whose
    total is :meth:`DiffPairLengthTracker.measure_net_from_pcb`).  The limits
    come from :func:`~kicad_tools.validate.diffpair_engagement.resolve_pair_skew_limits`.
    ``stackup`` defaults to :meth:`Stackup.from_pcb`.
    """
    if not net_class_map:
        return []
    from kicad_tools.router.kicad_length import KicadStackup, measure_pcb_net_parts
    from kicad_tools.validate.diffpair_engagement import resolve_pair_skew_limits

    if stackup is None:
        stackup = _board_stackup(pcb, num_copper_layers)
    kstack = KicadStackup.from_pcb(pcb, num_copper_layers)

    out: list[PairSkewMeasurement] = []
    for p_id, n_id, p_name, n_name in _routed_pairs(pcb, net_class_map):
        parts_p = measure_pcb_net_parts(pcb, p_id, kstack)
        parts_n = measure_pcb_net_parts(pcb, n_id, kstack)
        limits = resolve_pair_skew_limits(net_class_map, p_name, n_name, stackup)
        skew_ps = None
        if limits.delays is not None:
            t_p = limits.delays.delay_ps(parts_p.by_layer, parts_p.via_mm)
            t_n = limits.delays.delay_ps(parts_n.by_layer, parts_n.via_mm)
            skew_ps = abs(t_p - t_n)
        out.append(
            PairSkewMeasurement(
                p_id=p_id,
                n_id=n_id,
                p_name=p_name,
                n_name=n_name,
                skew_mm=abs(parts_p.total - parts_n.total),
                skew_ps=skew_ps,
                limits=limits,
                layers={p_name: parts_p.by_layer, n_name: parts_n.by_layer},
            )
        )
    return out


def _board_stackup(pcb: PCB, num_copper_layers: int) -> Any | None:
    """The board's stackup, else the default for its layer count, else ``None``."""
    from kicad_tools.physics.stackup import Stackup

    try:
        return Stackup.from_pcb(pcb)
    except Exception as exc:
        logger.debug("[diffpair-skew] no stackup on the board (%s); using a default", exc)
    try:
        if num_copper_layers <= 2:
            return Stackup.default_2layer()
        if num_copper_layers == 4:
            return Stackup.jlcpcb_4layer()
        if num_copper_layers == 6:
            return Stackup.default_6layer()
        return Stackup._create_generic_stackup(num_copper_layers)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[diffpair-skew] no stackup (%s); skew checked in mm only", exc)
        return None


def _routed_pairs(
    pcb: PCB, net_class_map: dict[str, NetClassRouting]
) -> list[tuple[int, int, str, str]]:
    """``(p_id, n_id, p_name, n_name)`` of every detected pair with both halves routed."""
    from kicad_tools.router.diffpair_detection import build_synth_routing, detect_diff_pairs

    net_names = {net_id: net.name for net_id, net in pcb.nets.items() if getattr(net, "name", None)}
    if not net_names:
        return []
    synth_routing, net_to_class = build_synth_routing(net_class_map)
    try:
        detected = detect_diff_pairs(
            net_names,
            net_class_routing=synth_routing,
            net_to_class=net_to_class,
            kicad_groups=getattr(pcb, "kicad_diff_pair_groups", None),
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[diffpair-skew] detector raised %s; treating as no skew data", exc)
        return []
    out = []
    for d in detected:
        p, n = d.pair.positive, d.pair.negative
        if _net_has_geometry(pcb, p.net_id) and _net_has_geometry(pcb, n.net_id):
            out.append((p.net_id, n.net_id, p.net_name, n.net_name))
    return out


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


@dataclass(frozen=True)
class ChainSkew:
    """Skew across a chain of diff-pair segments joined by series elements.

    ``segments`` lists each pair segment as ``(p_net, n_net)``;
    ``side_a_mm`` / ``side_b_mm`` are the summed routed lengths of the two
    conductors end to end (polarity-aligned through the series elements, so
    a swap across a part is followed rather than counted as skew).
    ``limit_mm`` is ``None`` when no class declares a chain limit.
    """

    segments: tuple[tuple[str, str], ...]
    side_a_mm: float
    side_b_mm: float
    limit_mm: float | None

    @property
    def skew_mm(self) -> float:
        return abs(self.side_a_mm - self.side_b_mm)


def derive_chain_skew(
    pcb: PCB,
    net_class_map: dict[str, NetClassRouting] | None,
    board_thickness_mm: float | None = None,
    num_copper_layers: int = 2,
) -> list[ChainSkew]:
    """Measure pair skew across series-element chains (Issue #6447).

    Two detected pairs are chained when a two-pad part (a series resistor,
    capacitor or ferrite) joins one net of each pair and a second two-pad
    part joins the remaining nets.  Pairs linked this way form a chain; the
    chain's two sides are the polarity-aligned sums of the member nets'
    routed lengths.  The part's own body length is not counted.  A pair with
    no chain partner is not reported -- the per-pair rule covers it.

    ``kct check`` measures per net segment; this adds the end-to-end figure.
    Returns ``[]`` without a ``net_class_map``, like the per-pair producers.
    """
    if not net_class_map:
        return []

    from kicad_tools.router.diffpair_detection import build_synth_routing, detect_diff_pairs
    from kicad_tools.router.diffpair_length import DiffPairLengthTracker

    net_names = {net_id: net.name for net_id, net in pcb.nets.items() if getattr(net, "name", None)}
    if not net_names:
        return []
    synth_routing, net_to_class = build_synth_routing(net_class_map)
    try:
        detected = detect_diff_pairs(
            net_names,
            net_class_routing=synth_routing,
            net_to_class=net_to_class,
            kicad_groups=getattr(pcb, "kicad_diff_pair_groups", None),
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[diffpair-chain-skew] detector raised %s; no chain data", exc)
        return []

    pairs = [d.pair for d in detected]
    net_to_pair: dict[str, tuple[int, int]] = {}  # name -> (pair index, 0=P / 1=N)
    for i, pair in enumerate(pairs):
        net_to_pair[pair.positive.net_name] = (i, 0)
        net_to_pair[pair.negative.net_name] = (i, 1)

    # Series links: a two-pad part between nets of two different pairs.
    links: list[tuple[str, str]] = []
    for fp in getattr(pcb, "footprints", ()):
        if len(fp.pads) != 2:
            continue
        a, b = fp.pads[0].net_name, fp.pads[1].net_name
        if a in net_to_pair and b in net_to_pair and net_to_pair[a][0] != net_to_pair[b][0]:
            links.append((a, b))

    # A pair link needs a part on each conductor, with one polarity relation.
    relation: dict[tuple[int, int], list[bool]] = {}
    for a, b in links:
        (ia, sa), (ib, sb) = net_to_pair[a], net_to_pair[b]
        relation.setdefault((min(ia, ib), max(ia, ib)), []).append(sa == sb)
    adjacency: dict[int, list[tuple[int, bool]]] = {}
    for (i, j), kinds in relation.items():
        if len(kinds) < 2 or len(set(kinds)) != 1:
            continue  # one conductor only, or contradictory polarity
        adjacency.setdefault(i, []).append((j, kinds[0]))
        adjacency.setdefault(j, []).append((i, kinds[0]))

    name_to_id = {v: k for k, v in net_names.items()}

    def length(name: str) -> float:
        return DiffPairLengthTracker.measure_net_from_pcb(
            pcb, name_to_id[name], board_thickness_mm, num_copper_layers
        )

    chains: list[ChainSkew] = []
    seen: set[int] = set()
    for root in sorted(adjacency):
        if root in seen:
            continue
        flip = {root: False}  # True when that pair's P lies on side B
        stack = [root]
        while stack:
            cur = stack.pop()
            for nxt, same in adjacency[cur]:
                if nxt not in flip:
                    flip[nxt] = flip[cur] if same else not flip[cur]
                    stack.append(nxt)
        seen.update(flip)
        members = sorted(flip)
        if not all(
            _net_has_geometry(pcb, n.net_id)
            for i in members
            for n in (pairs[i].positive, pairs[i].negative)
        ):
            continue
        side_a = side_b = 0.0
        for i in members:
            p_len = length(pairs[i].positive.net_name)
            n_len = length(pairs[i].negative.net_name)
            if flip[i]:
                p_len, n_len = n_len, p_len
            side_a += p_len
            side_b += n_len
        first = net_class_map.get(pairs[members[0]].positive.net_name)
        chains.append(
            ChainSkew(
                segments=tuple(
                    (pairs[i].positive.net_name, pairs[i].negative.net_name) for i in members
                ),
                side_a_mm=side_a,
                side_b_mm=side_b,
                limit_mm=getattr(first, "chain_skew_tolerance_mm", None),
            )
        )
    return chains
