"""Unit tests for kicad_tools.placement.hv_domains (issue #4373).

Covers the voltage/domain input contract (Phase 0): deriving per-ref domains
from a voltage map or an --hv-domains declaration, and building the
per-domain-pair required-creepage table from the governing standard.
"""

from __future__ import annotations

import json

import pytest

from kicad_tools.creepage.standards import StandardLookupError
from kicad_tools.placement.cost import Net
from kicad_tools.placement.hv_domains import (
    build_required_by_domain_pair,
    derive_ref_domains_from_declaration,
    derive_ref_domains_from_voltage_map,
    detect_derived_tap_exempt_pairs,
    load_hv_domains,
    load_voltage_map,
)


class TestDeriveFromVoltageMap:
    def test_ref_domain_is_highest_voltage_net(self) -> None:
        """A ref touching a mains net lands in the mains domain (highest |V|)."""
        nets = [
            Net(name="/AC_LINE", pins=[("R1", "1"), ("U2", "3")]),
            Net(name="/REF_1V65", pins=[("U2", "4"), ("C5", "1")]),
        ]
        voltage_map = {"/AC_LINE": 150.0, "/REF_1V65": 1.65}
        ref_domains, domain_voltages = derive_ref_domains_from_voltage_map(nets, voltage_map)
        # U2 touches both nets -> resolves to the higher-voltage (mains) domain.
        assert ref_domains["U2"] == "/AC_LINE"
        assert ref_domains["R1"] == "/AC_LINE"
        assert ref_domains["C5"] == "/REF_1V65"
        assert domain_voltages["/AC_LINE"] == pytest.approx(150.0)
        assert domain_voltages["/REF_1V65"] == pytest.approx(1.65)

    def test_negative_voltage_uses_magnitude(self) -> None:
        nets = [Net(name="/VNEG", pins=[("U1", "1"), ("U2", "1")])]
        ref_domains, domain_voltages = derive_ref_domains_from_voltage_map(nets, {"/VNEG": -48.0})
        assert ref_domains["U1"] == "/VNEG"
        assert domain_voltages["/VNEG"] == pytest.approx(48.0)

    def test_nets_without_voltage_are_ignored(self) -> None:
        nets = [Net(name="/UNKNOWN", pins=[("U1", "1"), ("U2", "1")])]
        ref_domains, domain_voltages = derive_ref_domains_from_voltage_map(nets, {})
        assert ref_domains == {}
        assert domain_voltages == {}


class TestDeriveFromDeclaration:
    def test_globs_assign_domains(self) -> None:
        declaration = {
            "mains": {"refs": ["J1", "R1*"], "voltage": 150},
            "signal": {"refs": ["U3"], "voltage": 3.3},
        }
        refs = ["J1", "R10", "R11", "U3", "C9"]
        ref_domains, domain_voltages = derive_ref_domains_from_declaration(refs, declaration)
        assert ref_domains == {"J1": "mains", "R10": "mains", "R11": "mains", "U3": "signal"}
        assert "C9" not in ref_domains
        assert domain_voltages == {"mains": pytest.approx(150.0), "signal": pytest.approx(3.3)}

    def test_ref_matching_two_domains_takes_higher_voltage(self) -> None:
        declaration = {
            "mains": {"refs": ["R1"], "voltage": 150},
            "signal": {"refs": ["R1"], "voltage": 3.3},
        }
        ref_domains, _ = derive_ref_domains_from_declaration(["R1"], declaration)
        assert ref_domains["R1"] == "mains"


class TestBuildRequiredByDomainPair:
    def test_mains_signal_pair_uses_step_up_creepage(self) -> None:
        # |150 - 1.65| ~ 148 V -> steps up to the 160 V row -> 1.6 mm at PD2/IIIa.
        required = build_required_by_domain_pair(
            {"mains": 150.0, "signal": 1.65},
            standard_id="iec60664",
            pollution_degree=2,
            material_group="IIIa",
        )
        assert required[("mains", "signal")] == pytest.approx(1.6)

    def test_pairs_below_threshold_are_omitted(self) -> None:
        # 3.3 V vs 1.65 V -> |dV| 1.65 < 30 V threshold -> no keepout entry.
        required = build_required_by_domain_pair(
            {"a": 3.3, "b": 1.65},
            hv_threshold=30.0,
        )
        assert required == {}

    def test_keys_are_order_independent(self) -> None:
        required = build_required_by_domain_pair({"z": 150.0, "a": 1.65})
        # sorted() -> key is ("a", "z"), not ("z", "a").
        assert ("a", "z") in required

    def test_out_of_range_voltage_raises(self) -> None:
        # |ΔV| above the highest tabulated creepage row must fail loud.
        with pytest.raises(StandardLookupError):
            build_required_by_domain_pair({"hv": 5000.0, "gnd": 0.0})


class TestLoaders:
    def test_load_voltage_map(self, tmp_path) -> None:
        p = tmp_path / "v.json"
        p.write_text(json.dumps({"/AC_LINE": 150, "/REF": 1.65}))
        assert load_voltage_map(p) == {"/AC_LINE": 150.0, "/REF": 1.65}

    def test_load_voltage_map_rejects_non_numeric(self, tmp_path) -> None:
        p = tmp_path / "v.json"
        p.write_text(json.dumps({"/AC_LINE": "high"}))
        with pytest.raises(ValueError):
            load_voltage_map(p)

    def test_load_voltage_map_rejects_bool(self, tmp_path) -> None:
        p = tmp_path / "v.json"
        p.write_text(json.dumps({"/AC_LINE": True}))
        with pytest.raises(ValueError):
            load_voltage_map(p)

    def test_load_hv_domains(self, tmp_path) -> None:
        p = tmp_path / "d.json"
        p.write_text(json.dumps({"mains": {"refs": ["J1"], "voltage": 150}}))
        assert load_hv_domains(p) == {"mains": {"refs": ["J1"], "voltage": 150}}

    def test_load_hv_domains_rejects_bad_refs(self, tmp_path) -> None:
        p = tmp_path / "d.json"
        p.write_text(json.dumps({"mains": {"refs": "J1"}}))
        with pytest.raises(ValueError):
            load_hv_domains(p)


class TestDetectDerivedTapExemptPairs:
    """Phase 3 (auto): derived-tap detection -> auto exempt_pairs (#4373)."""

    def _fixture_nets(self):
        """AC_LINE (HV) and V_AC_SENSE_RAW (LV tap) share divider ref R1.

        R1 bridges both nets; C5 is the signal-side footprint on the tap only;
        J1 is a second mains-domain footprint on AC_LINE.
        """
        return [
            Net(name="/AC_LINE", pins=[("R1", "1"), ("J1", "2")]),
            Net(name="/V_AC_SENSE_RAW", pins=[("R1", "2"), ("C5", "1")]),
        ]

    def _fixture_voltage_map(self):
        return {"/AC_LINE": 150.0, "/V_AC_SENSE_RAW": 1.65}

    def test_detects_tap_and_returns_cross_domain_pairs(self) -> None:
        nets = self._fixture_nets()
        vm = self._fixture_voltage_map()
        ref_domains, _ = derive_ref_domains_from_voltage_map(nets, vm)
        # R1 resolves to the mains domain; C5 to the tap domain.
        assert ref_domains["R1"] == "/AC_LINE"
        assert ref_domains["C5"] == "/V_AC_SENSE_RAW"

        exempt, advisories = detect_derived_tap_exempt_pairs(
            nets, ref_domains, vm, hv_threshold=30.0
        )
        # The signal-side C5 is exempted from BOTH mains-domain refs it faces.
        assert frozenset({"C5", "R1"}) in exempt
        assert frozenset({"C5", "J1"}) in exempt
        # One advisory naming both the tap and its parent.
        assert len(advisories) == 1
        assert "/V_AC_SENSE_RAW" in advisories[0]
        assert "/AC_LINE" in advisories[0]

    def test_bridging_ref_does_not_self_exempt(self) -> None:
        """R1 (mains domain) is never exempted against another mains ref."""
        nets = self._fixture_nets()
        vm = self._fixture_voltage_map()
        ref_domains, _ = derive_ref_domains_from_voltage_map(nets, vm)
        exempt, _ = detect_derived_tap_exempt_pairs(nets, ref_domains, vm)
        assert frozenset({"R1", "J1"}) not in exempt

    def test_lv_net_sharing_no_hv_ref_yields_no_exemption(self) -> None:
        nets = [
            Net(name="/AC_LINE", pins=[("R1", "1"), ("J1", "2")]),
            # Isolated LV net -- no ref in common with any HV net.
            Net(name="/REF_1V65", pins=[("U9", "1"), ("C9", "1")]),
        ]
        vm = {"/AC_LINE": 150.0, "/REF_1V65": 1.65}
        ref_domains, _ = derive_ref_domains_from_voltage_map(nets, vm)
        exempt, advisories = detect_derived_tap_exempt_pairs(nets, ref_domains, vm)
        assert exempt == set()
        assert advisories == []

    def test_unrelated_hv_net_is_not_exempted_against_tap(self) -> None:
        """A second HV domain (BANK+) sharing no ref with the tap keeps keepout."""
        nets = self._fixture_nets() + [
            Net(name="/BANK+", pins=[("Q1", "1"), ("D1", "1")]),
        ]
        vm = {"/AC_LINE": 150.0, "/V_AC_SENSE_RAW": 1.65, "/BANK+": 400.0}
        ref_domains, _ = derive_ref_domains_from_voltage_map(nets, vm)
        exempt, _ = detect_derived_tap_exempt_pairs(nets, ref_domains, vm)
        # C5 (tap) is NOT exempted against the unrelated BANK+ domain refs.
        assert frozenset({"C5", "Q1"}) not in exempt
        assert frozenset({"C5", "D1"}) not in exempt
        # ...but still exempt against its own parent.
        assert frozenset({"C5", "R1"}) in exempt

    def test_empty_voltage_map_is_empty(self) -> None:
        exempt, advisories = detect_derived_tap_exempt_pairs([], {}, {})
        assert exempt == set()
        assert advisories == []

    def test_threshold_boundary_net_is_hv_not_tap(self) -> None:
        """A net exactly at the threshold classifies as HV, so it is no tap."""
        nets = [
            Net(name="/AC_LINE", pins=[("R1", "1"), ("J1", "2")]),
            Net(name="/MID", pins=[("R1", "2"), ("C5", "1")]),
        ]
        vm = {"/AC_LINE": 150.0, "/MID": 30.0}
        ref_domains, _ = derive_ref_domains_from_voltage_map(nets, vm)
        exempt, advisories = detect_derived_tap_exempt_pairs(
            nets, ref_domains, vm, hv_threshold=30.0
        )
        # /MID at |V| == threshold is HV, not an LV tap -> no exemption.
        assert exempt == set()
        assert advisories == []

    def test_deterministic_output(self) -> None:
        nets = self._fixture_nets()
        vm = self._fixture_voltage_map()
        ref_domains, _ = derive_ref_domains_from_voltage_map(nets, vm)
        a = detect_derived_tap_exempt_pairs(nets, ref_domains, vm)
        b = detect_derived_tap_exempt_pairs(nets, ref_domains, vm)
        assert a == b
