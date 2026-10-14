"""``_partial`` and checkpoints dedupe preserved copper like the final output (#6626).

A partially-preserved net (stub copper on a net the router still has to
route) is re-routed, so the final ``<output>`` carries the routed copy only.
The deadline partial save and the periodic checkpoint used to append the
captured preserved copper verbatim as well, doubling the net.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

from kicad_tools.cli import route_cmd, route_deadline
from kicad_tools.router.primitives import Route, Segment
from tests.test_route_partial_preserved_copper_6553 import (  # noqa: F401
    _board_text,
    _clean_state,
    _copper,
    _CheckpointThenDeadline,
)


def _stub(net: int) -> Route:
    seg = Segment(x1=2, y1=2, x2=4, y2=2, width=0.2, layer=_layer(), net=net, net_name="A")
    return Route(net=net, net_name="A", segments=[seg], vias=[])


def _layer():
    from kicad_tools.router.layers import Layer

    return Layer.B_CU


def test_dedupe_helper_drops_rerouted_net_only():
    stub_a, stub_b = _stub(1), _stub(2)
    sexp = route_cmd._serialize_preserved_routes([stub_a, stub_b])
    routed = [Route(net=1, net_name="A", segments=[], vias=[])]
    kept = route_cmd._dedupe_preserved_sexp(sexp, [stub_a, stub_b], routed)
    assert "(net 1)" not in kept
    assert kept.count("(segment") == 1 and "(net 2)" in kept
    # A stub net keeps its preserved copper even though it is re-routed.
    kept_stub = route_cmd._dedupe_preserved_sexp(sexp, [stub_a, stub_b], routed, stub_net_ids={1})
    assert kept_stub == sexp
    assert route_cmd._dedupe_preserved_sexp(sexp, [stub_a, stub_b], []) == sexp


def test_checkpoint_drops_preserved_copper_of_rerouted_net(tmp_path: Path):
    source = tmp_path / "input.kicad_pcb"
    source.write_text(_board_text())
    output = tmp_path / "output.kicad_pcb"
    output.write_text(_board_text())
    stub = _stub(1)
    callback = route_cmd._make_checkpoint_callback(
        source,
        output,
        30.0,
        quiet=True,
        preserved_sexp=route_cmd._serialize_preserved_routes([stub]),
        preserved_routes=[stub],
    )
    routed = Route(
        net=1,
        net_name="A",
        segments=[
            Segment(x1=9.5, y1=10, x2=29.5, y2=10, width=0.2, layer=_layer(), net=1, net_name="A")
        ],
        vias=[],
    )
    callback(
        [routed],
        SimpleNamespace(iteration=0, routed_count=1, overflow=0),
    )
    text = output.read_text()
    assert len(re.findall(r"\(start 2(?:\.0+)? 2(?:\.0+)?\)", text)) == 0
    assert len(re.findall(r"\(start 9\.5", text)) == 1


def test_partial_drops_preserved_copper_of_rerouted_net(monkeypatch, tmp_path: Path):
    source = tmp_path / "input.kicad_pcb"
    # The 6553 fixture keeps its stub on a pad-less net; move it onto routed net A.
    source.write_text(_board_text().replace("(net 3))", "(net 1))"))
    output = tmp_path / "output.kicad_pcb"
    patch = _CheckpointThenDeadline(monkeypatch, deadline_on_call=1)

    rc = route_cmd._in_process_main_impl(
        [
            str(source),
            "-o",
            str(output),
            "--quiet",
            "--skip-drc",
            "--preserve-existing",
            "--no-auto-layers",
        ]
    )

    assert rc == route_deadline.TIMEOUT_EXIT
    text = output.with_stem(output.stem + "_partial").read_text()
    # Net 1 is re-routed, so its preserved segment and via are replaced, not doubled.
    assert _copper(text) == patch.expected
    assert len(re.findall(r"\(start 2(?:\.0+)? 2(?:\.0+)?\)", text)) == 0
    assert len(re.findall(r"\(at 3(?:\.0+)? 17(?:\.0+)?\)", text)) == 0
