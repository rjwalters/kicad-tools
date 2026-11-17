"""Regression tests for #6171: generate_dru on 2-layer profiles with ampacity."""

from __future__ import annotations

import pytest

from kicad_tools.manufacturers import get_manufacturer_ids, get_profile
from kicad_tools.manufacturers.dru_generator import generate_dru
from kicad_tools.physics.ampacity import width_for_current
from kicad_tools.router.rules import NetClassRouting


def _two_layer_cases():
    cases = []
    for mid in get_manufacturer_ids():
        profile = get_profile(mid)
        for key, rules in profile.design_rules.items():
            if rules.inner_copper_oz <= 0:
                cases.append((mid, key, rules))
    return cases


CASES = _two_layer_cases()


def test_cases_cover_two_layer_profiles():
    assert len(CASES) >= 11


@pytest.mark.parametrize("mid,key,rules", CASES, ids=[f"{c[0]}-{c[1]}" for c in CASES])
def test_two_layer_ampacity_emits_outer_only(mid, key, rules):
    nc = NetClassRouting(name="Power", target_ampacity=2.0)
    dru = generate_dru(rules, mid, net_classes=[nc])
    expected = width_for_current(2.0, copper_weight_oz=rules.outer_copper_oz, layer="external")
    assert "Ampacity Min Width (Power, external)" in dru
    assert f"(min {expected:.4f}mm)" in dru
    assert "internal" not in dru.lower().split("ampacity min width (power,")[-1]
    assert "Ampacity Min Width (Power, internal)" not in dru


def test_four_layer_still_emits_internal():
    rules = get_profile("jlcpcb").get_design_rules(4, 1.0)
    dru = generate_dru(
        rules, "jlcpcb", net_classes=[NetClassRouting(name="P", target_ampacity=2.0)]
    )
    assert "(P, external)" in dru and "(P, internal)" in dru


def test_validator_skips_inner_on_two_layer():
    from types import SimpleNamespace

    from kicad_tools.validate.rules.ampacity import AmpacityRule

    rules = get_profile("jlcpcb").get_design_rules(2, 1.0)
    rule = AmpacityRule({"N": 2.0})
    pcb = SimpleNamespace(
        segments=[
            SimpleNamespace(net_name="N", layer="In1.Cu", width=0.1, start=(0, 0), end=(1, 0))
        ]
    )
    assert not list(rule.check(pcb, rules).violations)
