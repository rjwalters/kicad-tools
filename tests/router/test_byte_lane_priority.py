"""Tests for inner-corner byte-lane priority bumping (Issue #2962).

The :meth:`Autorouter._apply_byte_lane_inner_priority` helper detects
mirrored byte-lane match groups (e.g. board 07's DDR data byte on a
mirrored QFN-48 pair) and demotes the second-inward row neighbours of
the inner-corner so the inner-corner net (the pad one step in from
each row corner) routes BEFORE the second-inward neighbour can claim
its lateral lane.

Judge feedback (PR #2969 review): an earlier broader plan also
demoted corner nets (0 / n-1), but that pushed DRC errors over the
allowlist on board 07's match-group regression gate.  The corner net
keeps its default rank now -- the heuristic only constrains positions
``(2, n-3)`` so the rest of the priority-sort ordering is untouched.

Root cause that motivates this helper (per the issue):

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

The helper's contract:

1.  **Identity on tiny inputs** -- ``net_order`` shorter than 4 is
    returned unchanged.
2.  **Identity on boards without match groups** -- no group
    declarations + no suffix-inference matches => identity.
3.  **Identity on small groups** -- groups with fewer than 5 members
    don't exhibit the mirrored byte-lane topology; the helper degrades
    to identity for them.
4.  **Inner-corner demotion** -- on a mirrored byte-lane group, the
    second-inward neighbour of each inner-corner is demoted behind
    its inner-corner sibling.  Corner nets and non-neighbour middle
    members keep their original priority position.
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
        # Below MIN_BYTE_LANE_SIZE=5 -> no demotion -> identity.
        assert out == net_ids


class TestInnerCornerDemotion:
    """Mirrored byte-lane promotes inner-corner above immediate neighbours."""

    def test_nine_net_byte_lane_demotes_second_inward(self) -> None:
        """9-net byte-lane (DDR-byte minus DQS pair): positions 2/6
        (second-inward) get demoted below positions 1/7 (inner-corner).

        Per Judge feedback on PR #2969, the corner pads at positions
        0 and n-1 keep their default rank -- only the second-inward
        neighbours are demoted.
        """
        router, net_ids, _ = _make_byte_lane_router(group_size=9)

        # Input order = creation order = sorted by y (pad position).
        # Position 0 = corner top, 1 = inner-corner top, 2 = second-
        # inward top, ..., 7 = inner-corner bottom, 8 = corner bottom.
        out = router._apply_byte_lane_inner_priority(net_ids)

        # Membership + length preserved.
        assert len(out) == len(net_ids)
        assert set(out) == set(net_ids)

        # The demoted neighbours are at sorted indices {2, 6} only --
        # the second-inward pads.  Corner pads (0, 8) keep their
        # default priority-sort rank.
        idx = {nid: i for i, nid in enumerate(out)}

        inner_top = net_ids[1]
        corner_top = net_ids[0]
        second_inward_top = net_ids[2]
        inner_bottom = net_ids[7]
        corner_bottom = net_ids[8]
        second_inward_bottom = net_ids[6]

        assert idx[inner_top] < idx[second_inward_top], (
            f"Inner-corner top ({inner_top}) must precede second-inward "
            f"top ({second_inward_top}): {idx[inner_top]} vs "
            f"{idx[second_inward_top]}"
        )
        assert idx[inner_bottom] < idx[second_inward_bottom], (
            f"Inner-corner bottom ({inner_bottom}) must precede "
            f"second-inward bottom ({second_inward_bottom}): "
            f"{idx[inner_bottom]} vs {idx[second_inward_bottom]}"
        )

        # Corner pads keep their default rank (rank-1) -- they are
        # tied with inner-corner under the demotion sort and preserve
        # original input order via the stable secondary key.  Concretely,
        # net_ids[0] was input position 0 and net_ids[1] was input
        # position 1, so corner_top (input pos 0) must precede
        # inner_top (input pos 1) in the output -- this is the
        # opposite of the previous (broader) implementation which
        # explicitly demoted corner_top below inner_top.
        assert idx[corner_top] < idx[inner_top], (
            f"Corner top ({corner_top}) keeps default rank and must "
            f"precede inner_top ({inner_top}) under stable secondary "
            f"key on input order: {idx[corner_top]} vs {idx[inner_top]}"
        )
        assert idx[inner_bottom] < idx[corner_bottom], (
            f"Inner-corner bottom ({inner_bottom}) at input position 7 "
            f"must precede corner_bottom ({corner_bottom}) at input "
            f"position 8 under stable secondary key: "
            f"{idx[inner_bottom]} vs {idx[corner_bottom]}"
        )

    def test_ten_net_byte_lane_preserves_middle_position(self) -> None:
        """A 10-net byte-lane (full DDR-byte) leaves middle nets in
        their priority-sort positions, only demoting the two
        second-inward neighbours of inner-corner pads.

        Per Judge feedback on PR #2969, only positions (2, n-3) are
        demoted -- corners (0, n-1) keep their default rank.
        """
        # Mimics the full DDR byte: 10 nets in a row.  Inner-corner
        # indices = (1, 8); demoted indices = (2, 7); middle indices
        # = (3, 4, 5, 6) which keep their rank.  Corners (0, 9) also
        # keep their rank now.
        router, net_ids, _ = _make_byte_lane_router(group_size=10)
        out = router._apply_byte_lane_inner_priority(net_ids)

        idx = {nid: i for i, nid in enumerate(out)}

        # Middle members (sorted indices 3..6) keep their relative
        # order: they all have the same default rank, so the stable
        # sort preserves input ordering between them.
        middle_ids = [net_ids[3], net_ids[4], net_ids[5], net_ids[6]]
        middle_positions = [idx[nid] for nid in middle_ids]
        assert middle_positions == sorted(middle_positions), (
            "Middle byte-lane members must keep their priority-sort "
            f"order; got positions {middle_positions} for ids {middle_ids}"
        )

        # The demoted neighbours (sorted indices 2 and 7 only) must
        # appear AFTER the middle and corner members in the output.
        demoted_ids = [net_ids[2], net_ids[7]]
        for mid in middle_ids:
            for did in demoted_ids:
                assert idx[mid] < idx[did], (
                    f"Middle net {mid} (pos {idx[mid]}) must precede "
                    f"demoted neighbour {did} (pos {idx[did]})"
                )

        # Corners (sorted indices 0 and 9) keep their default rank
        # and must precede the demoted second-inward neighbours.
        corner_ids = [net_ids[0], net_ids[9]]
        for cid in corner_ids:
            for did in demoted_ids:
                assert idx[cid] < idx[did], (
                    f"Corner net {cid} (pos {idx[cid]}) keeps default "
                    f"rank and must precede demoted second-inward "
                    f"neighbour {did} (pos {idx[did]})"
                )


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

        # Membership preserved.
        assert set(out) == set(net_order)
        assert len(out) == len(net_order)

        # Standalone nets keep their relative order (stable sort).
        idx = {nid: i for i, nid in enumerate(out)}
        extra_positions = [idx[nid] for nid in extra_ids]
        assert extra_positions == sorted(extra_positions), (
            "Standalone (non-group) nets must keep their priority-sort "
            f"order; got {extra_positions} for ids {extra_ids}"
        )


class TestPermutationInvariant:
    """Output is always a valid permutation of the input."""

    def test_all_inputs_preserved(self) -> None:
        router, net_ids, _ = _make_byte_lane_router(group_size=9)
        out = router._apply_byte_lane_inner_priority(net_ids)
        assert sorted(out) == sorted(net_ids), (
            "Helper must return a permutation of the input (no drops/dupes)"
        )

    def test_horizontal_row_orientation(self) -> None:
        """A horizontal row (pads share y, vary x) is detected by the
        axis-with-greater-variance rule and reordered along x."""
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
        idx = {nid: i for i, nid in enumerate(out)}

        # Sorted indices along x: 0=corner left, 1=inner-corner left,
        # 2=second-inward left, ..., 4=second-inward right,
        # 5=inner-corner right, 6=corner right.  Per Judge feedback
        # on PR #2969, only the second-inward neighbours (sorted
        # indices 2 and n-3=4) are demoted.  The inner-corner nets
        # (net_ids[1], net_ids[5]) must precede those second-inward
        # neighbours.  Corner nets (net_ids[0], net_ids[6]) keep
        # their default rank.
        assert idx[net_ids[1]] < idx[net_ids[2]]
        assert idx[net_ids[5]] < idx[net_ids[4]]


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
        # No primary component has 5+ group-member pads -> no demotion
        # plan -> identity.
        assert out == net_ids
