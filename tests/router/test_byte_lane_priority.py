"""Tests for the mirrored byte-lane scaffolding hook (Issue #2962).

The :meth:`Autorouter._apply_byte_lane_inner_priority` helper detects
mirrored byte-lane match groups (e.g. board 07's DDR data byte on a
mirrored QFN-48 pair) but, in this scaffolding-only cut, **returns the
input net order unchanged**.  The detection / projection / sort
machinery and the three integration hook sites (``route_all``,
``route_all_negotiated``, ``TwoPhaseRouter``) are preserved as the
surface for a future layered-escape PR.

PR #2969 design history (preserved as the AC for issue #2962's
net-ordering exploration):

- **Round 1** (broader plan): demoted both corner (0, n-1) AND
  second-inward (2, n-3).  Got 27/31 nets but DRC was 86, over the
  70 allowlist.
- **Round 2** (constrained, demote second-inward only): DRC dropped
  to 4 but yield regressed to 20/31 and ``match_group_length_skew``
  was silently not exercised.
- **Round 3** (promote inner-corner to rank 0 directly): the
  Judge's recommended "dual" interpretation.  Yield 24/31, DRC 12
  (well under 70 allowlist), but DQ5 still blocked by DQ4 with the
  identical 0.44mm clearance failure as round 2 -- the underlying
  constraint is geometric, not orderable.
- **Terminal outcome** (PR #2969): scaffolding-only.  The helper
  detects but does not reorder.
- **Issue #2983** (this contract): adds **corridor reservation**
  on top of the scaffolding.  Ordering remains identity (PR #2969's
  R1/R2/R3 contract preserved), but on multi-layer stack-ups the
  helper now reserves a per-net lateral corridor for each
  inner-corner pad (positions 1 and N-2 of the sorted row).  See
  ``test_byte_lane_corridor_reservation.py`` for the
  reservation-count contract.  The reservation primitive itself
  is :meth:`EscapeRouter.reserve_inner_corner_lane_corridor`, a
  single-ended generalisation of the diff-pair corridor primitive
  from PR #2911.

Root cause that motivates the eventual implementation (per the
issue):

    Pin row order on U1.25-35 is
    ``DQ0, DQ1, DQ2, DQ3, DM0, DQS_P, DQS_N, DQ4, DQ5, DQ6, DQ7``
    (mirrored on U2.1-11).  When DQS routes first (diff-pair pre-pass)
    and the remaining DQ nets fill in by ``_get_net_priority`` order
    (bbox-diagonal among same-class nets), DQ1 (pad index 1) and DQ6
    (pad index n-2) are bracketed by:
      * the corner net (DQ0 / DQ7) -- escapes via the corner gap, and
      * the second-inward net (DQ2 / DQ5) -- consumes the only
        remaining lateral lane.
    The inner-corner nets are squeezed out.

The helper's current scaffolding contract:

1.  **Identity on tiny inputs** -- ``net_order`` shorter than 4 is
    returned unchanged.
2.  **Identity on boards without match groups** -- no group
    declarations + no suffix-inference matches => identity.
3.  **Identity on small groups** -- groups with fewer than 5 members
    don't exhibit the mirrored byte-lane topology; the helper degrades
    to identity for them.
4.  **Identity on mirrored byte-lane groups** -- detection runs but
    no reorder is applied.  A future PR will replace this with a
    placement-aware layered-escape strategy.
5.  **Multi-group preservation** -- non-byte-lane groups in the same
    routing pass are not affected.
6.  **Length and membership invariant** -- output is always a
    permutation of the input.
"""

from __future__ import annotations

from kicad_tools.router.core import Autorouter
from kicad_tools.router.rules import NetClassRouting


# =============================================================================
# Helpers
# =============================================================================


def _make_byte_lane_router(
    *,
    group_name: str = "DDR_DATA_BYTE_0",
    group_size: int = 9,
    pitch: float = 0.8,
    priority: int = 1,
) -> tuple[Autorouter, list[int], list[str]]:
    """Build a synthetic router with a mirrored byte-lane match group.

    The fixture mimics board 07's DDR data byte: two mirrored
    components (U1 right edge, U2 left edge) face each other across
    a routing channel, with ``group_size`` nets each connecting a
    pair of pads (one on each component) on the same row.

    Args:
        group_name: ``length_match_group`` name (also class name).
        group_size: Number of nets in the byte-lane.  Default 9
            mirrors the post-diffpair-prepass DDR byte (DQ0..DQ7 + DM0,
            with DQS_P/DQS_N already filtered out as pre-routed).
        pitch: Vertical spacing between pads on each component.
        priority: Net-class priority.

    Returns:
        Tuple of (router, net_ids_in_creation_order, net_names).
        ``net_ids[0]`` corresponds to the topmost pad pair,
        ``net_ids[-1]`` to the bottommost pad pair.
    """
    cls = NetClassRouting(
        name=group_name,
        priority=priority,
        trace_width=0.15,
        clearance=0.10,
        length_critical=True,
        length_match_group=group_name,
        length_match_reference=None,
        length_match_tolerance_mm=0.1,
    )
    net_class_map: dict[str, NetClassRouting] = {}
    router = Autorouter(width=120.0, height=80.0, net_class_map=net_class_map)

    # Two mirrored components: U1 on the left (pads at x=40, vertical
    # row), U2 on the right (pads at x=80, vertical row).  Y-positions
    # spaced by ``pitch`` and centred on y=40.
    centre_y = 40.0
    base_y = centre_y - (group_size - 1) * pitch / 2.0

    net_ids: list[int] = []
    net_names: list[str] = []
    for i in range(group_size):
        net_id = i + 1
        net_name = f"DQ{i}"  # Names don't matter for the helper; arbitrary.
        net_ids.append(net_id)
        net_names.append(net_name)
        y = base_y + i * pitch

        # Pad on U1 (left component, right edge)
        router.add_component(
            "U1",
            [
                {
                    "number": str(25 + i),
                    "x": 40.0,
                    "y": y,
                    "net": net_id,
                    "net_name": net_name,
                }
            ],
        )
        # Mirrored pad on U2 (right component, left edge)
        router.add_component(
            "U2",
            [
                {
                    "number": str(1 + i),
                    "x": 80.0,
                    "y": y,
                    "net": net_id,
                    "net_name": net_name,
                }
            ],
        )
        net_class_map[net_name] = cls

    router.net_class_map = net_class_map
    return router, net_ids, net_names


# =============================================================================
# Tests
# =============================================================================


class TestIdentityOnTinyInputs:
    """Inputs that can't form a byte-lane return unchanged."""

    def test_empty_input(self) -> None:
        router = Autorouter(width=50.0, height=50.0)
        assert router._apply_byte_lane_inner_priority([]) == []

    def test_three_net_input_returns_identity(self) -> None:
        """Net-order length 3 < 4 -> identity, no detection attempted."""
        router, net_ids, _ = _make_byte_lane_router(group_size=3)
        # group_size 3 also < MIN_BYTE_LANE_SIZE=5 so even if we made it
        # through the length gate, the per-group threshold rejects it.
        out = router._apply_byte_lane_inner_priority(net_ids)
        assert out == net_ids


class TestIdentityOnNoGroups:
    """Boards without match groups receive identity output."""

    def test_no_match_groups_declared(self) -> None:
        """Plain nets with no length_match_group declaration -> identity."""
        router = Autorouter(width=80.0, height=80.0)
        net_ids = [1, 2, 3, 4, 5, 6]
        for nid in net_ids:
            nm = f"NET{nid}"
            router.add_component(
                f"R{nid}_A",
                [{"number": "1", "x": float(nid), "y": 5.0, "net": nid, "net_name": nm}],
            )
            router.add_component(
                f"R{nid}_B",
                [{"number": "1", "x": float(nid) + 1.0, "y": 5.0, "net": nid, "net_name": nm}],
            )
        # No suffix-inference pattern matches (NET1, NET2, ...) since
        # detect_match_groups requires >= 3 members with a numeric suffix
        # AND a recognisable bus prefix; "NET" is too generic.
        out = router._apply_byte_lane_inner_priority(net_ids)
        # Identity expected (no groups detected) OR a no-op permutation:
        # the helper short-circuits on empty ``net_to_group`` returning
        # the input list reference unchanged.
        assert out == net_ids


class TestIdentityOnSmallGroups:
    """Groups smaller than ``MIN_BYTE_LANE_SIZE`` return identity."""

    def test_four_member_group_no_promotion(self) -> None:
        """A 4-net group is below the byte-lane threshold."""
        router, net_ids, _ = _make_byte_lane_router(group_size=4)
        out = router._apply_byte_lane_inner_priority(net_ids)
        # Below MIN_BYTE_LANE_SIZE=5 -> no promotion -> identity.
        assert out == net_ids


class TestScaffoldingIdentityOnByteLane:
    """Mirrored byte-lane groups return identity, with corridor reservation.

    Issue #2983 updates the contract: the helper still returns the
    input net order unchanged (PR #2969 R1/R2/R3 proved net-ordering
    changes alone are insufficient and, in R1, regressed DRC), but
    the **corridor reservation** side-effect now runs for each
    detected mirrored byte-lane group.  The reservation lands ONLY
    when a routable inner layer is available in the stack-up; the
    fixture here does NOT pass a ``layer_stack`` so the internal
    2-layer fallback is in effect and the reservation is correctly
    skipped (the 2-layer guard prevents starving partner-net
    escapes — see ``test_byte_lane_corridor_reservation.py`` for
    the multi-layer reservation contract).

    The detection / projection / sort machinery runs eagerly so
    callers observe consistent intermediate state.  The integration
    hooks (``route_all``, ``route_all_negotiated``, ``TwoPhaseRouter``)
    consume the unchanged order and the escape pre-pass + main
    routing loop honour the per-cell reservation via
    ``RoutingGrid._mark_via``.
    """

    def test_nine_net_byte_lane_identity(self) -> None:
        """9-net byte-lane (DDR-byte minus DQS pair): detection runs,
        no reorder applied — output equals input.

        Issue #2983 contract: ordering is identity, AND on a
        multi-layer stack-up the corridor reservation runs as a
        side effect.  This fixture uses the default 2-layer
        Autorouter (no ``layer_stack``), so the reservation is
        correctly skipped here by the 2-layer guard.  See
        ``test_byte_lane_corridor_reservation.py`` for the
        reservation count assertions on 4-layer stack-ups.
        """
        router, net_ids, _ = _make_byte_lane_router(group_size=9)

        out = router._apply_byte_lane_inner_priority(net_ids)

        # Identity contract: output is the input list, unchanged.
        assert out == net_ids
        # Membership + length invariants hold trivially under identity.
        assert len(out) == len(net_ids)
        assert set(out) == set(net_ids)
        # 2-layer fallback => no reservation on this fixture.
        assert router._escape.byte_lane_corridor_reservations == 0

    def test_ten_net_byte_lane_identity(self) -> None:
        """A 10-net byte-lane (full DDR-byte): detection runs, but
        no reorder is applied — output equals input.

        See the module docstring for the R1/R2/R3 design-history
        trace explaining why net-ordering alone is insufficient,
        and ``test_byte_lane_corridor_reservation.py`` for the
        Issue #2983 reservation-count assertions on multi-layer
        stack-ups (where the corridor is actually carved out).
        """
        router, net_ids, _ = _make_byte_lane_router(group_size=10)
        out = router._apply_byte_lane_inner_priority(net_ids)

        # Identity contract.
        assert out == net_ids
        assert len(out) == len(net_ids)
        assert set(out) == set(net_ids)
        # 2-layer fallback => no reservation on this fixture.
        assert router._escape.byte_lane_corridor_reservations == 0


class TestMultiGroupPreservation:
    """A non-byte-lane group elsewhere in the routing pass is unaffected."""

    def test_non_group_nets_keep_position(self) -> None:
        """Add 3 ungrouped nets and confirm they keep their slots."""
        router, byte_lane_ids, _ = _make_byte_lane_router(group_size=9)

        # Add 3 standalone nets after the byte-lane group.  These have
        # NO match-group declaration, so the helper must leave them in
        # place relative to each other.
        extra_ids: list[int] = []
        for i in range(3):
            net_id = 100 + i
            nm = f"STANDALONE{i}"
            router.add_component(
                f"RX{i}_A",
                [{"number": "1", "x": 5.0, "y": 60.0 + i, "net": net_id, "net_name": nm}],
            )
            router.add_component(
                f"RX{i}_B",
                [{"number": "1", "x": 25.0, "y": 60.0 + i, "net": net_id, "net_name": nm}],
            )
            extra_ids.append(net_id)

        # ``net_class_map`` is unchanged from the byte-lane build, so
        # the standalone nets are ungrouped (default net class).
        net_order = byte_lane_ids + extra_ids
        out = router._apply_byte_lane_inner_priority(net_order)

        # Under the scaffolding cut the full input order is preserved,
        # which trivially keeps the standalone nets in place.
        assert out == net_order
        # Membership preserved.
        assert set(out) == set(net_order)
        assert len(out) == len(net_order)


class TestPermutationInvariant:
    """Output is always a valid permutation of the input."""

    def test_all_inputs_preserved(self) -> None:
        router, net_ids, _ = _make_byte_lane_router(group_size=9)
        out = router._apply_byte_lane_inner_priority(net_ids)
        assert sorted(out) == sorted(net_ids), (
            "Helper must return a permutation of the input (no drops/dupes)"
        )

    def test_horizontal_row_orientation(self) -> None:
        """A horizontal row (pads share y, vary x) exercises the
        axis-with-greater-variance detection branch but, in the
        scaffolding cut, still returns identity.

        Round-4 contract per PR #2969: even when the detection logic
        successfully classifies the row, no reorder is applied.  The
        axis-selection branch is exercised here for coverage so a
        future layered-escape implementation has a regression
        fingerprint to compare against.
        """
        cls = NetClassRouting(
            name="HORIZ_BUS",
            priority=1,
            trace_width=0.15,
            clearance=0.10,
            length_critical=True,
            length_match_group="HORIZ_BUS",
        )
        net_class_map: dict[str, NetClassRouting] = {}
        router = Autorouter(width=120.0, height=80.0, net_class_map=net_class_map)

        net_ids: list[int] = []
        for i in range(7):
            net_id = i + 1
            nm = f"HBUS{i}"
            # Horizontal row: y fixed, x varies.
            router.add_component(
                "UH",
                [{"number": str(i + 1), "x": 10.0 + i * 0.8, "y": 30.0,
                  "net": net_id, "net_name": nm}],
            )
            router.add_component(
                "UI",
                [{"number": str(i + 1), "x": 10.0 + i * 0.8, "y": 60.0,
                  "net": net_id, "net_name": nm}],
            )
            net_class_map[nm] = cls
            net_ids.append(net_id)
        router.net_class_map = net_class_map

        out = router._apply_byte_lane_inner_priority(net_ids)

        # Identity contract: horizontal row detection runs but no
        # reorder is applied.
        assert out == net_ids


class TestNonMirroredTopologyGracefulFallback:
    """A group whose members aren't co-located on one component shouldn't crash."""

    def test_distributed_pads_no_primary_component(self) -> None:
        """Each net's pads on DIFFERENT components (no primary picks up
        ``MIN_BYTE_LANE_SIZE`` members) -> identity, no crash."""
        cls = NetClassRouting(
            name="SCATTERED_BUS",
            priority=1,
            trace_width=0.15,
            clearance=0.10,
            length_critical=True,
            length_match_group="SCATTERED_BUS",
        )
        net_class_map: dict[str, NetClassRouting] = {}
        router = Autorouter(width=120.0, height=80.0, net_class_map=net_class_map)

        net_ids: list[int] = []
        for i in range(6):
            net_id = i + 1
            nm = f"SBUS{i}"
            # Each net has pads on a UNIQUE pair of components -- no
            # single component hosts >= MIN_BYTE_LANE_SIZE pads.
            router.add_component(
                f"COMP_A{i}",
                [{"number": "1", "x": 5.0, "y": 20.0 + i, "net": net_id, "net_name": nm}],
            )
            router.add_component(
                f"COMP_B{i}",
                [{"number": "1", "x": 80.0, "y": 20.0 + i, "net": net_id, "net_name": nm}],
            )
            net_class_map[nm] = cls
            net_ids.append(net_id)
        router.net_class_map = net_class_map

        out = router._apply_byte_lane_inner_priority(net_ids)
        # No primary component has 5+ group-member pads -> no promotion
        # plan -> identity.
        assert out == net_ids
