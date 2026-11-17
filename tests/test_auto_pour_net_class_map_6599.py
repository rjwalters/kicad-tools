"""Issue #6599: ``kct route`` auto-pour honours ``--net-class-map``.

Board 03's sidecar declares VBUS routed (``is_pour_net=False``,
``route_via="pathfinder"``), yet auto-pour used only the name heuristic and
turned VBUS into a pour, silently overriding the designer.  The precedence
rule is now: a net the map declares routed is never auto-poured (and the
skip is logged); nets absent from the map keep the name heuristic; a net the
caller forces via ``--skip-nets`` is still poured.
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.router.rules import NetClassRouting
from tests.test_auto_pour import _make_pcb

_NETS = [(1, "VCC"), (2, "VBUS"), (3, "SIG")]


def _board(tmp_path: Path) -> Path:
    pcb_path = tmp_path / "board.kicad_pcb"
    pcb_path.write_text(_make_pcb(net_defs=_NETS, pad_nets=_NETS + [(3, "SIG")]))
    return pcb_path


def _zone_nets(pcb_path: Path) -> set[str]:
    text = pcb_path.read_text()
    nets = set(re.findall(r'\(zone\s+.*?\(net_name\s+"([^"]+)"\)', text, re.DOTALL))
    nets |= set(re.findall(r'\(zone\s[^)]*\(net\s+"([^"]+)"\)', text))
    return nets


def _routed_vbus_map() -> dict[str, NetClassRouting]:
    return {
        "VCC": NetClassRouting(name="Power", is_pour_net=True, route_via="pour"),
        "VBUS": NetClassRouting(name="Power", is_pour_net=False, route_via="pathfinder"),
    }


class TestAutoPourHonoursNetClassMap:
    def test_map_routed_net_is_not_poured_and_logged(self, tmp_path: Path, capsys):
        from kicad_tools.router.auto_pour import auto_pour_if_missing

        pcb_path = _board(tmp_path)
        count, names = auto_pour_if_missing(pcb_path, net_class_map=_routed_vbus_map())

        assert names == ["VCC"]
        assert count == 1
        assert _zone_nets(pcb_path) == {"VCC"}
        out = capsys.readouterr().out
        assert "Auto-pour: skipping VBUS (net-class map declares it routed)" in out

    def test_without_map_both_power_nets_are_poured(self, tmp_path: Path, capsys):
        """Control: map-less users see no change (VBUS is heuristic POWER)."""
        from kicad_tools.router.auto_pour import auto_pour_if_missing

        pcb_path = _board(tmp_path)
        _count, names = auto_pour_if_missing(pcb_path)

        assert sorted(names) == ["VBUS", "VCC"]
        assert _zone_nets(pcb_path) == {"VBUS", "VCC"}
        assert "net-class map" not in capsys.readouterr().out

    def test_manual_route_via_is_also_not_poured(self, tmp_path: Path):
        from kicad_tools.router.auto_pour import auto_pour_if_missing

        pcb_path = _board(tmp_path)
        ncm = {"VBUS": NetClassRouting(name="Manual", route_via="manual")}
        _count, names = auto_pour_if_missing(pcb_path, net_class_map=ncm)
        assert names == ["VCC"]

    def test_is_pour_net_true_keeps_heuristic_pour(self, tmp_path: Path):
        """``is_pour_net=True`` is a pour declaration, not a routed one."""
        from kicad_tools.router.auto_pour import auto_pour_if_missing

        pcb_path = _board(tmp_path)
        ncm = {"VBUS": NetClassRouting(name="Power", is_pour_net=True, route_via="pathfinder")}
        _count, names = auto_pour_if_missing(pcb_path, net_class_map=ncm)
        assert sorted(names) == ["VBUS", "VCC"]

    def test_force_pour_nets_outrank_the_map(self, tmp_path: Path, capsys):
        """An explicit ``--skip-nets`` pour request still wins over the map."""
        from kicad_tools.router.auto_pour import auto_pour_if_missing

        pcb_path = _board(tmp_path)
        _count, names = auto_pour_if_missing(
            pcb_path, net_class_map=_routed_vbus_map(), force_pour_nets=["VBUS"]
        )
        assert sorted(names) == ["VBUS", "VCC"]
        assert "skipping VBUS" not in capsys.readouterr().out

    def test_map_routed_nets_count_as_signals_for_all_power_guard(self):
        """A routed-declared power net is routed, so it is a signal net."""
        from kicad_tools.router.auto_pour import classify_pour_candidates

        names = {1: "VIN", 2: "GND"}
        ncm = {"VIN": NetClassRouting(name="Power", route_via="pathfinder")}
        dropped: list[str] = []
        pour, signals, all_power = classify_pour_candidates(
            names, net_class_map=ncm, map_routed_out=dropped
        )
        assert [n for n, _ in pour] == ["GND"]
        assert signals == 1
        assert all_power is False
        assert dropped == ["VIN"]


class _Stop(BaseException):
    """Abort ``kct route`` right after auto-pour is called."""


@pytest.mark.parametrize(
    ("extra_args", "flow"),
    [
        (["--no-auto-layers"], "_run_main_impl"),
        (["--auto-layers"], "route_with_layer_escalation"),
    ],
)
def test_route_flows_forward_loaded_net_class_map(tmp_path: Path, extra_args, flow):
    """``kct route`` passes the parsed ``--net-class-map`` into auto-pour."""
    from kicad_tools.cli import route_cmd

    pcb_path = _board(tmp_path)
    map_path = tmp_path / "net_class_map.json"
    map_path.write_text(
        json.dumps(
            {
                "VBUS": {"name": "Power", "is_pour_net": False, "route_via": "pathfinder"},
                "VCC": {"name": "Power", "is_pour_net": True, "route_via": "pour"},
            }
        )
    )
    seen: dict = {}

    def _capture(*_a, **kwargs):
        seen.update(kwargs)
        seen["_caller"] = next(
            fr.function for fr in inspect.stack() if fr.filename.endswith("route_cmd.py")
        )
        raise _Stop

    with patch("kicad_tools.router.auto_pour.auto_pour_if_missing", side_effect=_capture):
        with pytest.raises(_Stop):
            route_cmd.main(
                [
                    str(pcb_path),
                    "-o",
                    str(tmp_path / "out.kicad_pcb"),
                    "--net-class-map",
                    str(map_path),
                    "--quiet",
                    *extra_args,
                ]
            )

    assert seen["_caller"] == flow
    ncm = seen.get("net_class_map")
    assert ncm is not None, f"{flow} flow did not forward net_class_map"
    assert ncm["VBUS"].route_via == "pathfinder"
    assert ncm["VBUS"].is_pour_net is False
