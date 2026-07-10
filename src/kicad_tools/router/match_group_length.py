"""N-trace match-group length / skew measurement (Issue #2688, Epic #2661 Phase 1B).

This module provides :class:`MatchGroupTracker`, a thin measurement-only layer
on top of the existing
:class:`~kicad_tools.router.diffpair_length.DiffPairLengthTracker`
(Epic #2556 Phase 3H, PR #2654) and
:class:`~kicad_tools.router.length.LengthTracker`
geometry primitives.  It records the routed length of every member of an
N-trace match group (DDR data byte, MIPI lane group, generic parallel bus)
and exposes per-group skew (``max(L) - min(L)``).

Design notes
============

* **Why a separate module rather than extending** :mod:`router.length`?
  The existing :class:`~kicad_tools.router.length.LengthTracker` is keyed
  on net id and uses an explicit :class:`LengthConstraint.match_group`
  mechanism (a ``str`` field set by the user).  This Phase 1B tracker
  layers a *named-group* view on top of those raw lengths so the
  serpentine tuner (Phase 2E), the DRC rule (Phase 2G), and the producer
  side (Phase 1D) can all consume a single ``{group_name: skew_mm}``
  dictionary without re-deriving group identity each call.  The
  module-separation rationale mirrors
  :mod:`~kicad_tools.router.diffpair_length`'s top-level docstring: trying
  to overload ``length.py`` would break the existing DDR / wide-bus tuning
  paths that legitimately use match groups.  Phase 1B keeps the legacy
  :func:`~kicad_tools.router.length.create_match_group`,
  :meth:`~kicad_tools.router.core.Autorouter.add_match_group`, and
  ``tune_match_group`` (``router/optimizer/serpentine.py:438``) code paths
  untouched.

* **Reuse, do not duplicate.**  The single source of truth for
  segment-length summation is
  :meth:`~kicad_tools.router.length.LengthTracker.calculate_route_length`
  at ``length.py:138-153``.  The single source of truth for combined
  segment + via length on a router-internal Route is
  :class:`DiffPairLengthTracker._measure_route`; this module reuses it
  via the private indirection :meth:`MatchGroupTracker._measure_route_total`
  which delegates to ``LengthTracker.calculate_route_length`` + the same
  via-length policy that PR #2654 established.  A drift-prevention test
  in ``tests/test_match_group_length.py`` mocks
  ``LengthTracker.calculate_route_length`` and asserts the tracker
  reaches it -- if a future contributor reimplements segment summation
  the test fails fast.

* **PCB-side measurement primitive delegates** to
  :meth:`DiffPairLengthTracker.measure_net_from_pcb` (the Phase 2.5c
  sister primitive, ``diffpair_length.py:308``).  The math is genuinely
  net-scoped (sum of segments + drilled via length for one net id),
  identical whether the net belongs to a diff pair or to an N-trace
  match group -- duplicating the body would re-introduce the exact
  drift risk that PR #2685's drift-prevention test prevents.  Phase 1B's
  :meth:`MatchGroupTracker.measure_net_from_pcb` is a one-line
  forwarder; the drift-prevention test in this module's suite confirms
  the two helpers stay byte-for-byte aligned.

* **Via-length policy.**  Identical to Phase 3H: when a route traverses
  a via the via contributes a drilled length equal to
  ``board_thickness_mm * |stack_index_delta| / (num_copper_layers - 1)``
  where ``stack_index_delta`` is the difference between the via's two
  layers' stack positions (NOT the raw KiCad enum values).  When
  ``board_thickness_mm`` is ``None`` (the default) each via contributes
  ``0.0`` (the documented zero-via-length default mirrored from
  :mod:`diffpair_length`).

* **Group identity is the group's name** (``MatchGroup.name``,
  e.g. ``"DDR_DATA"``).  The cache exposed by :meth:`get_all_skews`
  is keyed on this string (not on a tuple of net names like the
  diff-pair tracker), mirroring the existing
  :attr:`~kicad_tools.router.length.LengthTracker.match_groups`
  ``dict[str, list[int]]`` keying at ``length.py:94``.

* **Reference policy.**  :meth:`get_reference_length` implements the
  Phase 1A ``length_match_reference`` semantic:

  - When ``MatchGroup.reference_net_id is None`` -> the longest routed
    length in the group (matches the legacy ``tune_match_group``
    longest-as-target semantics).
  - When set -> the explicit net's length (the "pace-car" semantic).
    Returns ``None`` if the explicit reference is unrouted.

  The ``"clock"`` sentinel from Phase 1A is resolved upstream (the
  detector converts it to a ``reference_net_id`` before constructing
  the :class:`MatchGroup`); Phase 1B does not own protocol resolution.

* **Group-of-pairs composition** (``MatchGroup.pair_ids``).  The field
  is declared on the dataclass but unused in Phase 1B.  Phase 2F's
  serpentine tuner is the sole consumer: when a group's members are
  differential pairs, the inserted serpentine geometry is applied
  symmetrically to BOTH halves of the pair (preserving within-pair
  coupling).  Phase 1B's measurement is per-net regardless of pair
  affiliation -- a pair's lane length is left to Phase 2F to derive
  from the underlying per-net measurements stored here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from .diffpair_length import DiffPairLengthTracker
from .length import LengthTracker

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB

    from .primitives import Route


# =============================================================================
# Source provenance enum (mirrors DetectionSource at diffpair_detection.py:49)
# =============================================================================


class MatchGroupSource(Enum):
    """Where a match group came from in the Phase 1C layered detector.

    Mirrors :class:`~kicad_tools.router.diffpair_detection.DetectionSource`
    at ``router/diffpair_detection.py:49-54``.  The member names align
    byte-for-byte with the diff-pair source enum for cross-detector
    consistency (``SUFFIX``, not ``SUFFIX_INFERRED``).

    Members:
        EXPLICIT: Declared via
            :attr:`~kicad_tools.router.rules.NetClassRouting.length_match_group`
            (Phase 1A, Issue #2687).  Authoritative; overrides all others.
        KICAD_GROUP: KiCad project-file match-group annotation (Phase 1C).
        SUFFIX: DDR / MIPI / bus naming pattern inference (Phase 1C,
            opt-in).  Recognises ``DQ[0..N]``, ``A[0..N]``, ``CSI_DAT[0..N]``
            and friends; refuses low-confidence matches the way
            :mod:`diffpair_detection` refuses USB_CC1/CC2 (the #2527 lesson).
        LEGACY_API: :meth:`~kicad_tools.router.core.Autorouter.add_match_group`
            Python call site -- the pre-Epic-#2661 entry point that
            stays for backward compatibility.
    """

    EXPLICIT = "explicit"
    KICAD_GROUP = "kicad_group"
    SUFFIX = "suffix"
    LEGACY_API = "legacy_api"


# =============================================================================
# MatchGroup dataclass
# =============================================================================


@dataclass
class MatchGroup:
    """A declared or detected N-trace match group.

    Attributes:
        name: The group's identifier (e.g. ``"DDR_DATA"``,
            ``"MIPI_CSI_LANE0"``).  Used as the dictionary key in
            :meth:`MatchGroupTracker.get_all_skews`.  Must be unique
            within a routing pass.
        net_ids: Member nets recorded as individual single-ended traces.
            For a pure 4-net DDR-style group these are the only members.
            For a group-of-pairs (MIPI lane group, Phase 2F) the singles
            are the pair-anchored references (``"clock"`` net etc.);
            pair members live on :attr:`pair_ids`.
        pair_ids: Reserved for Phase 2F's group-of-pairs composition.
            Each entry is a ``(p_net_id, n_net_id)`` tuple identifying a
            differential pair member of the group.  Phase 1B declares
            the field but the measurement layer ignores it -- pair-aware
            reduction is a Phase 2F consumer concern.
        tolerance: Maximum allowed length skew in mm (``max - min`` across
            the group).  Defaults to ``0.5`` mm matching the conservative
            module-level default also used by
            :meth:`~kicad_tools.router.rules.NetClassRouting.effective_length_match_tolerance`.
        reference_net_id: Reference selection.  ``None`` means
            "longest in group" (the default :meth:`get_reference_length`
            policy); an explicit net id selects the "pace-car" semantic.
            The Phase 1A ``"clock"`` sentinel is resolved upstream by
            the detector (Phase 1C) before construction.
        source: Provenance recorded by the layered detector (Phase 1C).
            Defaults to :attr:`MatchGroupSource.LEGACY_API` so direct
            constructor use from the :meth:`Autorouter.add_match_group`
            entry point doesn't require an explicit kwarg.
    """

    name: str
    net_ids: list[int]
    pair_ids: list[tuple[int, int]] = field(default_factory=list)
    tolerance: float = 0.5
    reference_net_id: int | None = None
    source: MatchGroupSource = MatchGroupSource.LEGACY_API


# =============================================================================
# MatchGroupTracker
# =============================================================================


@dataclass
class MatchGroupTracker:
    """Tracks per-group routed length and skew for declared match groups.

    The tracker stores route lengths keyed on net id (matching the
    :class:`Route.net` attribute) and exposes per-group query methods
    that look up every member of a :class:`MatchGroup` and return their
    lengths or aggregate skew.

    Attributes:
        lengths: Mapping ``{net_id: routed_length_mm}`` for every group
            member recorded so far.  Populated by :meth:`record_routes`.

    The skew-cache is name-keyed (``dict[str, float]``) because group
    identity IS the name (see module docstring) -- this differs from
    :class:`DiffPairLengthTracker` whose cache is keyed on ``(p_name,
    n_name)`` because pair identity is two strings.
    """

    lengths: dict[int, float] = field(default_factory=dict)

    # =========================================================================
    # Recording
    # =========================================================================

    def record_routes(
        self,
        routes: list[Route],
        groups: list[MatchGroup],
        board_thickness_mm: float | None = None,
        num_copper_layers: int = 2,
        blind_buried_supported: bool = True,
    ) -> None:
        """Measure and record the routed length of each member of each group.

        Args:
            routes: All routed nets from the autorouter pass.
            groups: Match groups whose members to measure.  Each group's
                ``net_ids`` are looked up in ``routes`` and recorded
                independently.
            board_thickness_mm: Total stackup thickness in mm.  When
                ``None`` (the default), vias contribute ``0.0`` to the
                length (documented behavior -- see module docstring).
                When supplied, each via on a recorded route contributes
                ``board_thickness_mm * |Δstack_index| / (num_copper_layers - 1)``.
            num_copper_layers: Number of copper layers in the stack (used
                to compute per-via drilled length when ``board_thickness_mm``
                is supplied).  Defaults to ``2`` (typical 2-layer stack).
            blind_buried_supported: When ``False`` (Issue #4007), a
                standard (non-micro) via is measured as a full through-via
                to match KiCad's post-route via promotion on boards without
                blind/buried drilling.  Defaults to ``True`` (legacy
                partial-span behavior).

        Notes:
            * Routes whose ``net`` is not a member of any supplied group
              are ignored (this is a per-group tracker; non-member nets
              are invisible to it by design).
            * If a group's member is not present in ``routes`` (unrouted),
              that member's length simply isn't recorded;
              :meth:`get_group_skew` returns ``None`` for groups where
              fewer than 2 members are routed.
            * This method may be called multiple times; subsequent calls
              overwrite previously-recorded lengths for the same net ids
              and rebuild the name-keyed skew cache from scratch.
            * The implementation reuses
              :meth:`LengthTracker.calculate_route_length`
              (``length.py:138-153``) as the single source of truth for
              segment summation -- no segment math lives in this module.
        """
        # Collect the set of member net ids cheaply so non-member routes
        # short-circuit before we hit the per-route measurement.
        member_net_ids: set[int] = set()
        for grp in groups:
            member_net_ids.update(grp.net_ids)
            # Pair-id members participate in measurement too (Phase 2F
            # measures both halves of each pair; the dataclass exposes
            # the pair tuple but Phase 1B treats the halves as plain
            # net ids at measurement time).
            for p_id, n_id in grp.pair_ids:
                member_net_ids.add(p_id)
                member_net_ids.add(n_id)

        # Build a {net_id -> Route} lookup for O(1) access (a board can
        # have hundreds of routes; linear scans per group would be O(n*m)).
        routes_by_net: dict[int, Route] = {}
        for route in routes:
            if route.net in member_net_ids:
                routes_by_net[route.net] = route

        # Measure each member and refresh the name-keyed skew cache.  The
        # cache is rebuilt from scratch on every call (mirrors PR #2654's
        # behavior on the diff-pair tracker) so a stale group from a
        # previous call doesn't survive a second record_routes invocation.
        self._skew_map.clear()
        for grp in groups:
            measured: list[float] = []
            # Single-ended members.
            for net_id in grp.net_ids:
                route = routes_by_net.get(net_id)
                if route is None:
                    continue
                length = self._measure_route_total(
                    route,
                    board_thickness_mm,
                    num_copper_layers,
                    blind_buried_supported=blind_buried_supported,
                )
                self.lengths[net_id] = length
                measured.append(length)
            # Pair members (Phase 2F).  Each half is measured independently
            # in Phase 1B; Phase 2F's tuner is responsible for reducing
            # the pair to a single "lane length" if/when needed.
            for p_id, n_id in grp.pair_ids:
                p_route = routes_by_net.get(p_id)
                n_route = routes_by_net.get(n_id)
                if p_route is not None:
                    l_p = self._measure_route_total(
                        p_route,
                        board_thickness_mm,
                        num_copper_layers,
                        blind_buried_supported=blind_buried_supported,
                    )
                    self.lengths[p_id] = l_p
                    measured.append(l_p)
                if n_route is not None:
                    l_n = self._measure_route_total(
                        n_route,
                        board_thickness_mm,
                        num_copper_layers,
                        blind_buried_supported=blind_buried_supported,
                    )
                    self.lengths[n_id] = l_n
                    measured.append(l_n)

            # Populate the name-keyed cache only when ALL declared members
            # are routed -- a partially-routed group is excluded from the
            # bulk skew report (mirrors DiffPairLengthTracker.get_all_skews
            # which omits pairs with unrouted halves).
            expected_count = len(grp.net_ids) + 2 * len(grp.pair_ids)
            if expected_count >= 2 and len(measured) == expected_count:
                self._skew_map[grp.name] = max(measured) - min(measured)

    @staticmethod
    def _measure_route_total(
        route: Route,
        board_thickness_mm: float | None,
        num_copper_layers: int,
        blind_buried_supported: bool = True,
    ) -> float:
        """Return the geometric length of a route in mm, including via drill.

        Delegates to :meth:`LengthTracker.calculate_route_length` for the
        segment portion (single source of truth -- see ``length.py:138-153``)
        and to :meth:`DiffPairLengthTracker._via_length` for the per-via
        drilled-length policy (the Phase 3H formula at
        ``diffpair_length.py:190-223``).

        Keeping the math in the diff-pair tracker (rather than duplicating
        it here) preserves single source of truth: a future change to the
        via-length formula touches one place, and the drift-prevention
        test in this module's suite catches any reimplementation attempt.

        Args:
            route: The route whose length to measure.
            board_thickness_mm: Total stackup thickness in mm.  ``None``
                ->  vias contribute ``0.0``.
            num_copper_layers: Number of copper layers in the stack.
            blind_buried_supported: When ``False`` (Issue #4007), a
                standard (non-micro) via is measured as a full through-via
                (see :meth:`DiffPairLengthTracker._via_length`).  Defaults
                to ``True`` (legacy partial-span behavior).

        Returns:
            Total geometric length (segments + vias) in mm.
        """
        # Segment portion -- the single source of truth for segment
        # summation (do NOT inline the dx/dy/sqrt loop).
        total = LengthTracker.calculate_route_length(route)

        # Via portion.  When the caller did not supply board_thickness_mm
        # (or the stack has fewer than 2 layers) we cannot compute a
        # drilled length and document that vias contribute 0.0.  Mirrors
        # DiffPairLengthTracker._measure_route at diffpair_length.py:183.
        if board_thickness_mm is None or num_copper_layers <= 1:
            return total

        for via in route.vias:
            total += DiffPairLengthTracker._via_length(
                via,
                board_thickness_mm,
                num_copper_layers,
                blind_buried_supported=blind_buried_supported,
            )
        return total

    # =========================================================================
    # Per-group queries
    # =========================================================================

    def get_group_lengths(self, group: MatchGroup) -> dict[int, float]:
        """Return ``{net_id: routed_length_mm}`` for the routed members of ``group``.

        Members that are not yet routed are omitted from the result -- the
        returned dict reflects only the measurement state of the tracker
        for the declared member set.

        Args:
            group: A declared / detected match group.

        Returns:
            A new ``{net_id: routed_length_mm}`` dict (caller-owned;
            mutating it does not affect tracker state).
        """
        result: dict[int, float] = {}
        for net_id in group.net_ids:
            length = self.lengths.get(net_id)
            if length is not None:
                result[net_id] = length
        for p_id, n_id in group.pair_ids:
            l_p = self.lengths.get(p_id)
            if l_p is not None:
                result[p_id] = l_p
            l_n = self.lengths.get(n_id)
            if l_n is not None:
                result[n_id] = l_n
        return result

    def get_group_skew(self, group: MatchGroup) -> float | None:
        """Return ``max(L) - min(L)`` across ``group``'s routed members.

        Returns:
            The skew in mm, or ``None`` if fewer than 2 members are
            currently routed (the minimum required for ``max - min`` to
            be meaningful).
        """
        lengths = self.get_group_lengths(group)
        if len(lengths) < 2:
            return None
        values = lengths.values()
        return max(values) - min(values)

    def get_reference_length(self, group: MatchGroup) -> float | None:
        """Return the reference length for ``group`` per its policy.

        Implements the Phase 1A ``length_match_reference`` semantic:

        - ``group.reference_net_id is None`` -> the longest routed length
          among the group's members (matches the legacy ``tune_match_group``
          longest-as-target semantics at ``serpentine.py:438``).  Returns
          ``None`` when no members are routed.
        - ``group.reference_net_id`` is set -> the recorded length of that
          specific net (the "pace-car" semantic).  Returns ``None`` when
          the reference net itself is unrouted, regardless of the rest
          of the group.

        Args:
            group: A declared / detected match group.

        Returns:
            The reference length in mm, or ``None`` when no reference can
            be derived (unrouted reference, or unrouted group).
        """
        if group.reference_net_id is not None:
            # "Pace-car" semantic -- explicit net wins or nothing.
            return self.lengths.get(group.reference_net_id)

        # Longest-in-group default (matches the legacy tuner's behavior).
        lengths = self.get_group_lengths(group)
        if not lengths:
            return None
        return max(lengths.values())

    def get_all_skews(self) -> dict[str, float]:
        """Return ``{group_name: skew_mm}`` for fully-routed groups.

        Groups whose declared members are not all routed are omitted (the
        partial-routing case is silenced rather than reported as a noisy
        skew -- mirrors :meth:`DiffPairLengthTracker.get_all_skews`).
        Insertion order follows the alphabetical order of group names for
        deterministic iteration -- useful for snapshot-style tests and
        DRC report rendering.
        """
        return dict(sorted(self._skew_map.items(), key=lambda kv: kv[0]))

    # =========================================================================
    # PCB-side measurement primitive (mirrors Phase 2.5c, Issue #2675)
    # =========================================================================

    @staticmethod
    def measure_net_from_pcb(
        pcb: PCB,
        net_id: int,
        board_thickness_mm: float | None = None,
        num_copper_layers: int = 2,
        blind_buried_supported: bool = True,
    ) -> float:
        """Return the geometric routed length of a net in mm, read from a routed PCB.

        Delegates byte-for-byte to
        :meth:`DiffPairLengthTracker.measure_net_from_pcb`
        (``diffpair_length.py:308``).  The math is genuinely net-scoped
        (sum of segments + drilled via length for one net id) and is
        identical whether the net belongs to a differential pair or to an
        N-trace match group -- duplicating the body would re-introduce
        the drift risk that PR #2685's drift-prevention test was written
        to prevent.

        The forwarder shape is intentional: a Phase 2G DRC rule producer
        (``validate/match_group_skew.py``, future) can import either
        primitive without coupling to the diff-pair tracker, and the
        drift-prevention test in this module's suite asserts the two
        produce identical results for the same input.

        Args:
            pcb: The routed PCB to measure.
            net_id: The net number whose segments + vias to sum.
            board_thickness_mm: Total stackup thickness in mm.  When
                ``None`` (the default), vias contribute ``0.0`` to the
                length (documented zero-via-length policy).
            num_copper_layers: Number of copper layers in the stack
                (used to compute per-via drilled length when
                ``board_thickness_mm`` is supplied).  Defaults to ``2``.
            blind_buried_supported: When ``False`` (Issue #4007), a
                standard (non-micro) via is measured as a full through-via.
                Defaults to ``True`` (legacy partial-span behavior).

        Returns:
            Total geometric length (segments + vias) in mm.
        """
        return DiffPairLengthTracker.measure_net_from_pcb(
            pcb,
            net_id,
            board_thickness_mm=board_thickness_mm,
            num_copper_layers=num_copper_layers,
            blind_buried_supported=blind_buried_supported,
        )

    # =========================================================================
    # Internal name cache (populated by record_routes; used by get_all_skews)
    # =========================================================================

    _skew_map: dict[str, float] = field(default_factory=dict, init=False, repr=False)
