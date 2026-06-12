"""Tests for the layered differential pair detection (Issue #2558).

Covers all four detection paths plus tie-breaking, single-ended
refusal, power-rail false-positive prevention, and the _DP/_DN
base-name fix.

Test categories:

1. Suffix-only (regression).
2. Single-ended refusal list.
3. Power-rail filter (VCC_NEG / VBUS_POS etc.).
4. _DP/_DN base-name fix and collision avoidance.
5. Explicit declaration via NetClassRouting.diffpair_partner.
6. KiCad group via parse_diff_pair_templates_from_pcb().
7. Project diff_pairs via core.project_file.get_diff_pairs().
8. Layered tie-breaker: explicit > kicad_group > suffix.
9. Logging.
"""

from __future__ import annotations

import logging

import pytest

from kicad_tools.core.project_file import get_diff_pairs
from kicad_tools.router.diffpair import (
    is_single_ended_refused,
    parse_differential_signal,
)
from kicad_tools.router.diffpair_detection import (
    DetectionSource,
    detect_diff_pairs,
    parse_diff_pair_templates_from_pcb,
)
from kicad_tools.router.rules import NetClassRouting
from kicad_tools.sexp.parser import parse_string

# =============================================================================
# 1. Suffix-only (regression)
# =============================================================================


class TestSuffixOnlyRegression:
    """The legacy suffix path keeps working when no explicit / KiCad
    group sources are supplied."""

    def test_usb_d_plus_minus(self):
        net_names = {1: "USB_D+", 2: "USB_D-"}
        out = detect_diff_pairs(net_names)
        assert len(out) == 1
        assert out[0].source == DetectionSource.SUFFIX
        assert out[0].pair.name == "USB_D"
        assert out[0].pair.positive.net_name == "USB_D+"
        assert out[0].pair.negative.net_name == "USB_D-"

    def test_hdmi_pn_suffix(self):
        net_names = {1: "HDMI_TX0_P", 2: "HDMI_TX0_N"}
        out = detect_diff_pairs(net_names)
        assert len(out) == 1
        assert out[0].source == DetectionSource.SUFFIX
        assert out[0].pair.name == "HDMI_TX0"

    def test_clk_pos_neg(self):
        net_names = {1: "CLK_POS", 2: "CLK_NEG"}
        out = detect_diff_pairs(net_names)
        assert len(out) == 1
        assert out[0].source == DetectionSource.SUFFIX
        assert out[0].pair.name == "CLK"

    def test_no_pair_returns_empty(self):
        out = detect_diff_pairs({1: "VCC", 2: "GND", 3: "DATA0"})
        assert out == []

    def test_unpaired_p_signal_alone(self):
        # Only one half is present -- nothing to pair with.
        out = detect_diff_pairs({1: "USB_D+", 2: "VCC"})
        assert out == []


# =============================================================================
# 2. Single-ended refusal list
# =============================================================================


class TestSingleEndedRefusal:
    def test_usb_cc_refused(self):
        assert is_single_ended_refused("USB_CC1") is True
        assert is_single_ended_refused("USB_CC2") is True
        assert is_single_ended_refused("CC1") is True
        assert is_single_ended_refused("CC2") is True

    def test_sbu_refused(self):
        assert is_single_ended_refused("SBU1") is True
        assert is_single_ended_refused("SBU2") is True
        assert is_single_ended_refused("USB_SBU1") is True

    def test_normal_diff_not_refused(self):
        assert is_single_ended_refused("USB_D+") is False
        assert is_single_ended_refused("HDMI_TX0_P") is False
        assert is_single_ended_refused("CLK_POS") is False

    def test_parse_differential_signal_refuses_cc(self):
        assert parse_differential_signal("USB_CC1") is None
        assert parse_differential_signal("USB_CC2") is None

    def test_parse_differential_signal_refuses_sbu(self):
        assert parse_differential_signal("SBU1") is None
        assert parse_differential_signal("FOO_SBU2") is None

    def test_layered_detector_refuses_cc_pair(self):
        out = detect_diff_pairs({1: "USB_CC1", 2: "USB_CC2"})
        assert out == []

    def test_layered_detector_refuses_sbu_pair(self):
        out = detect_diff_pairs({1: "SBU1", 2: "SBU2"})
        assert out == []

    def test_layered_detector_refuses_prefix_cc_pair(self):
        out = detect_diff_pairs({1: "FOO_CC1", 2: "FOO_CC2"})
        assert out == []

    def test_explicit_declaration_overrides_refusal(self):
        # Designer can FORCE pairing of CC1/CC2 via explicit decl.
        net_names = {1: "USB_CC1", 2: "USB_CC2"}
        nc = NetClassRouting(name="USBCC", diffpair_partner="USB_CC2")
        out = detect_diff_pairs(
            net_names,
            net_class_routing={"USBCC": nc},
            net_to_class={"USB_CC1": "USBCC"},
        )
        assert len(out) == 1
        assert out[0].source == DetectionSource.EXPLICIT


# =============================================================================
# 3. Power-rail filter
# =============================================================================


class TestPowerRailFilter:
    def test_vcc_neg_not_a_pair(self):
        # VCC_NEG matches the POS/NEG suffix structurally but is a
        # power-rail name -- must not be pairable.
        assert parse_differential_signal("VCC_NEG") is None

    def test_vbus_pos_not_a_pair(self):
        assert parse_differential_signal("VBUS_POS") is None

    def test_vdd_neg_not_a_pair(self):
        assert parse_differential_signal("VDD_NEG") is None

    def test_clk_neg_still_works(self):
        # Non-power POS/NEG names still parse.
        result = parse_differential_signal("CLK_NEG")
        assert result is not None
        assert result[0] == "CLK"
        assert result[1] == "N"

    def test_layered_detector_skips_vcc_pair(self):
        out = detect_diff_pairs({1: "VCC_NEG", 2: "VCC_POS"})
        assert out == []

    def test_layered_detector_skips_vbus_pair(self):
        out = detect_diff_pairs({1: "VBUS_NEG", 2: "VBUS_POS"})
        assert out == []


# =============================================================================
# 4. _DP/_DN base-name fix
# =============================================================================


class TestDpDnBaseName:
    def test_usb_dp_base_includes_d(self):
        # Issue #2558 / A6: USB_DP must parse to base="USB_D".
        result = parse_differential_signal("USB_DP")
        assert result is not None
        base, polarity, notation = result
        assert base == "USB_D"
        assert polarity == "P"
        assert notation == "pn_suffix"

    def test_usb_dn_base_includes_d(self):
        result = parse_differential_signal("USB_DN")
        assert result is not None
        base, polarity, notation = result
        assert base == "USB_D"
        assert polarity == "N"

    def test_no_collision_with_plus_minus_pair(self):
        # Both pairs should be detected separately, no shared base.
        net_names = {
            1: "USB_D+",
            2: "USB_D-",
            3: "USB_DP",
            4: "USB_DN",
        }
        out = detect_diff_pairs(net_names)
        assert len(out) == 2
        # Each pair should claim distinct nets.
        all_ids: set[int] = set()
        for d in out:
            p_id, n_id = d.pair.get_net_ids()
            assert p_id not in all_ids
            assert n_id not in all_ids
            all_ids.add(p_id)
            all_ids.add(n_id)
        assert all_ids == {1, 2, 3, 4}


# =============================================================================
# 5. Explicit declaration
# =============================================================================


class TestExplicitDeclaration:
    def test_explicit_pair(self):
        net_names = {1: "CLK_A", 2: "CLK_B"}
        nc = NetClassRouting(name="CustomDiff", diffpair_partner="CLK_B")
        out = detect_diff_pairs(
            net_names,
            net_class_routing={"CustomDiff": nc},
            net_to_class={"CLK_A": "CustomDiff"},
        )
        assert len(out) == 1
        assert out[0].source == DetectionSource.EXPLICIT
        names = {out[0].pair.positive.net_name, out[0].pair.negative.net_name}
        assert names == {"CLK_A", "CLK_B"}

    def test_one_sided_declaration(self):
        # Only CLK_A's class declares CLK_B as partner; CLK_B's class
        # has no diffpair_partner.  Pair should still form.
        net_names = {1: "CLK_A", 2: "CLK_B"}
        nc_a = NetClassRouting(name="A", diffpair_partner="CLK_B")
        nc_b = NetClassRouting(name="B")  # no partner declared
        out = detect_diff_pairs(
            net_names,
            net_class_routing={"A": nc_a, "B": nc_b},
            net_to_class={"CLK_A": "A", "CLK_B": "B"},
        )
        assert len(out) == 1
        assert out[0].source == DetectionSource.EXPLICIT

    def test_bidirectional_declaration_emits_one_pair(self):
        # Both halves declare each other -- still one pair, not two.
        net_names = {1: "CLK_A", 2: "CLK_B"}
        nc_a = NetClassRouting(name="A", diffpair_partner="CLK_B")
        nc_b = NetClassRouting(name="B", diffpair_partner="CLK_A")
        out = detect_diff_pairs(
            net_names,
            net_class_routing={"A": nc_a, "B": nc_b},
            net_to_class={"CLK_A": "A", "CLK_B": "B"},
        )
        assert len(out) == 1

    def test_explicit_partner_missing_warns_and_skips(self, caplog):
        # Partner net doesn't exist in net_names -- skip with a warning.
        net_names = {1: "CLK_A"}
        nc = NetClassRouting(name="A", diffpair_partner="CLK_NONEXISTENT")
        with caplog.at_level(logging.WARNING):
            out = detect_diff_pairs(
                net_names,
                net_class_routing={"A": nc},
                net_to_class={"CLK_A": "A"},
            )
        assert out == []
        assert any("not in the net list" in r.message for r in caplog.records)


# =============================================================================
# 5b. Class-name-keyed fan-out pollution (Issue #3455)
# =============================================================================


class TestClassKeyedFanOut:
    """Issue #3455: a per-net partner annotation registered under its
    CLASS name must not fan out to every net of the class.

    Board-03 shape: ``USB_D+`` carries ``diffpair_partner='USB_D-'`` on
    a per-net ``NetClassRouting`` copy.  The autorouter's synth_routing
    idiom (``router/core.py``) registers that instance under the class
    name ``HighSpeed`` via ``setdefault``, after which the old
    class-keyed lookup paired EVERY HighSpeed net (USB_CC1, USB_CC2)
    with USB_D-.
    """

    _USB_NETS = {1: "USB_D+", 2: "USB_D-", 3: "USB_CC1", 4: "USB_CC2"}

    def _synth_routing_board03(self):
        """Mirror core.py's synth_routing idiom: net-name keys for every
        net plus a class-name key holding the FIRST net's instance
        (here: the annotated USB_D+ copy -- the worst case)."""
        plain = NetClassRouting(name="HighSpeed")
        annotated = NetClassRouting(name="HighSpeed", diffpair_partner="USB_D-")
        routing = {
            "USB_D+": annotated,
            "USB_D-": plain,
            "USB_CC1": plain,
            "USB_CC2": plain,
            "HighSpeed": annotated,  # setdefault winner = first net's instance
        }
        net_to_class = dict.fromkeys(self._USB_NETS.values(), "HighSpeed")
        return routing, net_to_class

    def test_cc_nets_not_polluted_by_class_keyed_annotation(self):
        routing, net_to_class = self._synth_routing_board03()
        out = detect_diff_pairs(
            self._USB_NETS,
            net_class_routing=routing,
            net_to_class=net_to_class,
        )
        assert len(out) == 1
        names = {out[0].pair.positive.net_name, out[0].pair.negative.net_name}
        assert names == {"USB_D+", "USB_D-"}
        assert out[0].source == DetectionSource.EXPLICIT

    def test_pure_class_keyed_map_selects_polarity_counterpart(self):
        # validate/match_group_skew convention: ONLY class-name keys.
        # The declaration can describe at most one pair; only the
        # polarity counterpart of USB_D- (USB_D+) may claim it.
        nc = NetClassRouting(name="HighSpeed", diffpair_partner="USB_D-")
        net_to_class = dict.fromkeys(self._USB_NETS.values(), "HighSpeed")
        out = detect_diff_pairs(
            self._USB_NETS,
            net_class_routing={"HighSpeed": nc},
            net_to_class=net_to_class,
        )
        assert len(out) == 1
        names = {out[0].pair.positive.net_name, out[0].pair.negative.net_name}
        assert names == {"USB_D+", "USB_D-"}

    def test_single_member_class_still_pairs_arbitrary_names(self):
        # Documented escape hatch: designers can pair USB-C CC1/CC2
        # explicitly.  A class with a single member net keeps working
        # even though CC2 has no polarity suffix.
        net_names = {1: "USB_CC1", 2: "USB_CC2"}
        nc = NetClassRouting(name="USBCC", diffpair_partner="USB_CC2")
        out = detect_diff_pairs(
            net_names,
            net_class_routing={"USBCC": nc},
            net_to_class={"USB_CC1": "USBCC"},
        )
        assert len(out) == 1
        names = {out[0].pair.positive.net_name, out[0].pair.negative.net_name}
        assert names == {"USB_CC1", "USB_CC2"}

    def test_multi_member_class_with_non_polarity_partner_refused(self):
        # Two members compete for a partner that has no polarity suffix
        # -- ambiguous, so nobody pairs.
        net_names = {1: "USB_CC1", 2: "SIG_X", 3: "USB_CC2"}
        nc = NetClassRouting(name="Misc", diffpair_partner="USB_CC2")
        out = detect_diff_pairs(
            net_names,
            net_class_routing={"Misc": nc},
            net_to_class={"USB_CC1": "Misc", "SIG_X": "Misc", "USB_CC2": "Misc"},
        )
        assert out == []

    def test_bus_indices_not_paired_by_class_declaration(self):
        # TX0/TX1 are bus indices, not polarity halves; a class-level
        # partner declaration must not couple them.
        net_names = {1: "TX0", 2: "TX1", 3: "USB_D-"}
        nc = NetClassRouting(name="HighSpeed", diffpair_partner="USB_D-")
        out = detect_diff_pairs(
            net_names,
            net_class_routing={"HighSpeed": nc},
            net_to_class={"TX0": "HighSpeed", "TX1": "HighSpeed"},
        )
        assert out == []

    def test_net_name_keyed_entry_remains_authoritative(self):
        # net-name-keyed declarations (autorouter net_class_map style)
        # are honored verbatim, including arbitrary partner names.
        net_names = {1: "USB_CC1", 2: "USB_CC2"}
        nc = NetClassRouting(name="USBCC", diffpair_partner="USB_CC2")
        out = detect_diff_pairs(
            net_names,
            net_class_routing={"USB_CC1": nc},
            net_to_class={"USB_CC1": "USBCC", "USB_CC2": "USBCC"},
        )
        assert len(out) == 1
        names = {out[0].pair.positive.net_name, out[0].pair.negative.net_name}
        assert names == {"USB_CC1", "USB_CC2"}

    def test_conflicting_declarations_keep_first_and_warn(self, caplog):
        # Two nets both name the same partner via net-name-keyed
        # entries: only one pair is emitted (a net belongs to at most
        # one pair) and the conflict is logged.
        net_names = {1: "USB_D+", 2: "USB_D-", 3: "USB_CC2"}
        nc_dp = NetClassRouting(name="A", diffpair_partner="USB_D-")
        nc_cc = NetClassRouting(name="B", diffpair_partner="USB_D-")
        with caplog.at_level(logging.WARNING):
            out = detect_diff_pairs(
                net_names,
                net_class_routing={"USB_D+": nc_dp, "USB_CC2": nc_cc},
                net_to_class={"USB_D+": "A", "USB_CC2": "B"},
            )
        assert len(out) == 1
        names = {out[0].pair.positive.net_name, out[0].pair.negative.net_name}
        assert names == {"USB_D+", "USB_D-"}
        assert any("conflicts" in r.message for r in caplog.records)

    def test_ambiguous_polarity_rivals_warn_and_skip(self, caplog):
        # Two distinct nets both parse as the polarity counterpart of
        # the declared partner -- the EXPLICIT declaration is ambiguous
        # and is skipped with a warning.  Suffix inference may still
        # pair USB_D+/USB_D- on its own merit afterwards.
        net_names = {1: "USB_D+", 2: "USB_D_P", 3: "USB_D-"}
        nc = NetClassRouting(name="HS", diffpair_partner="USB_D-")
        with caplog.at_level(logging.WARNING):
            out = detect_diff_pairs(
                net_names,
                net_class_routing={"HS": nc},
                net_to_class={"USB_D+": "HS", "USB_D_P": "HS", "USB_D-": "HS"},
            )
        assert all(p.source != DetectionSource.EXPLICIT for p in out)
        assert any("polarity counterparts" in r.message for r in caplog.records)


# =============================================================================
# 6. KiCad group
# =============================================================================


class TestKicadGroup:
    def test_pcb_diff_pair_template_parsed(self):
        sexp_text = """
        (kicad_pcb
            (version 20240108)
            (diff_pair_template
                (positive "USB_D+")
                (negative "USB_D-"))
            (diff_pair_template
                (positive "HDMI_P")
                (negative "HDMI_N")))
        """
        root = parse_string(sexp_text)
        pairs = parse_diff_pair_templates_from_pcb(root)
        assert ("USB_D+", "USB_D-") in pairs
        assert ("HDMI_P", "HDMI_N") in pairs
        assert len(pairs) == 2

    def test_pcb_round_trip_does_not_lose_directive(self):
        # After bc0c0eb7 round-trip smoke check: serializing then
        # re-parsing a PCB containing diff_pair_template must keep it.
        from kicad_tools.sexp.parser import serialize_sexp

        sexp_text = """
        (kicad_pcb
            (version 20240108)
            (diff_pair_template
                (positive "USB_D+")
                (negative "USB_D-")))
        """
        root = parse_string(sexp_text)
        serialized = serialize_sexp(root)
        assert "diff_pair_template" in serialized
        assert "USB_D+" in serialized
        assert "USB_D-" in serialized
        # Parse again and verify we still see the directive.
        root2 = parse_string(serialized)
        pairs = parse_diff_pair_templates_from_pcb(root2)
        assert ("USB_D+", "USB_D-") in pairs

    def test_layered_detector_uses_kicad_group(self):
        net_names = {1: "ARBITRARY_A", 2: "ARBITRARY_B"}
        kicad_groups = [("ARBITRARY_A", "ARBITRARY_B")]
        out = detect_diff_pairs(net_names, kicad_groups=kicad_groups)
        assert len(out) == 1
        assert out[0].source == DetectionSource.KICAD_GROUP

    def test_kicad_group_skipped_when_nets_missing(self):
        net_names = {1: "FOO"}  # 'BAR' is not present
        out = detect_diff_pairs(
            net_names,
            kicad_groups=[("FOO", "BAR")],
        )
        assert out == []


# =============================================================================
# 7. Project diff_pairs JSON field
# =============================================================================


class TestProjectDiffPairsField:
    def test_get_diff_pairs_empty_when_field_missing(self):
        data = {"net_settings": {}}
        assert get_diff_pairs(data) == []

    def test_get_diff_pairs_returns_list(self):
        data = {
            "net_settings": {
                "diff_pairs": [
                    {"p": "USB_D+", "n": "USB_D-"},
                    {"p": "CLK_P", "n": "CLK_N"},
                ],
            },
        }
        result = get_diff_pairs(data)
        assert {"p": "USB_D+", "n": "USB_D-"} in result
        assert {"p": "CLK_P", "n": "CLK_N"} in result
        assert len(result) == 2

    def test_get_diff_pairs_skips_malformed(self):
        data = {
            "net_settings": {
                "diff_pairs": [
                    {"p": "USB_D+", "n": "USB_D-"},
                    {"p": "MISSING_N"},  # malformed -- no n
                    "not_a_dict",  # wrong type
                ],
            },
        }
        result = get_diff_pairs(data)
        assert len(result) == 1
        assert result[0]["p"] == "USB_D+"

    def test_get_diff_pairs_when_settings_missing(self):
        # Field-absent project shouldn't crash.
        data: dict = {}
        assert get_diff_pairs(data) == []


# =============================================================================
# 8. Tie-breaker: explicit > kicad_group > suffix
# =============================================================================


class TestTieBreaker:
    def test_explicit_wins_over_suffix(self):
        # Suffix sees USB_D+ <-> USB_D-, but explicit declaration says
        # USB_D+ pairs with USB_OTHER.  Explicit must win.
        net_names = {1: "USB_D+", 2: "USB_D-", 3: "USB_OTHER"}
        nc = NetClassRouting(name="A", diffpair_partner="USB_OTHER")
        out = detect_diff_pairs(
            net_names,
            net_class_routing={"A": nc},
            net_to_class={"USB_D+": "A"},
        )
        # Find the pair that includes USB_D+ -- it must pair with USB_OTHER.
        plus_pair = next(
            d
            for d in out
            if d.pair.positive.net_name == "USB_D+" or d.pair.negative.net_name == "USB_D+"
        )
        partner = (
            plus_pair.pair.negative.net_name
            if plus_pair.pair.positive.net_name == "USB_D+"
            else plus_pair.pair.positive.net_name
        )
        assert partner == "USB_OTHER"
        assert plus_pair.source == DetectionSource.EXPLICIT
        # USB_D- should be left unpaired since USB_D+ is consumed.
        for d in out:
            assert "USB_D-" not in {
                d.pair.positive.net_name,
                d.pair.negative.net_name,
            }

    def test_explicit_wins_over_kicad_group(self):
        # KiCad group says USB_D+ <-> USB_D-, explicit says
        # USB_D+ <-> USB_OTHER.  Explicit wins.
        net_names = {1: "USB_D+", 2: "USB_D-", 3: "USB_OTHER"}
        nc = NetClassRouting(name="A", diffpair_partner="USB_OTHER")
        out = detect_diff_pairs(
            net_names,
            net_class_routing={"A": nc},
            net_to_class={"USB_D+": "A"},
            kicad_groups=[("USB_D+", "USB_D-")],
        )
        plus_pair = next(
            d
            for d in out
            if d.pair.positive.net_name == "USB_D+" or d.pair.negative.net_name == "USB_D+"
        )
        assert plus_pair.source == DetectionSource.EXPLICIT
        partner = (
            plus_pair.pair.negative.net_name
            if plus_pair.pair.positive.net_name == "USB_D+"
            else plus_pair.pair.positive.net_name
        )
        assert partner == "USB_OTHER"

    def test_kicad_group_wins_over_suffix(self):
        # Suffix would say USB_D+ <-> USB_D-, but KiCad group says
        # USB_D+ <-> USB_OTHER.  Group wins.
        net_names = {1: "USB_D+", 2: "USB_D-", 3: "USB_OTHER"}
        out = detect_diff_pairs(
            net_names,
            kicad_groups=[("USB_D+", "USB_OTHER")],
        )
        plus_pair = next(
            d
            for d in out
            if d.pair.positive.net_name == "USB_D+" or d.pair.negative.net_name == "USB_D+"
        )
        assert plus_pair.source == DetectionSource.KICAD_GROUP
        # USB_D- should be left unpaired.
        for d in out:
            assert "USB_D-" not in {
                d.pair.positive.net_name,
                d.pair.negative.net_name,
            }


# =============================================================================
# 9. Logging
# =============================================================================


class TestLogging:
    def test_each_source_emits_distinct_label(self, caplog):
        net_names = {
            1: "USB_D+",  # suffix path
            2: "USB_D-",
            3: "ARB_A",  # kicad group path
            4: "ARB_B",
            5: "EXPL_A",  # explicit path
            6: "EXPL_B",
        }
        nc = NetClassRouting(name="X", diffpair_partner="EXPL_B")
        with caplog.at_level(logging.INFO, logger="kicad_tools.router.diffpair_detection"):
            out = detect_diff_pairs(
                net_names,
                net_class_routing={"X": nc},
                net_to_class={"EXPL_A": "X"},
                kicad_groups=[("ARB_A", "ARB_B")],
            )
        sources = {d.source for d in out}
        assert sources == {
            DetectionSource.EXPLICIT,
            DetectionSource.KICAD_GROUP,
            DetectionSource.SUFFIX,
        }
        # Check log lines mention each source label.
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "source: explicit" in joined
        assert "source: kicad_group" in joined
        assert "source: suffix" in joined


# =============================================================================
# 10. Net-name reverse-mapping edge cases
# =============================================================================


class TestEdgeCases:
    def test_kicad_group_does_not_double_pair(self):
        # If the same nets appear in both kicad_groups and suffix
        # detection, only the higher-priority source counts.
        net_names = {1: "USB_D+", 2: "USB_D-"}
        out = detect_diff_pairs(
            net_names,
            kicad_groups=[("USB_D+", "USB_D-")],
        )
        assert len(out) == 1
        assert out[0].source == DetectionSource.KICAD_GROUP

    def test_empty_kicad_groups_list(self):
        net_names = {1: "USB_D+", 2: "USB_D-"}
        out = detect_diff_pairs(net_names, kicad_groups=[])
        assert len(out) == 1
        assert out[0].source == DetectionSource.SUFFIX

    def test_explicit_pair_orders_p_first(self):
        # When explicit decl uses recognizable polarity suffixes,
        # the positive side is the +/_P/_DP/_POS one regardless of
        # which side declared.
        net_names = {1: "DAT_N", 2: "DAT_P"}
        nc = NetClassRouting(name="X", diffpair_partner="DAT_P")
        out = detect_diff_pairs(
            net_names,
            net_class_routing={"X": nc},
            net_to_class={"DAT_N": "X"},
        )
        assert len(out) == 1
        assert out[0].pair.positive.net_name == "DAT_P"
        assert out[0].pair.negative.net_name == "DAT_N"


# =============================================================================
# 11. Backward-compat acceptance from issue body
# =============================================================================


class TestBackwardCompatAcceptance:
    def test_board_03_usb_dp_dm_via_suffix(self):
        # Mirrors what board 03 would surface: USB_D+/USB_D- alongside
        # USB_CC1/USB_CC2 (single-ended).  The diff pair is detected,
        # the CC pair is refused.
        net_names = {
            1: "USB_D+",
            2: "USB_D-",
            3: "USB_CC1",
            4: "USB_CC2",
            5: "VBUS",
            6: "GND",
        }
        out = detect_diff_pairs(net_names)
        assert len(out) == 1
        names = {out[0].pair.positive.net_name, out[0].pair.negative.net_name}
        assert names == {"USB_D+", "USB_D-"}
        assert out[0].source == DetectionSource.SUFFIX

    def test_no_kwargs_behaves_like_legacy(self):
        # When the caller passes only net_names, behaviour is identical
        # to the legacy detect_differential_pairs() suffix detector.
        from kicad_tools.router.diffpair import detect_differential_pairs as legacy

        net_names = {1: "USB_D+", 2: "USB_D-", 3: "ETH_TX+", 4: "ETH_TX-"}
        layered = detect_diff_pairs(net_names)
        legacy_pairs = legacy(net_names)
        assert len(layered) == len(legacy_pairs)
        layered_keys = {(d.pair.positive.net_name, d.pair.negative.net_name) for d in layered}
        legacy_keys = {(p.positive.net_name, p.negative.net_name) for p in legacy_pairs}
        assert layered_keys == legacy_keys


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
