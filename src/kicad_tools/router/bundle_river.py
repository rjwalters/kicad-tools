"""Facing-row inversion analysis for the scoped bundle river planner.

Issue #4053 (Phase 3 of epic #4049): a *scoped* river-routing planner for
straight facing-column bundles.  The board-07 DDR data byte is a **full
bus reversal** between two mirrored QFN-48 pin columns (U1's right column
carries ``DQ0`` at the top ``DQ7`` at the bottom; U2's facing left column
carries the same nets in the *opposite* row order), so every pair of nets
whose relative row order flips between the two columns must cross.  Planar
same-layer lane ordering cannot resolve a reversal (the "conflict graph"
of a full reversal is a complete graph): each crossing pair needs one net
to hop to an inner layer to pass under its partner.

This module owns the *pure, search-free* geometry step that the epic's
curation scoped for v1:

    resolve both facing rows -> diff their permutation -> emit the
    inversion set (the required crossing pairs) directly from the static
    placement.

Keeping it a free function (rather than a method on ``Autorouter``) makes
the crossing-set computation unit-testable on a small synthetic two-row
fixture without constructing a full router or board geometry, which the
curated Test Plan requires (assert the correct inversion pairs for a known
reversal AND an *empty* set for a genuinely planar, non-reversed two-row
fixture — the over-triggering regression guard).

The consumer (``Autorouter._apply_byte_lane_inner_priority``) reserves one
inner-layer via-hop corridor per inverted pair, gated behind the default-
OFF ``enable_bundle_river_planner`` flag (the #4051 precedent: even a
geometry-only auto-detect regressed production, so v1 ships opt-in).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RowMember:
    """One net's position on a facing row.

    Attributes:
        net_id: The net's integer id.
        net_name: The net's name (used to match members across the two
            facing rows — the bundle is a one-to-one name-matched bus).
        projection: The member's 1-D coordinate along the row's long axis
            (y for a vertical column, x for a horizontal one).  Only the
            *relative order* of projections is used, so the absolute
            coordinate system does not matter.
    """

    net_id: int
    net_name: str
    projection: float


@dataclass(frozen=True)
class InvertedPair:
    """A required crossing between two nets of a facing-column bundle.

    ``net_a`` / ``net_b`` are ordered so ``net_a`` is the *outer* net on
    the primary row (smaller sorted index): its relative row order flips
    versus ``net_b`` between the two facing columns, so on a single shared
    escape strip the two lanes must cross.  ``loser_net_id`` names the net
    v1 gives an inner-layer via-hop corridor to (the crossing net that
    dips under its partner); see ``choose_via_hop_loser`` for the rule.
    """

    net_a_id: int
    net_b_id: int
    net_a_name: str
    net_b_name: str
    loser_net_id: int


def _sorted_row(members: list[RowMember]) -> list[RowMember]:
    """Return members sorted by projection (ascending), name as tiebreak.

    The name tiebreak keeps the ordering deterministic when two pads share
    a projection (degenerate, but defends against nondeterministic output
    feeding the reservation pass).
    """
    return sorted(members, key=lambda m: (m.projection, m.net_name))


def compute_row_permutation(
    primary_row: list[RowMember],
    secondary_row: list[RowMember],
) -> list[tuple[str, int, int]] | None:
    """Diff the two facing rows into a per-net (primary_rank, secondary_rank).

    Both rows are sorted along their own long axis; each net's rank is its
    index in that sort.  The returned list is keyed by net name (the bus is
    matched one-to-one by name), one entry per net present in BOTH rows.

    Returns ``None`` when the rows do not form a clean one-to-one matched
    bus (different net-name sets between the rows), which is v1's explicit
    non-goal (partial reversals / non-matched net counts belong to #3673's
    shelved general planner).  A ``None`` return tells the caller to skip
    the planner for this group rather than guess.
    """
    if not primary_row or not secondary_row:
        return None

    primary_names = {m.net_name for m in primary_row}
    secondary_names = {m.net_name for m in secondary_row}
    # v1 restriction: exactly one-to-one name matching between the rows.
    if primary_names != secondary_names:
        return None
    if len(primary_names) != len(primary_row) or len(secondary_names) != len(secondary_row):
        # A net appears twice on a row -> not a clean single-column bus.
        return None

    primary_sorted = _sorted_row(primary_row)
    secondary_sorted = _sorted_row(secondary_row)

    primary_rank = {m.net_name: i for i, m in enumerate(primary_sorted)}
    secondary_rank = {m.net_name: i for i, m in enumerate(secondary_sorted)}
    net_id = {m.net_name: m.net_id for m in primary_sorted}

    perm: list[tuple[str, int, int]] = []
    for name in sorted(primary_rank, key=lambda n: primary_rank[n]):
        perm.append((name, net_id[name], secondary_rank[name]))
    return perm


def choose_via_hop_loser(
    net_a_id: int,
    net_a_name: str,
    net_b_id: int,
    net_b_name: str,
) -> int:
    """Pick which net of an inverted pair gets the inner-layer via hop.

    Deterministic rule: the net whose *name* sorts later takes the hop.
    The choice is arbitrary for correctness (either net can be the one to
    dip under) but must be stable so the reservation pass is reproducible
    across runs and the unit tests can pin it.  Name-sort (rather than
    net-id) keeps it independent of net-numbering churn between board
    regenerations.
    """
    if net_a_name <= net_b_name:
        return net_b_id
    return net_a_id


def compute_facing_row_inversions(
    primary_row: list[RowMember],
    secondary_row: list[RowMember],
) -> list[InvertedPair]:
    """Compute the crossing set (inverted pairs) between two facing rows.

    This is the search-free core of the v1 planner.  Two nets form an
    inverted pair when their relative order flips between the primary row
    and the secondary row — i.e. net A is above net B on the primary
    column but below it on the secondary column.  On a single shared
    F.Cu escape strip such a pair MUST cross, so one of the two needs an
    inner-layer via hop to pass under the other.

    For a *genuinely planar* (non-reversed) bundle — where both rows sort
    into the same net order — the inversion set is EMPTY, so the caller
    reserves no via-hop corridors and behaves exactly as before (the
    curated over-triggering regression guard).

    For a *full* bus reversal of ``n`` matched nets this returns all
    ``C(n, 2)`` pairs (every relative order flips).

    Args:
        primary_row: Members on the primary facing column.
        secondary_row: Members on the mirrored facing column.

    Returns:
        The list of inverted pairs (empty when the rows are co-oriented
        or when the rows are not a clean matched bus).  Ordering is
        deterministic (by the primary row's sorted rank of ``net_a``,
        then ``net_b``).
    """
    perm = compute_row_permutation(primary_row, secondary_row)
    if perm is None:
        return []

    inversions: list[InvertedPair] = []
    n = len(perm)
    for i in range(n):
        name_i, id_i, sec_i = perm[i]
        for j in range(i + 1, n):
            name_j, id_j, sec_j = perm[j]
            # perm is ordered by primary rank, so i < j means net_i is
            # above net_j on the primary column.  They cross iff net_i is
            # BELOW net_j on the secondary column (secondary rank flips).
            if sec_i > sec_j:
                loser = choose_via_hop_loser(id_i, name_i, id_j, name_j)
                inversions.append(
                    InvertedPair(
                        net_a_id=id_i,
                        net_b_id=id_j,
                        net_a_name=name_i,
                        net_b_name=name_j,
                        loser_net_id=loser,
                    )
                )
    return inversions


def via_hop_loser_nets(inversions: list[InvertedPair]) -> list[int]:
    """Return the distinct via-hop nets, one per inverted pair, deduped.

    A net can be the "loser" of several inverted pairs (in a full
    reversal the innermost nets lose many crossings).  For the corridor
    reservation pass we only need to hop each losing net ONCE — the hop
    lets it cross under every partner it inverts against — so this dedups
    the loser set preserving first-seen order (deterministic).
    """
    seen: set[int] = set()
    out: list[int] = []
    for pair in inversions:
        if pair.loser_net_id not in seen:
            seen.add(pair.loser_net_id)
            out.append(pair.loser_net_id)
    return out


# ===========================================================================
# Issue #4256 (A3, Track A / epic #4243): the discrete BundlePlan allocator.
#
# A2 (#4255, merged) made a diff-pair rip-up/relief transaction atomic so a
# committed "P-routed / N-stranded" state is unrepresentable.  A3 adds the
# multi-net half: a DISCRETE, combinatorial corridor allocator that assigns
# simultaneously-feasible escape lanes to a whole ``CoupledGroup`` (the TMDS
# D0/D1/D2 bundle = 6 nets) BEFORE any A* search, so greedy N-1 contention is
# removed by construction.
#
# Why discrete, not joint A*: #4065 proved even the 2-net coupled joint
# search basin-floods identically in Python and C++; a 6-net joint search is
# exponentially worse.  The multi-net reasoning is ordered interval packing
# over the shared escape strip + an inner-layer via-hop budget for the
# crossing ("losing") nets, solved by exact graph colouring with
# backtracking over the tiny (~6) member set — NOT grid A*.
#
# The allocator emits either a simultaneously-feasible ``BundlePlan`` (every
# member gets a lane; no two lanes share a cell on a layer) OR an explicit
# ``infeasible`` verdict — NEVER a silent partial.  Infeasibility is a
# first-class output: if the committed placement over-subscribes the inner
# via-hop budget, the honest answer is "no feasible joint assignment exists
# at this placement," which is exactly the signal A4 needs before it chases a
# topological impossibility.
# ===========================================================================

# Lane layer tags.  A lane lives either on the shared outer escape strip
# (F.Cu) or on a reserved inner-layer via-hop channel (a "losing" net that
# dips under its crossing partner).
LANE_LAYER_OUTER = "outer"
LANE_LAYER_INNER = "inner"


@dataclass(frozen=True)
class CoupledMember:
    """One net of a ``CoupledGroup`` (a length-match / diff-pair bundle).

    Attributes:
        net_id: The net's integer id.
        net_name: The net's name (used to match the two facing rows and to
            break projection ties deterministically).
        primary_projection: The member's 1-D coordinate along the primary
            facing column's long axis (only relative order matters).
        secondary_projection: The same net's 1-D coordinate on the mirrored
            (secondary) facing column.  A member whose relative order flips
            between the two columns must cross its partner (an inversion).
        pair_partner_id: For a diff-pair member, the net id of its partner
            leg (``None`` for a single-ended member).  The allocator carries
            this so a plan assigns lanes to BOTH legs or NEITHER — the
            coupling constraint that, together with A2's atomic transaction,
            makes a committed "P-routed / N-stranded" state unrepresentable.
    """

    net_id: int
    net_name: str
    primary_projection: float
    secondary_projection: float
    pair_partner_id: int | None = None


@dataclass(frozen=True)
class CoupledGroup:
    """The set of nets allocated together (e.g. TMDS D0/D1/D2 = 6 nets).

    A ``CoupledGroup`` is the unit the discrete allocator reasons over: its
    members' primary/secondary projections determine the inversion set, and
    its diff-pair links determine the atomic coupling constraint.
    """

    group_name: str
    members: tuple[CoupledMember, ...]

    def primary_row(self) -> list[RowMember]:
        """The primary facing column as ``RowMember`` projections."""
        return [RowMember(m.net_id, m.net_name, m.primary_projection) for m in self.members]

    def secondary_row(self) -> list[RowMember]:
        """The mirrored (secondary) facing column as ``RowMember`` projections."""
        return [RowMember(m.net_id, m.net_name, m.secondary_projection) for m in self.members]


@dataclass(frozen=True)
class EscapeLane:
    """One member's assigned escape lane in a ``BundlePlan``.

    Attributes:
        net_id: The owning net's id.
        net_name: The owning net's name.
        layer: ``LANE_LAYER_OUTER`` (shared F.Cu escape strip) or
            ``LANE_LAYER_INNER`` (a reserved inner-layer via-hop channel for
            a crossing/losing net).
        order_index: The lane's ordered slot WITHIN its layer (the interval
            packing position).  Slots are unique per layer, so no two lanes
            share a cell on the same layer — the feasibility invariant.
        via_hop: ``True`` iff this is a losing net that dips to the inner
            layer to pass under its crossing partner.
        pair_partner_id: The diff-pair partner leg (``None`` if single-ended).
    """

    net_id: int
    net_name: str
    layer: str
    order_index: int
    via_hop: bool
    pair_partner_id: int | None = None


@dataclass(frozen=True)
class BundlePlan:
    """The allocator's verdict for one ``CoupledGroup``.

    Either ``feasible`` (every member carries an ``EscapeLane`` and no two
    lanes share a cell on a layer) or infeasible (``lanes`` empty, ``reason``
    explains the over-subscription).  There is no partial state: a feasible
    plan lanes EVERY member; an infeasible plan lanes NONE — this is the
    "both legs or neither" guarantee at plan granularity.
    """

    group_name: str
    feasible: bool
    lanes: tuple[EscapeLane, ...] = ()
    reason: str = ""
    inner_lanes_required: int = 0
    inner_lane_budget: int = 0

    @property
    def infeasible(self) -> bool:
        """Convenience inverse of ``feasible``."""
        return not self.feasible

    def lane_for(self, net_id: int) -> EscapeLane | None:
        """Return the lane assigned to ``net_id`` (``None`` if unassigned)."""
        for lane in self.lanes:
            if lane.net_id == net_id:
                return lane
        return None

    def via_hop_lanes(self) -> list[EscapeLane]:
        """The lanes that dip to the inner layer (losing nets)."""
        return [lane for lane in self.lanes if lane.via_hop]

    @classmethod
    def infeasible_plan(
        cls,
        group_name: str,
        reason: str,
        *,
        inner_lanes_required: int = 0,
        inner_lane_budget: int = 0,
    ) -> BundlePlan:
        """Build an explicit infeasibility verdict (no lanes)."""
        return cls(
            group_name=group_name,
            feasible=False,
            lanes=(),
            reason=reason,
            inner_lanes_required=inner_lanes_required,
            inner_lane_budget=inner_lane_budget,
        )


def _min_graph_colouring(
    nodes: list[int],
    edges: list[tuple[int, int]],
) -> dict[int, int]:
    """Exact minimum vertex colouring by backtracking (tiny N only).

    Assigns each node a colour in ``0..k-1`` such that no edge joins two
    same-coloured nodes, using the MINIMUM number of colours ``k``.  For the
    coupled-group allocator the node set is a handful of "losing" nets and
    the edges are their mutual crossings, so an exact backtracking search
    (iterative deepening on ``k``) is instant and gives an HONEST inner-layer
    lane count for the feasibility verdict — a greedy upper bound could
    over-report and declare a feasible bundle infeasible.

    ``nodes`` order is respected (callers pass a deterministic order), so the
    colouring is reproducible.
    """
    if not nodes:
        return {}
    adj: dict[int, set[int]] = {n: set() for n in nodes}
    for a, b in edges:
        if a in adj and b in adj and a != b:
            adj[a].add(b)
            adj[b].add(a)

    def _try(i: int, k: int, colouring: dict[int, int]) -> bool:
        if i == len(nodes):
            return True
        node = nodes[i]
        for colour in range(k):
            if all(colouring.get(nb) != colour for nb in adj[node]):
                colouring[node] = colour
                if _try(i + 1, k, colouring):
                    return True
                del colouring[node]
        return False

    for k in range(1, len(nodes) + 1):
        colouring: dict[int, int] = {}
        if _try(0, k, colouring):
            return colouring
    # Unreachable (k == len(nodes) always succeeds), but keep the type total.
    return {n: i for i, n in enumerate(nodes)}


def allocate_bundle_plan(
    group: CoupledGroup,
    *,
    inner_lane_budget: int = 1,
) -> BundlePlan:
    """Discretely allocate simultaneously-feasible escape lanes for a group.

    This is the multi-net half of Track A (#4256): a combinatorial corridor
    allocator that runs ONCE per coupled group (not a hot A* loop).  It
    formulates escape-lane assignment as ordered interval packing over the
    shared outer escape strip plus an inner-layer via-hop budget for the
    crossing ("losing") nets, and reasons over the tiny member set exactly.

    Algorithm:

    1. **Clean-bus guard.**  If the two facing rows are not a clean
       one-to-one name-matched bus (``compute_row_permutation`` is ``None``),
       return an explicit infeasible verdict — the same #4053 non-goal
       discipline (partial / non-matched buses belong to the shelved #3673
       general planner, not here).
    2. **Coupling guard.**  Every referenced diff-pair partner must be a
       member of the group; otherwise the group cannot be allocated
       atomically (a leg would be routed elsewhere) — infeasible.
    3. **Planar case.**  When the inversion set is empty (a co-oriented
       bundle), assign trivial in-order lanes on the outer strip — no via
       hops.  This is the over-trigger guard: a planar bundle must not
       reserve inner-layer channels.
    4. **Reversal case.**  Each crossing pair's ``loser`` (``choose_via_hop_loser``)
       must dip to the inner layer.  Two losing nets that ALSO cross each
       other cannot share one inner lane, so the losers' mutual-crossing
       graph is coloured exactly: the colour count is the number of inner
       via-hop lanes the plan needs.  If that exceeds ``inner_lane_budget``
       the bundle is genuinely over-subscribed at this placement →
       **infeasible** (a first-class output, NOT a silent partial).  The
       non-losing nets keep the outer strip in projection order (they no
       longer cross anything, so the interval packing is trivially conflict
       free), and each losing net takes its coloured inner lane.

    Args:
        group: The coupled group (TMDS bundle, etc.) to allocate.
        inner_lane_budget: How many independent inner-layer via-hop channels
            are available at this placement.  On board 07's 4-layer tier-1
            stack the inner copper layers are PLANES, so the only alternate
            routing surface is B.Cu → a realistic budget of ``1``.

    Returns:
        A feasible ``BundlePlan`` (every member laned, no two lanes sharing a
        cell on a layer) or an explicit infeasible ``BundlePlan``.
    """
    members = list(group.members)
    if len(members) < 2:
        return BundlePlan.infeasible_plan(
            group.group_name,
            f"coupled group has {len(members)} member(s); nothing to allocate",
        )

    primary_row = group.primary_row()
    secondary_row = group.secondary_row()

    perm = compute_row_permutation(primary_row, secondary_row)
    if perm is None:
        return BundlePlan.infeasible_plan(
            group.group_name,
            "coupled group is not a clean one-to-one name-matched bus "
            "(partial / non-matched reversals are out of scope for A3)",
        )

    ids = {m.net_id for m in members}
    for m in members:
        if m.pair_partner_id is not None and m.pair_partner_id not in ids:
            return BundlePlan.infeasible_plan(
                group.group_name,
                f"diff-pair partner {m.pair_partner_id} of net {m.net_id} "
                "is not a member of the coupled group; cannot allocate atomically",
            )

    # Interval-packing order on the shared strip = primary projection order.
    primary_sorted = sorted(members, key=lambda m: (m.primary_projection, m.net_name))

    inversions = compute_facing_row_inversions(primary_row, secondary_row)

    if not inversions:
        # Planar bundle: trivial in-order lanes, no inner via hops.
        lanes = tuple(
            EscapeLane(
                net_id=m.net_id,
                net_name=m.net_name,
                layer=LANE_LAYER_OUTER,
                order_index=i,
                via_hop=False,
                pair_partner_id=m.pair_partner_id,
            )
            for i, m in enumerate(primary_sorted)
        )
        return BundlePlan(
            group_name=group.group_name,
            feasible=True,
            lanes=lanes,
            inner_lanes_required=0,
            inner_lane_budget=inner_lane_budget,
        )

    # Reversal: the loser of each crossing dips to an inner via-hop lane.
    losers = set(via_hop_loser_nets(inversions))

    # Inner-layer conflict graph: two losers that ALSO cross each other
    # cannot share one inner lane.
    inner_conflicts = [
        (p.net_a_id, p.net_b_id)
        for p in inversions
        if p.net_a_id in losers and p.net_b_id in losers
    ]
    # Colour the losers in a deterministic (primary-projection) order.
    loser_order = [m.net_id for m in primary_sorted if m.net_id in losers]
    colouring = _min_graph_colouring(loser_order, inner_conflicts)
    inner_lanes_required = (max(colouring.values()) + 1) if colouring else 0

    if inner_lanes_required > inner_lane_budget:
        return BundlePlan.infeasible_plan(
            group.group_name,
            f"{inner_lanes_required} inner via-hop lane(s) required to resolve "
            f"{len(inner_conflicts)} mutual crossing(s) among {len(losers)} "
            f"losing net(s), but the inner-layer via-hop budget is "
            f"{inner_lane_budget}: no simultaneously-feasible single-strip "
            "assignment exists at this placement",
            inner_lanes_required=inner_lanes_required,
            inner_lane_budget=inner_lane_budget,
        )

    # Feasible.  Non-losers hold the outer strip in projection order (they
    # no longer cross anything); losers take their coloured inner lane.
    partner_of = {m.net_id: m.pair_partner_id for m in members}
    lanes_list: list[EscapeLane] = []
    outer_index = 0
    for m in primary_sorted:
        if m.net_id in losers:
            lanes_list.append(
                EscapeLane(
                    net_id=m.net_id,
                    net_name=m.net_name,
                    layer=LANE_LAYER_INNER,
                    order_index=colouring[m.net_id],
                    via_hop=True,
                    pair_partner_id=partner_of.get(m.net_id),
                )
            )
        else:
            lanes_list.append(
                EscapeLane(
                    net_id=m.net_id,
                    net_name=m.net_name,
                    layer=LANE_LAYER_OUTER,
                    order_index=outer_index,
                    via_hop=False,
                    pair_partner_id=partner_of.get(m.net_id),
                )
            )
            outer_index += 1

    return BundlePlan(
        group_name=group.group_name,
        feasible=True,
        lanes=tuple(lanes_list),
        inner_lanes_required=inner_lanes_required,
        inner_lane_budget=inner_lane_budget,
    )
