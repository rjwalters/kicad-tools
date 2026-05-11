"""Tests for layered match-group detection (Issue #2689, Epic #2661 Phase 1C).

Covers the three detection sources, tie-breaking, small-group refusal,
clock-sentinel resolution, and drift-prevention against
:data:`kicad_tools.analysis.trace_length.CRITICAL_NET_PATTERNS`.

Test categories:

1. EXPLICIT source -- ``NetClassRouting.length_match_group`` declarations.
2. LEGACY_API source -- legacy ``LengthTracker.match_groups`` entries.
3. SUFFIX source -- opt-in :data:`BUS_GROUP_PATTERNS` inference.
4. Priority tie-breaking: EXPLICIT > LEGACY_API > SUFFIX.
5. False-positive refusals: counter signals, GPIO-as-address, etc.
6. Clock sentinel resolution.
7. Drift prevention: CRITICAL_NET_PATTERNS coverage.
"""

from __future__ import annotations

import logging
import re

from kicad_tools.analysis.trace_length import CRITICAL_NET_PATTERNS
from kicad_tools.router.length import LengthTracker, create_match_group
from kicad_tools.router.match_group_detection import (
    BUS_GROUP_PATTERNS,
    MatchGroup,
    MatchGroupSource,
    detect_match_groups,
)
from kicad_tools.router.rules import NetClassRouting

# =============================================================================
# 1. EXPLICIT source
# =============================================================================


class TestExplicitDeclaration:
    """Groups declared via ``NetClassRouting.length_match_group``."""

    def test_single_class_single_group(self):
        net_names = {1: "DQ0", 2: "DQ1", 3: "DQ2", 4: "DQ3"}
        nc = NetClassRouting(name="DDR", length_match_group="DDR_BYTE0")
        net_class_routing = {"DDR": nc}
        net_to_class = dict.fromkeys(net_names.values(), "DDR")

        out = detect_match_groups(
            net_names,
            net_class_routing=net_class_routing,
            net_to_class=net_to_class,
        )
        assert len(out) == 1
        assert out[0].source == MatchGroupSource.EXPLICIT
        assert out[0].group_id == "DDR_BYTE0"
        assert out[0].members == [1, 2, 3, 4]
        assert out[0].reference is None  # No reference policy -> "longest"

    def test_multiple_classes_merge_into_single_group(self):
        """The MIPI-lane use case: per-pair classes share a group."""
        net_names = {
            1: "CSI_DAT0_P",
            2: "CSI_DAT0_N",
            3: "CSI_DAT1_P",
            4: "CSI_DAT1_N",
        }
        nc0 = NetClassRouting(name="MIPI_LANE0", length_match_group="MIPI_CSI")
        nc1 = NetClassRouting(name="MIPI_LANE1", length_match_group="MIPI_CSI")
        net_class_routing = {"MIPI_LANE0": nc0, "MIPI_LANE1": nc1}
        net_to_class = {
            "CSI_DAT0_P": "MIPI_LANE0",
            "CSI_DAT0_N": "MIPI_LANE0",
            "CSI_DAT1_P": "MIPI_LANE1",
            "CSI_DAT1_N": "MIPI_LANE1",
        }

        out = detect_match_groups(
            net_names,
            net_class_routing=net_class_routing,
            net_to_class=net_to_class,
        )
        assert len(out) == 1
        assert out[0].group_id == "MIPI_CSI"
        assert out[0].members == [1, 2, 3, 4]

    def test_explicit_reference_resolution(self):
        net_names = {1: "DQ0", 2: "DQ1", 3: "DQ2", 4: "DQS_P"}
        nc = NetClassRouting(
            name="DDR",
            length_match_group="DDR_BYTE0",
            length_match_reference="DQS_P",
        )
        net_class_routing = {"DDR": nc}
        net_to_class = dict.fromkeys(net_names.values(), "DDR")

        out = detect_match_groups(
            net_names,
            net_class_routing=net_class_routing,
            net_to_class=net_to_class,
        )
        assert len(out) == 1
        assert out[0].reference == 4  # DQS_P's net id.

    def test_explicit_reference_missing_from_netlist_falls_back(self, caplog):
        net_names = {1: "DQ0", 2: "DQ1", 3: "DQ2"}
        nc = NetClassRouting(
            name="DDR",
            length_match_group="DDR_BYTE0",
            length_match_reference="NONEXISTENT",
        )
        with caplog.at_level(logging.WARNING):
            out = detect_match_groups(
                net_names,
                net_class_routing={"DDR": nc},
                net_to_class=dict.fromkeys(net_names.values(), "DDR"),
            )
        assert out[0].reference is None
        assert any("not in the net list" in r.message for r in caplog.records)

    def test_explicit_reference_not_a_member_falls_back(self, caplog):
        net_names = {1: "DQ0", 2: "DQ1", 3: "DQ2", 4: "OTHER_NET"}
        nc_ddr = NetClassRouting(
            name="DDR",
            length_match_group="DDR_BYTE0",
            length_match_reference="OTHER_NET",
        )
        nc_other = NetClassRouting(name="OTHER")
        net_to_class = {
            "DQ0": "DDR",
            "DQ1": "DDR",
            "DQ2": "DDR",
            "OTHER_NET": "OTHER",
        }
        with caplog.at_level(logging.WARNING):
            out = detect_match_groups(
                net_names,
                net_class_routing={"DDR": nc_ddr, "OTHER": nc_other},
                net_to_class=net_to_class,
            )
        assert out[0].reference is None
        assert any("not a member of the group" in r.message for r in caplog.records)

    def test_no_net_class_routing_returns_empty(self):
        out = detect_match_groups({1: "DQ0", 2: "DQ1"})
        assert out == []

    def test_class_without_group_field_skipped(self):
        net_names = {1: "DQ0", 2: "DQ1"}
        nc = NetClassRouting(name="DDR")  # No length_match_group set.
        out = detect_match_groups(
            net_names,
            net_class_routing={"DDR": nc},
            net_to_class=dict.fromkeys(net_names.values(), "DDR"),
        )
        assert out == []


# =============================================================================
# 2. LEGACY_API source
# =============================================================================


class TestLegacyApiSource:
    """Groups already registered via ``Autorouter.add_match_group(...)``."""

    def test_legacy_group_detected(self):
        net_names = {10: "MEM_D0", 11: "MEM_D1", 12: "MEM_D2", 13: "MEM_D3"}
        constraints = create_match_group("MEM_BUS", [10, 11, 12, 13], tolerance=0.2)
        tracker = LengthTracker(constraints=constraints)

        out = detect_match_groups(net_names, length_tracker=tracker)
        assert len(out) == 1
        assert out[0].source == MatchGroupSource.LEGACY_API
        assert out[0].group_id == "MEM_BUS"
        assert out[0].members == [10, 11, 12, 13]
        assert out[0].reference is None

    def test_legacy_with_no_tracker_skipped(self):
        out = detect_match_groups({10: "MEM_D0"})
        assert out == []

    def test_empty_legacy_tracker_skipped(self):
        tracker = LengthTracker()
        out = detect_match_groups({10: "MEM_D0"}, length_tracker=tracker)
        assert out == []


# =============================================================================
# 3. SUFFIX source
# =============================================================================


class TestSuffixInference:
    """Opt-in suffix-based pattern matching via BUS_GROUP_PATTERNS."""

    def test_off_by_default(self):
        net_names = {i: f"DQ{i}" for i in range(8)}
        out = detect_match_groups(net_names)
        assert out == []

    def test_ddr_data_byte_inferred_when_enabled(self):
        net_names = {i: f"DQ{i}" for i in range(8)}
        out = detect_match_groups(net_names, enable_suffix_inference=True)
        assert len(out) == 1
        assert out[0].source == MatchGroupSource.SUFFIX
        assert out[0].group_id == "DDR_DATA"
        assert out[0].members == list(range(8))

    def test_address_bus_16_nets(self):
        # Address-line false-positive risk -- only valid when group is large.
        net_names = {i: f"A{i}" for i in range(16)}
        out = detect_match_groups(net_names, enable_suffix_inference=True)
        assert len(out) == 1
        assert out[0].group_id == "ADDR_BUS"
        assert len(out[0].members) == 16

    def test_mipi_csi_lane_data(self):
        net_names = {
            1: "CSI_DAT0_P",
            2: "CSI_DAT0_N",
            3: "CSI_DAT1_P",
            4: "CSI_DAT1_N",
            5: "CSI_DAT2_P",
            6: "CSI_DAT2_N",
            7: "CSI_DAT3_P",
            8: "CSI_DAT3_N",
        }
        out = detect_match_groups(net_names, enable_suffix_inference=True)
        assert len(out) == 1
        assert out[0].group_id == "MIPI_CSI_DATA"
        assert len(out[0].members) == 8

    def test_hdmi_tmds_data_lanes_clock_excluded(self):
        # 6 data nets (3 lanes x P/N) + 2 clock nets.  The clock is
        # excluded from the data-lane group (Phase 2F's job to
        # compose lanes-vs-clock).
        net_names = {
            1: "TMDS_D0_P",
            2: "TMDS_D0_N",
            3: "TMDS_D1_P",
            4: "TMDS_D1_N",
            5: "TMDS_D2_P",
            6: "TMDS_D2_N",
            7: "TMDS_CLK_P",
            8: "TMDS_CLK_N",
        }
        out = detect_match_groups(net_names, enable_suffix_inference=True)
        # The data lanes form one group of 6 (TMDS_CLK_* doesn't match
        # the data pattern).
        data_groups = [g for g in out if g.group_id == "HDMI_TMDS_DATA"]
        assert len(data_groups) == 1
        assert len(data_groups[0].members) == 6
        assert 7 not in data_groups[0].members
        assert 8 not in data_groups[0].members


# =============================================================================
# 4. Priority tie-breaking
# =============================================================================


class TestSourcePriority:
    """EXPLICIT > LEGACY_API > SUFFIX claim semantics."""

    def test_explicit_preempts_suffix(self):
        # 8 DDR-style nets that would otherwise be inferred as "DDR_DATA".
        # An explicit class declaration claims them as "CUSTOM_BUS".
        net_names = {i: f"DQ{i}" for i in range(8)}
        nc = NetClassRouting(name="DDR", length_match_group="CUSTOM_BUS")
        out = detect_match_groups(
            net_names,
            net_class_routing={"DDR": nc},
            net_to_class=dict.fromkeys(net_names.values(), "DDR"),
            enable_suffix_inference=True,
        )
        # Exactly ONE group, named CUSTOM_BUS (not DDR_DATA), source EXPLICIT.
        assert len(out) == 1
        assert out[0].group_id == "CUSTOM_BUS"
        assert out[0].source == MatchGroupSource.EXPLICIT

    def test_explicit_preempts_legacy_on_overlap(self):
        net_names = {i: f"DQ{i}" for i in range(4)}
        nc = NetClassRouting(name="DDR", length_match_group="EXPLICIT_BUS")
        constraints = create_match_group("LEGACY_BUS", [0, 1, 2, 3], tolerance=0.2)
        tracker = LengthTracker(constraints=constraints)

        out = detect_match_groups(
            net_names,
            net_class_routing={"DDR": nc},
            net_to_class=dict.fromkeys(net_names.values(), "DDR"),
            length_tracker=tracker,
        )
        # Only the EXPLICIT group is reported.
        assert len(out) == 1
        assert out[0].group_id == "EXPLICIT_BUS"
        assert out[0].source == MatchGroupSource.EXPLICIT

    def test_legacy_preempts_suffix(self):
        net_names = {i: f"DQ{i}" for i in range(8)}
        constraints = create_match_group("LEGACY_DDR", list(range(8)), tolerance=0.2)
        tracker = LengthTracker(constraints=constraints)

        out = detect_match_groups(
            net_names,
            length_tracker=tracker,
            enable_suffix_inference=True,
        )
        assert len(out) == 1
        assert out[0].group_id == "LEGACY_DDR"
        assert out[0].source == MatchGroupSource.LEGACY_API

    def test_legacy_partial_coverage_only_suffix_takes_rest(self):
        # 4 DQ nets; legacy claims [0,1]; suffix would want all 4.
        # Legacy partially overlaps explicit-claim set -> dropped;
        # then suffix sees all 4 unclaimed and emits.  Or: if legacy
        # is allowed but only claims 2, suffix gets remaining 2 ->
        # too small, refused.
        #
        # Concretely: a legacy group of 2 nets is below min size
        # but the detector doesn't refuse legacy groups (legacy was
        # already declared by an agent).  Legacy emits, leaving 2
        # DQ-nets for suffix, which refuses (size 2 < 3).
        net_names = {i: f"DQ{i}" for i in range(4)}
        constraints = create_match_group("LEGACY_PAIR", [0, 1], tolerance=0.2)
        tracker = LengthTracker(constraints=constraints)

        out = detect_match_groups(
            net_names,
            length_tracker=tracker,
            enable_suffix_inference=True,
        )
        # Legacy emits its 2-net group; suffix refuses the remaining 2.
        assert len(out) == 1
        assert out[0].source == MatchGroupSource.LEGACY_API
        assert out[0].members == [0, 1]


# =============================================================================
# 5. False-positive refusal cases
# =============================================================================


class TestFalsePositiveRefusal:
    """Suffix inference refuses low-confidence groups."""

    def test_counter_signal_two_dq_nets_refused(self):
        """Two DQ-named nets (counter signals) don't form a group."""
        net_names = {1: "DQ0", 2: "DQ1"}
        out = detect_match_groups(net_names, enable_suffix_inference=True)
        assert out == []

    def test_single_a_gpio_refused(self):
        """A single ``A0`` net is not enough for an address bus."""
        net_names = {1: "A0"}
        out = detect_match_groups(net_names, enable_suffix_inference=True)
        assert out == []

    def test_two_address_lines_refused(self):
        """Even two address-like nets are below the min-group threshold."""
        net_names = {1: "A0", 2: "A1"}
        out = detect_match_groups(net_names, enable_suffix_inference=True)
        assert out == []

    def test_partial_coverage_extracts_only_dq(self):
        """DQ\\d+ matches but unrelated net is left alone."""
        net_names = {
            1: "DQ0",
            2: "DQ1",
            3: "DQ2",
            4: "SOMETHING_ELSE",
        }
        out = detect_match_groups(net_names, enable_suffix_inference=True)
        # 3 DQ nets is exactly the min-group threshold; emit the group.
        assert len(out) == 1
        assert out[0].group_id == "DDR_DATA"
        assert out[0].members == [1, 2, 3]
        assert 4 not in out[0].members


# =============================================================================
# 6. DDR strobe + mask interactions
# =============================================================================


class TestDDRStrobeMaskInteraction:
    """The DDR strobe / mask patterns should be present but typically
    refused due to small group size."""

    def test_ddr_byte_with_mask_and_strobe(self):
        # 8 DQ nets (form DDR_DATA), 1 DM (mask -- below threshold),
        # 2 DQS (strobe -- below threshold).
        net_names = {
            1: "DQ0",
            2: "DQ1",
            3: "DQ2",
            4: "DQ3",
            5: "DQ4",
            6: "DQ5",
            7: "DQ6",
            8: "DQ7",
            9: "DM0",
            10: "DQS_P",
            11: "DQS_N",
        }
        out = detect_match_groups(net_names, enable_suffix_inference=True)
        # Only the DDR_DATA group is reported; mask + strobe individually
        # are below min-group threshold.
        assert len(out) == 1
        assert out[0].group_id == "DDR_DATA"
        assert out[0].members == [1, 2, 3, 4, 5, 6, 7, 8]


# =============================================================================
# 7. Clock sentinel resolution
# =============================================================================


class TestClockSentinelResolution:
    """``length_match_reference="clock"`` finds a clock-named member."""

    def test_clock_p_resolved_via_clk_pattern(self):
        net_names = {1: "MIPI_CLK_P", 2: "DAT0_P", 3: "DAT1_P"}
        nc = NetClassRouting(
            name="MIPI",
            length_match_group="MIPI_LANE",
            length_match_reference="clock",
        )
        out = detect_match_groups(
            net_names,
            net_class_routing={"MIPI": nc},
            net_to_class=dict.fromkeys(net_names.values(), "MIPI"),
        )
        assert len(out) == 1
        assert out[0].reference == 1  # MIPI_CLK_P

    def test_sclk_resolved_via_clk_suffix(self):
        net_names = {1: "DATA0", 2: "DATA1", 3: "SCLK"}
        nc = NetClassRouting(
            name="SPI",
            length_match_group="SPI_BUS",
            length_match_reference="clock",
        )
        out = detect_match_groups(
            net_names,
            net_class_routing={"SPI": nc},
            net_to_class=dict.fromkeys(net_names.values(), "SPI"),
        )
        assert len(out) == 1
        assert out[0].reference == 3  # SCLK

    def test_clockdiv2_resolved_via_clock_pattern(self):
        net_names = {1: "DATA0", 2: "DATA1", 3: "CLOCKDIV2"}
        nc = NetClassRouting(
            name="BUS",
            length_match_group="BUS_GROUP",
            length_match_reference="clock",
        )
        out = detect_match_groups(
            net_names,
            net_class_routing={"BUS": nc},
            net_to_class=dict.fromkeys(net_names.values(), "BUS"),
        )
        assert out[0].reference == 3  # CLOCKDIV2

    def test_no_clock_match_falls_back_with_warning(self, caplog):
        net_names = {1: "DATA0", 2: "DATA1", 3: "DATA2"}
        nc = NetClassRouting(
            name="BUS",
            length_match_group="BUS_GROUP",
            length_match_reference="clock",
        )
        with caplog.at_level(logging.WARNING):
            out = detect_match_groups(
                net_names,
                net_class_routing={"BUS": nc},
                net_to_class=dict.fromkeys(net_names.values(), "BUS"),
            )
        assert out[0].reference is None
        assert any(
            "'clock' sentinel" in r.message and "no member" in r.message for r in caplog.records
        )

    def test_multiple_clock_matches_picks_lowest_id_with_warning(self, caplog):
        net_names = {1: "CLK_IN", 2: "DATA0", 3: "CLK_OUT"}
        nc = NetClassRouting(
            name="BUS",
            length_match_group="BUS_GROUP",
            length_match_reference="clock",
        )
        with caplog.at_level(logging.WARNING):
            out = detect_match_groups(
                net_names,
                net_class_routing={"BUS": nc},
                net_to_class=dict.fromkeys(net_names.values(), "BUS"),
            )
        assert out[0].reference == 1  # Lowest net id wins.
        assert any("'clock' sentinel matched" in r.message for r in caplog.records)


# =============================================================================
# 8. Drift prevention
# =============================================================================


class TestDriftPrevention:
    """Ensure this module doesn't reimplement CRITICAL_NET_PATTERNS."""

    def test_critical_net_patterns_import_resolves(self):
        # If the symbol is renamed upstream, this import (and our
        # module's import) breaks.  The test is the early-warning.
        assert isinstance(CRITICAL_NET_PATTERNS, list)
        assert len(CRITICAL_NET_PATTERNS) > 0

    def test_clock_regex_indices_still_point_at_clock_patterns(self):
        # CRITICAL_NET_PATTERNS[0..3] are the four clock regexes
        # (^CLK, CLK$, CLOCK, _CLK_).  If any of those moves, the
        # _CLOCK_REGEX_INDICES constant in match_group_detection
        # must be updated.  Each index's fragment matches a distinct
        # clock-naming convention.
        expected_fragments = ["^CLK", "CLK$", "CLOCK", "_CLK_"]
        for idx, fragment in zip(range(4), expected_fragments, strict=True):
            assert fragment in CRITICAL_NET_PATTERNS[idx], (
                f"CRITICAL_NET_PATTERNS[{idx}] no longer contains "
                f"{fragment!r}; update _CLOCK_REGEX_INDICES in "
                f"match_group_detection.py"
            )

    def test_bus_group_patterns_dq_fragment_overlaps_critical_net_patterns(self):
        # The DQ regex in our table must align with the per-net
        # classifier so the two cannot silently diverge.  Specifically:
        # CRITICAL_NET_PATTERNS contains r"(?i)^DQ\d" and our
        # BUS_GROUP_PATTERNS contains r"(?i)^DQ\d+$".  Both
        # discriminate on "starts with DQ followed by digits".
        bus_patterns_str = [p.pattern for p, _ in BUS_GROUP_PATTERNS]
        assert any("DQ" in p for p in bus_patterns_str)
        assert any("DQ" in p for p in CRITICAL_NET_PATTERNS)

    def test_bus_group_patterns_dqs_fragment_aligned(self):
        bus_patterns_str = [p.pattern for p, _ in BUS_GROUP_PATTERNS]
        assert any("DQS" in p for p in bus_patterns_str)
        assert any("DQS" in p for p in CRITICAL_NET_PATTERNS)

    def test_bus_group_patterns_dm_fragment_aligned(self):
        bus_patterns_str = [p.pattern for p, _ in BUS_GROUP_PATTERNS]
        assert any(re.search(r"DM", p) for p in bus_patterns_str)
        assert any(re.search(r"DM", p) for p in CRITICAL_NET_PATTERNS)

    def test_bus_group_patterns_address_fragment_aligned(self):
        bus_patterns_str = [p.pattern for p, _ in BUS_GROUP_PATTERNS]
        assert any(re.search(r"A.*\\d", p) for p in bus_patterns_str)
        assert any(re.search(r"A.*\\d", p) for p in CRITICAL_NET_PATTERNS)


# =============================================================================
# 9. Output determinism
# =============================================================================


class TestOutputDeterminism:
    """Members within a group are sorted by net id; sources are emitted
    in declaration order (EXPLICIT, LEGACY_API, SUFFIX)."""

    def test_members_sorted_by_net_id(self):
        net_names = {30: "DQ0", 10: "DQ1", 20: "DQ2"}
        out = detect_match_groups(net_names, enable_suffix_inference=True)
        assert out[0].members == [10, 20, 30]

    def test_sources_emitted_in_priority_order(self):
        # Construct a netlist where each source emits one group, with
        # disjoint membership.
        net_names = {
            1: "EXPLICIT_NET_0",
            2: "EXPLICIT_NET_1",
            3: "EXPLICIT_NET_2",
            10: "MEM_D0",
            11: "MEM_D1",
            12: "MEM_D2",
            20: "DQ0",
            21: "DQ1",
            22: "DQ2",
        }
        nc = NetClassRouting(name="EX", length_match_group="EXPLICIT_GROUP")
        net_to_class = {
            "EXPLICIT_NET_0": "EX",
            "EXPLICIT_NET_1": "EX",
            "EXPLICIT_NET_2": "EX",
        }
        constraints = create_match_group("LEGACY_GROUP", [10, 11, 12], tolerance=0.2)
        tracker = LengthTracker(constraints=constraints)

        out = detect_match_groups(
            net_names,
            net_class_routing={"EX": nc},
            net_to_class=net_to_class,
            length_tracker=tracker,
            enable_suffix_inference=True,
        )
        assert len(out) == 3
        assert out[0].source == MatchGroupSource.EXPLICIT
        assert out[1].source == MatchGroupSource.LEGACY_API
        assert out[2].source == MatchGroupSource.SUFFIX


# =============================================================================
# 10. MatchGroup dataclass smoke
# =============================================================================


class TestMatchGroupDataclass:
    def test_default_members_empty(self):
        g = MatchGroup(group_id="TEST")
        assert g.members == []
        assert g.reference is None
        assert g.source == MatchGroupSource.EXPLICIT

    def test_explicit_construction(self):
        g = MatchGroup(
            group_id="DDR_DATA",
            members=[1, 2, 3, 4],
            reference=2,
            source=MatchGroupSource.SUFFIX,
        )
        assert g.group_id == "DDR_DATA"
        assert g.members == [1, 2, 3, 4]
        assert g.reference == 2
        assert g.source == MatchGroupSource.SUFFIX

    def test_source_enum_has_three_values(self):
        # No KICAD_GROUP -- intentionally absent (no KiCad analog
        # for match groups yet).
        values = {s.value for s in MatchGroupSource}
        assert values == {"explicit", "legacy_api", "suffix"}


# =============================================================================
# 11. End-to-end fixture scenarios from the curator's expanded plan
# =============================================================================


class TestExpandedFixtures:
    """The curator-spec'd fixture scenarios in the issue body."""

    def test_ddr_data_byte_10_nets_explicit(self):
        """DDR byte: 8 data + 1 mask + 2 strobe = 11 nets in one group."""
        net_names = {
            1: "DQ0",
            2: "DQ1",
            3: "DQ2",
            4: "DQ3",
            5: "DQ4",
            6: "DQ5",
            7: "DQ6",
            8: "DQ7",
            9: "DM0",
            10: "DQS_P",
            11: "DQS_N",
        }
        nc = NetClassRouting(
            name="DDR_BYTE0",
            length_match_group="DDR_DATA_BYTE_0",
            length_match_reference="DQS_P",
        )
        out = detect_match_groups(
            net_names,
            net_class_routing={"DDR_BYTE0": nc},
            net_to_class=dict.fromkeys(net_names.values(), "DDR_BYTE0"),
        )
        assert len(out) == 1
        assert out[0].group_id == "DDR_DATA_BYTE_0"
        assert len(out[0].members) == 11
        assert out[0].reference == 10  # DQS_P

    def test_4_lane_mipi_csi_via_explicit(self):
        """4-lane MIPI CSI: 1 clock pair + 4 data pairs = 10 nets."""
        net_names = {
            1: "CSI_CLK_P",
            2: "CSI_CLK_N",
            3: "CSI_DAT0_P",
            4: "CSI_DAT0_N",
            5: "CSI_DAT1_P",
            6: "CSI_DAT1_N",
            7: "CSI_DAT2_P",
            8: "CSI_DAT2_N",
            9: "CSI_DAT3_P",
            10: "CSI_DAT3_N",
        }
        nc = NetClassRouting(
            name="MIPI_CSI",
            length_match_group="MIPI_CSI_BUS",
            length_match_reference="clock",
        )
        out = detect_match_groups(
            net_names,
            net_class_routing={"MIPI_CSI": nc},
            net_to_class=dict.fromkeys(net_names.values(), "MIPI_CSI"),
        )
        assert len(out) == 1
        assert len(out[0].members) == 10
        # "clock" sentinel finds CSI_CLK_P (lowest id, matches ^CLK
        # via CLK_).
        assert out[0].reference == 1
