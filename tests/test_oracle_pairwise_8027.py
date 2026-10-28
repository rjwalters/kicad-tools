"""Issue #8027: oracle pour completion honours the --voltage-map pairwise table."""

from types import SimpleNamespace

from kicad_tools.cli.route_cmd import _audited_trace_copper, _record_oracle_copper
from kicad_tools.router.oracle_completion import PourLinkCloser
from kicad_tools.router.pairwise_clearance import PairwiseClearanceTable
from kicad_tools.router.primitives import Layer, Route, Segment, Via


def _table() -> PairwiseClearanceTable:
    return PairwiseClearanceTable(
        dru=0.2,
        net_voltages={"GND": 0.0, "HV": 150.0},
        required_by_pair={("GND", "HV"): 1.6},
    )


def _closer(table=None) -> PourLinkCloser:
    return PourLinkCloser(
        via_size=0.6, via_drill=0.3, clearance=0.2, trace_width=0.2, pairwise_table=table
    )


def test_pair_clearance_unchanged_without_table():
    assert _closer().pair_clearance("GND", "HV") == 0.2


def test_pair_clearance_uses_table():
    closer = _closer(_table())
    assert closer.pair_clearance("GND", "HV") == 1.6
    assert closer.pair_clearance("GND", "/HV") == 1.6
    assert closer.pair_clearance("GND", "OTHER") == 0.2
    assert closer.pair_clearance("GND", None) == 0.2


def test_link_rules_carry_pairwise_clearance():
    from kicad_tools.router.link_router import LinkRouteRules

    pcb = SimpleNamespace(nets={1: SimpleNamespace(name="GND"), 2: SimpleNamespace(name="HV")})
    rules = _closer(_table()).link_rules(LinkRouteRules(), pcb, "GND")
    assert rules.clearance_by_net[2] == 1.6
    assert rules.clearance_by_net[1] == 0.2


def test_weld_clearance_raised_for_hv_neighbour_even_without_class_map(tmp_path):
    closer = _closer(_table())
    # Unreadable board: conservative path must still return the HV requirement.
    got = closer.weld_clearance(tmp_path / "missing.kicad_pcb", {"GND": {"U1.1"}})
    assert got == 1.6


def test_audit_includes_oracle_vias(tmp_path):
    pcb = tmp_path / "out.kicad_pcb"
    pcb.write_text(
        '(kicad_pcb (net 0 "") (net 1 "GND")\n'
        '  (via (at 10 10) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))\n'
        '  (segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu") (net 1)))\n'
    )
    known = Route(
        net=1,
        net_name="GND",
        segments=[Segment(0, 0, 1, 0, 0.2, Layer.F_CU, net=1, net_name="GND")],
        vias=[],
    )
    router = SimpleNamespace(routes=[known], _emitted_preserved_routes=[])
    _record_oracle_copper(router, pcb)
    audited = _audited_trace_copper(router, id_to_name={1: "GND"})
    vias = [v for r in audited for v in r.vias]
    assert [(v.x, v.y) for v in vias] == [(10.0, 10.0)]
    # The already-routed segment is not double counted.
    assert sum(len(r.segments) for r in audited) == 1
    assert isinstance(vias[0], Via)
