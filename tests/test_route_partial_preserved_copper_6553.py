"""Every ``kct route`` flow's ``_partial`` keeps ``--preserve-existing`` copper (#6553).

#6322 / PR #6552 made the layer-escalation flow hand its preserved input copper
to the partial save (``_interrupt_state["source_copper"]``).  The adaptive,
combined and main (single-attempt) flows bound the partial-save state without
it.  Once a checkpoint has rewritten the staged ``<output>`` -- the placement
source the save reads, and whose segments/vias the save strips -- the
preserved copper survived nowhere, so ``<stem>_partial.kicad_pcb`` lost it.

Each case drives the real CLI on a real two-net board with one preserved
segment and one preserved via, forces a checkpoint onto ``<output>`` and then
lets a ``--timeout`` deadline land.  The ``_partial`` must hold the router's
copper plus the preserved copper exactly once.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.cli import route_cmd, route_deadline
from kicad_tools.router.core import Autorouter

_PARTS = {"R1": (10.0, 10.0), "R2": (30.0, 10.0)}
_NET_OF = {("R1", "1"): 1, ("R2", "1"): 1, ("R1", "2"): 2, ("R2", "2"): 2}
_NETS = {1: "A", 2: "B"}

# Preserved input copper, away from the pads so it does not block routing.
_FIXED_SEGMENT = '  (segment (start 2 2) (end 4 2) (width 0.2) (layer "B.Cu") (net 1))\n'
_FIXED_VIA = '  (via (at 3 17) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))\n'


def _board_text() -> str:
    footprints = ""
    for ref, (x, y) in _PARTS.items():
        pads = "".join(
            f'    (pad "{pin}" smd rect (at {dx} 0) (size 0.6 0.6) (layers "F.Cu") '
            f'(net {_NET_OF[(ref, pin)]} "{_NETS[_NET_OF[(ref, pin)]]}"))\n'
            for pin, dx in (("1", -0.5), ("2", 0.5))
        )
        footprints += (
            f'  (footprint "R_0402" (layer "F.Cu") (at {x} {y})\n'
            f'    (property "Reference" "{ref}")\n{pads}  )\n'
        )
    nets = "".join(f'  (net {num} "{name}")\n' for num, name in _NETS.items())
    return (
        '(kicad_pcb\n  (version 20240108)\n  (generator "test")\n'
        "  (general (thickness 1.6))\n"
        '  (layers\n    (0 "F.Cu" signal)\n    (31 "B.Cu" signal)\n'
        '    (44 "Edge.Cuts" user)\n  )\n'
        '  (gr_rect (start 0 0) (end 40 20) (layer "Edge.Cuts") (width 0.1))\n'
        '  (net 0 "")\n' + nets + footprints + _FIXED_SEGMENT + _FIXED_VIA + ")\n"
    )


@pytest.fixture(autouse=True)
def _clean_state(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(route_deadline.CONTROL_ENV, str(tmp_path / "control.json"))
    yield
    route_cmd._interrupt_state.update(router=None, output_path=None, pcb_path=None)
    route_cmd._reset_interrupt_outcome()
    route_cmd._reset_partial_save_state()
    route_deadline.reset_deadline_state()


def _copper(text: str) -> tuple[int, int]:
    return text.count("(segment"), len(re.findall(r"\(via\s", text))


class _CheckpointThenDeadline:
    """Route for real and checkpoint onto ``<output>``; then hit the deadline.

    The adaptive and combined flows only hand the partial save a router once
    an attempt has finished, so for them the first attempt is left one net
    short (``drop_net``) to keep the ladder going, and the deadline lands
    during the second.  Every call checkpoints first, so by the time the
    deadline fires ``<output>`` holds routed-only copper.
    """

    def __init__(self, monkeypatch, *, deadline_on_call: int):
        self.expected: tuple[int, int] | None = None
        self.checkpoints = 0
        self.calls = 0
        original = Autorouter.route_all_negotiated

        def route(router, *args, **kwargs):
            self.calls += 1
            result = original(router, *args, **kwargs)
            if self.calls < deadline_on_call:
                router.routes = [r for r in router.routes if r.net != 2]
            callback = kwargs.get("checkpoint_callback")
            if callback is not None:
                metrics = SimpleNamespace(iteration=0, routed_count=1, overflow=0)
                callback(list(router.routes), metrics)
                self.checkpoints += 1
            if self.calls < deadline_on_call:
                return result
            saved_router = route_cmd._interrupt_state["router"]
            assert saved_router is not None, "the flow bound no router for the partial save"
            self.expected = _copper(saved_router.to_sexp())
            raise route_deadline.RouteDeadlineExpired()

        monkeypatch.setattr(Autorouter, "route_all_negotiated", route)


# flow -> (argv selecting it, the routing call the deadline lands in)
_FLOWS = {
    "adaptive": (("--no-auto-layers", "--adaptive-rules"), 2),
    "combined": (("--auto-layers", "--adaptive-rules"), 2),
    "main": (("--no-auto-layers",), 1),
    "layer-escalation": (("--auto-layers",), 1),
}


@pytest.mark.parametrize("flow", list(_FLOWS), ids=list(_FLOWS))
def test_partial_keeps_preserved_copper_exactly_once(monkeypatch, tmp_path, flow):
    source = tmp_path / "input.kicad_pcb"
    source.write_text(_board_text())
    output = tmp_path / "output.kicad_pcb"
    flow_args, deadline_on_call = _FLOWS[flow]
    patch = _CheckpointThenDeadline(monkeypatch, deadline_on_call=deadline_on_call)

    rc = route_cmd._in_process_main_impl(
        [
            str(source),
            "-o",
            str(output),
            "--quiet",
            "--skip-drc",
            "--preserve-existing",
            *flow_args,
        ]
    )

    assert rc == route_deadline.TIMEOUT_EXIT
    assert patch.checkpoints >= 1, "no checkpoint replaced <output> before the deadline"
    # The checkpoint really did overwrite the placement source: the preserved
    # copper is gone from <output> on disk ...
    assert "(start 2 2)" not in output.read_text().replace(".0", "")
    partial = output.with_stem(output.stem + "_partial")
    assert partial.is_file(), f"the {flow} flow saved no _partial board"
    text = partial.read_text()
    segments, vias = patch.expected
    # ... yet the partial carries it, exactly once, next to the router's copper.
    assert _copper(text) == (segments + 1, vias + 1)
    assert len(re.findall(r"\(start 2(?:\.0+)? 2(?:\.0+)?\)", text)) == 1
    assert len(re.findall(r"\(at 3(?:\.0+)? 17(?:\.0+)?\)", text)) == 1
