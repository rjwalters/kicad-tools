"""Board 05 phase paths resolve and a peak-current mutation is caught (Issue #7210).

Root cause: the PHASE_* In2.Cu tracks end 0.35 um from the via centre
(x=20.9375 vs 20.938), so the exact-centreline resolver never joined the
via barrel to the inner-layer route and reported the path unresolved.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.manufacturers import DesignRules
from kicad_tools.router.current_paths import (
    CurrentPathSpec,
    PathEndpoint,
    resolve_current_path,
)
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.rules.path_ampacity import PathAmpacityRule

BOARD = (
    Path(__file__).resolve().parent.parent
    / "boards/05-bldc-motor-controller/output/bldc_controller_routed.kicad_pcb"
)
PHASES = {"PHASE_A": ("5", "1"), "PHASE_B": ("8", "2"), "PHASE_C": ("9", "3")}


@pytest.fixture(scope="module")
def pcb() -> PCB:
    return PCB.load(str(BOARD))


def _spec(net: str, continuous_a: float, pulsed_a: float | None = None) -> CurrentPathSpec:
    u2, j2 = PHASES[net]
    return CurrentPathSpec(
        name=net,
        net_name=net,
        source=PathEndpoint("U2", u2),
        sink=PathEndpoint("J2", j2),
        continuous_a=continuous_a,
        pulsed_a=pulsed_a,
    )


@pytest.mark.parametrize("net", sorted(PHASES))
def test_phase_path_resolves(pcb: PCB, net: str) -> None:
    resolution = resolve_current_path(pcb, _spec(net, 0.5, 1.0))
    assert resolution.status == "resolved", resolution.reason


@pytest.mark.parametrize("net", sorted(PHASES))
def test_peak_current_mutation_is_caught(pcb: PCB, net: str) -> None:
    rules = DesignRules(
        min_trace_width_mm=0.127,
        min_clearance_mm=0.127,
        min_via_drill_mm=0.2,
        min_via_diameter_mm=0.45,
        min_annular_ring_mm=0.13,
        outer_copper_oz=1.0,
        inner_copper_oz=0.5,
    )
    resolved = PathAmpacityRule([_spec(net, 0.1)]).check(pcb, rules)
    assert not any("unresolved" in e.message.lower() for e in resolved.errors)
    mutated = PathAmpacityRule([_spec(net, 0.1, 30.0)]).check(pcb, rules)
    assert len(mutated.errors) > len(resolved.errors)
