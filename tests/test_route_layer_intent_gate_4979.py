"""CLI-level tests for the hard layer-intent gate (issue #4979).

``--strict-layers`` promotes a net class's ``avoid_layers`` from a soft cost
bias to a HARD no-go set.  Both grid backends honoured that during search; the
LATTICE engine -- the ``--complete`` default -- had no ``avoid_layers``
plumbing at all, so a softstart rev-C run committed 29 x 2.6 mm ``/PGND``
segments plus 8 thin ones onto the explicitly forbidden ``In2.Cu`` reference
plane and reported no layer-intent failure anywhere: not in the banner, not in
the exit code, not in the completion report.

These tests pin the two halves of the fix from the CLI's own surface:

1. a 4-layer ``--strict-layers`` lattice run whose class forbids both inner
   layers writes copper on the outer layers ONLY, and exits cleanly;
2. the post-route gate itself -- new (this run's) findings replace the SUCCESS
   banner and move the exit code, while INHERITED findings (copper the input
   board already carried, re-emitted under ``--preserve-existing``) are
   reported without re-blaming the run.

Boards are fully synthetic S-expression strings (same approach as
``test_route_pairwise_gate_4588.py``) -- the softstart rev-C board from the
original report is local-only and must never be a CI dependency.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from kicad_tools.cli.route_cmd import (
    _audit_layer_intent,
    _layer_intent_escalation_exit,
    _new_layer_intent_violations,
    _print_layer_intent_addendum,
)
from kicad_tools.cli.route_cmd import main as route_main
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import DesignRules, NetClassRouting

_INNER_LAYERS = ("In1.Cu", "In2.Cu")


def _pad(number: str, x: float, y: float, net: int, net_name: str) -> str:
    return (
        f'(pad "{number}" smd rect (at {x} {y}) (size 0.6 0.6) '
        f'(layers "F.Cu" "F.Paste" "F.Mask") (net {net} "{net_name}"))'
    )


def _fp(ref: str, uid: int, x: float, y: float, pads: str) -> str:
    return f"""  (footprint "Resistor_SMD:R_0603_1608Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000{uid:02d}")
    (at {x} {y})
    (property "Reference" "{ref}" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    {pads}
  )
"""


def _walled_four_layer_board() -> str:
    """4-layer board whose only ``/PGND`` crossing is an INNER-layer shortcut.

    A foreign ``WALL`` net carries a full-height wall on BOTH outer layers at
    x=30, between the two ``/PGND`` pads; ``In1.Cu``/``In2.Cu`` carry no wall
    copper at all.  So the single geometric route for ``/PGND`` is a via dip
    onto a layer its class forbids -- the exact shape of the reported defect,
    where the completion pass took that shortcut and shipped 2.6 mm copper
    onto the ``In2.Cu`` reference plane.

    ``/SIG_A`` (no layer constraint) crosses the same wall on an inner layer,
    so a ``--complete`` run over this board has a MIX of one closed link and
    one honest decline -- the exit-8 path, not the "nothing routed" fatal
    code 1.
    """
    parts = [
        _fp("R1", 10, 10.0, 20.0, _pad("1", 0, 0, 1, "/PGND")),
        _fp("R2", 11, 50.0, 20.0, _pad("1", 0, 0, 1, "/PGND")),
        _fp("R3", 12, 10.0, 32.0, _pad("1", 0, 0, 2, "/SIG_A")),
        _fp("R4", 13, 50.0, 32.0, _pad("1", 0, 0, 2, "/SIG_A")),
        _fp("W1", 14, 30.0, 5.0, _pad("1", 0, 0, 3, "WALL")),
        _fp("W2", 15, 30.0, 35.0, _pad("1", 0, 0, 3, "WALL")),
    ]
    wall = (
        '  (segment (start 30 0) (end 30 40) (width 2.0) (layer "F.Cu") (net 3))\n'
        '  (segment (start 30 0) (end 30 40) (width 2.0) (layer "B.Cu") (net 3))\n'
    )
    return f"""(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup
    (pad_to_mask_clearance 0)
  )
  (net 0 "")
  (net 1 "/PGND")
  (net 2 "/SIG_A")
  (net 3 "WALL")
  (gr_rect (start 0 0) (end 60 40)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
{wall}{"".join(parts)})
"""


def _four_layer_board(extra_copper: str = "") -> str:
    """Synthetic 4-layer board with one ``/PGND`` link and one ``/SIG_A`` link."""
    parts = [
        _fp("R1", 10, 103.0, 105.0, _pad("1", 0, 0, 1, "/PGND")),
        _fp("R2", 11, 125.0, 105.0, _pad("1", 0, 0, 1, "/PGND")),
        _fp("R3", 12, 103.0, 112.0, _pad("1", 0, 0, 2, "/SIG_A")),
        _fp("R4", 13, 125.0, 112.0, _pad("1", 0, 0, 2, "/SIG_A")),
    ]
    return f"""(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup
    (pad_to_mask_clearance 0)
  )
  (net 0 "")
  (net 1 "/PGND")
  (net 2 "/SIG_A")
  (gr_rect (start 100 100) (end 130 118)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
{extra_copper}{"".join(parts)})
"""


def _write_net_class_map(tmp_path: Path) -> Path:
    """The ``/PGND`` class from the #4979 report: outer layers only."""
    path = tmp_path / "route-map.json"
    path.write_text(
        json.dumps(
            {
                "/PGND": {
                    "name": "HV",
                    "trace_width": 0.4,
                    "clearance": 0.2,
                    "is_pour_net": False,
                    "preferred_layers": ["F.Cu", "B.Cu"],
                    "avoid_layers": list(_INNER_LAYERS),
                }
            }
        )
    )
    return path


def _route_args(pcb: Path, out: Path, net_class_map: Path, *, strict: bool) -> list[str]:
    args = [
        str(pcb),
        "-o",
        str(out),
        "--route-engine",
        "lattice",
        # The lattice engine only dispatches through the basic per-net path
        # (issue #4280); the negotiated default is rejected outright.
        "--strategy",
        "basic",
        "--layers",
        "4",
        "--no-auto-layers",
        "--net-class-map",
        str(net_class_map),
        # The gate is not a DRC sub-step; --skip-drc keeps these tests free of
        # a kicad-cli dependency without disabling it.
        "--skip-drc",
    ]
    if strict:
        args.append("--strict-layers")
    return args


def _copper_layers_for_net(board_text: str, net: int) -> set[str]:
    """Layers carrying ``(segment ...)`` copper for ``net`` in a written board.

    Deliberately a raw text scan of the WRITTEN file rather than a parse
    through ``PCB``: the acceptance criterion is about the bytes the user
    ships, so the check should not share a code path with the writer.
    """
    layers: set[str] = set()
    for chunk in board_text.split("(segment")[1:]:
        body = chunk.split("(segment")[0]
        net_match = re.search(r"\(net\s+(\d+)\s*\)", body)
        layer_match = re.search(r'\(layer\s+"([^"]+)"\)', body)
        if net_match is None or layer_match is None:
            continue
        if int(net_match.group(1)) == net:
            layers.add(layer_match.group(1))
    return layers


@pytest.fixture
def four_layer_board(tmp_path: Path) -> Path:
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(_four_layer_board())
    return pcb


class TestStrictLayersEndToEnd:
    """A ``--strict-layers`` lattice run never writes forbidden-layer copper."""

    def test_pgnd_routes_on_outer_layers_only(
        self, four_layer_board: Path, tmp_path: Path, capsys
    ) -> None:
        net_class_map = _write_net_class_map(tmp_path)
        out = tmp_path / "routed.kicad_pcb"

        rc = route_main(_route_args(four_layer_board, out, net_class_map, strict=True))
        captured = capsys.readouterr().out

        assert out.exists()
        written = out.read_text()
        pgnd_layers = _copper_layers_for_net(written, 1)
        # Non-vacuous: the constrained net really was routed (a gate that
        # passes because nothing was emitted would prove nothing).
        assert pgnd_layers, f"/PGND emitted no copper at all:\n{captured[-2000:]}"
        assert pgnd_layers <= {"F.Cu", "B.Cu"}, (
            f"/PGND copper landed on a forbidden layer: {pgnd_layers}"
        )
        # The gate found nothing to report, so the run is an ordinary success.
        assert "ROUTING FAILED: hard layer-intent violations" not in captured
        assert "SUCCESS" in captured
        assert rc == 0, f"unexpected exit {rc}: {captured[-2000:]}"


class TestCompleteMode:
    """``--complete`` -- the flow the defect was reported on (#4979).

    The original run was ``kct route ... --complete --route-engine lattice
    --strict-layers``; completion is also the flow where a net's ONLY
    remaining geometric option is often the forbidden shortcut, because every
    easy link is already closed.  A completion pass must decline such a link
    (and say the layer intent is why), never ship it as partial progress.
    """

    @staticmethod
    def _args(board: Path, out: Path, net_class_map: Path, report: Path) -> list[str]:
        return [
            str(board),
            "-o",
            str(out),
            "--complete",
            # WALL's two outer-layer segments are a fixed obstacle, not work:
            # its B.Cu run is a separate island, which the connectivity model
            # would otherwise hand to completion as an unconnected link.
            "--complete-exclude-nets",
            "WALL",
            "--complete-report",
            str(report),
            "--route-engine",
            "lattice",
            "--layers",
            "4",
            "--no-auto-layers",
            "--strict-layers",
            "--net-class-map",
            str(net_class_map),
            "--skip-drc",
        ]

    def test_completion_declines_the_forbidden_shortcut(self, tmp_path: Path, capsys) -> None:
        board = tmp_path / "walled.kicad_pcb"
        board.write_text(_walled_four_layer_board())
        out = tmp_path / "completed.kicad_pcb"
        report = tmp_path / "complete-report.json"

        rc = route_main(self._args(board, out, _write_net_class_map(tmp_path), report))
        captured = capsys.readouterr().out

        written = out.read_text()
        # The whole point: no forbidden copper is written as partial progress.
        assert not (_copper_layers_for_net(written, 1) & set(_INNER_LAYERS)), (
            f"/PGND copper on a forbidden layer: {_copper_layers_for_net(written, 1)}"
        )
        # Non-vacuous: the UNCONSTRAINED net took the same inner-layer
        # shortcut, so the wall really does force one (the decline above is
        # the layer constraint talking, not an impossible board).
        assert _copper_layers_for_net(written, 2) & set(_INNER_LAYERS), (
            "expected /SIG_A to cross on an inner layer"
        )
        # A declined link is an unclosed link: exit 8, not a false success,
        # and NOT the layer-intent gate's exit 3 (no forbidden copper exists).
        assert rc == 8, f"unexpected exit {rc}: {captured[-2000:]}"
        assert "ROUTING FAILED: hard layer-intent violations" not in captured

    def test_residual_is_reported_as_layer_constrained(self, tmp_path: Path, capsys) -> None:
        board = tmp_path / "walled.kicad_pcb"
        board.write_text(_walled_four_layer_board())
        out = tmp_path / "completed.kicad_pcb"
        report = tmp_path / "complete-report.json"

        route_main(self._args(board, out, _write_net_class_map(tmp_path), report))
        captured = capsys.readouterr().out

        assert report.exists(), captured[-2000:]
        links = json.loads(report.read_text())["unroutable_links"]
        pgnd = [entry for entry in links if entry["net"] == "/PGND"]
        assert pgnd, f"no /PGND residual reported: {links}"
        entry = pgnd[0]
        # Machine-readable attribution, plus the reason string itself: this
        # residual is a refusal, not congestion.
        assert entry["layer_constrained"] is True, entry
        assert "layer-constrained" in entry["reason"], entry
        assert "layer intent" in captured


class TestGateWiring:
    """The post-route gate's own contract, exercised without a full board run."""

    class _StubRouter:
        """Minimal stand-in with the attributes the gate reads."""

        def __init__(self, routed, preserved):
            self.rules = DesignRules(strict_layers=True)
            self.layer_stack = LayerStack.four_layer_sig_gnd_pwr_sig()
            self.net_class_map = {
                "/PGND": NetClassRouting(
                    name="HV",
                    trace_width=2.6,
                    clearance=0.4,
                    preferred_layers=[0, 3],
                    avoid_layers=[1, 2],
                )
            }
            self.routes = routed
            self._emitted_preserved_routes = preserved

    @staticmethod
    def _pgnd_route(layer: Layer) -> Route:
        return Route(
            net=1,
            net_name="/PGND",
            segments=[
                Segment(
                    x1=0.0,
                    y1=0.0,
                    x2=5.0,
                    y2=0.0,
                    width=2.6,
                    layer=layer,
                    net=1,
                    net_name="/PGND",
                )
            ],
        )

    def test_freshly_routed_forbidden_copper_is_a_new_violation(self) -> None:
        router = self._StubRouter([self._pgnd_route(Layer.IN2_CU)], [])

        found = _audit_layer_intent(router, args=None, id_to_name={1: "/PGND"})

        assert [(v.layer, v.inherited) for v in found] == [("In2.Cu", False)]
        # Exit-code contract: "routing succeeded but the copper is dirty".
        assert _layer_intent_escalation_exit(0, found) == 3
        assert _layer_intent_escalation_exit(2, found) == 4
        assert _layer_intent_escalation_exit(1, found) == 1  # fatal codes pass through

    def test_preserved_forbidden_copper_is_inherited_and_does_not_gate(self) -> None:
        router = self._StubRouter([], [self._pgnd_route(Layer.IN1_CU)])

        found = _audit_layer_intent(router, args=None, id_to_name={1: "/PGND"})

        assert [(v.layer, v.inherited) for v in found] == [("In1.Cu", True)]
        assert _new_layer_intent_violations(found) == []
        # Inherited-only findings are reported, never re-blamed on this run.
        assert _layer_intent_escalation_exit(0, found) == 0

    def test_inherited_only_addendum_does_not_read_as_an_accusation(self, capsys) -> None:
        """Wording check: the inherited-only fold-in never says "0 newly created".

        A user reading "0 newly created forbidden-layer segment(s) detected"
        on a run whose only findings came from the INPUT board would go
        looking for a router bug that is not there.
        """
        router = self._StubRouter([], [self._pgnd_route(Layer.IN1_CU)])
        found = _audit_layer_intent(router, args=None, id_to_name={1: "/PGND"})

        _print_layer_intent_addendum(found)
        text = capsys.readouterr().out

        assert "0 newly created" not in text
        assert "inherited from the input board" in text
        assert "[inherited] /PGND segment on In1.Cu" in text

    def test_mixed_addendum_leads_with_the_newly_created_count(self, capsys) -> None:
        router = self._StubRouter(
            [self._pgnd_route(Layer.IN2_CU)], [self._pgnd_route(Layer.IN1_CU)]
        )
        found = _audit_layer_intent(router, args=None, id_to_name={1: "/PGND"})

        _print_layer_intent_addendum(found)
        text = capsys.readouterr().out

        assert "1 newly created" in text
        assert "1 more inherited from the input board" in text

    def test_outer_layer_copper_is_a_strict_no_op(self) -> None:
        router = self._StubRouter([self._pgnd_route(Layer.F_CU)], [])

        assert _audit_layer_intent(router, args=None, id_to_name={1: "/PGND"}) == []

    def test_no_hard_constraint_is_a_strict_no_op(self) -> None:
        router = self._StubRouter([self._pgnd_route(Layer.IN2_CU)], [])
        router.rules = DesignRules(strict_layers=False)

        assert _audit_layer_intent(router, args=None, id_to_name={1: "/PGND"}) == []
