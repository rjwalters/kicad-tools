"""Re-derive match-group length-skew data from a routed PCB + net-class map.

Issue #2710, Epic #2661 Phase 2.5G.

The :class:`~kicad_tools.validate.rules.match_group_length_skew.MatchGroupLengthSkewRule`
(Phase 2G / Issue #2702, PR #2711) consumes caller-supplied
``group_skew_data: dict[str, float]`` (name-keyed), a
``tracker_match_groups: list[MatchGroup]`` declaration list, and an
optional per-group ``threshold_map: dict[str, float]``.  The rule
itself deliberately does NOT re-derive any of these inputs to avoid
drift between the router's per-group length tracker
(:class:`~kicad_tools.router.match_group_length.MatchGroupTracker`) and
the validator's skew check.

This module provides the producer side -- a thin shim that:

1. Detects match groups on a routed PCB using the same layered
   detector the router uses (:func:`match_group_detection.detect_match_groups`).
2. Sums per-net length from PCB-side segments + vias (via
   :meth:`MatchGroupTracker.measure_net_from_pcb`, a thin forwarder to
   :meth:`DiffPairLengthTracker.measure_net_from_pcb`) for every
   declared member of each detected group.
3. Builds the ``group_skew_data`` (``max(L) - min(L)`` across the
   group), the detected ``tracker_match_groups`` list, and the per-group
   ``threshold_map`` from each group's net class
   :meth:`NetClassRouting.effective_length_match_tolerance`.

Sister of :mod:`kicad_tools.validate.diffpair_skew` (Phase 2.5c, PR
#2685) -- this is the third producer-side wiring follow-up in Epic
#2661, mirroring the diff-pair counterpart byte-for-byte modulo type
renames (group-name keying instead of net-name-tuple keying).

Why re-derive instead of persist?

The recommended approach per the curator review on #2702 / #2710 is to
recover skew at validation time rather than persist it as PCB metadata.
PCB segment/via geometry IS the source of truth for routed length; the
skew is a pure function of (group_member_net_ids, geometry,
net_class_map.length_match_tolerance).  This avoids needing to round-
trip Phase 1B's :class:`MatchGroupTracker` state through the PCB schema
and keeps the validate->router boundary thin.

Boundary discipline:

This module is the validate-side dependency on the router's match-group
primitives.  The rule itself remains router-independent.  The
:meth:`MatchGroupTracker.measure_net_from_pcb` forwarder is preferred
over reaching into ``router/diffpair_length.py`` directly so the import
boundary in this package stays clean (validate/match_group_skew.py
imports from router/match_group_length.py only).  Tests live in
``tests/test_validate_match_group_skew.py`` and the drift-prevention
test mirrors PR #2685's pattern.

Group-of-pairs note (Phase 2F reserve):

The :class:`MatchGroup` dataclass has a ``pair_ids`` field reserved for
Phase 2F group-of-pairs composition.  Phase 1B's measurement layer
ignores it; this producer follows suit and only sums ``net_ids`` until
Phase 2F lands.  This matches the Phase 1B forwarder semantics and the
rule's current member-count documentation.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.router.match_group_length import MatchGroup
    from kicad_tools.router.rules import NetClassRouting
    from kicad_tools.schema.pcb import PCB


logger = logging.getLogger(__name__)


def derive_group_skew_data(
    pcb: PCB,
    net_class_map: dict[str, NetClassRouting] | None,
    board_thickness_mm: float | None = None,
    num_copper_layers: int = 2,
) -> tuple[dict[str, float], list[MatchGroup], dict[str, float]]:
    """Re-derive ``(group_skew_data, tracker_match_groups, threshold_map)``.

    Walks the PCB's net table, runs the layered match-group detector,
    and sums per-net length from PCB-side segments + vias for each
    detected group's members.  Returns the three inputs the
    :class:`~kicad_tools.validate.rules.match_group_length_skew.MatchGroupLengthSkewRule`
    expects.

    Args:
        pcb: The routed PCB to inspect.  ``pcb.nets`` is consulted for
            the detector; ``pcb.segments_in_net`` and ``pcb.vias_in_net``
            are consulted for length measurement.
        net_class_map: Map of ``{net_name: NetClassRouting}`` (the
            autorouter convention used by
            :attr:`~kicad_tools.router.core.AutoRouter.net_class_map`).
            ``None`` or empty returns ``({}, [], {})`` so the standalone
            ``kct check`` path degrades gracefully to a no-op.
        board_thickness_mm: Total stackup thickness in mm.  When
            ``None`` (the default), vias contribute ``0.0`` to the
            length.  Mirrors
            :meth:`~kicad_tools.router.match_group_length.MatchGroupTracker.record_routes`
            and the curator's documented policy on #2647.
        num_copper_layers: Number of copper layers in the stack (used
            to compute per-via drilled length when ``board_thickness_mm``
            is supplied).  Defaults to ``2``.

    Returns:
        ``(group_skew_data, tracker_match_groups, threshold_map)`` where

        * ``group_skew_data`` is a ``{group_name -> skew_mm}`` dict
          matching :meth:`MatchGroupTracker.get_all_skews` shape.
          Groups with any unrouted member are omitted (graceful
          degradation, matching the producer-side tracker's "all
          members routed" gating).
        * ``tracker_match_groups`` is the detected list of
          :class:`MatchGroup` instances.  Threaded through to the rule's
          ``tracker_match_groups`` parameter so the rule knows which
          groups are "declared" -- groups not in this list are ignored
          per Phase 2G's declaration-gating contract (#2702 AC #6).
        * ``threshold_map`` is a ``{group_name -> tolerance_mm}`` map of
          per-group tolerance overrides.  Each group's tolerance comes
          from :meth:`NetClassRouting.effective_length_match_tolerance`
          on the net class of the group's first declared member (groups
          should span one net class; when members diverge, the first
          member by net id wins -- deterministic given
          :class:`MatchGroup`'s sorted-by-net-id member ordering).

    Notes:
        Idempotence guarantee (drift-prevention AC): given the same
        physical routing and the same net classes, this function returns
        the same ``group_skew_data`` as the producer-side
        :meth:`MatchGroupTracker.get_all_skews` for routes recorded
        via :meth:`MatchGroupTracker.record_routes`.  This is the
        property tested in ``tests/test_validate_match_group_skew.py``.

        Suffix inference is OFF here (``enable_suffix_inference=False``,
        the detector's default).  Standalone ``kct check`` follows the
        conservative-validation convention -- groups must be explicitly
        declared via ``NetClassRouting.length_match_group`` (or the
        legacy ``add_match_group`` API).  Suffix inference is opt-in via
        CLI flag, not on by default.
    """
    if not net_class_map:
        return {}, [], {}

    # Local imports keep the validate -> router boundary explicit: this
    # is the validate-side dependency on the router's match-group
    # primitives.  See module docstring.
    from kicad_tools.router.match_group_detection import detect_match_groups
    from kicad_tools.router.match_group_length import MatchGroupTracker
    from kicad_tools.validate.rules.match_group_length_skew import (
        DEFAULT_MATCH_GROUP_TOLERANCE_MM,
    )

    # Build the {net_id: net_name} map the detector expects.
    net_names: dict[int, str] = {}
    for net_id, net in pcb.nets.items():
        net_name = getattr(net, "name", None)
        if not net_name:
            continue
        net_names[net_id] = net_name

    if not net_names:
        return {}, [], {}

    # Synthesise a {net_name: class_name} map + a class-name-keyed
    # routing dict so the layered detector can consult
    # ``NetClassRouting.length_match_group`` explicit declarations.
    # Mirrors the autorouter convention -- see
    # :func:`derive_skew_data` in ``diffpair_skew.py:126-149`` (byte-
    # for-byte equivalent idiom).
    net_to_class: dict[str, str] = {}
    synth_routing: dict = dict(net_class_map)
    for net_name, nc in net_class_map.items():
        cls_name = getattr(nc, "name", None)
        if cls_name is None:
            continue
        net_to_class[net_name] = cls_name
        synth_routing.setdefault(cls_name, nc)

    try:
        detected = detect_match_groups(
            net_names,
            net_class_routing=synth_routing,
            net_to_class=net_to_class,
            # Conservative-validation: suffix inference is opt-in via CLI
            # flag, not on by default in standalone ``kct check``.
            enable_suffix_inference=False,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[match-group-skew] detector raised %s; treating as no skew data",
            exc,
        )
        return {}, [], {}

    group_skew_data: dict[str, float] = {}
    threshold_map: dict[str, float] = {}

    # Build the {net_id -> net_name} reverse so we can look up the net
    # class for any member via the autorouter's name-keyed map.
    for grp in detected:
        # Phase 2F reserve: pair_ids are ignored here -- the Phase 1B
        # measurement layer does the same, and the rule's documented
        # member-count today is len(net_ids) + 2 * len(pair_ids).  We
        # match Phase 1B byte-for-byte (only net_ids contribute to the
        # skew computation in this phase).
        if not grp.net_ids:
            logger.debug(
                "[match-group-skew] %s skipped (no single-ended members in Phase 1B scope)",
                grp.name,
            )
            continue

        # Gating: every member must have at least one segment or via on
        # the PCB.  This mirrors :meth:`MatchGroupTracker.get_all_skews`
        # which only populates the name-keyed cache when ALL declared
        # members are routed (a partially-routed group is excluded from
        # the bulk skew report -- see match_group_length.py:310-316).
        any_unrouted = False
        measured: list[float] = []
        for net_id in grp.net_ids:
            if not _net_has_geometry(pcb, net_id):
                any_unrouted = True
                break
            length = MatchGroupTracker.measure_net_from_pcb(
                pcb,
                net_id,
                board_thickness_mm=board_thickness_mm,
                num_copper_layers=num_copper_layers,
            )
            measured.append(length)

        if any_unrouted or len(measured) < 2:
            logger.debug(
                "[match-group-skew] %s skipped (partial routing or <2 members)",
                grp.name,
            )
            continue

        skew_mm = max(measured) - min(measured)
        group_skew_data[grp.name] = skew_mm

        # Per-group threshold: use the net class of the first declared
        # member (groups should span one class; if they diverge, the
        # first-member-by-net-id wins -- deterministic given
        # :class:`MatchGroup`'s sorted-by-net-id member ordering from
        # detect_match_groups).
        first_net_id = grp.net_ids[0]
        first_net_name = net_names.get(first_net_id)
        nc = net_class_map.get(first_net_name) if first_net_name else None
        if nc is not None and hasattr(nc, "effective_length_match_tolerance"):
            threshold_map[grp.name] = nc.effective_length_match_tolerance(
                default=DEFAULT_MATCH_GROUP_TOLERANCE_MM,
            )
        else:
            threshold_map[grp.name] = DEFAULT_MATCH_GROUP_TOLERANCE_MM

        logger.info(
            "[match-group-skew] %s skew=%.3f mm (members=%d, tolerance=%.3f)",
            grp.name,
            skew_mm,
            len(measured),
            threshold_map[grp.name],
        )

    # Sort the skew dict by group name for deterministic iteration --
    # matches :meth:`MatchGroupTracker.get_all_skews` exactly (see
    # match_group_length.py:447-448).
    group_skew_data = dict(sorted(group_skew_data.items(), key=lambda kv: kv[0]))

    return group_skew_data, detected, threshold_map


def _net_has_geometry(pcb: PCB, net_id: int) -> bool:
    """Return True if ``net_id`` has at least one segment or via on ``pcb``.

    Used to mirror :meth:`MatchGroupTracker.get_all_skews` semantics:
    groups where one member is entirely unrouted are omitted from the
    ``group_skew_data`` dict.  Without this gate, an unrouted member
    would emit a spurious ``skew_mm = max(L) - 0 = max(L)`` entry that
    would fire a bogus DRC violation.
    """
    for _seg in pcb.segments_in_net(net_id):
        return True
    for _via in pcb.vias_in_net(net_id):
        return True
    return False
