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
  the KiCad stackup height between the layers it connects, and track chains
  entering pads/vias are clipped to their centres -- exactly what
  ``kicad-cli`` reports (Issue #6725; see
  :mod:`kicad_tools.router.kicad_length`, the single shared implementation).
  The legacy ``board_thickness_mm`` / ``num_copper_layers`` arguments only
  build KiCad's default stackup when no ``length_model`` is supplied; with
  neither, vias contribute **0.0 mm** (Phase 3I sizes serpentines off
  explicit values).
* **Per-class skew tolerance** lives on
  :attr:`~kicad_tools.router.rules.NetClassRouting.skew_tolerance_mm`
  (added in this issue) with accessor
  :meth:`~kicad_tools.router.rules.NetClassRouting.effective_skew_tolerance`.
  The tracker itself measures unconditionally; the DRC rule (Phase 3J /
  Issue J) consumes the accessor to decide whether to fire.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .kicad_length import KicadLengthModel, measure_pcb_net
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
        length_model: KicadLengthModel | None = None,
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
                to build KiCad's default stackup when ``board_thickness_mm``
                is supplied).  Defaults to ``2`` (typical 2-layer stack).
            length_model: Issue #6725 -- the kicad-cli-exact measurement
                (:class:`~kicad_tools.router.kicad_length.KicadLengthModel`:
                board stackup plus pad copper).  Takes precedence over
                ``board_thickness_mm`` / ``num_copper_layers``.

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
                    p_route, board_thickness_mm, num_copper_layers, length_model=length_model
                )

            n_route = routes_by_net.get(n_id)
            if n_route is not None:
                self.lengths[n_id] = self._measure_route(
                    n_route, board_thickness_mm, num_copper_layers, length_model=length_model
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
        blind_buried_supported: bool = True,
        length_model: KicadLengthModel | None = None,
    ) -> float:
        """Return the kicad-cli length of a route in mm (issue #6725).

        Delegates to :mod:`kicad_tools.router.kicad_length`, the single
        implementation shared with :meth:`measure_net_from_pcb`: segment
        lengths, minus the in-copper portion of every track chain that enters
        a pad/via (replaced by a straight line to its centre), plus each via's
        KiCad stackup height between the layers it connects.

        ``length_model`` carries the board stackup and pad copper.  Without
        one, ``board_thickness_mm`` / ``num_copper_layers`` build KiCad's
        default stackup (no pad copper, so no pad trim); when
        ``board_thickness_mm`` is ``None`` each via contributes ``0.0`` and
        only the segment length is returned.

        ``blind_buried_supported`` is accepted for backward compatibility and
        ignored: KiCad measures the connected span whatever the barrel (it
        supersedes the #4007 / #6502 / #6678 promotion model).
        """
        del blind_buried_supported
        if length_model is None:
            if board_thickness_mm is None or num_copper_layers <= 1:
                return LengthTracker.calculate_route_length(route)
            length_model = KicadLengthModel.default(board_thickness_mm, num_copper_layers)
        return length_model.route_length(route)

    @staticmethod
    def _via_length(
        via: Via,
        board_thickness_mm: float,
        num_copper_layers: int,
        blind_buried_supported: bool = True,
    ) -> float:
        """KiCad stackup height of ``via``'s drilled span on the default stackup.

        Issue #6725: kept as a convenience for callers without a route
        context.  The authoritative figure is the one
        :func:`~kicad_tools.router.kicad_length.net_length` produces, which also
        narrows the span to the layers the via actually connects.
        ``blind_buried_supported`` is ignored (see :meth:`_measure_route`).
        """
        del blind_buried_supported
        from .kicad_length import KicadStackup, route_geometry
        from .primitives import Route as _Route

        stackup = KicadStackup.default(board_thickness_mm, num_copper_layers)
        _segs, vias = route_geometry(_Route(net=0, net_name="", vias=[via]))
        return stackup.span(*vias[0].layers)

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
        # Inner layers: InN.Cu sits at stack position N.  Issue #6099: read
        # ``stack_order`` -- ``value`` stops matching past In4.Cu (In5.Cu=6).
        return layer.stack_order

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
        blind_buried_supported: bool = True,
        length_model: KicadLengthModel | None = None,
    ) -> float:
        """Return the kicad-cli length of a net in mm, read from a routed PCB.

        Issue #6725: this is the PCB form of the one shared measurement in
        :mod:`kicad_tools.router.kicad_length` (the Route form is
        :meth:`_measure_route`).  It reproduces what ``kicad-cli`` reports:
        segment lengths, minus the in-copper portion of each track chain that
        enters a pad/via (replaced by a straight line to the centre), plus
        each via's KiCad stackup height between the layers it connects.  The
        stackup is the board's own (explicit ``(setup (stackup))`` or KiCad's
        default), so ``board_thickness_mm``, ``num_copper_layers`` and
        ``blind_buried_supported`` are accepted for compatibility and ignored.

        Used by :func:`kicad_tools.validate.diffpair_skew.derive_skew_data`
        and the match-group checker, and by the router's tuners through the
        same function, so ``kct check`` and the tuners cannot drift.

        Args:
            pcb: The routed PCB to measure.
            net_id: The net number whose segments + vias to measure.
            length_model: Optional pre-built model (avoids re-reading the
                board's stackup and pads once per net).

        Returns:
            Total length (segments + vias) in mm.
        """
        del board_thickness_mm, blind_buried_supported
        if length_model is not None:
            return length_model.net_length_pcb(pcb, net_id)
        return measure_pcb_net(pcb, net_id, num_copper_hint=num_copper_layers)

    # =========================================================================
    # Internal name cache (populated by record_routes; used by get_all_skews)
    # =========================================================================

    _skew_map: dict[tuple[str, str], float] = field(default_factory=dict, init=False, repr=False)
