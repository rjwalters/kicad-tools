"""Issue #6561: the router's fab clearance floor matches the emitted ``.kicad_dru``.

``MfrLimits.min_clearance`` is flat (jlcpcb 0.127mm for every stackup), while
the manufacturer profile -- what ``kct check --mfr`` enforces and what the
shipped / generated ``.kicad_dru`` ``Clearance`` rule carries -- is per
``<layers>layer_<oz>oz`` (jlcpcb 4-layer 0.1016, 6-layer 0.0889, and 2-layer
2oz 0.1524mm, which the flat value undercut).

The chosen single source of truth is the profile:
:func:`~kicad_tools.router.mfr_limits.resolve_clearance` returns the profile's
``min_clearance_mm`` whenever the stackup is known, and the flat value only as
the layer-agnostic fallback.  These tests pin that the router floor equals the
DRU floor for every jlcpcb stackup, in both directions.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.manufacturers import get_profile
from kicad_tools.manufacturers.dru_generator import generate_dru
from kicad_tools.router.mfr_limits import MFR_JLCPCB, resolve_clearance
from kicad_tools.router.rules import DesignRules, NetClassRouting

RULES_DIR = Path(__file__).resolve().parents[1] / "src" / "kicad_tools" / "manufacturers" / "rules"

_KEY_RE = re.compile(r"^(\d+)layer_(\d+)oz$")
_CLEARANCE_RE = re.compile(
    r'\(rule "Clearance - [^"]*"\s*\(constraint clearance \(min ([0-9.]+)mm\)\)\)'
)


def _stackup_keys(mfr: str) -> list[tuple[int, int]]:
    keys = []
    for key in get_profile(mfr).design_rules:
        match = _KEY_RE.match(key)
        if match:
            keys.append((int(match.group(1)), int(match.group(2))))
    return sorted(keys)


def _dru_clearance(text: str) -> float:
    match = _CLEARANCE_RE.search(text)
    assert match, "no general 'Clearance - <mfr>' rule in the .kicad_dru"
    return float(match.group(1))


JLCPCB_CASES = [("jlcpcb", layers, oz) for layers, oz in _stackup_keys("jlcpcb")]
TIER1_CASES = [("jlcpcb-tier1", layers, oz) for layers, oz in _stackup_keys("jlcpcb-tier1")]


def test_every_jlcpcb_stackup_is_covered() -> None:
    """Guard against the parametrization silently going empty."""
    assert {(layers, oz) for _, layers, oz in JLCPCB_CASES} >= {
        (2, 1),
        (2, 2),
        (4, 1),
        (4, 2),
        (6, 1),
    }
    assert TIER1_CASES


@pytest.mark.parametrize(("mfr", "layers", "oz"), JLCPCB_CASES)
def test_router_floor_equals_shipped_dru(mfr: str, layers: int, oz: int) -> None:
    """The router floor is the committed ``.kicad_dru`` clearance, exactly."""
    path = RULES_DIR / f"{mfr}-{layers}layer-{oz}oz.kicad_dru"
    assert path.is_file(), f"missing shipped rules file {path.name}"
    assert resolve_clearance(mfr, layers, oz) == pytest.approx(
        _dru_clearance(path.read_text()), abs=1e-9
    )


@pytest.mark.parametrize(("mfr", "layers", "oz"), JLCPCB_CASES + TIER1_CASES)
def test_router_floor_equals_generated_dru(mfr: str, layers: int, oz: int) -> None:
    """The router floor is the clearance ``generate_dru`` emits for the stackup.

    jlcpcb-tier1 ships no ``.kicad_dru`` of its own, so this is its parity
    check against the rules ``kct`` writes for it.
    """
    rules = get_profile(mfr).get_design_rules(layers, float(oz))
    emitted = _dru_clearance(generate_dru(rules, manufacturer_name="JLCPCB"))
    assert resolve_clearance(mfr, layers, oz) == pytest.approx(emitted, abs=1e-9)


@pytest.mark.parametrize("mfr", ["jlcpcb", "jlcpcb-tier1", "jlcpcb_tier1"])
def test_two_layer_two_oz_never_below_dru(mfr: str) -> None:
    """The unsafe row: 2-layer 2oz must not resolve below 0.1524mm."""
    assert resolve_clearance(mfr, 2, 2) == pytest.approx(0.1524)
    assert resolve_clearance(mfr, 2, 2.0) >= 0.1524 - 1e-9


@pytest.mark.parametrize(("layers", "expected"), [(4, 0.1016), (6, 0.0889)])
def test_multilayer_floor_follows_the_fab(layers: int, expected: float) -> None:
    """4/6-layer floors are the fab's, not the stricter flat 2-layer value."""
    assert resolve_clearance("jlcpcb", layers, 1) == pytest.approx(expected)


def test_unknown_stackup_keeps_the_flat_fallback() -> None:
    """No layer count -> the pre-#6561 flat value, never a guessed stackup."""
    assert resolve_clearance("jlcpcb") == MFR_JLCPCB.min_clearance
    assert resolve_clearance("jlcpcb", None, 2.0) == MFR_JLCPCB.min_clearance
    assert resolve_clearance(None, 4, 1) is None
    assert resolve_clearance("no-such-fab", 4, 1) is None


def _diff_class(gap: float) -> NetClassRouting:
    return NetClassRouting(
        name="USB", trace_width=0.2, clearance=0.15, intra_pair_clearance=gap, coupled_routing=True
    )


def test_pair_gap_uses_the_stackup_floor_on_six_layers() -> None:
    """``pair_gap`` floors at the 6-layer jlcpcb value when it is threaded."""
    floor = resolve_clearance("jlcpcb", 6, 1)
    rules = DesignRules(manufacturer="jlcpcb", mfr_clearance_floor=floor)
    # A class gap between the 6-layer floor and the flat 0.127 is honoured.
    resolved = rules.pair_gap(_diff_class(0.1))
    assert resolved.gap_mm == pytest.approx(0.1)
    assert not resolved.raised
    # A class gap below the 6-layer floor is raised to it, naming the fab.
    resolved = rules.pair_gap(_diff_class(0.05))
    assert resolved.gap_mm == pytest.approx(0.0889)
    assert resolved.source == "jlcpcb minimum clearance"


def test_pair_gap_raises_to_two_oz_floor() -> None:
    rules = DesignRules(
        manufacturer="jlcpcb", mfr_clearance_floor=resolve_clearance("jlcpcb", 2, 2)
    )
    assert rules.pair_gap(_diff_class(0.13)).gap_mm == pytest.approx(0.1524)


def test_pair_gap_without_stackup_keeps_flat_floor() -> None:
    rules = DesignRules(manufacturer="jlcpcb")
    assert rules.pair_gap(_diff_class(0.1)).gap_mm == pytest.approx(MFR_JLCPCB.min_clearance)


def _write_board(tmp_path: Path, layers: int) -> Path:
    names = ["F.Cu"] + [f"In{i}.Cu" for i in range(1, layers - 1)] + ["B.Cu"]
    ids = [0] + list(range(1, layers - 1)) + [31]
    layer_lines = "\n".join(
        f'    ({i} "{name}" signal)' for i, name in zip(ids, names, strict=True)
    )
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(
        "(kicad_pcb (version 20240108) (generator test)\n"
        f'  (layers\n{layer_lines}\n    (44 "Edge.Cuts" user)\n  )\n'
        '  (net 0 "")\n)\n'
    )
    return pcb


@pytest.mark.parametrize(("layers", "expected"), [(2, 0.127), (4, 0.1016), (6, 0.0889)])
def test_route_cli_floor_follows_board_stackup(
    tmp_path: Path, layers: int, expected: float
) -> None:
    """``kct route``'s floor resolver reads the board's layer count."""
    from kicad_tools.cli.route_cmd import _resolve_route_clearance_floor

    pcb = _write_board(tmp_path, layers)
    args = SimpleNamespace(manufacturer="jlcpcb", layers="auto", copper=None)
    assert _resolve_route_clearance_floor(args, pcb) == pytest.approx(expected)


def test_route_cli_floor_honours_copper_flag(tmp_path: Path) -> None:
    from kicad_tools.cli.route_cmd import _resolve_route_clearance_floor

    pcb = _write_board(tmp_path, 2)
    args = SimpleNamespace(manufacturer="jlcpcb", layers="auto", copper="2")
    assert _resolve_route_clearance_floor(args, pcb) == pytest.approx(0.1524)


def test_route_auto_resolver_reports_stackup_floor(tmp_path: Path) -> None:
    """``resolve_routing_clearances`` (route-auto / MCP) uses the same floor."""
    from kicad_tools.router.clearance_resolver import resolve_routing_clearances

    pcb = _write_board(tmp_path, 6)
    resolved = resolve_routing_clearances(
        pcb, target_mm=0.05, manufacturer="jlcpcb", discover_manufacturer=False
    )
    assert resolved.fab_floor_mm == pytest.approx(0.0889)
    assert resolved.trace_mm == pytest.approx(0.0889)
