"""Issue #6678: blind/buried vias in diff-pair length and the MCP via writer.

Follow-up to #6662, which gave the router ``Via`` a ``span_type`` (KiCad's
``blind`` / ``buried`` keyword) so ``Via.drilled_layers`` keeps an imported
blind or buried via's declared span.  Two consumers still treated every
non-micro via as a full-stack through via:

* ``DiffPairLengthTracker`` measured a blind/buried via as the full board
  thickness on a board without blind/buried support, overstating length and
  skew for a pair that crosses one.
* The MCP routing tool's via writer always wrote a non-micro via through,
  dropping its ``blind`` / ``buried`` keyword.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.router.diffpair_length import DiffPairLengthTracker
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Via
from kicad_tools.schema.pcb import PCB

THICKNESS = 1.6
LAYERS = 4
ONE_SPAN = THICKNESS / (LAYERS - 1)

_VIA_RE = re.compile(
    r'\(via(?:\s+(micro|blind|buried))?\s*\(.*?\(layers\s+"([^"]+)"\s+"([^"]+)"\)', re.S
)


def _vias(text: str) -> list[tuple[str | None, tuple[str, str]]]:
    return [(m.group(1) or None, (m.group(2), m.group(3))) for m in _VIA_RE.finditer(text)]


def _router_via(layers, *, is_micro: bool = False, span_type: str | None = None) -> Via:
    return Via(
        x=1.0,
        y=2.0,
        drill=0.2,
        diameter=0.45,
        layers=layers,
        net=1,
        is_micro=is_micro,
        span_type=span_type,
    )


# ---------------------------------------------------------------------------
# Router Route path: DiffPairLengthTracker._via_length
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("blind_buried_supported", [True, False])
def test_buried_in1_in2_via_contributes_only_its_span(blind_buried_supported):
    via = _router_via((Layer.IN1_CU, Layer.IN2_CU), span_type="buried")
    length = DiffPairLengthTracker._via_length(
        via, THICKNESS, LAYERS, blind_buried_supported=blind_buried_supported
    )
    assert length == pytest.approx(ONE_SPAN)


@pytest.mark.parametrize("blind_buried_supported", [True, False])
def test_blind_via_contributes_only_its_span(blind_buried_supported):
    via = _router_via((Layer.F_CU, Layer.IN2_CU), span_type="blind")
    length = DiffPairLengthTracker._via_length(
        via, THICKNESS, LAYERS, blind_buried_supported=blind_buried_supported
    )
    assert length == pytest.approx(2 * ONE_SPAN)


@pytest.mark.parametrize("blind_buried_supported", [True, False])
def test_through_and_micro_unchanged(blind_buried_supported):
    # A plain router via is a through barrel whatever its logical pair.
    for layers in ((Layer.F_CU, Layer.B_CU), (Layer.IN1_CU, Layer.IN2_CU)):
        through = _router_via(layers)
        assert DiffPairLengthTracker._via_length(
            through, THICKNESS, LAYERS, blind_buried_supported=blind_buried_supported
        ) == pytest.approx(THICKNESS)
    micro = _router_via((Layer.F_CU, Layer.IN1_CU), is_micro=True)
    assert DiffPairLengthTracker._via_length(
        micro, THICKNESS, LAYERS, blind_buried_supported=blind_buried_supported
    ) == pytest.approx(ONE_SPAN)


# ---------------------------------------------------------------------------
# PCB path: DiffPairLengthTracker.measure_net_from_pcb
# ---------------------------------------------------------------------------

_HEAD = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal)
    (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "")
  (net 1 "SIG")
  (gr_rect (start 90 90) (end 120 110) (stroke (width 0.1) (type default)) (fill none)
    (layer "Edge.Cuts") (uuid "e1"))
"""


def _board(tmp_path: Path, vias: str = "") -> tuple[PCB, Path]:
    path = tmp_path / "board.kicad_pcb"
    path.write_text(_HEAD + vias + ")\n")
    return PCB.load(str(path)), path


@pytest.mark.parametrize(
    "via_text,expected",
    [
        ('(via buried (at 100 100) (size 0.45) (drill 0.2) (layers "In1.Cu" "In2.Cu")', ONE_SPAN),
        ('(via blind (at 100 100) (size 0.45) (drill 0.2) (layers "F.Cu" "In1.Cu")', ONE_SPAN),
        ('(via micro (at 100 100) (size 0.3) (drill 0.1) (layers "F.Cu" "In1.Cu")', ONE_SPAN),
        ('(via (at 100 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu")', THICKNESS),
    ],
)
@pytest.mark.parametrize("blind_buried_supported", [True, False])
def test_pcb_path_measures_declared_span(tmp_path, via_text, expected, blind_buried_supported):
    pcb, _ = _board(tmp_path, f'  {via_text} (net 1) (uuid "v1"))\n')
    length = DiffPairLengthTracker.measure_net_from_pcb(
        pcb,
        1,
        board_thickness_mm=THICKNESS,
        num_copper_layers=LAYERS,
        blind_buried_supported=blind_buried_supported,
    )
    assert length == pytest.approx(expected)


# ---------------------------------------------------------------------------
# MCP via writer round trip
# ---------------------------------------------------------------------------


def test_mcp_persist_keeps_blind_and_buried(tmp_path):
    from kicad_tools.mcp.tools.routing import _persist_routing_result_to_pcb

    pcb, path = _board(tmp_path)
    result = SimpleNamespace(
        segments=[],
        vias=[
            _router_via((Layer.IN1_CU, Layer.IN2_CU), span_type="buried"),
            Via(
                x=5.0,
                y=2.0,
                drill=0.2,
                diameter=0.45,
                layers=(Layer.F_CU, Layer.IN1_CU),
                net=1,
                span_type="blind",
            ),
            # Unchanged: a plain router via is written through, micro keeps its span.
            Via(x=8.0, y=2.0, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.IN1_CU), net=1),
            Via(
                x=11.0,
                y=2.0,
                drill=0.1,
                diameter=0.3,
                layers=(Layer.F_CU, Layer.IN1_CU),
                net=1,
                is_micro=True,
            ),
        ],
    )
    _, vias_written, _, _ = _persist_routing_result_to_pcb(pcb, result, "SIG")
    assert vias_written == 4
    pcb.save(str(path))
    text = path.read_text()
    assert sorted(_vias(text), key=repr) == sorted(
        [
            ("buried", ("In1.Cu", "In2.Cu")),
            ("blind", ("F.Cu", "In1.Cu")),
            (None, ("F.Cu", "B.Cu")),
            ("micro", ("F.Cu", "In1.Cu")),
        ],
        key=repr,
    )
    # And the round trip reads them back with the same keyword.
    reloaded = PCB.load(str(path))
    assert sorted(v.via_type or "" for v in reloaded.vias) == ["", "blind", "buried", "micro"]
