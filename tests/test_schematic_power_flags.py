"""PWR_FLAG declares a driver, never a global net name (#5015)."""

import pytest

from kicad_tools.schematic.models import Schematic
from kicad_tools.schematic.models.elements import Label, PowerSymbol, Wire


def flagged_rail(rail):
    sch = Schematic("External power")
    sch.wires.extend([Wire(100, 100, 109, 100), Wire(103, 100, 112, 100)])
    sch.labels.append(Label(text=rail, x=100, y=100))
    sch.power_symbols.extend(
        [PowerSymbol(f"power:{rail}", 100, 100), PowerSymbol("power:PWR_FLAG", 112, 100)]
    )
    return sch


@pytest.mark.parametrize("rail", ["+5V", "GND", "+3.3V"])
def test_flag_does_not_name_or_short_external_rail(rail):
    sch = flagged_rail(rail)
    assert not [i for i in sch.validate() if i["type"] == "collinear_net_conflict"]
    assert sch.get_statistics()["power_nets"] == [rail]
    _, names, _ = sch._build_connectivity_graph()
    assert "PWR_FLAG" not in {name for values in names.values() for name in values}


def test_flag_does_not_hide_real_rail_short():
    sch = flagged_rail("+5V")
    sch.labels.append(Label(text="GND", x=112, y=100))
    conflicts = [i for i in sch.validate() if i["type"] == "collinear_net_conflict"]
    assert conflicts
    assert "+5V" in conflicts[0]["message"] and "GND" in conflicts[0]["message"]


def test_synthesized_rail_kept_in_statistics():
    sch = flagged_rail("+3.3V")
    sch.power_symbols[0].lib_id = "kicad_tools_pwr:+3.3V"
    assert sch.get_statistics()["power_nets"] == ["+3.3V"]


def test_flagged_wire_allows_same_net_route():
    sch = flagged_rail("+5V")
    assert not sch._stub_would_collide((103, 100), (112, 100), "+5V")


@pytest.mark.parametrize("rail", ["+5V", "GND"])
def test_flag_drives_connected_rail_only(rail):
    sch = Schematic("External driver")
    sch.wires.append(Wire(100, 80, 100, 100))
    sch.power_symbols.extend(
        [
            PowerSymbol(f"power:{rail}", 100, 100),
            PowerSymbol("power:PWR_FLAG", 100, 80),
            PowerSymbol("power:+3.3V", 150, 100),
        ]
    )
    issues = sch.validate_power_nets()
    assert not [issue for issue in issues if issue.net in {rail, "PWR_FLAG"}]
    assert [issue for issue in issues if issue.net == "+3.3V"]
