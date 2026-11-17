"""Production escalation retains placement metadata for its selected winner.

Use real first-rung routing with controlled statistics to exercise dispatch
without making this control depend on congestion or routing latency.
"""

import json
from collections import Counter

import pytest

from kicad_tools import router as router_module
from kicad_tools.cli import route_cmd
from kicad_tools.schema.pcb import PCB
from tests.test_routing_placement_disposition import board_text


@pytest.mark.parametrize("minimum", ["1.0", "0.1"])
def test_production_layer_winner_keeps_placement(tmp_path, monkeypatch, minimum, capsys):
    source = tmp_path / "source.kicad_pcb"
    source.write_text(board_text().replace('"PLANE"', '"GND"'))
    original = source.read_bytes()
    output, report = tmp_path / "routed.kicad_pcb", tmp_path / "report.json"
    load = router_module.load_pcb_for_routing
    seen, selected = [], []
    choose = route_cmd.select_result

    def select(args, router):
        selected.append(router)
        return choose(args, router)

    monkeypatch.setattr(route_cmd, "select_result", select)
    monkeypatch.setattr(route_cmd, "_should_use_escape_routing", lambda *a: False)
    monkeypatch.setattr(route_cmd, "_auto_skip_pour_nets", lambda *a, **k: ([], []))

    def controlled_load(*a, **kw):
        router, mapping = load(*a, **kw)
        index = len(seen)
        seen.append((router, kw["placement_disposition"], kw["layer_stack"].num_layers))
        route = router.route_all
        stats = router.get_statistics

        def controlled_route(*a, **kw):
            if index == 0:
                route(*a, **kw)
                router.power_stall_abort = True
                router.power_stall_nets = ["GND"]
            return router.routes

        def controlled_stats(*a, **kw):
            result = stats(*a, **kw)
            result["nets_routed"] = 1 if index == 0 else 0
            return result

        monkeypatch.setattr(router, "route_all", controlled_route)
        monkeypatch.setattr(router, "get_statistics", controlled_stats)
        return router, mapping

    monkeypatch.setattr(router_module, "load_pcb_for_routing", controlled_load)
    rc = route_cmd.main(
        [
            str(source),
            "-o",
            str(output),
            "--backend",
            "python",
            "--strategy",
            "basic",
            "--max-layers",
            "4",
            "--min-completion",
            minimum,
            "--no-optimize",
            "--complete-report",
            str(report),
        ]
    )
    stdout = capsys.readouterr().out
    assert "Status: SUCCESS" not in stdout
    assert "Design routed successfully" not in stdout
    assert "Minimum viable configuration found" not in stdout
    assert rc != 0
    assert source.read_bytes() == original
    assert selected and selected[-1] is seen[0][0]
    assert seen[0][1].requested_invalid_nets == {"BAD"}
    if minimum == "1.0":
        assert len(seen) >= 2 and seen[0][2] == 2 and seen[1][2] == 4
        assert any("GND" in item[1].plane_excluded_nets for item in seen[1:])
    else:
        assert len(seen) == 1
    assert all(item[1].requested_invalid_nets == {"BAD"} for item in seen)
    disposition = json.loads(report.read_text())["placement_disposition"]
    assert disposition["requested_blocked_nets"] == ["BAD"]
    assert disposition["clean_success"] is False
    assert disposition["plane_excluded_nets"] == []
    before, after = PCB.load(source), PCB.load(output)

    def membership(pcb):
        return Counter((f.reference, p.number, p.net_name) for f in pcb.footprints for p in f.pads)

    assert membership(before) == membership(after)

    def copper(pcb):
        return Counter(
            (s.start, s.end, s.width, s.layer) for s in pcb.segments if s.net_name == "BAD"
        )

    assert copper(before) == copper(after)
    assert any(s.net_name == "GOOD" and s.start != s.end for s in after.segments)
