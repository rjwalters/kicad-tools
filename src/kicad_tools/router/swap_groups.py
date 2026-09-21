"""Declared swap-group assignment (Issue #5522, Phase 1 of Epic #5511).

Phase 1 of the netlist-degrees-of-freedom epic: when a designer DECLARES a
``swap_group`` on a subset of a bundle's nets (:attr:`~kicad_tools.router.
rules.NetClassRouting.swap_group`), and the bundle is measured REVERSED
(:func:`~kicad_tools.router.stuck_classifier._resolve_bundle_orientation`),
this module computes the within-group pad-to-net re-binding on the secondary
facing component that minimises facing-row crossings among the declared
group -- and reports the residual crossings against undeclared ("fixed")
siblings sharing the same length-match group.

**Report-only.**  Nothing here mutates a board or proposes an applicator
action; it is pure data computed from :class:`~kicad_tools.router.
bundle_river.RowMember` projections, reusing :func:`~kicad_tools.router.
bundle_river.compute_facing_row_inversions` (Issue #4053) rather than
reinventing crossing-counting.

**Declared-only.**  Swap-group membership comes ONLY from the caller's
``group_nets`` argument (traced back to a sidecar ``swap_group`` declaration
by the classifier).  This module never infers membership from net names,
pin functions, or footprints.

Algorithm (sort-and-pair, no LAP solver -- reserved for a later phase with a
second cost): restrict both facing rows to the declared group's members,
sort each by its own projection, and re-bind rank-for-rank -- the primary
row's rank-``i`` net is assigned to whichever physical secondary-row slot
currently sits at rank ``i``.  For an already co-oriented (planar) subset
this naturally degenerates to the identity permutation (the over-triggering
guard falls out of the algorithm rather than needing a special case).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kicad_tools.router.bundle_river import RowMember, compute_facing_row_inversions

__all__ = [
    "MIN_SWAP_GROUP_NETS",
    "SwapAssignment",
    "propose_swap_assignment",
]

# A swap of a single net is a no-op (nothing to pair it against); two is the
# smallest group a rebinding can meaningfully act on.
MIN_SWAP_GROUP_NETS = 2


def _sorted_row(members: list[RowMember]) -> list[RowMember]:
    """Sort by projection ascending, net name as a deterministic tiebreak."""
    return sorted(members, key=lambda m: (m.projection, m.net_name))


@dataclass(frozen=True)
class SwapAssignment:
    """The pure, geometry-only result of a sort-and-pair swap proposal.

    ``net_rebinding`` maps the net name CURRENTLY occupying a secondary-row
    slot to the net name that slot should carry after the swap -- filtered to
    non-identity entries only (an unchanged slot is simply absent, mirroring
    how :attr:`~kicad_tools.router.placement_delta.PlacementDelta.component_id`
    is only emitted when non-empty).  An empty ``net_rebinding`` on an
    already co-oriented subset IS the identity result / no-op guard.

    ``group_crossings_before`` / ``group_crossings_after`` /
    ``fixed_members`` are populated only when the caller supplied
    ``whole_group_rows`` (the wider match-group membership, which may include
    undeclared siblings that stay fixed); otherwise they mirror
    ``crossings_before`` / ``crossings_after`` and ``fixed_members`` is empty.
    """

    net_rebinding: dict[str, str]
    crossings_before: int
    crossings_after: int
    group_crossings_before: int
    group_crossings_after: int
    fixed_members: tuple[str, ...] = field(default_factory=tuple)


def propose_swap_assignment(
    primary_row: list[RowMember],
    secondary_row: list[RowMember],
    group_nets: set[str] | frozenset[str],
    *,
    whole_group_rows: tuple[list[RowMember], list[RowMember]] | None = None,
) -> SwapAssignment | None:
    """Propose a crossing-minimising pad re-binding for a declared swap group.

    Args:
        primary_row: The bundle's primary facing row (kept fixed).
        secondary_row: The bundle's secondary facing row (the one whose
            pad-to-net bindings the proposal re-assigns).
        group_nets: The DECLARED swap group's net names.  Only these nets
            may be re-bound; everything else is fixed by omission.
        whole_group_rows: Optional ``(primary_row, secondary_row)`` pair
            already subset to the WIDER match-group membership (e.g. the
            sidecar's ``length_match_group``, which may include undeclared
            fixed siblings).  When given, the "whole group" crossing
            figures are computed over it with the swap-group members
            re-bound and the undeclared siblings held fixed, so residual
            crossings against them are counted and attributed rather than
            hidden.

    Returns:
        A :class:`SwapAssignment`, or ``None`` when fewer than
        :data:`MIN_SWAP_GROUP_NETS` of the declared group's nets are shared
        by both rows (mirrors the ``MIN_FACING_ROW_NETS`` guard in
        :func:`~kicad_tools.router.stuck_classifier._resolve_bundle_orientation`
        -- there is nothing to pair).
    """
    primary_by_name = {m.net_name: m for m in primary_row if m.net_name in group_nets}
    secondary_by_name = {m.net_name: m for m in secondary_row if m.net_name in group_nets}
    shared = set(primary_by_name) & set(secondary_by_name)
    if len(shared) < MIN_SWAP_GROUP_NETS:
        return None

    primary_sub = [primary_by_name[name] for name in shared]
    secondary_sub = [secondary_by_name[name] for name in shared]

    crossings_before = len(compute_facing_row_inversions(primary_sub, secondary_sub))

    primary_sorted = _sorted_row(primary_sub)
    secondary_sorted = _sorted_row(secondary_sub)

    # Rank-for-rank re-bind: the physical slot at secondary rank i (currently
    # occupied by secondary_sorted[i]'s net) is assigned the net that sits at
    # primary rank i.  net_id is looked up from the primary row: the same net
    # carries the same id everywhere on the board, so either row would do.
    id_by_name = {m.net_name: m.net_id for m in primary_sub}
    net_rebinding: dict[str, str] = {}
    new_secondary_sub: list[RowMember] = []
    for old_slot, new_name in zip(
        secondary_sorted, (m.net_name for m in primary_sorted), strict=True
    ):
        new_secondary_sub.append(
            RowMember(
                net_id=id_by_name[new_name],
                net_name=new_name,
                projection=old_slot.projection,
            )
        )
        if old_slot.net_name != new_name:
            net_rebinding[old_slot.net_name] = new_name

    crossings_after = len(compute_facing_row_inversions(primary_sub, new_secondary_sub))

    if whole_group_rows is None:
        return SwapAssignment(
            net_rebinding=net_rebinding,
            crossings_before=crossings_before,
            crossings_after=crossings_after,
            group_crossings_before=crossings_before,
            group_crossings_after=crossings_after,
        )

    whole_primary, whole_secondary = whole_group_rows
    whole_primary_by_name = {m.net_name: m for m in whole_primary}
    whole_secondary_by_name = {m.net_name: m for m in whole_secondary}
    whole_shared = set(whole_primary_by_name) & set(whole_secondary_by_name)

    whole_primary_sub = [whole_primary_by_name[name] for name in whole_shared]
    whole_secondary_sub = [whole_secondary_by_name[name] for name in whole_shared]
    group_crossings_before = len(
        compute_facing_row_inversions(whole_primary_sub, whole_secondary_sub)
    )

    rebound_whole_secondary = [
        (
            RowMember(
                net_id=id_by_name[net_rebinding[m.net_name]],
                net_name=net_rebinding[m.net_name],
                projection=m.projection,
            )
            if m.net_name in net_rebinding
            else m
        )
        for m in whole_secondary_sub
    ]
    group_crossings_after = len(
        compute_facing_row_inversions(whole_primary_sub, rebound_whole_secondary)
    )

    fixed_members = tuple(sorted(whole_shared - shared))

    return SwapAssignment(
        net_rebinding=net_rebinding,
        crossings_before=crossings_before,
        crossings_after=crossings_after,
        group_crossings_before=group_crossings_before,
        group_crossings_after=group_crossings_after,
        fixed_members=fixed_members,
    )
