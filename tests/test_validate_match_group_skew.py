"""Tests for the validate-side match-group skew producer wiring (Issue #2710).

Sister of ``tests/test_validate_diffpair_skew.py`` (PR #2685) -- the
third producer-side wiring follow-up in Epic #2661, mirroring the
diff-pair counterpart byte-for-byte modulo type renames (group-name
keying instead of net-name-tuple keying).

Covers:

- ``derive_group_skew_data(pcb, None)`` -> empty result (standalone-CLI
  graceful no-op).
- Empty / no-nets PCB -> empty result.
- Basic per-group skew measurement (symmetric, asymmetric, multi-group).
- Partial-routing gating: groups with any unrouted member are omitted
  (mirrors :meth:`MatchGroupTracker.get_all_skews` semantics).
- Per-class tolerance override via ``length_match_tolerance_mm``.
- **Drift-prevention**: ``derive_group_skew_data`` on a PCB matches
  :meth:`MatchGroupTracker.get_all_skews` on the router-internal
  :class:`Route` form of the same physical routing byte-for-byte.
- Via-traversing routes: PCB-side measurement matches router-side when
  ``board_thickness_mm`` is supplied (no router context-only state).
- Drift-prevention: ``DEFAULT_MATCH_GROUP_TOLERANCE_MM`` matches
  ``NetClassRouting.effective_length_match_tolerance`` default.
- End-to-end via :class:`DRCChecker.check_match_group_length_skew`:
  ``rules_checked_by_rule['match_group_length_skew'] >= 1`` on a routed
  board with a declared group.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.router.primitives import Route

# ---------------------------------------------------------------------------
# Stubs (mirror tests/test_validate_diffpair_skew.py)
# ---------------------------------------------------------------------------


@dataclass
class _StubNet:
    number: int
    name: str


@dataclass
class _StubSegment:
    """PCB-schema-shape segment stub.

    Matches :class:`kicad_tools.schema.pcb.Segment`:
    ``start: tuple[float, float]`` / ``end: tuple[float, float]`` (NOT
    the router-internal ``x1/y1/x2/y2`` fields).
    """

    start: tuple[float, float]
    end: tuple[float, float]
    width: float = 0.2
    layer: str = "F.Cu"
    net_number: int = 0
    net_name: str = ""
    uuid: str = ""


@dataclass
class _StubVia:
    """PCB-schema-shape via stub.

    Matches :class:`kicad_tools.schema.pcb.Via`: ``layers: list[str]`` of
    KiCad layer name strings (e.g., ``["F.Cu", "B.Cu"]``).
    """

    position: tuple[float, float] = (0.0, 0.0)
    size: float = 0.6
    drill: float = 0.3
    layers: list[str] = field(default_factory=lambda: ["F.Cu", "B.Cu"])
    net_number: int = 0
    net_name: str = ""
    uuid: str = ""


@dataclass
class _StubPCB:
    """Minimal PCB stub used by :func:`derive_group_skew_data`.

    Implements ``nets``, ``segments_in_net``, ``vias_in_net`` -- the
    only attributes/methods consulted by the helper.
    """

    _nets: dict[int, _StubNet] = field(default_factory=dict)
    _segments: list[_StubSegment] = field(default_factory=list)
    _vias: list[_StubVia] = field(default_factory=list)

    @property
    def nets(self) -> dict[int, _StubNet]:
        return self._nets

    def segments_in_net(self, net_number: int):
        for seg in self._segments:
            if seg.net_number == net_number:
                yield seg

    def vias_in_net(self, net_number: int):
        for via in self._vias:
            if via.net_number == net_number:
                yield via


def _make_ddr_pcb(
    *,
    net_lengths_mm: dict[int, float | None],
    net_names_map: dict[int, str] | None = None,
) -> _StubPCB:
    """Construct a stub PCB with one horizontal segment per net.

    ``net_lengths_mm`` maps net_number -> length (None to omit geometry).
    Each segment is placed on F.Cu at y = (net_id * 1.0), starting at x=0.
    """
    if net_names_map is None:
        # Default DDR-style names DQ0..DQN.
        net_names_map = {nid: f"DQ{i}" for i, nid in enumerate(net_lengths_mm.keys())}

    nets: dict[int, _StubNet] = {0: _StubNet(0, "")}
    for nid, name in net_names_map.items():
        nets[nid] = _StubNet(nid, name)

    segs: list[_StubSegment] = []
    for nid, length in net_lengths_mm.items():
        if length is None:
            continue
        segs.append(
            _StubSegment(
                start=(0.0, float(nid)),
                end=(length, float(nid)),
                net_number=nid,
                net_name=net_names_map[nid],
            )
        )
    return _StubPCB(_nets=nets, _segments=segs)


# ---------------------------------------------------------------------------
# Graceful no-op tests
# ---------------------------------------------------------------------------


class TestDeriveGroupSkewDataNoOp:
    """Standalone-CLI graceful no-op paths."""

    def test_no_net_class_map_returns_empty(self):
        """``derive_group_skew_data(pcb, None)`` -> empty 3-tuple."""
        from kicad_tools.validate.match_group_skew import derive_group_skew_data

        pcb = _make_ddr_pcb(net_lengths_mm={10: 10.0, 11: 10.0, 12: 10.0, 13: 10.0})
        skew_data, groups, threshold_map = derive_group_skew_data(pcb, None)
        assert skew_data == {}
        assert groups == []
        assert threshold_map == {}

    def test_empty_net_class_map_returns_empty(self):
        """``derive_group_skew_data(pcb, {})`` -> empty result (idempotent with None)."""
        from kicad_tools.validate.match_group_skew import derive_group_skew_data

        pcb = _make_ddr_pcb(net_lengths_mm={10: 10.0, 11: 10.0, 12: 10.0, 13: 10.0})
        skew_data, groups, threshold_map = derive_group_skew_data(pcb, {})
        assert skew_data == {}
        assert groups == []
        assert threshold_map == {}

    def test_pcb_with_no_nets_returns_empty(self):
        """Edge case: a PCB whose net table has only the empty net 0."""
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.match_group_skew import derive_group_skew_data

        nc = NetClassRouting(name="DDR", length_match_group="DDR_DATA")
        net_class_map = {"DQ0": nc}

        pcb = _StubPCB(_nets={0: _StubNet(0, "")})
        skew_data, groups, threshold_map = derive_group_skew_data(pcb, net_class_map)
        assert skew_data == {}
        assert groups == []
        assert threshold_map == {}

    def test_no_declared_groups_returns_empty(self):
        """net_class_map with nets but no ``length_match_group`` -> empty result.

        Without an explicit group declaration AND with suffix inference
        OFF (the producer's default), the detector finds no groups.
        """
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.match_group_skew import derive_group_skew_data

        # Net class declared but NO length_match_group set.
        nc = NetClassRouting(name="GENERAL")
        net_class_map = {"DQ0": nc, "DQ1": nc, "DQ2": nc, "DQ3": nc}

        pcb = _make_ddr_pcb(net_lengths_mm={10: 10.0, 11: 10.0, 12: 10.0, 13: 10.0})
        skew_data, groups, threshold_map = derive_group_skew_data(pcb, net_class_map)
        assert skew_data == {}
        assert groups == []
        assert threshold_map == {}


# ---------------------------------------------------------------------------
# Basic per-group skew measurement
# ---------------------------------------------------------------------------


class TestDeriveGroupSkewDataBasic:
    """Basic measurement + per-group tolerance assignment."""

    def test_symmetric_group_zero_skew(self):
        """4-trace DDR group, all equal length -> skew_mm == 0.0."""
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.match_group_skew import derive_group_skew_data

        nc = NetClassRouting(name="DDR", length_match_group="DDR_DATA")
        net_class_map = {"DQ0": nc, "DQ1": nc, "DQ2": nc, "DQ3": nc}

        pcb = _make_ddr_pcb(net_lengths_mm={10: 10.0, 11: 10.0, 12: 10.0, 13: 10.0})
        skew_data, groups, threshold_map = derive_group_skew_data(pcb, net_class_map)

        assert "DDR_DATA" in skew_data
        assert skew_data["DDR_DATA"] == 0.0
        # Detector returned a single 4-member group.
        assert len(groups) == 1
        assert groups[0].name == "DDR_DATA"
        assert sorted(groups[0].net_ids) == [10, 11, 12, 13]
        # Threshold from class default (0.5).
        assert threshold_map["DDR_DATA"] == 0.5

    def test_asymmetric_group_returns_max_minus_min(self):
        """4-trace group: lengths [10, 10.5, 11, 12] -> skew = 2.0."""
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.match_group_skew import derive_group_skew_data

        nc = NetClassRouting(name="DDR", length_match_group="DDR_DATA")
        net_class_map = {"DQ0": nc, "DQ1": nc, "DQ2": nc, "DQ3": nc}

        pcb = _make_ddr_pcb(net_lengths_mm={10: 10.0, 11: 10.5, 12: 11.0, 13: 12.0})
        skew_data, _, _ = derive_group_skew_data(pcb, net_class_map)

        assert "DDR_DATA" in skew_data
        assert abs(skew_data["DDR_DATA"] - 2.0) < 1e-9

    def test_per_class_tolerance_override_propagates(self):
        """``length_match_tolerance_mm`` on the net class reaches threshold_map."""
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.match_group_skew import derive_group_skew_data

        nc = NetClassRouting(
            name="MIPI_CSI",
            length_match_group="MIPI_CSI",
            length_match_tolerance_mm=1.0,  # MIPI D-PHY budget
        )
        net_class_map = {"CSI0": nc, "CSI1": nc, "CSI2": nc, "CSI3": nc}

        pcb = _make_ddr_pcb(
            net_lengths_mm={10: 10.0, 11: 10.0, 12: 10.0, 13: 10.0},
            net_names_map={10: "CSI0", 11: "CSI1", 12: "CSI2", 13: "CSI3"},
        )
        _, _, threshold_map = derive_group_skew_data(pcb, net_class_map)

        assert threshold_map["MIPI_CSI"] == 1.0

    def test_multiple_groups_independent(self):
        """Two groups, distinct net classes, independent skew + tolerance."""
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.match_group_skew import derive_group_skew_data

        nc_ddr = NetClassRouting(
            name="DDR_BYTE0",
            length_match_group="DDR_BYTE0",
            length_match_tolerance_mm=0.3,
        )
        nc_mipi = NetClassRouting(
            name="MIPI",
            length_match_group="MIPI",
            length_match_tolerance_mm=1.5,
        )
        net_class_map = {
            "DQ0": nc_ddr,
            "DQ1": nc_ddr,
            "DQ2": nc_ddr,
            "DQ3": nc_ddr,
            "CSI0": nc_mipi,
            "CSI1": nc_mipi,
            "CSI2": nc_mipi,
            "CSI3": nc_mipi,
        }

        pcb = _make_ddr_pcb(
            net_lengths_mm={
                10: 10.0,
                11: 10.0,
                12: 10.0,
                13: 10.5,  # skew = 0.5
                20: 20.0,
                21: 21.0,  # skew = 2.0
                22: 22.0,
                23: 22.0,
            },
            net_names_map={
                10: "DQ0",
                11: "DQ1",
                12: "DQ2",
                13: "DQ3",
                20: "CSI0",
                21: "CSI1",
                22: "CSI2",
                23: "CSI3",
            },
        )
        skew_data, groups, threshold_map = derive_group_skew_data(pcb, net_class_map)

        assert "DDR_BYTE0" in skew_data
        assert abs(skew_data["DDR_BYTE0"] - 0.5) < 1e-9
        assert threshold_map["DDR_BYTE0"] == 0.3

        assert "MIPI" in skew_data
        assert abs(skew_data["MIPI"] - 2.0) < 1e-9
        assert threshold_map["MIPI"] == 1.5

        # Detector returned both groups.
        assert len(groups) == 2
        names = {g.name for g in groups}
        assert names == {"DDR_BYTE0", "MIPI"}

    def test_skew_dict_is_alphabetically_sorted(self):
        """Skew dict iteration matches MatchGroupTracker.get_all_skews ordering."""
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.match_group_skew import derive_group_skew_data

        nc_a = NetClassRouting(name="A", length_match_group="ZZZ_LATE")
        nc_b = NetClassRouting(name="B", length_match_group="AAA_EARLY")
        net_class_map = {
            "NET_Z0": nc_a,
            "NET_Z1": nc_a,
            "NET_Z2": nc_a,
            "NET_A0": nc_b,
            "NET_A1": nc_b,
            "NET_A2": nc_b,
        }

        pcb = _make_ddr_pcb(
            net_lengths_mm={
                10: 10.0,
                11: 10.0,
                12: 10.0,
                20: 5.0,
                21: 5.0,
                22: 5.0,
            },
            net_names_map={
                10: "NET_Z0",
                11: "NET_Z1",
                12: "NET_Z2",
                20: "NET_A0",
                21: "NET_A1",
                22: "NET_A2",
            },
        )
        skew_data, _, _ = derive_group_skew_data(pcb, net_class_map)
        # Order: AAA_EARLY before ZZZ_LATE.
        assert list(skew_data.keys()) == ["AAA_EARLY", "ZZZ_LATE"]


# ---------------------------------------------------------------------------
# Partial-routing gating
# ---------------------------------------------------------------------------


class TestDeriveGroupSkewDataPartialRouting:
    """Groups with any unrouted member are omitted (graceful degradation)."""

    def test_unrouted_member_omits_group(self):
        """One member unrouted -> group omitted from skew_data."""
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.match_group_skew import derive_group_skew_data

        nc = NetClassRouting(name="DDR", length_match_group="DDR_DATA")
        net_class_map = {"DQ0": nc, "DQ1": nc, "DQ2": nc, "DQ3": nc}

        # DQ2 unrouted.
        pcb = _make_ddr_pcb(net_lengths_mm={10: 10.0, 11: 10.0, 12: None, 13: 10.0})
        skew_data, _, threshold_map = derive_group_skew_data(pcb, net_class_map)

        # Group omitted because partial routing.
        assert skew_data == {}
        assert threshold_map == {}

    def test_all_members_unrouted_omits_group(self):
        """Entirely unrouted group -> omitted."""
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.match_group_skew import derive_group_skew_data

        nc = NetClassRouting(name="DDR", length_match_group="DDR_DATA")
        net_class_map = {"DQ0": nc, "DQ1": nc, "DQ2": nc, "DQ3": nc}

        pcb = _make_ddr_pcb(net_lengths_mm={10: None, 11: None, 12: None, 13: None})
        skew_data, _, threshold_map = derive_group_skew_data(pcb, net_class_map)

        assert skew_data == {}
        assert threshold_map == {}


# ---------------------------------------------------------------------------
# Pair-only groups (Issue #3916)
# ---------------------------------------------------------------------------


class TestDeriveGroupSkewDataPairOnly:
    """Groups composed exclusively of diff pairs must be length-checked.

    Regression coverage for Issue #3916: MIPI_CSI_LANES / HDMI_TMDS_LANES
    on board 07 exit ``_extract_pair_ids`` with ``net_ids=[]`` and a
    fully-populated ``pair_ids``.  The old ``if not grp.net_ids: continue``
    guard silently dropped those groups so the skew rule never fired.  The
    producer now measures diff-pair members via the pair-average
    ``(L_P + L_N) / 2`` contribution.
    """

    def test_pair_only_group_produces_skew(self):
        """AC6: a group of 2+ diff pairs yields a non-empty skew_data dict.

        Three MIPI lanes (P/N per lane), all legs routed.  With net_ids=[]
        and pair_ids populated, the group must still be measured and appear
        in group_skew_data with a correct between-lane skew value.
        """
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.match_group_skew import derive_group_skew_data

        nc = NetClassRouting(
            name="MIPI",
            length_match_group="MIPI_LANES",
            length_match_tolerance_mm=0.05,
        )
        # 3 lanes: DAT0 (P/N), DAT1 (P/N), CLK (P/N).  _P/_N suffixes make
        # the detector pair them, so the group ends up pair-only.
        net_class_map = {
            "MIPI_DAT0_P": nc,
            "MIPI_DAT0_N": nc,
            "MIPI_DAT1_P": nc,
            "MIPI_DAT1_N": nc,
            "MIPI_CLK_P": nc,
            "MIPI_CLK_N": nc,
        }
        # Lane averages: lane0 = (10.0+10.0)/2 = 10.0, lane1 = 11.0,
        # lane2 = 12.0  ->  between-lane skew = 12.0 - 10.0 = 2.0
        pcb = _make_ddr_pcb(
            net_lengths_mm={
                40: 10.0,  # DAT0_P
                41: 10.0,  # DAT0_N
                42: 11.0,  # DAT1_P
                43: 11.0,  # DAT1_N
                44: 12.0,  # CLK_P
                45: 12.0,  # CLK_N
            },
            net_names_map={
                40: "MIPI_DAT0_P",
                41: "MIPI_DAT0_N",
                42: "MIPI_DAT1_P",
                43: "MIPI_DAT1_N",
                44: "MIPI_CLK_P",
                45: "MIPI_CLK_N",
            },
        )
        skew_data, groups, threshold_map = derive_group_skew_data(pcb, net_class_map)

        # The group is pair-only: net_ids empty, three pairs.
        assert len(groups) == 1
        assert groups[0].name == "MIPI_LANES"
        assert groups[0].net_ids == []
        assert len(groups[0].pair_ids) == 3

        # AC6: pair-only group is measured, not skipped.
        assert "MIPI_LANES" in skew_data
        assert abs(skew_data["MIPI_LANES"] - 2.0) < 1e-9
        # Threshold falls back to the P-leg-of-first-pair net class.
        assert threshold_map["MIPI_LANES"] == 0.05

    def test_pair_average_not_per_leg_semantics(self):
        """AC7: each pair contributes ONE averaged entry, not two per-leg entries.

        Lane 0: L_P=10.0, L_N=10.1 -> average 10.05.
        Lane 1: L_P=10.3, L_N=10.3 -> average 10.30.
        Pair-average skew = 10.30 - 10.05 = 0.25mm.
        Per-leg (wrong) skew would be max(10.3) - min(10.0) = 0.30mm.
        """
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.match_group_skew import derive_group_skew_data

        nc = NetClassRouting(
            name="HDMI",
            length_match_group="TMDS_LANES",
            length_match_tolerance_mm=0.075,
        )
        net_class_map = {
            "TMDS_D0_P": nc,
            "TMDS_D0_N": nc,
            "TMDS_D1_P": nc,
            "TMDS_D1_N": nc,
        }
        pcb = _make_ddr_pcb(
            net_lengths_mm={
                50: 10.0,  # D0_P
                51: 10.1,  # D0_N  -> lane0 avg 10.05
                52: 10.3,  # D1_P
                53: 10.3,  # D1_N  -> lane1 avg 10.30
            },
            net_names_map={
                50: "TMDS_D0_P",
                51: "TMDS_D0_N",
                52: "TMDS_D1_P",
                53: "TMDS_D1_N",
            },
        )
        skew_data, groups, _ = derive_group_skew_data(pcb, net_class_map)

        assert groups[0].net_ids == []
        assert len(groups[0].pair_ids) == 2
        assert "TMDS_LANES" in skew_data
        # Pair-average, NOT per-leg.
        assert abs(skew_data["TMDS_LANES"] - 0.25) < 1e-9
        # Guard the anti-pattern explicitly: must NOT be the per-leg 0.30.
        assert abs(skew_data["TMDS_LANES"] - 0.30) > 1e-3

    def test_pair_with_one_unrouted_leg_omits_group(self):
        """AC8: a pair with one unrouted leg omits the whole group.

        Matches the single-ended unrouted-member gating: if either leg of
        any pair has zero geometry, the group is excluded from skew_data.
        """
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.match_group_skew import derive_group_skew_data

        nc = NetClassRouting(
            name="MIPI",
            length_match_group="MIPI_LANES",
            length_match_tolerance_mm=0.05,
        )
        net_class_map = {
            "MIPI_DAT0_P": nc,
            "MIPI_DAT0_N": nc,
            "MIPI_DAT1_P": nc,
            "MIPI_DAT1_N": nc,
        }
        # DAT1_N (net 43) unrouted -> its pair, hence the group, is dropped.
        pcb = _make_ddr_pcb(
            net_lengths_mm={
                40: 10.0,  # DAT0_P
                41: 10.0,  # DAT0_N
                42: 11.0,  # DAT1_P
                43: None,  # DAT1_N unrouted
            },
            net_names_map={
                40: "MIPI_DAT0_P",
                41: "MIPI_DAT0_N",
                42: "MIPI_DAT1_P",
                43: "MIPI_DAT1_N",
            },
        )
        skew_data, _, threshold_map = derive_group_skew_data(pcb, net_class_map)

        assert skew_data == {}
        assert threshold_map == {}

    def test_single_pair_group_below_min_members(self):
        """A group with a single diff pair contributes one entry -> skipped.

        The ``< 2 members`` guard stays: one pair collapses to one averaged
        value (len(measured) == 1), which is not enough for max-min skew.
        """
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.match_group_skew import derive_group_skew_data

        nc = NetClassRouting(
            name="MIPI",
            length_match_group="MIPI_LANES",
            length_match_tolerance_mm=0.05,
        )
        net_class_map = {"MIPI_DAT0_P": nc, "MIPI_DAT0_N": nc}
        pcb = _make_ddr_pcb(
            net_lengths_mm={40: 10.0, 41: 10.2},
            net_names_map={40: "MIPI_DAT0_P", 41: "MIPI_DAT0_N"},
        )
        skew_data, _, _ = derive_group_skew_data(pcb, net_class_map)

        # One pair -> one averaged member -> below the 2-member floor.
        assert skew_data == {}

    def test_mixed_single_ended_and_pair_members(self):
        """A group with both net_ids and pair_ids measures both kinds.

        Regression guard for DDR-style groups (single-ended DQ nets +
        a DQS pair): the pair contributes its average alongside the
        single-ended lengths.
        """
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.match_group_skew import derive_group_skew_data

        nc = NetClassRouting(
            name="DDR",
            length_match_group="DDR_BYTE",
            length_match_tolerance_mm=0.5,
        )
        net_class_map = {
            "DQ0": nc,
            "DQ1": nc,
            "DQS_P": nc,
            "DQS_N": nc,
        }
        # DQ0=10.0, DQ1=10.0, DQS pair avg = (10.4+10.6)/2 = 10.5
        # skew = 10.5 - 10.0 = 0.5
        pcb = _make_ddr_pcb(
            net_lengths_mm={
                60: 10.0,  # DQ0
                61: 10.0,  # DQ1
                62: 10.4,  # DQS_P
                63: 10.6,  # DQS_N
            },
            net_names_map={
                60: "DQ0",
                61: "DQ1",
                62: "DQS_P",
                63: "DQS_N",
            },
        )
        skew_data, groups, _ = derive_group_skew_data(pcb, net_class_map)

        assert len(groups) == 1
        assert sorted(groups[0].net_ids) == [60, 61]
        assert len(groups[0].pair_ids) == 1
        assert "DDR_BYTE" in skew_data
        assert abs(skew_data["DDR_BYTE"] - 0.5) < 1e-9


# ---------------------------------------------------------------------------
# Drift-prevention tests (the core property tested in this issue).
# ---------------------------------------------------------------------------


class TestGroupSkewDataMatchesRouter:
    """``derive_group_skew_data`` MUST match ``MatchGroupTracker.get_all_skews``.

    Builds the same physical routing in both forms (router-internal
    Route + PCB-schema segments) and asserts byte-for-byte equality of
    the resulting skew dicts.  If a future change touches segment-length
    computation in one place but not the other, this test fires.

    Three scenarios:

    - Symmetric 4-trace group (skew = 0).
    - Asymmetric 4-trace group (skew > 0).
    - Via-traversing 4-trace group (validates ``board_thickness_mm`` parity).
    """

    def _build_both_forms(
        self,
        *,
        net_segments: dict[int, list[tuple[tuple[float, float], tuple[float, float]]]],
        net_vias_layers: dict[int, list[list[str]]] | None = None,
        net_names_map: dict[int, str] | None = None,
    ) -> tuple[_StubPCB, list[Route]]:
        """Return ``(pcb_stub, routes)`` for the same physical routing."""
        from kicad_tools.router.layers import Layer
        from kicad_tools.router.primitives import Route, Segment, Via

        if net_names_map is None:
            net_names_map = {nid: f"DQ{i}" for i, nid in enumerate(net_segments.keys())}
        if net_vias_layers is None:
            net_vias_layers = {}

        # PCB-schema-shape stub.
        nets: dict[int, _StubNet] = {0: _StubNet(0, "")}
        for nid, name in net_names_map.items():
            nets[nid] = _StubNet(nid, name)

        pcb_segments: list[_StubSegment] = []
        for nid, segs in net_segments.items():
            for start, end in segs:
                pcb_segments.append(
                    _StubSegment(
                        start=start,
                        end=end,
                        net_number=nid,
                        net_name=net_names_map[nid],
                    )
                )

        pcb_vias: list[_StubVia] = []
        for nid, vias_layers in net_vias_layers.items():
            for layers in vias_layers:
                pcb_vias.append(
                    _StubVia(
                        layers=list(layers),
                        net_number=nid,
                        net_name=net_names_map[nid],
                    )
                )

        pcb = _StubPCB(_nets=nets, _segments=pcb_segments, _vias=pcb_vias)

        # Router-internal Route shape.
        _layer_lookup = {
            "F.Cu": Layer.F_CU,
            "B.Cu": Layer.B_CU,
            "In1.Cu": Layer.IN1_CU,
            "In2.Cu": Layer.IN2_CU,
        }

        routes: list[Route] = []
        for nid, segs in net_segments.items():
            route = Route(
                net=nid,
                net_name=net_names_map[nid],
                segments=[
                    Segment(
                        x1=start[0],
                        y1=start[1],
                        x2=end[0],
                        y2=end[1],
                        width=0.2,
                        layer=Layer.F_CU,
                        net=nid,
                        net_name=net_names_map[nid],
                    )
                    for start, end in segs
                ],
                vias=[
                    Via(
                        x=0.0,
                        y=0.0,
                        drill=0.3,
                        diameter=0.6,
                        layers=(
                            _layer_lookup[layers[0]],
                            _layer_lookup[layers[-1]],
                        ),
                        net=nid,
                        net_name=net_names_map[nid],
                    )
                    for layers in net_vias_layers.get(nid, [])
                ],
            )
            routes.append(route)

        return pcb, routes

    def _make_match_group(self, name: str, net_ids: list[int]):
        from kicad_tools.router.match_group_length import MatchGroup, MatchGroupSource

        return MatchGroup(
            name=name,
            net_ids=sorted(net_ids),
            pair_ids=[],
            source=MatchGroupSource.EXPLICIT,
        )

    def test_symmetric_group_byte_for_byte_match(self):
        """Equal-length 4-trace group: both paths return {"DDR_DATA": 0.0}."""
        from kicad_tools.router.match_group_length import MatchGroupTracker
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.match_group_skew import derive_group_skew_data

        net_ids = [10, 11, 12, 13]
        names = {nid: f"DQ{i}" for i, nid in enumerate(net_ids)}
        pcb, routes = self._build_both_forms(
            net_segments={
                10: [((0.0, 0.0), (10.0, 0.0))],
                11: [((0.0, 1.0), (10.0, 1.0))],
                12: [((0.0, 2.0), (10.0, 2.0))],
                13: [((0.0, 3.0), (10.0, 3.0))],
            },
            net_names_map=names,
        )

        # Router-side: record routes via tracker.
        tracker = MatchGroupTracker()
        group = self._make_match_group("DDR_DATA", net_ids)
        tracker.record_routes(routes=routes, groups=[group])
        tracker_skews = tracker.get_all_skews()

        # Validate-side: re-derive from PCB + net class map.
        nc = NetClassRouting(name="DDR", length_match_group="DDR_DATA")
        net_class_map = {names[nid]: nc for nid in net_ids}
        rederived_skews, _, _ = derive_group_skew_data(pcb, net_class_map)

        assert rederived_skews == tracker_skews, (
            "drift-prevention AC: validator-side derive_group_skew_data must match "
            "producer-side MatchGroupTracker.get_all_skews byte-for-byte "
            "for the same physical routing"
        )
        # Sanity: zero skew for symmetric group.
        assert rederived_skews["DDR_DATA"] == 0.0

    def test_asymmetric_group_byte_for_byte_match(self):
        """Length-mismatched 4-trace group: both paths return the same skew."""
        from kicad_tools.router.match_group_length import MatchGroupTracker
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.match_group_skew import derive_group_skew_data

        net_ids = [10, 11, 12, 13]
        names = {nid: f"DQ{i}" for i, nid in enumerate(net_ids)}
        pcb, routes = self._build_both_forms(
            net_segments={
                10: [((0.0, 0.0), (10.0, 0.0))],
                11: [((0.0, 1.0), (10.5, 1.0))],
                12: [((0.0, 2.0), (11.0, 2.0))],
                13: [((0.0, 3.0), (12.5, 3.0))],
            },
            net_names_map=names,
        )

        tracker = MatchGroupTracker()
        group = self._make_match_group("DDR_DATA", net_ids)
        tracker.record_routes(routes=routes, groups=[group])
        tracker_skews = tracker.get_all_skews()

        nc = NetClassRouting(name="DDR", length_match_group="DDR_DATA")
        net_class_map = {names[nid]: nc for nid in net_ids}
        rederived_skews, _, _ = derive_group_skew_data(pcb, net_class_map)

        assert rederived_skews == tracker_skews
        # Sanity: max - min = 12.5 - 10.0 = 2.5.
        assert abs(rederived_skews["DDR_DATA"] - 2.5) < 1e-9

    def test_via_traversing_group_byte_for_byte_match(self):
        """Via on one member: PCB-side via length formula matches router-side.

        Validates that :meth:`MatchGroupTracker.measure_net_from_pcb`
        (the forwarder) produces the same result as the router-side
        :meth:`MatchGroupTracker.record_routes` path when
        ``board_thickness_mm`` is supplied to both.
        """
        from kicad_tools.router.match_group_length import MatchGroupTracker
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.match_group_skew import derive_group_skew_data

        net_ids = [10, 11, 12, 13]
        names = {nid: f"DQ{i}" for i, nid in enumerate(net_ids)}
        pcb, routes = self._build_both_forms(
            net_segments={
                10: [((0.0, 0.0), (10.0, 0.0))],
                11: [((0.0, 1.0), (10.0, 1.0))],
                12: [((0.0, 2.0), (10.0, 2.0))],
                13: [((0.0, 3.0), (10.0, 3.0))],
            },
            net_vias_layers={10: [["F.Cu", "B.Cu"]]},  # F.Cu->B.Cu via on DQ0
            net_names_map=names,
        )

        # Both sides supply the same board_thickness_mm.
        board_thickness_mm = 1.6
        num_copper_layers = 2

        tracker = MatchGroupTracker()
        group = self._make_match_group("DDR_DATA", net_ids)
        tracker.record_routes(
            routes=routes,
            groups=[group],
            board_thickness_mm=board_thickness_mm,
            num_copper_layers=num_copper_layers,
        )
        tracker_skews = tracker.get_all_skews()

        nc = NetClassRouting(name="DDR", length_match_group="DDR_DATA")
        net_class_map = {names[nid]: nc for nid in net_ids}
        rederived_skews, _, _ = derive_group_skew_data(
            pcb,
            net_class_map,
            board_thickness_mm=board_thickness_mm,
            num_copper_layers=num_copper_layers,
        )

        assert rederived_skews == tracker_skews, (
            "via-traversing drift-prevention: PCB-side via length formula "
            "must produce the same result as router-side"
        )
        # Sanity: DQ0 has +1.6mm via length -> skew = 1.6.
        assert abs(rederived_skews["DDR_DATA"] - 1.6) < 1e-9


# ---------------------------------------------------------------------------
# Via-inclusive skew: board_thickness_mm must be threaded through (Issue #3915)
# ---------------------------------------------------------------------------


class TestDeriveGroupSkewViaContribution:
    """A member carrying an extra via must add its drilled length to the skew.

    Regression for Issue #3915: when ``board_thickness_mm`` is ``None``
    (the pre-fix DRCChecker callsite default), each via contributes
    ``0.0 mm`` and the skew is via-blind.  When the value is threaded in,
    the extra via's drilled length appears in the skew.
    """

    def _pcb_with_extra_via(self) -> _StubPCB:
        """4-trace group, equal copper length, one member with an extra via.

        DQ0/DQ1/DQ2/DQ3 all have a 10mm F.Cu segment; DQ0 additionally
        carries one F.Cu->B.Cu via.  With via length counted, DQ0 is the
        longest member and the group skew equals the per-via drilled
        length.
        """
        nets: dict[int, _StubNet] = {0: _StubNet(0, "")}
        names = {10: "DQ0", 11: "DQ1", 12: "DQ2", 13: "DQ3"}
        for nid, name in names.items():
            nets[nid] = _StubNet(nid, name)

        segs = [
            _StubSegment(
                start=(0.0, float(nid)),
                end=(10.0, float(nid)),
                net_number=nid,
                net_name=names[nid],
            )
            for nid in names
        ]
        vias = [_StubVia(layers=["F.Cu", "B.Cu"], net_number=10, net_name="DQ0")]
        return _StubPCB(_nets=nets, _segments=segs, _vias=vias)

    def test_via_length_included_when_thickness_supplied(self):
        """With ``board_thickness_mm=1.6``, the extra via adds ~1.6mm skew."""
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.match_group_skew import derive_group_skew_data

        nc = NetClassRouting(name="DDR", length_match_group="DDR_DATA")
        net_class_map = {"DQ0": nc, "DQ1": nc, "DQ2": nc, "DQ3": nc}

        pcb = self._pcb_with_extra_via()
        skew_data, _, _ = derive_group_skew_data(
            pcb,
            net_class_map,
            board_thickness_mm=1.6,
            num_copper_layers=4,
        )

        # The extra F.Cu->B.Cu via on a 1.6mm / 4L stack adds the full
        # stack thickness (top-to-bottom drilled span) to DQ0.  Every
        # other member is 10.0mm, so the skew is exactly that via length.
        assert "DDR_DATA" in skew_data
        assert skew_data["DDR_DATA"] > 1.5, (
            "via-inclusive skew regression (Issue #3915): a member with an "
            "extra full-stack via must contribute its drilled length to the skew"
        )
        assert abs(skew_data["DDR_DATA"] - 1.6) < 1e-9

    def test_via_length_ignored_when_thickness_none(self):
        """Pre-fix behaviour: ``board_thickness_mm=None`` -> via contributes 0.0."""
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.match_group_skew import derive_group_skew_data

        nc = NetClassRouting(name="DDR", length_match_group="DDR_DATA")
        net_class_map = {"DQ0": nc, "DQ1": nc, "DQ2": nc, "DQ3": nc}

        pcb = self._pcb_with_extra_via()
        # board_thickness_mm defaults to None -> via-blind.
        skew_data, _, _ = derive_group_skew_data(pcb, net_class_map)

        assert "DDR_DATA" in skew_data
        # All copper lengths equal; via is invisible without thickness.
        assert skew_data["DDR_DATA"] == 0.0


class TestCheckerThreadsBoardThickness:
    """DRCChecker must thread ``board_thickness_mm`` + ``layers`` into the producer.

    Integration regression for Issue #3915: the DRCChecker callsite
    previously omitted both, so via lengths were dropped and an
    over-tolerance via-skew silently passed.
    """

    def _pcb_with_extra_via(self) -> _StubPCB:
        nets: dict[int, _StubNet] = {0: _StubNet(0, "")}
        names = {10: "DQ0", 11: "DQ1", 12: "DQ2", 13: "DQ3"}
        for nid, name in names.items():
            nets[nid] = _StubNet(nid, name)
        segs = [
            _StubSegment(
                start=(0.0, float(nid)),
                end=(10.0, float(nid)),
                net_number=nid,
                net_name=names[nid],
            )
            for nid in names
        ]
        # DQ0 and DQ2 each carry one extra full-stack via.
        vias = [
            _StubVia(layers=["F.Cu", "B.Cu"], net_number=10, net_name="DQ0"),
            _StubVia(layers=["F.Cu", "B.Cu"], net_number=12, net_name="DQ2"),
        ]
        return _StubPCB(_nets=nets, _segments=segs, _vias=vias)

    def test_via_skew_fires_on_four_layer_board(self):
        """layers=4 board: extra vias push skew over tolerance -> violation."""
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.checker import DRCChecker

        pcb = self._pcb_with_extra_via()
        nc = NetClassRouting(name="DDR", length_match_group="DDR_DATA")
        net_class_map = {"DQ0": nc, "DQ1": nc, "DQ2": nc, "DQ3": nc}

        checker = DRCChecker(
            pcb,
            manufacturer="jlcpcb",
            layers=4,
            net_class_map=net_class_map,
        )
        # jlcpcb 4L default board_thickness_mm is 1.6.
        assert checker.design_rules.board_thickness_mm == 1.6

        results = checker.check_match_group_length_skew()

        assert results.rules_checked == 1
        assert len(results.violations) == 1
        v = results.violations[0]
        assert v.rule_id == "match_group_length_skew"
        assert "DDR_DATA" in v.message
        assert "mm" in v.message
        # Via-inclusive skew must exceed the default 0.5mm tolerance.
        assert v.actual_value > 1.5
        assert v.required_value == 0.5

    def test_via_skew_invisible_without_fix(self):
        """Guard: the same board must be silently via-blind if thickness=None.

        Directly calls the producer with the pre-fix arguments to pin the
        exact regression the DRCChecker fix closes.
        """
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.match_group_skew import derive_group_skew_data

        pcb = self._pcb_with_extra_via()
        nc = NetClassRouting(name="DDR", length_match_group="DDR_DATA")
        net_class_map = {"DQ0": nc, "DQ1": nc, "DQ2": nc, "DQ3": nc}

        # Pre-fix callsite: no board_thickness_mm, no num_copper_layers.
        skew_blind, _, _ = derive_group_skew_data(pcb, net_class_map)
        # Post-fix callsite: threaded through.
        skew_seen, _, _ = derive_group_skew_data(
            pcb, net_class_map, board_thickness_mm=1.6, num_copper_layers=4
        )

        assert skew_blind["DDR_DATA"] == 0.0
        assert skew_seen["DDR_DATA"] > 1.5


class TestTwoLayerViaFreeUnaffected:
    """AC5: a 2L via-free group reports 0.0mm skew with or without params."""

    def test_via_free_group_identical_with_explicit_params(self):
        """Passing ``board_thickness_mm=1.6, num_copper_layers=2`` changes nothing.

        A via-free group has no via-length terms, so threading the
        thickness through is a no-op for its skew.
        """
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.match_group_skew import derive_group_skew_data

        nc = NetClassRouting(name="DDR", length_match_group="DDR_DATA")
        net_class_map = {"DQ0": nc, "DQ1": nc, "DQ2": nc, "DQ3": nc}

        pcb = _make_ddr_pcb(net_lengths_mm={10: 10.0, 11: 10.0, 12: 10.0, 13: 10.0})

        default_skew, _, _ = derive_group_skew_data(pcb, net_class_map)
        explicit_skew, _, _ = derive_group_skew_data(
            pcb, net_class_map, board_thickness_mm=1.6, num_copper_layers=2
        )

        assert default_skew == explicit_skew
        assert explicit_skew["DDR_DATA"] == 0.0


# ---------------------------------------------------------------------------
# Constants drift-prevention (mirrors test_validate_diffpair_skew.py)
# ---------------------------------------------------------------------------


class TestDefaultMatchGroupToleranceDriftPrevention:
    """``DEFAULT_MATCH_GROUP_TOLERANCE_MM`` MUST equal accessor default.

    If a future change touches one constant without the other, this
    drift-prevention test fires.  Mirrors the equivalent test in
    ``tests/test_validate_diffpair_skew.py``.
    """

    def test_module_default_matches_accessor_default(self):
        import inspect

        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.rules.match_group_length_skew import (
            DEFAULT_MATCH_GROUP_TOLERANCE_MM,
        )

        sig = inspect.signature(NetClassRouting.effective_length_match_tolerance)
        accessor_default = sig.parameters["default"].default

        assert accessor_default == DEFAULT_MATCH_GROUP_TOLERANCE_MM, (
            "DEFAULT_MATCH_GROUP_TOLERANCE_MM in validate/rules/match_group_length_skew "
            "must match the default arg of NetClassRouting.effective_length_match_tolerance "
            "(both 0.5 mm).  If you changed one, change the other."
        )

    def test_default_matches_via_constructed_instance(self):
        """``NetClassRouting().effective_length_match_tolerance()`` matches constant."""
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.rules.match_group_length_skew import (
            DEFAULT_MATCH_GROUP_TOLERANCE_MM,
        )

        nc = NetClassRouting(name="x")
        assert nc.effective_length_match_tolerance() == DEFAULT_MATCH_GROUP_TOLERANCE_MM


# ---------------------------------------------------------------------------
# Integration: DRCChecker.check_match_group_length_skew uses the new wiring.
# ---------------------------------------------------------------------------


class TestCheckerIntegration:
    """End-to-end: DRCChecker.check_match_group_length_skew picks up the wiring."""

    def test_no_net_class_map_remains_no_op(self):
        """Standalone-CLI invocation (no net_class_map) -> 0 violations, 0 rules_checked.

        AC #1: graceful degradation contract preserved.
        """
        from kicad_tools.validate.checker import DRCChecker

        pcb = _make_ddr_pcb(net_lengths_mm={10: 10.0, 11: 12.0, 12: 14.0, 13: 16.0})
        # No net_class_map -> rule is no-op.
        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2)
        results = checker.check_match_group_length_skew()

        assert len(results.violations) == 0
        assert results.rules_checked == 0

    def test_with_net_class_map_fires_when_over_tolerance(self):
        """With net_class_map + over-tolerance group -> rule fires."""
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.checker import DRCChecker

        # Skew = max - min = 12 - 10 = 2.0 mm, well over default 0.5.
        pcb = _make_ddr_pcb(net_lengths_mm={10: 10.0, 11: 10.5, 12: 11.0, 13: 12.0})
        nc = NetClassRouting(name="DDR", length_match_group="DDR_DATA")
        net_class_map = {"DQ0": nc, "DQ1": nc, "DQ2": nc, "DQ3": nc}

        checker = DRCChecker(
            pcb,
            manufacturer="jlcpcb",
            layers=2,
            net_class_map=net_class_map,
        )
        results = checker.check_match_group_length_skew()

        assert results.rules_checked == 1
        assert results.rules_checked_by_rule.get("match_group_length_skew") == 1
        assert len(results.violations) == 1
        v = results.violations[0]
        assert v.rule_id == "match_group_length_skew"
        assert "DDR_DATA" in v.message
        assert abs(v.actual_value - 2.0) < 1e-9
        assert v.required_value == 0.5

    def test_with_net_class_map_passes_when_under_tolerance(self):
        """With net_class_map + within-tolerance group -> no fire, rule still checked."""
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.checker import DRCChecker

        # Skew = 0.3 mm, under default 0.5.
        pcb = _make_ddr_pcb(net_lengths_mm={10: 10.0, 11: 10.1, 12: 10.2, 13: 10.3})
        nc = NetClassRouting(name="DDR", length_match_group="DDR_DATA")
        net_class_map = {"DQ0": nc, "DQ1": nc, "DQ2": nc, "DQ3": nc}

        checker = DRCChecker(
            pcb,
            manufacturer="jlcpcb",
            layers=2,
            net_class_map=net_class_map,
        )
        results = checker.check_match_group_length_skew()

        assert results.rules_checked == 1
        assert results.rules_checked_by_rule.get("match_group_length_skew") == 1
        assert len(results.violations) == 0

    def test_undeclared_group_does_not_fire(self):
        """No ``length_match_group`` set -> no group detected, no fire."""
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.checker import DRCChecker

        # Skew is large but no group is declared via length_match_group.
        pcb = _make_ddr_pcb(net_lengths_mm={10: 10.0, 11: 15.0, 12: 20.0, 13: 25.0})
        nc = NetClassRouting(name="DEFAULT")  # no length_match_group
        net_class_map = {"DQ0": nc, "DQ1": nc, "DQ2": nc, "DQ3": nc}

        checker = DRCChecker(
            pcb,
            manufacturer="jlcpcb",
            layers=2,
            net_class_map=net_class_map,
        )
        results = checker.check_match_group_length_skew()

        assert results.rules_checked == 0
        assert len(results.violations) == 0

    def test_partial_routing_silently_skipped(self):
        """One unrouted member -> group dropped silently (no spurious violation)."""
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.checker import DRCChecker

        # DQ2 unrouted: without gating, we'd see skew = max(10) - 0 = 10mm
        # and fire a bogus violation.  With gating, the group is dropped.
        pcb = _make_ddr_pcb(net_lengths_mm={10: 10.0, 11: 10.0, 12: None, 13: 10.0})
        nc = NetClassRouting(name="DDR", length_match_group="DDR_DATA")
        net_class_map = {"DQ0": nc, "DQ1": nc, "DQ2": nc, "DQ3": nc}

        checker = DRCChecker(
            pcb,
            manufacturer="jlcpcb",
            layers=2,
            net_class_map=net_class_map,
        )
        results = checker.check_match_group_length_skew()

        # Group dropped: no rules_checked, no violation.
        assert results.rules_checked == 0
        assert len(results.violations) == 0
