"""Issue #6669: a ``--net-class-map`` *pour* net is never left without copper.

Board 06rf's sidecar declares ``VBUS_USB`` a pour (``is_pour_net=True``,
``route_via="pour"``), but the name classifier reads it as HIGH_SPEED (it
contains ``USB``).  Auto-pour and auto-skip classified by name only, so no
zone was made and the net never reached ``_pour_nets_without_zones``; the
router, which reads the merged map, then skipped it as a pour net.  4 of its
5 pads were stranded.

Invariant: after auto-pour and auto-skip, a net the router treats as a pour
net either has a zone or is in ``no_zone_nets`` (routed as traces, #1841).
The inverse direction (#6599, map-routed beats name-pour) is unchanged.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.router.net_class import NetClass
from kicad_tools.router.rules import NetClassRouting
from tests.test_auto_pour import _make_pcb

_NETS = [(1, "GND"), (2, "VBUS_USB"), (3, "SIG")]


def _board(tmp_path: Path, zones: list[str] | None = None) -> Path:
    pcb_path = tmp_path / "board.kicad_pcb"
    pcb_path.write_text(_make_pcb(net_defs=_NETS, pad_nets=_NETS + [(3, "SIG")], zones=zones))
    return pcb_path


def _zone_nets(pcb_path: Path) -> set[str]:
    text = pcb_path.read_text()
    nets = set(re.findall(r'\(zone\s+.*?\(net_name\s+"([^"]+)"\)', text, re.DOTALL))
    nets |= set(re.findall(r'\(zone\s[^)]*\(net\s+"([^"]+)"\)', text))
    return nets


def _pour_map() -> dict[str, NetClassRouting]:
    return {
        "GND": NetClassRouting(name="Ground", is_pour_net=True, route_via="pour"),
        "VBUS_USB": NetClassRouting(name="Power", is_pour_net=True, route_via="pour"),
        "SIG": NetClassRouting(name="Default"),
    }


def _zone(net_id: int, name: str) -> str:
    return (
        f'(zone (net {net_id}) (net_name "{name}") (layer "B.Cu") (hatch edge 0.5) '
        "(polygon (pts (xy 0 0) (xy 50 0) (xy 50 50) (xy 0 50))))"
    )


def test_name_classifier_misses_vbus_usb():
    """Precondition: the name heuristic alone does not see a pour."""
    from kicad_tools.router.auto_pour import classify_pour_candidates

    pour, _signals, _all_power = classify_pour_candidates({1: "GND", 2: "VBUS_USB", 3: "SIG"})
    assert [n for n, _ in pour] == ["GND"]


def test_map_pour_net_is_a_pour_candidate():
    from kicad_tools.router.auto_pour import classify_pour_candidates

    pour, signals, all_power = classify_pour_candidates(
        {1: "GND", 2: "VBUS_USB", 3: "SIG"}, net_class_map=_pour_map()
    )
    assert dict(pour) == {"GND": NetClass.GROUND, "VBUS_USB": NetClass.POWER}
    assert signals == 1
    assert all_power is False


def test_map_pour_applies_to_unclassified_net():
    """A name the classifier cannot classify at all still honours the map."""
    from kicad_tools.router.auto_pour import classify_pour_candidates

    ncm = {"NODE_X": NetClassRouting(name="Power", is_pour_net=True)}
    pour, signals, _ = classify_pour_candidates(
        {1: "GND", 2: "NODE_X", 3: "SIG"}, net_class_map=ncm
    )
    assert "NODE_X" in dict(pour)
    assert signals == 1


def test_auto_pour_creates_zone_for_map_pour_net(tmp_path: Path):
    from kicad_tools.router.auto_pour import auto_pour_if_missing

    pcb_path = _board(tmp_path)
    _count, names = auto_pour_if_missing(pcb_path, net_class_map=_pour_map())
    assert sorted(names) == ["GND", "VBUS_USB"]
    assert _zone_nets(pcb_path) == {"GND", "VBUS_USB"}


def test_auto_pour_without_map_is_unchanged(tmp_path: Path):
    from kicad_tools.router.auto_pour import auto_pour_if_missing

    pcb_path = _board(tmp_path)
    _count, names = auto_pour_if_missing(pcb_path)
    assert names == ["GND"]


def test_map_routed_power_net_still_gets_no_zone(tmp_path: Path):
    """#6599 direction is unchanged: a map-routed power-named net is routed."""
    from kicad_tools.router.auto_pour import auto_pour_if_missing

    pcb_path = tmp_path / "board.kicad_pcb"
    nets = [(1, "GND"), (2, "VBUS"), (3, "SIG")]
    pcb_path.write_text(_make_pcb(net_defs=nets, pad_nets=nets + [(3, "SIG")]))
    ncm = {"VBUS": NetClassRouting(name="Power", is_pour_net=False, route_via="pathfinder")}
    _count, names = auto_pour_if_missing(pcb_path, net_class_map=ncm)
    assert names == ["GND"]


def test_auto_skip_skips_map_pour_net_with_zone(tmp_path: Path):
    from kicad_tools.router.auto_pour import auto_skip_pour_nets

    pcb_path = _board(tmp_path, zones=[_zone(1, "GND"), _zone(2, "VBUS_USB")])
    skip: list[str] = []
    skipped, no_zone = auto_skip_pour_nets(pcb_path, skip, quiet=True, net_class_map=_pour_map())
    assert sorted(skipped) == ["GND", "VBUS_USB"]
    assert no_zone == []


def test_auto_skip_hands_back_zoneless_map_pour_net(tmp_path: Path):
    """No zone could be made: the net must be routed as traces (#1841)."""
    from kicad_tools.router.auto_pour import auto_skip_pour_nets

    pcb_path = _board(tmp_path, zones=[_zone(1, "GND")])
    skip: list[str] = []
    skipped, no_zone = auto_skip_pour_nets(pcb_path, skip, quiet=True, net_class_map=_pour_map())
    assert skipped == ["GND"]
    assert no_zone == ["VBUS_USB"]


def test_auto_skip_backstop_is_pour_net_with_pathfinder(tmp_path: Path):
    """``is_pour_net=True`` + ``route_via="pathfinder"``: the router's
    ``_is_pour_net`` still skips it, so a zoneless one must be handed back."""
    from kicad_tools.router.auto_pour import auto_skip_pour_nets

    pcb_path = _board(tmp_path, zones=[_zone(1, "GND")])
    ncm = {"VBUS_USB": NetClassRouting(name="Power", is_pour_net=True, route_via="pathfinder")}
    skipped, no_zone = auto_skip_pour_nets(pcb_path, [], quiet=True, net_class_map=ncm)
    assert "VBUS_USB" not in skipped
    assert "VBUS_USB" in no_zone


def test_router_never_skips_a_zoneless_pour_net(tmp_path: Path):
    """End-to-end invariant over auto-pour + auto-skip + the router predicate."""
    from kicad_tools.router.auto_pour import auto_pour_if_missing, auto_skip_pour_nets

    ncm = _pour_map()
    pcb_path = _board(tmp_path)
    auto_pour_if_missing(pcb_path, net_class_map=ncm)
    skip: list[str] = []
    _skipped, no_zone = auto_skip_pour_nets(pcb_path, skip, quiet=True, net_class_map=ncm)
    zones = _zone_nets(pcb_path)
    for name, routing in ncm.items():
        router_skips = routing.is_pour_net and name not in no_zone
        if router_skips:
            assert name in zones, f"{name} skipped by the router with no zone"


class _Stop(BaseException):
    """Abort ``kct route`` right after auto-skip is called."""


@pytest.mark.parametrize("extra_args", [["--no-auto-layers"], ["--auto-layers"]])
def test_route_flows_forward_map_to_auto_skip(tmp_path: Path, extra_args):
    from kicad_tools.cli import route_cmd

    pcb_path = _board(tmp_path)
    map_path = tmp_path / "net_class_map.json"
    map_path.write_text(
        json.dumps({"VBUS_USB": {"name": "Power", "is_pour_net": True, "route_via": "pour"}})
    )
    seen: dict = {}

    def _capture(*_a, **kwargs):
        seen.update(kwargs)
        raise _Stop

    with (
        patch("kicad_tools.router.auto_pour.auto_pour_if_missing", return_value=(0, [])),
        patch("kicad_tools.cli.route_cmd._auto_skip_pour_nets", side_effect=_capture),
    ):
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

    ncm = seen.get("net_class_map")
    assert ncm is not None
    assert ncm["VBUS_USB"].is_pour_net is True
