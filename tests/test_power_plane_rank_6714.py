"""Issue #6714: the inner power plane goes to the most widespread rail.

On a 4-layer single-ground board with several power pours, the one inner
power plane (``In2.Cu``) used to go to whichever power net the classifier
listed first -- net-number order.  Once #6686 made board 06rf's ``VBUS_USB``
(net 1, five pads on J1/U1) a pour, it took the plane, and the board-wide
``+3V3`` (eleven pads on six parts) was pushed onto a carved ``F.Cu``
outline.  Connector ``J3``'s +3V3 pin then sat inside the ``+1V2`` F.Cu
pour with no plane to drop a via to, and the welds of its GND neighbours
could only land in the +1V2 sliver beside the pin row: 2 pour links stayed
open after the oracle.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from kicad_tools.router.net_class import NetClass
from kicad_tools.schema.pcb import PCB
from kicad_tools.zones.generator import _assign_layers_for_pour_nets, _power_plane_rank

REPO = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO / "boards" / "06-diffpair-test" / "regression-fixture"

POUR_NETS = [
    ("VBUS_USB", NetClass.POWER),
    ("+3V3", NetClass.POWER),
    ("+1V8", NetClass.POWER),
    ("+1V2", NetClass.POWER),
    ("GND", NetClass.GROUND),
]


def _layers(assignments: list[tuple[str, str, int]]) -> dict[str, tuple[str, int]]:
    return {net: (layer, prio) for net, layer, prio in assignments}


class TestPlaneRankAssignment:
    def test_without_rank_first_power_net_keeps_the_plane(self) -> None:
        """No rank: the legacy rule (first listed power net) is unchanged."""
        got = _layers(_assign_layers_for_pour_nets(4, POUR_NETS))
        assert got["VBUS_USB"] == ("In2.Cu", 0)
        assert got["+3V3"] == ("F.Cu", 1)

    def test_widest_rail_takes_the_plane(self) -> None:
        # 06rf's footprint counts.
        rank = {
            "VBUS_USB": (2.0,),
            "+3V3": (6.0,),
            "+1V8": (2.0,),
            "+1V2": (3.0,),
            "GND": (20.0,),
        }
        got = _layers(_assign_layers_for_pour_nets(4, POUR_NETS, plane_rank=rank))
        assert got["GND"] == ("In1.Cu", 1)
        assert got["+3V3"] == ("In2.Cu", 0)
        # The rest keep their listed order (and so their relative priority)
        # on F.Cu, with distinct priorities.
        assert got["VBUS_USB"] == ("F.Cu", 1)
        assert got["+1V8"] == ("F.Cu", 2)
        assert got["+1V2"] == ("F.Cu", 3)

    def test_tie_keeps_listed_order(self) -> None:
        nets = [("GND", NetClass.GROUND), ("A", NetClass.POWER), ("B", NetClass.POWER)]
        rank = {"A": (5.0,), "B": (5.0,)}
        got = _layers(_assign_layers_for_pour_nets(4, nets, plane_rank=rank))
        assert got["A"] == ("In2.Cu", 0)

    def test_unranked_net_loses_to_ranked_one(self) -> None:
        nets = [("GND", NetClass.GROUND), ("A", NetClass.POWER), ("B", NetClass.POWER)]
        got = _layers(_assign_layers_for_pour_nets(4, nets, plane_rank={"B": (1.0,)}))
        assert got["B"] == ("In2.Cu", 0)

    @pytest.mark.parametrize("layers", [2, 4])
    def test_rank_ignored_where_there_is_no_plane_choice(self, layers: int) -> None:
        """2-layer boards and single-power-net boards are unchanged."""
        nets = [("GND", NetClass.GROUND), ("A", NetClass.POWER)]
        rank = {"A": (1.0,)}
        assert _assign_layers_for_pour_nets(
            layers, nets, plane_rank=rank
        ) == _assign_layers_for_pour_nets(layers, nets)
        two = [("GND", NetClass.GROUND), ("A", NetClass.POWER), ("B", NetClass.POWER)]
        if layers == 2:
            assert _assign_layers_for_pour_nets(
                2, two, plane_rank={"B": (9.0,)}
            ) == _assign_layers_for_pour_nets(2, two)

    def test_ground_only_board_with_rank(self) -> None:
        nets = [("GND", NetClass.GROUND)]
        assert _assign_layers_for_pour_nets(4, nets, plane_rank={"GND": (1.0,)}) == [
            ("GND", "In1.Cu", 1)
        ]

    def test_split_ground_unchanged(self) -> None:
        nets = [
            ("GNDA", NetClass.GROUND),
            ("GNDD", NetClass.GROUND),
            ("A", NetClass.POWER),
            ("B", NetClass.POWER),
        ]
        rank = {"B": (9.0,)}
        assert _assign_layers_for_pour_nets(
            4, nets, plane_rank=rank
        ) == _assign_layers_for_pour_nets(4, nets)


class TestBoard06rf:
    def test_rank_of_06rf_power_nets(self) -> None:
        pcb = PCB.load(str(FIXTURE_DIR / "diffpair_test.kicad_pcb"))
        rank = {net: _power_plane_rank(pcb, net) for net, _ in POUR_NETS}
        assert rank["+3V3"] == (6.0,)  # J1, J3, U1, U2, U3, U4
        assert rank["VBUS_USB"] == (2.0,)  # J1, U1
        assert max(("VBUS_USB", "+3V3", "+1V8", "+1V2"), key=rank.__getitem__) == "+3V3"
        assert _power_plane_rank(pcb, "NO_SUCH_NET") == (0.0,)

    def test_route_auto_pour_puts_3v3_on_the_inner_plane(self, tmp_path: Path) -> None:
        """``kct route``'s auto-pour on 06rf: +3V3 owns In2.Cu, VBUS_USB is F.Cu."""
        from kicad_tools.router.auto_pour import auto_pour_if_missing
        from kicad_tools.router.rules import net_class_map_from_path

        board = tmp_path / "diffpair_test.kicad_pcb"
        shutil.copy(FIXTURE_DIR / "diffpair_test.kicad_pcb", board)
        net_class_map = net_class_map_from_path(FIXTURE_DIR / "net_class_map.json")
        created, _ = auto_pour_if_missing(
            board, quiet=True, edge_clearance=0.3, net_class_map=net_class_map
        )
        assert created == 5
        zones = {(z.net_name, z.layer) for z in PCB.load(str(board)).zones}
        assert ("GND", "In1.Cu") in zones
        assert ("+3V3", "In2.Cu") in zones
        assert ("VBUS_USB", "F.Cu") in zones
        assert {layer for net, layer in zones if net == "+3V3"} == {"In2.Cu"}


def test_07rf_tie_keeps_the_listed_plane_net(tmp_path: Path) -> None:
    """Board 07rf: +1V2 and +1V8 both feed 5 parts, so +1V2 keeps In2.Cu.

    Breaking that tie by pad spread or pad count moved the plane to +1V8
    and stranded the +1V2 BGA ball U4.C2 on the F.Cu pour (0 -> 2 open pour
    links on the lattice bench), which is why the rank is the footprint
    count alone.
    """
    from kicad_tools.router.auto_pour import auto_pour_if_missing
    from kicad_tools.router.rules import net_class_map_from_path

    fixture = REPO / "boards" / "07-matchgroup-test" / "regression-fixture"
    board = tmp_path / "matchgroup_test.kicad_pcb"
    shutil.copy(fixture / "matchgroup_test.kicad_pcb", board)
    pcb = PCB.load(str(board))
    assert _power_plane_rank(pcb, "+1V2") == _power_plane_rank(pcb, "+1V8") == (5.0,)
    auto_pour_if_missing(
        board,
        quiet=True,
        edge_clearance=0.3,
        net_class_map=net_class_map_from_path(fixture / "net_class_map.json"),
    )
    zones = {(z.net_name, z.layer) for z in PCB.load(str(board)).zones}
    assert ("+1V2", "In2.Cu") in zones
    assert ("+1V8", "F.Cu") in zones
