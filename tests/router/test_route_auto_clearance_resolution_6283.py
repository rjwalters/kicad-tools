"""``kct route-auto`` / MCP ``route_net`` resolve clearance from the project (Issue #6283).

Before #6283 both tools built their :class:`~kicad_tools.router.rules.DesignRules`
from ``ParsedDesignRules.to_design_rules()``, which reads the ``.kicad_pcb``
alone: a ``.kicad_pro`` board minimum or a ``.kicad_dru`` via rule that
``kicad-cli pcb drc`` enforces was invisible to them.  ``kct route`` already
resolved both values (#6280); this pins the other half of the
Autorouter / RoutingOrchestrator split onto the same resolver.

Cases:

(a) a ``.kicad_pro`` board minimum raises both trace and via clearance;
(b) a ``.kicad_dru`` via-scoped rule raises only ``via_clearance``, and an
    end-to-end ``route_net_auto`` run keeps traces that far off a foreign via;
(c) no sidecars and no discoverable fab give today's values exactly;
(d) a sub-floor declaration is raised to the discovered fab floor, with a
    warning surfaced in the tool's ``warnings``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.router.clearance_resolver import (
    RuleSource,
    resolve_routing_clearances,
)

PCB_MIN_CLEARANCE = 0.15

VIA_RULE_DRU = (
    "(version 1)\n"
    '(rule "Via clearance"\n'
    "  (condition \"A.Type == 'Via' || B.Type == 'Via'\")\n"
    "  (constraint clearance (min {mm}mm)))\n"
)

_VIA_OFFSET_MM = 0.57
_VIA_DIAMETER = 0.6


def _pad(ref: str, uid: int, x: float, y: float, net: int, name: str) -> str:
    return f"""  (footprint "TestPoint:TP"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000{uid:02d}")
    (at {x} {y})
    (property "Reference" "{ref}" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "TP" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net {net} "{name}"))
  )
"""


def _board(
    tmp_path: Path,
    *,
    project: dict | None = None,
    dru: str | None = None,
    target_fab: str | None = None,
) -> Path:
    """SIG runs west-east past a lone AUX via ``_VIA_OFFSET_MM`` off its line.

    The ``.kicad_pcb`` declares ``PCB_MIN_CLEARANCE`` in a legacy
    ``(net_class …)`` block -- the only value ``to_design_rules()`` sees.
    """
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(
        f"""(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "SIG")
  (net 2 "AUX")
  (net_class "Default" "" (clearance {PCB_MIN_CLEARANCE}) (trace_width 0.2)
    (via_dia {_VIA_DIAMETER}) (via_drill 0.3) (add_net "SIG") (add_net "AUX"))
  (gr_rect (start 100 100) (end 130 110)
    (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
{_pad("TP1", 10, 104.0, 105, 1, "SIG")}{_pad("TP2", 11, 126.0, 105, 1, "SIG")}{_pad("TP3", 12, 115.0, 108.5, 2, "AUX")}  (via (at 115 {105 + _VIA_OFFSET_MM}) (size {_VIA_DIAMETER}) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2) (uuid "00000000-0000-0000-0000-000000000099"))
)
"""
    )
    if project is not None:
        pcb.with_suffix(".kicad_pro").write_text(json.dumps(project))
    if dru is not None:
        pcb.with_suffix(".kicad_dru").write_text(dru)
    if target_fab is not None:
        (tmp_path / "project.kct").write_text(
            "kct_version: '1.0'\n"
            "project:\n"
            "  name: t\n"
            "requirements:\n"
            "  manufacturing:\n"
            f"    target_fab: {target_fab}\n"
        )
    return pcb


def _project(*, netclass: float = 0.15, min_clearance: float | None = None) -> dict:
    project: dict = {"net_settings": {"classes": [{"name": "Default", "clearance": netclass}]}}
    if min_clearance is not None:
        project["board"] = {"design_settings": {"rules": {"min_clearance": min_clearance}}}
    return project


def _pcb_only_rules(pcb: Path):
    from kicad_tools.router.io import parse_pcb_design_rules

    return parse_pcb_design_rules(pcb.read_text())


def _route_net_rules(pcb: Path, monkeypatch) -> object:
    """The ``DesignRules`` the MCP ``route_net`` tool hands to its Autorouter."""
    import kicad_tools.router as router_pkg
    from kicad_tools.mcp.tools.routing import route_net

    seen: dict = {}

    class _Stop(Exception):
        pass

    class _SpyRouter:
        def __init__(self, *, rules, **_kwargs):
            seen["rules"] = rules
            raise _Stop

    monkeypatch.setattr(router_pkg, "Autorouter", _SpyRouter)
    with pytest.raises(_Stop):
        route_net(str(pcb), "SIG")
    return seen["rules"]


# ---------------------------------------------------------------------------
# (a) / (c) / (d): the shared helper
# ---------------------------------------------------------------------------


def test_pcb_min_clearance_is_what_to_design_rules_sees(tmp_path) -> None:
    """Guard the fixture: the board's own setup declares 0.15mm."""
    assert _pcb_only_rules(_board(tmp_path)).min_clearance == pytest.approx(PCB_MIN_CLEARANCE)


def test_a_project_board_minimum_raises_trace_and_via(tmp_path) -> None:
    pcb = _board(tmp_path, project=_project(min_clearance=0.2))

    resolved = resolve_routing_clearances(pcb, target_mm=PCB_MIN_CLEARANCE)

    assert resolved.trace_mm == pytest.approx(0.2)
    assert resolved.via_mm == pytest.approx(0.2)
    assert resolved.trace.source == RuleSource.PROJECT_MIN_CLEARANCE
    assert resolved.manufacturer is None
    assert resolved.warnings == []


def test_a_dru_via_rule_raises_only_the_via_clearance(tmp_path) -> None:
    pcb = _board(tmp_path, project=_project(), dru=VIA_RULE_DRU.format(mm=0.3))

    resolved = resolve_routing_clearances(pcb, target_mm=PCB_MIN_CLEARANCE)

    assert resolved.trace_mm == pytest.approx(PCB_MIN_CLEARANCE)
    assert resolved.via_mm == pytest.approx(0.3)
    assert resolved.via.source == RuleSource.PROJECT_DRU_VIA


def test_no_sidecars_and_no_fab_keep_todays_values_exactly(tmp_path) -> None:
    pcb = _board(tmp_path)
    before = _pcb_only_rules(pcb).to_design_rules()

    resolved = resolve_routing_clearances(pcb, target_mm=_pcb_only_rules(pcb).min_clearance)

    # Byte-identical: the same float, not merely approximately equal.
    assert resolved.trace_mm == before.trace_clearance
    assert resolved.via_mm == before.via_clearance
    assert resolved.warnings == []


def test_an_unreadable_dru_contributes_nothing(tmp_path) -> None:
    pcb = _board(tmp_path, dru="(this is (not a valid rule file")

    resolved = resolve_routing_clearances(pcb, target_mm=PCB_MIN_CLEARANCE)

    assert resolved.trace_mm == pytest.approx(PCB_MIN_CLEARANCE)
    assert resolved.via_mm == pytest.approx(PCB_MIN_CLEARANCE)


def test_a_sub_floor_declaration_is_raised_to_the_discovered_fab(tmp_path) -> None:
    from kicad_tools.router.mfr_limits import resolve_clearance

    floor = resolve_clearance("oshpark")
    assert floor is not None and floor > 0.1
    pcb = _board(tmp_path, project=_project(netclass=0.1), target_fab="oshpark")

    resolved = resolve_routing_clearances(pcb, target_mm=0.1)

    assert resolved.manufacturer == "oshpark"
    assert resolved.trace_mm == pytest.approx(floor)
    assert resolved.via_mm == pytest.approx(floor)
    assert resolved.warnings, "a sub-floor declaration must not be raised silently"
    assert "oshpark" in resolved.warnings[0]


# ---------------------------------------------------------------------------
# The MCP route_net tool (Autorouter path)
# ---------------------------------------------------------------------------


def test_route_net_builds_rules_from_the_project(tmp_path, monkeypatch) -> None:
    pcb = _board(tmp_path, project=_project(min_clearance=0.2), dru=VIA_RULE_DRU.format(mm=0.3))

    rules = _route_net_rules(pcb, monkeypatch)

    assert rules.trace_clearance == pytest.approx(0.2)
    assert rules.via_clearance == pytest.approx(0.3)


def test_route_net_without_sidecars_is_unchanged(tmp_path, monkeypatch) -> None:
    pcb = _board(tmp_path)
    before = _pcb_only_rules(pcb).to_design_rules()

    rules = _route_net_rules(pcb, monkeypatch)

    assert rules.trace_clearance == before.trace_clearance
    assert rules.via_clearance == before.via_clearance


# ---------------------------------------------------------------------------
# route_net_auto (RoutingOrchestrator path)
# ---------------------------------------------------------------------------


def _orchestrator_rules(pcb: Path, monkeypatch):
    from kicad_tools.mcp.tools.routing import route_net_auto
    from kicad_tools.router import orchestrator as orch_mod

    seen: dict = {}
    original_init = orch_mod.RoutingOrchestrator.__init__

    def _spy_init(self, *args, **kwargs):
        seen["rules"] = kwargs.get("rules")
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(orch_mod.RoutingOrchestrator, "__init__", _spy_init)
    result = route_net_auto(str(pcb), "SIG", strategy="global", enable_repair=False)
    return seen["rules"], result


def test_route_net_auto_routes_at_the_declared_values(tmp_path, monkeypatch) -> None:
    pcb = _board(tmp_path, project=_project(min_clearance=0.2), dru=VIA_RULE_DRU.format(mm=0.3))

    rules, _ = _orchestrator_rules(pcb, monkeypatch)

    assert rules.trace_clearance == pytest.approx(0.2)
    assert rules.via_clearance == pytest.approx(0.3)


def test_route_net_auto_surfaces_the_fab_floor_warning(tmp_path, monkeypatch) -> None:
    pcb = _board(tmp_path, project=_project(netclass=0.1), target_fab="oshpark")

    _, result = _orchestrator_rules(pcb, monkeypatch)

    assert any("oshpark" in w for w in result["warnings"]), result["warnings"]


def test_route_net_auto_keeps_traces_off_a_foreign_via_under_a_dru_rule(tmp_path) -> None:
    """Mirror of the #6280 end-to-end case, through ``route_net_auto``.

    A regression guard rather than a discriminating test: the hierarchical
    strategy ``auto`` lands on already raises its per-net clearance to the
    board's per-pair requirement (#6122, ``_net_required_clearance``), so
    this held before #6283 too.  What #6283 changes -- the base
    ``DesignRules`` every strategy and the output gate start from -- is
    pinned by ``test_route_net_auto_routes_at_the_declared_values``.
    """
    from kicad_tools.mcp.tools.routing import route_net_auto

    from .test_via_clearance_resolution_6280 import _sig_gap_to_foreign_via

    pcb = _board(tmp_path, project=_project(), dru=VIA_RULE_DRU.format(mm=0.3))
    out = tmp_path / "routed.kicad_pcb"

    result = route_net_auto(str(pcb), "SIG", output_path=str(out))

    assert result["success"], result
    gap = _sig_gap_to_foreign_via(out)
    assert gap >= 0.3 - 1e-6, f"SIG passes {gap:.4f}mm from a foreign via under a 0.30mm rule"
