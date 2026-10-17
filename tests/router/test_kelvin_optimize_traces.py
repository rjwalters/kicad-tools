"""``kct optimize-traces`` must not join Kelvin branches outside the shunt (#6889).

#5398 gated the post-route optimizer inside ``kct route``.  ``kct
optimize-traces`` rewrites the segments of an already-routed board through a
different entry point (:func:`kicad_tools.router.optimizer.pcb.optimize_pcb`),
so it needs the same gate, built from the board file's own Kelvin topology.

The fixture is the #5398 geometry written out as a board: a 45-degree
chamfer on the force branch's corner lands on the sense branch.
"""

from __future__ import annotations

from pathlib import Path

from shapely.geometry import LineString, box
from shapely.ops import unary_union

from kicad_tools.cli import main as cli_main
from kicad_tools.router.optimizer.pcb import parse_segments

SHUNT_METAL = box(1.6, 9.6, 2.4, 10.4)
FORCE = [(2, 10), (8, 10), (8, 14)]
# Clear of the force corner at (8, 10), inside the 0.5 mm chamfer that replaces it.
SENSE = [(2, 10), (2, 12.6), (7.55, 12.6), (7.55, 10.3)]


def _footprint(ref: str, lib: str, x: float, y: float, size: float, net: str) -> str:
    return (
        f'\t(footprint "{lib}"\n'
        '\t\t(layer "F.Cu")\n'
        f'\t\t(uuid "fp-{ref}")\n'
        f"\t\t(at {x} {y})\n"
        f'\t\t(property "Reference" "{ref}" (at 0 -1.5) (layer "F.SilkS") (uuid "ref-{ref}"))\n'
        f'\t\t(pad "1" smd rect (at 0 0) (size {size} {size}) '
        f'(layers "F.Cu" "F.Paste" "F.Mask") (net 1 "{net}"))\n'
        "\t)\n"
    )


def _segments(points, net: int, tag: str) -> str:
    return "".join(
        f"\t(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width 0.2) "
        f'(layer "F.Cu") (net {net}) (uuid "{tag}-{i}"))\n'
        for i, (a, b) in enumerate(zip(points, points[1:], strict=False))
    )


def star_pcb(net_name: str, sense=SENSE, sense_pad: float = 0.4) -> str:
    """A routed two-branch star: shunt R1, force terminal Q1, sense terminal U1."""
    return (
        "(kicad_pcb\n"
        "\t(version 20240108)\n"
        '\t(generator "test")\n'
        "\t(general\n\t\t(thickness 1.6)\n\t)\n"
        '\t(paper "A4")\n'
        '\t(layers\n\t\t(0 "F.Cu" signal)\n\t\t(31 "B.Cu" signal)\n'
        '\t\t(44 "Edge.Cuts" user)\n\t)\n'
        "\t(setup\n\t\t(pad_to_mask_clearance 0)\n\t)\n"
        '\t(net 0 "")\n'
        f'\t(net 1 "{net_name}")\n'
        "\t(gr_rect (start 0 0) (end 20 20) (stroke (width 0.1) (type default)) "
        '(fill none) (layer "Edge.Cuts") (uuid "edge"))\n'
        + _footprint("R1", "Resistor_SMD:R_2512_6332Metric", 2, 10, 0.8, net_name)
        + _footprint("Q1", "Package_TO_SOT_SMD:SOT-23", 8, 14, 0.8, net_name)
        + _footprint("U1", "Package_SO:SOIC-8", sense[-1][0], sense[-1][1], sense_pad, net_name)
        + _segments(FORCE, 1, "force")
        + _segments(sense, 1, "sense")
        + ")\n"
    )


def branch_count(pcb_text: str, net_name: str) -> int:
    """Separate pieces of the net's copper once the shunt pad is cut away."""
    segments = parse_segments(pcb_text)[net_name]
    copper = unary_union(
        [LineString([s.start, s.end]).buffer(s.width / 2) for s in segments]
    ).difference(SHUNT_METAL)
    return len(getattr(copper, "geoms", [copper]))


def _optimize(tmp_path: Path, text: str) -> str:
    board = tmp_path / "board.kicad_pcb"
    board.write_text(text)
    out = tmp_path / "out.kicad_pcb"
    assert cli_main(["optimize-traces", str(board), "-o", str(out), "--quiet"]) == 0
    return out.read_text()


def test_kelvin_star_stays_a_star(tmp_path, capsys):
    text = star_pcb("ISENSE_TEST")
    assert branch_count(text, "ISENSE_TEST") == 2
    out = _optimize(tmp_path, text)
    assert branch_count(out, "ISENSE_TEST") == 2
    assert "Kelvin branch isolation: 1 route(s)" in capsys.readouterr().err


def test_kelvin_branches_still_optimized_when_apart(tmp_path):
    """The gate blocks contact only: a rewrite that keeps branches apart still lands."""
    far_sense = [(2, 10), (2, 12.6), (6.0, 12.6), (6.0, 11.4)]
    text = star_pcb("ISENSE_TEST", sense=far_sense)
    out = _optimize(tmp_path, text)
    assert branch_count(out, "ISENSE_TEST") == 2
    assert len(parse_segments(out)["ISENSE_TEST"]) > len(parse_segments(text)["ISENSE_TEST"])


def test_rewrite_onto_another_terminals_pad_is_rejected(tmp_path, capsys):
    """The chamfer would land on the sense terminal's pad, not on its route."""
    # U1's 0.6 mm pad at (7.55, 10.45) sits inside the chamfer's sweep while
    # the sense route, coming straight down to the pad, stays clear of it.
    sense = [(2, 10), (2, 12.6), (7.55, 12.6), (7.55, 10.45)]
    out = _optimize(tmp_path, star_pcb("ISENSE_TEST", sense=sense, sense_pad=0.6))
    kept = {(s.start, s.end) for s in parse_segments(out)["ISENSE_TEST"]}
    assert set(zip(FORCE, FORCE[1:], strict=False)) <= kept  # force corner not chamfered
    assert "Kelvin branch isolation: 1 route(s)" in capsys.readouterr().err


def test_non_kelvin_board_unchanged(tmp_path):
    """An ordinary net with the same copper is optimized exactly as before."""
    text = star_pcb("PWM_TEST")
    out = _optimize(tmp_path, text)
    assert branch_count(out, "PWM_TEST") == 1  # same-net merge is fine here
