"""The chained ``--complete`` pass inherits the recipe's constraints (Issue #8674).

``kct route`` chains a lattice ``--complete`` pass after a grid route that left
signal nets open.  That inner pass used to run without the outer run's
``--layers``, ``--voltage-map`` (and its lookup settings), ``--edge-clearance``
and ``--current-paths``, so it re-detected its own layer stack, dropped the HV
pairwise table and fell back to the manufacturer edge clearance.

The board here is generated in the test: synthetic, generic net names, not
derived from any real design.  It is a 60 x 40 mm 4-layer board with a GND pour
on ``In1.Cu`` and three open links, one per constraint:

* ``/SIG_A`` -- one pad is ringed by foreign copper on both outer layers, so the
  only way out is an inner layer.  Under ``--layers 4`` both inner layers are
  planes and the link must stay open.
* ``/SIG_B`` -- an ``F.Cu`` wall stops 2 mm short of the board edge.  The short
  way round runs through that gap, inside ``--edge-clearance 1.6``; the legal
  routes are the long way round or under the wall on ``B.Cu``.
* ``/SIG_C`` -- the short path runs through a 3 mm gap in a 150 V net's copper,
  1.25 mm from each side where IEC 60664-1 (PD2, IIIa) requires 1.6 mm.

Only the outer grid negotiation is stubbed (it writes the generated stage
board, as a route that left those links open would).  Argument parsing, the
real ``_chain_complete_pass`` and the real inner ``--complete`` run are not.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

import kicad_tools.cli.route_cmd as route_cmd

OX, OY, W, H = 100.0, 100.0, 60.0, 40.0
EDGE_CLEARANCE_MM = 1.6
POUR_LAYER = "In1.Cu"
TRACK_W = 0.25


def _seg(x1: float, y1: float, x2: float, y2: float, layer: str, net: int) -> str:
    return (
        f"  (segment (start {x1} {y1}) (end {x2} {y2}) (width {TRACK_W}) "
        f'(layer "{layer}") (net {net}))'
    )


def _tht(ref: str, x: float, y: float, net: int, name: str) -> str:
    return f"""  (footprint "Test:THT" (layer "F.Cu") (at {x} {y})
    (property "Reference" "{ref}" (at 0 -3 0) (layer "F.SilkS"))
    (property "Value" "x" (at 0 3 0) (layer "F.Fab"))
    (pad "1" thru_hole circle (at 0 0) (size 2.4 2.4) (drill 1.2) (layers "*.Cu" "*.Mask") (net {net} "{name}")))"""


def _smd(ref: str, x: float, y: float, net: int, name: str) -> str:
    return f"""  (footprint "Test:SMD" (layer "F.Cu") (at {x} {y})
    (property "Reference" "{ref}" (at 0 -2 0) (layer "F.SilkS"))
    (property "Value" "x" (at 0 2 0) (layer "F.Fab"))
    (pad "1" smd rect (at 0 0) (size 1.0 1.0) (layers "F.Cu" "F.Mask") (net {net} "{name}")))"""


def _stage_board() -> str:
    """The board a grid route would hand the chained pass: three links open."""
    gnd, hv, wall, sig_a, sig_b, sig_c = 1, 2, 3, 4, 5, 6
    parts = [
        _tht("J1", 104, 136, gnd, "GND"),
        # /SIG_A: A1 is ringed on both outer layers; A2 is free.
        _tht("J2", 108, 110, sig_a, "/SIG_A"),
        _tht("J3", 108, 130, sig_a, "/SIG_A"),
        # /SIG_B: either side of an F.Cu wall that leaves a 2 mm gap at the top edge.
        _smd("R1", 127, 103, sig_b, "/SIG_B"),
        _smd("R2", 133, 103, sig_b, "/SIG_B"),
        # /HV_A (150 V): two pads joined on B.Cu, with F.Cu stubs leaving a 3 mm gap.
        _tht("J4", 148, 116, hv, "/HV_A"),
        _tht("J5", 148, 134, hv, "/HV_A"),
        # /SIG_C: either side of the F.Cu gap.
        _smd("R3", 143, 125, sig_c, "/SIG_C"),
        _smd("R4", 153, 125, sig_c, "/SIG_C"),
    ]
    copper = []
    for layer in ("F.Cu", "B.Cu"):
        copper += [
            _seg(105, 107, 111, 107, layer, wall),
            _seg(111, 107, 111, 113, layer, wall),
            _seg(111, 113, 105, 113, layer, wall),
            _seg(105, 113, 105, 107, layer, wall),
        ]
    copper += [
        _seg(130, 102, 130, 136, "F.Cu", wall),
        _seg(148, 116, 148, 123.5, "F.Cu", hv),
        _seg(148, 134, 148, 126.5, "F.Cu", hv),
        _seg(148, 116, 156, 116, "B.Cu", hv),
        _seg(156, 116, 156, 134, "B.Cu", hv),
        _seg(156, 134, 148, 134, "B.Cu", hv),
    ]
    x0, y0, x1, y1 = OX + 0.5, OY + 0.5, OX + W - 0.5, OY + H - 0.5
    pour = f"({x0} {y0}) ({x1} {y0}) ({x1} {y1}) ({x0} {y1})".replace("(", "(xy ")
    newline = "\n"
    return f"""(kicad_pcb
  (version 20260206)
  (generator "pcbnew")
  (generator_version "10.0")
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (4 "In1.Cu" power)
    (6 "In2.Cu" power)
    (2 "B.Cu" signal)
    (5 "F.SilkS" user "F.Silkscreen")
    (1 "F.Mask" user)
    (3 "B.Mask" user)
    (25 "Edge.Cuts" user)
    (35 "F.Fab" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net {gnd} "GND")
  (net {hv} "/HV_A")
  (net {wall} "/WALL")
  (net {sig_a} "/SIG_A")
  (net {sig_b} "/SIG_B")
  (net {sig_c} "/SIG_C")
  (gr_rect (start {OX} {OY}) (end {OX + W} {OY + H})
    (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
{newline.join(parts)}
{newline.join(copper)}
  (zone
    (net "GND")
    (layer "{POUR_LAYER}")
    (hatch edge 0.5)
    (connect_pads yes (clearance 0.2))
    (min_thickness 0.2)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
    (polygon (pts {pour}))
    (filled_polygon (layer "{POUR_LAYER}") (pts {pour}))
  )
)
"""


VOLTAGES = {"/HV_A": 150.0, "/SIG_C": 5.0}
_SEGMENT = re.compile(
    r"\(segment\s+\(start\s+([-\d.]+)\s+([-\d.]+)\)\s+\(end\s+([-\d.]+)\s+([-\d.]+)\)\s+"
    r'\(width\s+([-\d.]+)\)\s+\(layer\s+"([^"]+)"\)',
    re.DOTALL,
)
_VIA = re.compile(r"\(via\s+(?:\w+\s+)?\(at\s+([-\d.]+)\s+([-\d.]+)\)\s+\(size\s+([-\d.]+)\)")


def _copper(pcb: Path) -> tuple[set, set]:
    text = pcb.read_text()
    segments = {
        (float(a), float(b), float(c), float(d), float(w), layer)
        for a, b, c, d, w, layer in _SEGMENT.findall(text)
    }
    vias = {(float(x), float(y), float(size)) for x, y, size in _VIA.findall(text)}
    return segments, vias


def _edge_gap(x: float, y: float, radius: float) -> float:
    """Copper-to-edge distance of a disc of ``radius`` centred on (x, y)."""
    return min(x - OX, OX + W - x, y - OY, OY + H - y) - radius


def _failing_pairs(pcb: Path, vmap: Path) -> set[tuple[str, str]]:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "creepage",
            str(pcb),
            "--voltage-map",
            str(vmap),
            "--standard",
            "iec60664",
            "--pollution-degree",
            "2",
            "--material-group",
            "IIIa",
            "--waive-same-footprint",
            "--waive-attach-zone",
            "--format",
            "json",
        ],  # fmt: skip
        capture_output=True,
        text=True,
    )
    pairs = json.loads(proc.stdout)["pairs"]
    return {tuple(sorted((p["net_a"], p["net_b"]))) for p in pairs if not p["pass"]}


def _route_with_stubbed_negotiation(tmp: Path, recipe: list[str]) -> dict:
    """Run ``kct route`` with the outer negotiation replaced by the stage board."""
    stage = tmp / "stage.kicad_pcb"
    stage.write_text(_stage_board())
    board = tmp / "board.kicad_pcb"
    board.write_text(_stage_board())
    vmap = tmp / "voltage_map.json"
    vmap.write_text(json.dumps(VOLTAGES))
    out = tmp / "routed.kicad_pcb"
    outer_argv = [
        str(board), "-o", str(out),
        "--layers", "4",
        "--edge-clearance", str(EDGE_CLEARANCE_MM),
        "--no-auto-pour",
        "--seed", "1",
        *(str(vmap) if token == "VMAP" else token for token in recipe),
    ]  # fmt: skip

    real_run = route_cmd._run_main_impl
    inner_argv: list[list[str]] = []

    def run_main_impl(args, parser, argv):
        if getattr(args, "complete", False):
            inner_argv.append(list(argv))
            return real_run(args, parser, argv)
        # The outer grid negotiation: routed everything but the three links.
        Path(args.output).write_text(stage.read_text())
        return 2

    import io
    from contextlib import redirect_stdout

    patch = pytest.MonkeyPatch()
    patch.setattr(route_cmd, "_run_main_impl", run_main_impl)
    log = io.StringIO()
    try:
        with redirect_stdout(log):
            rc = route_cmd.main(outer_argv)
    finally:
        patch.undo()
    return {
        "rc": rc,
        "stage": stage,
        "out": out,
        "vmap": vmap,
        "log": log.getvalue(),
        "inner_argv": inner_argv,
    }


@pytest.fixture(scope="module")
def chained(tmp_path_factory):
    return _route_with_stubbed_negotiation(
        tmp_path_factory.mktemp("chain8674"),
        [
            "--voltage-map",
            "VMAP",
            "--creepage-standard",
            "iec60664",
            "--pollution-degree",
            "2",
            "--material-group",
            "IIIa",
            "--no-current-paths",
            "--skip-drc",
        ],  # fmt: skip
    )


def _new_copper(chained) -> tuple[set, set]:
    stage_segments, stage_vias = _copper(chained["stage"])
    segments, vias = _copper(chained["out"])
    return segments - stage_segments, vias - stage_vias


def test_chained_pass_ran_and_added_copper(chained):
    """Guards the tests below against passing on a pass that never ran."""
    assert len(chained["inner_argv"]) == 1, chained["log"]
    assert "Chained completion pass closed" in chained["log"], chained["log"]
    segments, _vias = _new_copper(chained)
    assert segments, "the chained pass added no copper; the fixture checks nothing"


def test_inner_pass_inherits_the_explicit_layer_stack(chained):
    argv = chained["inner_argv"][0]
    assert argv[argv.index("--layers") + 1] == "4"
    # The inner pass printed the named stack, not an auto-detected one.
    inner_log = chained["log"].split("Chained completion pass:", 1)[1]
    assert "auto-detected" not in inner_log
    assert "SIG-SIG-GND-PWR" not in inner_log


def test_no_completion_copper_on_the_pour_layer(chained):
    segments, _vias = _new_copper(chained)
    on_planes = sorted(s for s in segments if s[5] in (POUR_LAYER, "In2.Cu"))
    assert not on_planes, f"completion copper on a plane layer: {on_planes[:5]}"


def test_no_completion_copper_inside_the_edge_clearance(chained):
    segments, vias = _new_copper(chained)
    close = [
        s
        for s in segments
        if min(_edge_gap(s[0], s[1], s[4] / 2), _edge_gap(s[2], s[3], s[4] / 2))
        < EDGE_CLEARANCE_MM - 1e-6
    ]
    close += [v for v in vias if _edge_gap(v[0], v[1], v[2] / 2) < EDGE_CLEARANCE_MM - 1e-6]
    assert not close, f"completion copper inside {EDGE_CLEARANCE_MM}mm of the edge: {close[:5]}"


def test_completion_load_reports_the_hv_pairwise_clearance(chained):
    inner_log = chained["log"].split("Chained completion pass:", 1)[1]
    assert f"HV pairwise clearance: {len(VOLTAGES)} mapped nets, 1 cross-pairs" in inner_log


def test_completion_adds_no_failing_creepage_pair(chained):
    before = _failing_pairs(chained["stage"], chained["vmap"])
    after = _failing_pairs(chained["out"], chained["vmap"])
    assert after <= before, f"completion copper added failing pairs: {sorted(after - before)}"


def test_edge_link_is_closed_the_legal_way(chained):
    """The constraints must not simply stop the pass: /SIG_B has a legal route."""
    from kicad_tools.router.partial_rescue import partially_connected_signal_nets

    still_open = set(
        partially_connected_signal_nets(
            chained["out"], manufacturer="jlcpcb", include_unrouted=True
        )
    )
    assert "/SIG_B" not in still_open
    # Both outer layers ring J2 and the inner layers are planes: no legal route.
    assert "/SIG_A" in still_open


def test_pass_violating_a_declared_current_path_is_refused(tmp_path):
    """``--current-paths`` reaches the pass and its audit decides promotion.

    No search plans by the declared paths; the pass's post-route DRC audits them.
    /SIG_B is declared a 10 A branch, which the pass closes with a signal-width
    track, so the chain must keep the negotiated board and say why.
    """
    paths = tmp_path / "declared_paths.json"
    paths.write_text(
        json.dumps(
            {
                "paths": [
                    {
                        "name": "B_TRUNK",
                        "net": "/SIG_B",
                        "source": {"ref": "R1", "pad": "1"},
                        "sink": {"ref": "R2", "pad": "1"},
                        "continuous_a": 10.0,
                    }
                ]
            }
        )
    )
    run = _route_with_stubbed_negotiation(tmp_path, ["--current-paths", str(paths)])
    argv = run["inner_argv"][0]
    assert argv[argv.index("--current-paths") + 1] == str(paths)
    assert "path_ampacity: Current path 'B_TRUNK'" in run["log"]
    assert "Chained completion pass refused" in run["log"], run["log"]
    assert run["out"].read_text() == run["stage"].read_text()
    assert not list(tmp_path.glob("*.chain*"))
