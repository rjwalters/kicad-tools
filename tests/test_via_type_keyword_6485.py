"""No writer emits a keyword-less partial-span via (Issue #6485).

KiCad decides a via's drilled span from its type keyword, not from its
``(layers ...)`` pair.  Measured on kicad-cli 10.0.6: a ``(via ...)`` node
with no ``micro`` / ``blind`` / ``buried`` token is a THROUGH via whatever the
pair says -- ``pcb upgrade`` rewrites the pair to ``"F.Cu" "B.Cu"`` and
``pcb export drill`` puts the hole in the through-hole file.  A keyword-less
``(layers "F.Cu" "In1.Cu")`` therefore tells every reader of the file that the
barrel stops at In1 while the fabricated barrel shorts In2 and B.Cu to it.

The live producer on ``main`` was the grid coupled diff-pair builder,
``CoupledPathfinder._build_route_from_path``.

The kicad-cli verdicts pinned here are re-measured when kicad-cli is
installed, like ``tests/test_copper_contact_parity.py``.  How the *readers*
treat a keyword-less partial-span via they did not write is Issue #6493.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.core.layers import resolve_via_type
from kicad_tools.router.diffpair_routing import CoupledPathfinder, _via_spans_layer
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad, Via
from kicad_tools.router.rules import DesignRules
from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp.builders import via_node

OUTER = {"F.Cu", "B.Cu"}

# ``(via`` + optional type token ... ``(layers "A" "B")``.  Works on both the
# router's string emitter and KiCad's / the schema's pretty-printed nodes.
_VIA_RE = re.compile(
    r'\(via(?:\s+(micro|blind|buried))?\s*\(.*?\(layers\s+"([^"]+)"\s+"([^"]+)"\)', re.S
)


def _vias(text: str) -> list[tuple[str | None, tuple[str, str]]]:
    """Every via in ``text`` as ``(keyword, (layer, layer))``."""
    return [(m.group(1) or None, (m.group(2), m.group(3))) for m in _VIA_RE.finditer(text)]


def _keywordless_partial(text: str) -> list[tuple[str, str]]:
    """The node this issue makes unwritable: no keyword, not the outer pair."""
    return [layers for keyword, layers in _vias(text) if keyword is None and set(layers) != OUTER]


# ---------------------------------------------------------------------------
# The write-time rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "layers,via_type,expected",
    [
        (("F.Cu", "B.Cu"), None, (None, ("F.Cu", "B.Cu"))),
        (("B.Cu", "F.Cu"), None, (None, ("B.Cu", "F.Cu"))),
        (("F.Cu", "In1.Cu"), None, ("blind", ("F.Cu", "In1.Cu"))),
        (("In2.Cu", "B.Cu"), None, ("blind", ("In2.Cu", "B.Cu"))),
        (("In1.Cu", "In2.Cu"), None, ("buried", ("In1.Cu", "In2.Cu"))),
        # KiCad does not relabel by span: an explicit keyword is kept.
        (("In1.Cu", "B.Cu"), "buried", ("buried", ("In1.Cu", "B.Cu"))),
        (("F.Cu", "In1.Cu"), "micro", ("micro", ("F.Cu", "In1.Cu"))),
        (("F.Cu", "B.Cu"), "micro", ("micro", ("F.Cu", "B.Cu"))),
        # An explicit through via is drilled through, whatever pair was given.
        (("F.Cu", "In1.Cu"), "through", (None, ("F.Cu", "B.Cu"))),
        # Nothing can be inferred from a pair that is not two copper layers.
        (("F.Cu", "F.Mask"), None, (None, ("F.Cu", "F.Mask"))),
    ],
)
def test_resolve_via_type(layers, via_type, expected):
    assert resolve_via_type(layers, via_type) == expected


def test_resolve_via_type_rejects_unknown_type():
    """A typo must not silently become a through via."""
    with pytest.raises(ValueError, match="Unknown via type"):
        resolve_via_type(("F.Cu", "In1.Cu"), "blnd")


# ---------------------------------------------------------------------------
# Router primitive: the type decides
# ---------------------------------------------------------------------------


def _router_via(layers: tuple[Layer, Layer], *, is_micro: bool = False) -> Via:
    return Via(x=1.0, y=2.0, drill=0.2, diameter=0.3, layers=layers, net=1, is_micro=is_micro)


def test_router_via_partial_span_is_written_through():
    text = _router_via((Layer.F_CU, Layer.IN1_CU)).to_sexp()
    assert _vias(text) == [(None, ("F.Cu", "B.Cu"))]


def test_router_micro_via_keeps_span_and_keyword():
    text = _router_via((Layer.F_CU, Layer.IN1_CU), is_micro=True).to_sexp()
    assert text.startswith("(via micro")
    assert _vias(text) == [("micro", ("F.Cu", "In1.Cu"))]


@pytest.mark.parametrize("layers", [(Layer.F_CU, Layer.B_CU), (Layer.B_CU, Layer.F_CU)])
def test_router_through_via_is_written_as_held(layers):
    """No churn: an outer pair keeps the order its producer gave it."""
    text = _router_via(layers).to_sexp()
    assert _vias(text) == [(None, (layers[0].kicad_name, layers[1].kicad_name))]


def test_router_micro_via_on_two_layer_board():
    text = _router_via((Layer.F_CU, Layer.B_CU), is_micro=True).to_sexp()
    assert _vias(text) == [("micro", ("F.Cu", "B.Cu"))]


def test_diffpair_gates_see_the_drilled_barrel():
    """A span gate must not skip layers the fabricated barrel occupies."""
    partial = _router_via((Layer.F_CU, Layer.IN1_CU))
    assert all(
        _via_spans_layer(partial, layer)
        for layer in (Layer.F_CU, Layer.IN1_CU, Layer.IN2_CU, Layer.B_CU)
    )
    micro = _router_via((Layer.F_CU, Layer.IN1_CU), is_micro=True)
    assert _via_spans_layer(micro, Layer.IN1_CU)
    assert not _via_spans_layer(micro, Layer.IN2_CU)
    assert not _via_spans_layer(micro, Layer.B_CU)


# ---------------------------------------------------------------------------
# The live producer: coupled diff-pair on a four-signal-layer grid
# ---------------------------------------------------------------------------


def _coupled_four_layer_routes(monkeypatch, backend: str):
    """A coupled pair forced through a layer change; only In1.Cu is open.

    A wall at x = 3.0 blocks F.Cu, In2.Cu and B.Cu, so the pair has to leave
    F.Cu, cross on In1.Cu and come back -- four vias whose LOGICAL transition
    is F.Cu <-> In1.Cu.
    """
    monkeypatch.setenv("KCT_COUPLED_CPP", "1" if backend == "cpp" else "0")
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.1,
        via_drill=0.2,
        via_diameter=0.3,
        via_clearance=0.05,
        grid_resolution=0.1,
        min_hole_to_hole=0.5,
    )
    grid = RoutingGrid(
        width=6.0, height=3.0, rules=rules, layer_stack=LayerStack.four_layer_all_signal()
    )
    grid.add_keepout(3.0, 0.0, 3.0, 3.0, layers=[Layer.F_CU, Layer.B_CU, Layer.IN2_CU])

    def pad(x: float, y: float, net: int, name: str, ref: str) -> Pad:
        return Pad(
            x=x, y=y, width=0.2, height=0.2, net=net, net_name=name, layer=Layer.F_CU,
            ref=ref, pin="1",
        )  # fmt: skip

    p_start, p_end = pad(1.0, 1.2, 1, "D_P", "A"), pad(5.0, 1.2, 1, "D_P", "B")
    n_start, n_end = pad(1.0, 1.8, 2, "D_N", "C"), pad(5.0, 1.8, 2, "D_N", "D")
    for item in (p_start, p_end, n_start, n_end):
        grid.add_pad(item)
    pathfinder = CoupledPathfinder(
        grid=grid, rules=rules, target_spacing_cells=6, min_spacing_cells=6
    )
    result = pathfinder.route_coupled(p_start, p_end, n_start, n_end, max_iterations_budget=200000)
    if backend == "cpp" and pathfinder.last_coupled_backend != "cpp":
        pytest.skip("C++ coupled backend not built")
    assert pathfinder.last_coupled_backend == backend
    assert result is not None, "the fixture must route"
    return result


@pytest.mark.parametrize("backend", ["cpp", "python"])
def test_coupled_route_vias_are_through(monkeypatch, backend):
    """Fails on main: both backends returned ``(F.Cu, In1.Cu)`` vias."""
    routes = _coupled_four_layer_routes(monkeypatch, backend)
    vias = [via for route in routes for via in route.vias]
    assert len(vias) == 4, "the wall forces a layer change out and back on each leg"
    for via in vias:
        assert not via.is_micro
        assert set(via.layers) == {Layer.F_CU, Layer.B_CU}
    # The search really did use the inner layer -- the vias are through
    # barrels, the TRANSITION is still F.Cu <-> In1.Cu.
    assert {seg.layer for route in routes for seg in route.segments} == {
        Layer.F_CU,
        Layer.IN1_CU,
    }


@pytest.mark.parametrize("backend", ["cpp", "python"])
def test_serialised_four_layer_route_has_no_keywordless_partial_via(monkeypatch, backend):
    """Regression scan over a router-serialised four-layer route."""
    routes = _coupled_four_layer_routes(monkeypatch, backend)
    text = "\n".join(route.to_sexp() for route in routes)
    assert len(_vias(text)) == 4
    assert _keywordless_partial(text) == []


def test_scan_catches_the_node_it_guards_against():
    """The scan above is not vacuous."""
    bad = '(via (at 1 2) (size 0.3) (drill 0.2) (layers "F.Cu" "In1.Cu") (net 1))'
    assert _keywordless_partial(bad) == [("F.Cu", "In1.Cu")]
    assert _keywordless_partial(bad.replace("(via ", "(via blind ")) == []


# ---------------------------------------------------------------------------
# Board-file creation APIs: the span decides
# ---------------------------------------------------------------------------


def _node_text(layers, via_type=None) -> str:
    return via_node(1.0, 2.0, 0.6, 0.3, layers, 1, "u-1", via_type=via_type).to_string()


@pytest.mark.parametrize(
    "layers,via_type,expected",
    [
        (("F.Cu", "In1.Cu"), None, "blind"),
        (("In1.Cu", "In2.Cu"), None, "buried"),
        (("F.Cu", "B.Cu"), None, None),
        (("B.Cu", "F.Cu"), None, None),
        # Explicit blind / buried used to be dropped silently.
        (("F.Cu", "In1.Cu"), "blind", "blind"),
        (("In1.Cu", "B.Cu"), "buried", "buried"),
        (("F.Cu", "In1.Cu"), "micro", "micro"),
    ],
)
def test_via_node_keyword(layers, via_type, expected):
    text = _node_text(layers, via_type)
    assert _vias(text) == [(expected, layers)]
    # A bare atom, never a quoted string (KiCad refuses ``(via "buried" ...)``).
    assert '"blind"' not in text and '"buried"' not in text and '"micro"' not in text
    assert _keywordless_partial(text) == []


_FOUR_LAYER_HEAD = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal)
    (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "")
  (net 1 "SIG")
  (net 2 "OTHER")
  (gr_rect (start 90 90) (end 120 110) (stroke (width 0.1) (type default)) (fill none)
    (layer "Edge.Cuts") (uuid "e1"))
"""


def _pads(net: int, name: str, xs: list[float], y: float = 100.0) -> str:
    return "".join(
        f'  (footprint "R" (layer "F.Cu") (uuid "fp{name}{i}{y}") (at {x} {y})\n'
        f'    (property "Reference" "R{name}{i}{int(y)}" (at 0 0 0) (layer "F.SilkS")'
        f' (uuid "r{name}{i}{y}"))\n'
        f'    (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") (net {net} "{name}")))\n'
        for i, x in enumerate(xs)
    )


def _short_fixture_without_sig_vias() -> str:
    """SIG runs on In1.Cu between two F.Cu pads; OTHER crosses (100, 100) on B.Cu.

    SIG's two vias -- the ones under test -- are added by the caller.  With a
    via that really stops at In1.Cu the OTHER track on B.Cu is clear of it; a
    through barrel at (100, 100) lands on that track.
    """
    body = (
        _pads(1, "SIG", [100, 110]) + _pads(2, "OTHER", [105], 95) + _pads(2, "OTHER", [105], 105)
    )
    body += (
        '  (segment (start 100 100) (end 110 100) (width 0.25) (layer "In1.Cu") (net 1)'
        ' (uuid "s1"))\n'
        '  (via (at 105 95) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2) (uuid "v3"))\n'
        '  (via (at 105 105) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2) (uuid "v4"))\n'
        '  (segment (start 105 95) (end 100 95) (width 0.25) (layer "B.Cu") (net 2) (uuid "o1"))\n'
        '  (segment (start 100 95) (end 100 105) (width 0.25) (layer "B.Cu") (net 2) (uuid "o2"))\n'
        '  (segment (start 100 105) (end 105 105) (width 0.25) (layer "B.Cu") (net 2)'
        ' (uuid "o3"))\n'
    )
    return _FOUR_LAYER_HEAD + body + ")\n"


def _load(tmp_path: Path, text: str, name: str = "board") -> tuple[PCB, Path]:
    path = tmp_path / f"{name}.kicad_pcb"
    path.write_text(text)
    return PCB.load(str(path)), path


def _add_sig_vias(pcb: PCB, **kwargs) -> None:
    """Add SIG's two vias at the pad centres, in sheet coordinates."""
    ox, oy = pcb._board_origin
    for x in (100.0, 110.0):
        assert pcb.add_via(x - ox, 100.0 - oy, size=0.6, drill=0.3, net="SIG", **kwargs)


def test_add_via_partial_span_is_saved_blind(tmp_path, caplog):
    pcb, path = _load(tmp_path, _short_fixture_without_sig_vias())
    with caplog.at_level(logging.WARNING, logger="kicad_tools.schema.pcb"):
        _add_sig_vias(pcb, layers=("F.Cu", "In1.Cu"))
    # Deriving an HDI via type is never silent.
    assert "partial span" in caplog.text and "blind" in caplog.text
    assert [via.via_type for via in pcb.vias if via.net_name == "SIG"] == ["blind", "blind"]
    pcb.save(str(path))
    text = path.read_text()
    assert _vias(text).count(("blind", ("F.Cu", "In1.Cu"))) == 2
    assert _keywordless_partial(text) == []
    # ... and it survives a reload.
    assert [v.via_type for v in PCB.load(str(path)).vias if v.net_name == "SIG"] == ["blind"] * 2


@pytest.mark.parametrize(
    "layers,via_type,expected",
    [
        (("F.Cu", "In1.Cu"), "micro", ("micro", ("F.Cu", "In1.Cu"))),
        (("In1.Cu", "In2.Cu"), None, ("buried", ("In1.Cu", "In2.Cu"))),
        (("F.Cu", "In1.Cu"), "through", (None, ("F.Cu", "B.Cu"))),
        (("F.Cu", "B.Cu"), None, (None, ("F.Cu", "B.Cu"))),
    ],
)
def test_add_via_saved_keyword(tmp_path, layers, via_type, expected):
    pcb, path = _load(tmp_path, _short_fixture_without_sig_vias())
    before = _vias(path.read_text())
    _add_sig_vias(pcb, layers=layers, via_type=via_type)
    pcb.save(str(path))
    text = path.read_text()
    after = _vias(text)
    assert after.count(expected) - before.count(expected) == 2
    assert _keywordless_partial(text) == []


def test_add_via_rejects_unknown_type(tmp_path):
    pcb, _ = _load(tmp_path, _short_fixture_without_sig_vias())
    with pytest.raises(ValueError, match="Unknown via type"):
        pcb.add_via(1.0, 1.0, layers=("F.Cu", "In1.Cu"), via_type="laser")


def test_mcp_persist_keeps_micro_and_drills_the_rest_through(tmp_path):
    """The MCP routing tool dropped ``is_micro`` and kept the logical pair."""
    from kicad_tools.mcp.tools.routing import _persist_routing_result_to_pcb

    pcb, path = _load(tmp_path, _short_fixture_without_sig_vias())
    before = _vias(path.read_text())
    result = SimpleNamespace(
        segments=[],
        vias=[
            Via(
                x=3.0,
                y=3.0,
                drill=0.1,
                diameter=0.25,
                layers=(Layer.F_CU, Layer.IN1_CU),
                is_micro=True,
            ),  # fmt: skip
            Via(x=6.0, y=3.0, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.IN1_CU)),
        ],
    )
    _, vias_written, _, _ = _persist_routing_result_to_pcb(pcb, result, "SIG")
    assert vias_written == 2
    pcb.save(str(path))
    text = path.read_text()
    after = _vias(text)
    assert after.count(("micro", ("F.Cu", "In1.Cu"))) == 1
    assert after.count((None, ("F.Cu", "B.Cu"))) - before.count((None, ("F.Cu", "B.Cu"))) == 1
    assert _keywordless_partial(text) == []


def test_block_exporter_never_writes_a_keywordless_partial_via():
    from kicad_tools.pcb.exporter import KiCadPCBExporter

    exporter = KiCadPCBExporter.__new__(KiCadPCBExporter)
    exporter._uuid = lambda: "u-1"  # type: ignore[method-assign]
    exporter._format_coord = lambda value: str(value)  # type: ignore[method-assign]
    assert _vias(exporter._generate_via(1, 2, 0.6, 0.3, ("F.Cu", "In1.Cu"), 1)) == [
        ("blind", ("F.Cu", "In1.Cu"))
    ]
    assert _vias(exporter._generate_via(1, 2, 0.6, 0.3, ("F.Cu", "B.Cu"), 1)) == [
        (None, ("F.Cu", "B.Cu"))
    ]


# ---------------------------------------------------------------------------
# Live kicad-cli: the written via is the via we meant
# ---------------------------------------------------------------------------


def _kicad_cli() -> Path | None:
    from kicad_tools.cli.runner import find_kicad_cli

    return find_kicad_cli()


def _kicad_shorts(path: Path) -> int:
    out = path.with_suffix(".drc.json")
    subprocess.run(
        [str(_kicad_cli()), "pcb", "drc", "--format", "json", "--severity-all",
         "-o", str(out), str(path)],
        capture_output=True,
        check=False,
    )  # fmt: skip
    violations = json.loads(out.read_text()).get("violations", [])
    return sum(1 for violation in violations if violation["type"] == "shorting_items")


def _kicad_resaved(path: Path) -> str:
    """The board as KiCad itself writes it back."""
    subprocess.run(
        [str(_kicad_cli()), "pcb", "upgrade", "--force", str(path)],
        capture_output=True,
        check=True,
    )
    return path.read_text()


# kicad-cli 10.0.6 ``shorting_items`` on the short fixture, by how SIG's two
# ``F.Cu``/``In1.Cu`` vias are written.
SHORT_GOLDENS = {"keywordless": 1, "add_via": 0, "add_via_through": 1}


def _short_board(tmp_path: Path, how: str) -> Path:
    if how == "keywordless":
        # The node this issue removes, written by hand: what PCB.add_via and
        # the router used to produce for a partial span.
        text = _short_fixture_without_sig_vias().rstrip()[:-1] + "".join(
            f'  (via (at {x} 100) (size 0.6) (drill 0.3) (layers "F.Cu" "In1.Cu") (net 1)'
            f' (uuid "v{x}"))\n'
            for x in (100, 110)
        ) + ")\n"  # fmt: skip
        path = tmp_path / f"{how}.kicad_pcb"
        path.write_text(text)
        return path
    pcb, path = _load(tmp_path, _short_fixture_without_sig_vias(), how)
    _add_sig_vias(
        pcb, layers=("F.Cu", "In1.Cu"), via_type="through" if how == "add_via_through" else None
    )
    pcb.save(str(path))
    return path


@pytest.mark.skipif(_kicad_cli() is None, reason="kicad-cli not installed")
@pytest.mark.parametrize("how", sorted(SHORT_GOLDENS))
def test_short_fixture_matches_live_kicad_cli(tmp_path, how):
    """A partial span written through ``PCB.add_via`` is the via KiCad builds.

    Keyword-less, the barrel is drilled through and lands on OTHER's B.Cu
    track.  Written through ``add_via`` it carries ``blind`` and stops at
    In1.Cu.  Asked for as ``through``, it says so in its layers and KiCad
    reports the same short our own readers can now see.
    """
    assert _kicad_shorts(_short_board(tmp_path, how)) == SHORT_GOLDENS[how]


@pytest.mark.skipif(_kicad_cli() is None, reason="kicad-cli not installed")
def test_kicad_resave_does_not_change_what_we_wrote(tmp_path):
    """Through stays through; blind, buried and micro keep keyword and span.

    KiCad rewrites a keyword-less partial pair to ``"F.Cu" "B.Cu"`` on save.
    Nothing we write may be rewritten: the router's via already says
    ``F.Cu``/``B.Cu``, and the typed vias keep their layers.
    """
    router_through = _router_via((Layer.F_CU, Layer.IN1_CU)).to_sexp()
    router_micro = Via(
        x=4.0, y=2.0, drill=0.1, diameter=0.25, layers=(Layer.F_CU, Layer.IN1_CU), is_micro=True
    ).to_sexp()
    nodes = [
        via_node(6.0, 2.0, 0.6, 0.3, ("F.Cu", "In1.Cu"), 1, "00000000-0000-0000-0000-000000000001"),
        via_node(8.0, 2.0, 0.6, 0.3, ("In1.Cu", "In2.Cu"), 1, "00000000-0000-0000-0000-000000000002"),
    ]  # fmt: skip
    body = "\n".join([router_through, router_micro, *(node.to_string() for node in nodes)])
    path = tmp_path / "resave.kicad_pcb"
    path.write_text(_FOUR_LAYER_HEAD + body + "\n)\n")
    written = sorted(_vias(path.read_text()), key=str)
    assert written == sorted(
        [
            (None, ("F.Cu", "B.Cu")),
            ("micro", ("F.Cu", "In1.Cu")),
            ("blind", ("F.Cu", "In1.Cu")),
            ("buried", ("In1.Cu", "In2.Cu")),
        ],
        key=str,
    )
    assert sorted(_vias(_kicad_resaved(path)), key=str) == written


@pytest.mark.skipif(_kicad_cli() is None, reason="kicad-cli not installed")
def test_kicad_rewrites_the_keywordless_partial_via(tmp_path):
    """The premise, re-measured: this is why the node must be unwritable."""
    path = tmp_path / "premise.kicad_pcb"
    path.write_text(
        _FOUR_LAYER_HEAD + '  (via (at 1 2) (size 0.6) (drill 0.3) (layers "F.Cu" "In1.Cu") (net 1)'
        ' (uuid "00000000-0000-0000-0000-000000000003"))\n)\n'
    )
    assert _vias(_kicad_resaved(path)) == [(None, ("F.Cu", "B.Cu"))]
