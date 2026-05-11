"""Per-pair length-match (skew) computation (Issue #2647, Epic #2556 Phase 3H).

This module provides :class:`DiffPairLengthTracker`, a thin measurement-only
layer on top of the existing :class:`~kicad_tools.router.length.LengthTracker`
geometry primitive (specifically the
:meth:`~kicad_tools.router.length.LengthTracker.calculate_route_length`
staticmethod) that records the routed length of each half of a detected
differential pair and exposes per-pair skew (``|L_p - L_n|``).

Design notes
============

* **Why a separate class rather than extending** :mod:`router.length`? The
  existing :class:`LengthTracker` is keyed on net id and uses an explicit
  :class:`LengthConstraint.match_group` mechanism (a `str` field set by the
  user).  Diff pairs are *detected* (see
  :class:`~kicad_tools.router.diffpair_detection.DetectedPair`) and need a
  ``(P, N)`` keying with both halves attributed separately -- the
  match-group violation type is keyed on the group name, not the pair.
  Trying to overload `length.py` would break DDR / wide-bus tuning that
  legitimately uses match groups.  Phase 3H keeps the match-group code
  paths untouched.
* **Why not populate** :attr:`DifferentialPair.routed_length_p`/`routed_length_n`?
  Those fields exist on :class:`~kicad_tools.router.diffpair.DifferentialPair`
  (`router/diffpair.py:131-132`) but are stale pre-Phase-1 legacy state.
  The Phase 3H tracker is the authoritative store; populating the legacy
  fields would risk drift between two sources of truth.  A follow-up issue
  can remove the legacy fields once they are confirmed unreferenced.
* **Via-length policy.**  When a route traverses a via, the via contributes
  a geometric drilled length equal to::

      board_thickness_mm * |layer_index_delta| / (num_copper_layers - 1)

  where ``layer_index_delta`` is computed from the via's
  :attr:`~kicad_tools.router.primitives.Via.layers` tuple of two
  :class:`~kicad_tools.core.types.CopperLayer` enums.  When the caller does
  not supply ``board_thickness_mm`` the via contributes **0.0 mm** to the
  length (with a documented caveat) rather than a guessed default -- this
  policy mirrors the curator's spec on #2647 and ensures Phase 3I sizes
  serpentines off explicit values.
* **Per-class skew tolerance** lives on
  :attr:`~kicad_tools.router.rules.NetClassRouting.skew_tolerance_mm`
  (added in this issue) with accessor
  :meth:`~kicad_tools.router.rules.NetClassRouting.effective_skew_tolerance`.
  The tracker itself measures unconditionally; the DRC rule (Phase 3J /
  Issue J) consumes the accessor to decide whether to fire.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .length import LengthTracker

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB

    from .diffpair_detection import DetectedPair
    from .primitives import Route, Via


@dataclass
class DiffPairLengthTracker:
    """Tracks per-pair routed length and skew for detected differential pairs.

    The tracker stores route lengths keyed on net id (matching the
    :class:`Route.net` attribute) and exposes per-pair query methods that
    look up both halves of a :class:`~kicad_tools.router.diffpair_detection.DetectedPair`
    and return their lengths or skew.

    Attributes:
        lengths: Mapping ``{net_id: routed_length_mm}`` for every net
            recorded so far.  Populated by :meth:`record_routes`.
    """

    lengths: dict[int, float] = field(default_factory=dict)

    # =========================================================================
    # Recording
    # =========================================================================

    def record_routes(
        self,
        routes: list[Route],
        detected_pairs: list[DetectedPair],
        board_thickness_mm: float | None = None,
        num_copper_layers: int = 2,
    ) -> None:
        """Measure and record the routed length of each half of each detected pair.

        Args:
            routes: All routed nets from the autorouter pass.
            detected_pairs: Pairs from
                :func:`~kicad_tools.router.diffpair_detection.detect_diff_pairs`.
                Each pair's ``pair.positive.net_id`` and
                ``pair.negative.net_id`` are looked up in ``routes`` to
                measure each half independently.
            board_thickness_mm: Total stackup thickness in mm.  When
                ``None`` (the default), vias contribute ``0.0`` to the
                length (documented behavior -- see module docstring).
                When supplied, each via on a recorded route contributes
                ``board_thickness_mm * |Δlayer_index| / (num_copper_layers - 1)``.
            num_copper_layers: Number of copper layers in the stack (used
                to compute per-via drilled length when ``board_thickness_mm``
                is supplied).  Defaults to ``2`` (typical 2-layer stack).

        Notes:
            * Routes whose ``net`` is not the P or N of any detected pair
              are ignored (this is a per-pair tracker; non-pair nets are
              invisible to it by design).
            * If a pair's P or N is not present in ``routes`` (unrouted),
              the missing side simply doesn't get recorded; :meth:`get_skew`
              returns ``None`` for such pairs.
            * This method may be called multiple times; subsequent calls
              overwrite previously-recorded lengths for the same net ids.
        """
        # Collect the set of net ids that belong to a detected pair so we
        # can skip non-pair routes cheaply.
        pair_net_ids: set[int] = set()
        for dp in detected_pairs:
            pair_net_ids.add(dp.pair.positive.net_id)
            pair_net_ids.add(dp.pair.negative.net_id)

        # Build a {net_id -> Route} lookup for O(1) access (a board can
        # have hundreds of routes; linear scans per pair would be O(n*m)).
        routes_by_net: dict[int, Route] = {}
        for route in routes:
            if route.net in pair_net_ids:
                routes_by_net[route.net] = route

        # Measure each side of each detected pair, and refresh the
        # name-keyed skew cache used by :meth:`get_all_skews`.
        self._skew_map.clear()
        for dp in detected_pairs:
            p_id = dp.pair.positive.net_id
            n_id = dp.pair.negative.net_id

            p_route = routes_by_net.get(p_id)
            if p_route is not None:
                self.lengths[p_id] = self._measure_route(
                    p_route, board_thickness_mm, num_copper_layers
                )

            n_route = routes_by_net.get(n_id)
            if n_route is not None:
                self.lengths[n_id] = self._measure_route(
                    n_route, board_thickness_mm, num_copper_layers
                )

            # Populate the name-keyed cache only when BOTH halves are
            # routed; an unrouted half yields ``None`` from get_skew and
            # is intentionally omitted from get_all_skews.
            l_p = self.lengths.get(p_id)
            l_n = self.lengths.get(n_id)
            if l_p is not None and l_n is not None:
                self._skew_map[(dp.pair.positive.net_name, dp.pair.negative.net_name)] = abs(
                    l_p - l_n
                )

    @staticmethod
    def _measure_route(
        route: Route,
        board_thickness_mm: float | None,
        num_copper_layers: int,
    ) -> float:
        """Return the geometric length of a route in mm, including via drill.

        Segment length is computed via
        :meth:`LengthTracker.calculate_route_length` (single source of truth
        for segment summation -- see `length.py:138-153`).  Via length is
        computed from the via's ``layers`` tuple and the supplied
        ``board_thickness_mm`` / ``num_copper_layers``; when
        ``board_thickness_mm`` is ``None`` each via contributes ``0.0``.
        """
        # Segment portion (reuses the existing primitive -- no duplication).
        total = LengthTracker.calculate_route_length(route)

        # Via portion.  When the caller did not supply board_thickness_mm
        # (or the stack has fewer than 2 layers) we cannot compute a
        # drilled length and document that vias contribute 0.0.
        if board_thickness_mm is None or num_copper_layers <= 1:
            return total

        for via in route.vias:
            total += DiffPairLengthTracker._via_length(via, board_thickness_mm, num_copper_layers)
        return total

    @staticmethod
    def _via_length(
        via: Via,
        board_thickness_mm: float,
        num_copper_layers: int,
    ) -> float:
        """Return the geometric drilled length of ``via`` in mm.

        Computed as ``board_thickness_mm * |stack_index_delta| / (num_copper_layers - 1)``
        where ``stack_index_delta`` is the difference between the via's
        two layers' **stack positions** in an ``num_copper_layers``-deep
        stack (NOT the raw KiCad layer enum values).  The stack positions
        are the natural ordering of populated copper layers; for example:

        * 2-layer stack populates ``[F.Cu, B.Cu]`` -> stack indices ``[0, 1]``.
          A F.Cu->B.Cu via gives ``1.6 * 1 / 1 = 1.6`` mm.
        * 4-layer stack populates ``[F.Cu, In1.Cu, In2.Cu, B.Cu]`` ->
          stack indices ``[0, 1, 2, 3]``.  A F.Cu->In1.Cu blind via
          gives ``1.6 * 1 / 3 ≈ 0.533`` mm; a F.Cu->B.Cu through-via
          gives ``1.6 * 3 / 3 = 1.6`` mm.
        * 6-layer stack populates ``[F.Cu, In1..In4, B.Cu]`` -> stack
          indices ``[0, 1, 2, 3, 4, 5]``.

        Using stack positions (rather than the raw 0/1/2/3/4/5 enum
        values, which always treat F.Cu->B.Cu as a "5-layer span" even
        on a 2-layer board) is correct because the via's physical
        drilled length depends on how many dielectric layers it actually
        crosses, not on KiCad's nominal layer numbering.
        """
        layer_start, layer_end = via.layers
        idx_start = DiffPairLengthTracker._stack_position(layer_start, num_copper_layers)
        idx_end = DiffPairLengthTracker._stack_position(layer_end, num_copper_layers)
        delta = abs(idx_start - idx_end)
        return board_thickness_mm * delta / (num_copper_layers - 1)

    @staticmethod
    def _stack_position(layer, num_copper_layers: int) -> int:
        """Map a :class:`CopperLayer` to its stack-position index for an N-layer stack.

        Populated copper layers in a KiCad N-layer stack are always
        ``[F.Cu, In1.Cu, In2.Cu, ..., In(N-2).Cu, B.Cu]``.  ``F.Cu`` is
        always position 0; ``B.Cu`` is always position ``num_copper_layers - 1``;
        inner layers map by their enum order (``IN1_CU`` -> 1, ``IN2_CU`` -> 2,
        and so on).

        This is intentionally robust to an unusual stack (e.g. a board
        that only populates F.Cu and In1.Cu): the function still returns
        sensible indices for those two layers.
        """
        # F.Cu and B.Cu are anchored at the ends of any populated stack.
        from kicad_tools.core.types import CopperLayer

        if layer == CopperLayer.F_CU:
            return 0
        if layer == CopperLayer.B_CU:
            return num_copper_layers - 1
        # Inner layers: IN1_CU=1, IN2_CU=2, etc.  Their enum values
        # happen to match their stack positions for typical 4- and 6-layer
        # stacks because the enum was designed that way (see core/types.py).
        return layer.value

    # =========================================================================
    # Per-pair queries
    # =========================================================================

    def get_pair_lengths(self, pair: DetectedPair) -> tuple[float, float] | None:
        """Return ``(L_p, L_n)`` in mm for ``pair``, or ``None`` if either half is unrouted.

        Args:
            pair: A detected pair (the ``DetectedPair.pair`` attribute is
                a :class:`DifferentialPair` whose ``.positive.net_id`` and
                ``.negative.net_id`` are looked up in the recorded lengths).

        Returns:
            ``(L_p, L_n)`` if both halves have been recorded, else ``None``.
        """
        p_id = pair.pair.positive.net_id
        n_id = pair.pair.negative.net_id
        l_p = self.lengths.get(p_id)
        l_n = self.lengths.get(n_id)
        if l_p is None or l_n is None:
            return None
        return (l_p, l_n)

    def get_skew(self, pair: DetectedPair) -> float | None:
        """Return ``|L_p - L_n|`` in mm for ``pair``, or ``None`` if either half is unrouted.

        This is the per-pair skew that Phase 3I uses as a serpentine
        target and Phase 3J fires DRC violations on.
        """
        lengths = self.get_pair_lengths(pair)
        if lengths is None:
            return None
        l_p, l_n = lengths
        return abs(l_p - l_n)

    def get_all_skews(self) -> dict[tuple[str, str], float]:
        """Return ``{(p_net_name, n_net_name): skew_mm}`` for all recorded pairs.

        Pairs whose P or N is unrouted are omitted.  The dictionary is
        keyed on ``(p_net_name, n_net_name)`` (not net ids) so callers
        can render skew reports without back-references.  Insertion order
        follows ``p_net_name`` alphabetically for deterministic iteration.
        """
        # The tracker stores only net ids and lengths; the public
        # ``(p_net_name, n_net_name)`` keying requires DetectedPair data
        # to be available at query time.  Since record_routes stores
        # lengths by net id, get_all_skews relies on the caller to also
        # call record_routes with the detected_pairs list (which is the
        # normal call shape).  To make this method usable in isolation
        # we cache the pair name mapping when record_routes runs.
        return dict(sorted(self._skew_map.items(), key=lambda kv: kv[0][0]))

    # =========================================================================
    # PCB-side measurement primitive (Phase 2.5c / Issue #2675)
    # =========================================================================

    @staticmethod
    def measure_net_from_pcb(
        pcb: PCB,
        net_id: int,
        board_thickness_mm: float | None = None,
        num_copper_layers: int = 2,
    ) -> float:
        """Return the geometric routed length of a net in mm, read from a routed PCB.

        Sister primitive to :meth:`_measure_route` -- the latter consumes the
        router-internal :class:`~kicad_tools.router.primitives.Route` shape
        (``segments[i].x1/y1/x2/y2``), while this method consumes the PCB
        schema shape exposed by :meth:`~kicad_tools.schema.pcb.PCB.segments_in_net`
        and :meth:`~kicad_tools.schema.pcb.PCB.vias_in_net` (segments use
        ``start: tuple[float, float]`` / ``end: tuple[float, float]``, vias
        use ``layers: list[str]`` of KiCad layer name strings).

        Used by :func:`kicad_tools.validate.diffpair_skew.derive_skew_data`
        (Phase 2.5c, Epic #2556) to re-derive per-pair skew from a routed
        PCB without round-tripping through the router-internal Route form.
        Keeping the formula here (rather than duplicating it in the
        validate package) preserves single source of truth for diff-pair
        length-with-vias computation -- if a future change touches one
        primitive, the byte-for-byte drift-prevention test in
        ``tests/test_validate_diffpair_skew.py`` fires.

        Args:
            pcb: The routed PCB to measure.
            net_id: The net number whose segments + vias to sum.
            board_thickness_mm: Total stackup thickness in mm.  When
                ``None`` (the default), vias contribute ``0.0`` to the
                length (documented behavior -- mirrors
                :meth:`record_routes`).
            num_copper_layers: Number of copper layers in the stack (used
                to compute per-via drilled length when
                ``board_thickness_mm`` is supplied).  Defaults to ``2``.

        Returns:
            Total geometric length (segments + vias) in mm.

        Notes:
            * Segment length is computed via the same
              ``sqrt(dx*dx + dy*dy)`` formula used by
              :meth:`~kicad_tools.router.length.LengthTracker.calculate_route_length`
              (router-internal Route form).  The drift-prevention test
              in ``tests/test_validate_diffpair_skew.py`` confirms the
              two paths produce identical results for the same physical
              routing.
            * Via length uses the same stack-position formula as
              :meth:`_via_length` -- the via's ``layers: list[str]`` is
              mapped to :class:`~kicad_tools.core.types.CopperLayer`
              enums via :meth:`CopperLayer.from_kicad_name` before the
              delta is computed.
            * Vias with malformed layer names (not a known KiCad copper
              layer) contribute ``0.0`` to the length rather than
              crashing -- defensive against legacy / hand-edited PCBs.
        """
        from kicad_tools.core.types import CopperLayer

        # Segment portion: identical formula to LengthTracker.calculate_route_length
        # (length.py:139) and DiffPairLengthTracker._measure_route (above).
        total = 0.0
        for seg in pcb.segments_in_net(net_id):
            dx = seg.end[0] - seg.start[0]
            dy = seg.end[1] - seg.start[1]
            total += math.sqrt(dx * dx + dy * dy)

        # Via portion: identical formula to _via_length (above) but the
        # input shape differs (PCB-side Via.layers is list[str] of KiCad
        # layer name strings, not a tuple of CopperLayer enums).
        if board_thickness_mm is None or num_copper_layers <= 1:
            return total

        for via in pcb.vias_in_net(net_id):
            layers = via.layers
            if not layers or len(layers) < 2:
                # Malformed via (no layer span) -- skip rather than crash.
                continue
            try:
                layer_start = CopperLayer.from_kicad_name(layers[0])
                layer_end = CopperLayer.from_kicad_name(layers[-1])
            except ValueError:
                # Unknown layer name -- conservative skip.
                continue
            idx_start = DiffPairLengthTracker._stack_position(layer_start, num_copper_layers)
            idx_end = DiffPairLengthTracker._stack_position(layer_end, num_copper_layers)
            delta = abs(idx_start - idx_end)
            total += board_thickness_mm * delta / (num_copper_layers - 1)

        return total

    # =========================================================================
    # Internal name cache (populated by record_routes; used by get_all_skews)
    # =========================================================================

    _skew_map: dict[tuple[str, str], float] = field(default_factory=dict, init=False, repr=False)
