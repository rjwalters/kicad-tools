"""#8527: per-net ampacity targets must not become class-wide DRU floors."""

from __future__ import annotations

from dataclasses import replace

from kicad_tools.manufacturers import get_profile
from kicad_tools.manufacturers.dru_generator import generate_dru
from kicad_tools.manufacturers.project_generator import dedupe_net_classes
from kicad_tools.physics.ampacity import width_for_current
from kicad_tools.router.rules import NetClassRouting


def _map(targets: dict[str, float | None], cls: str = "HV"):
    return {n: NetClassRouting(name=cls, target_ampacity=t) for n, t in targets.items()}


def test_one_net_in_class_of_five_is_net_scoped():
    nets = {f"N{i}": None for i in range(5)}
    nets["N2"] = 15.0
    classes = dedupe_net_classes(_map(nets))
    rules = get_profile("jlcpcb").get_design_rules(4, 1.0)
    dru = generate_dru(rules, "jlcpcb", net_classes=classes)
    assert "A.NetName == 'N2'" in dru
    assert "A.NetClass == 'HV'" not in dru
    for n in ("N0", "N1", "N3", "N4"):
        assert f"'{n}'" not in dru


def test_distinct_targets_get_distinct_widths():
    classes = dedupe_net_classes(_map({"A": 15.0, "B": 15.0, "C": 25.0}))
    rules = get_profile("jlcpcb").get_design_rules(4, 1.0)
    dru = generate_dru(rules, "jlcpcb", net_classes=classes)
    w15 = width_for_current(15.0, copper_weight_oz=rules.outer_copper_oz, layer="external")
    w25 = width_for_current(25.0, copper_weight_oz=rules.outer_copper_oz, layer="external")
    assert f"(min {w15:.4f}mm)" in dru and f"(min {w25:.4f}mm)" in dru
    assert "A.NetName == 'A' || A.NetName == 'B'" in dru


def test_uniform_class_target_stays_class_wide():
    classes = dedupe_net_classes(_map({"A": 5.0, "B": 5.0}))
    assert len(classes) == 1 and not classes[0].net_scope
    rules = get_profile("jlcpcb").get_design_rules(4, 1.0)
    assert "A.NetClass == 'HV'" in generate_dru(rules, "jlcpcb", net_classes=classes)


def test_two_oz_outer_gives_narrower_external_width():
    rules = replace(get_profile("jlcpcb").get_design_rules(4, 1.0), outer_copper_oz=2.0)
    dru = generate_dru(
        rules, "jlcpcb", net_classes=[NetClassRouting(name="P", target_ampacity=25.0)]
    )
    expected = width_for_current(25.0, copper_weight_oz=2.0, layer="external")
    assert f"(min {expected:.4f}mm)" in dru
    assert expected < 13


def test_net_name_with_quote_is_escaped():
    classes = dedupe_net_classes(_map({"it's": 3.0, "B": None}))
    rules = get_profile("jlcpcb").get_design_rules(2, 1.0)
    assert "A.NetName == 'it\\'s'" in generate_dru(rules, "jlcpcb", net_classes=classes)
