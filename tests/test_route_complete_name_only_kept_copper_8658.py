"""Kept copper on a name-only board keeps its net identity (#8658).

On a KiCad 10 name-only board (``(net "NAME")``, no numeric net table) the
optimizer parser gives every kept trace and via net id 0.  ``kct route``'s
capture of that copper (``_capture_preserved_routes``) used to pass the id on
unchanged, so the short check saw a new trace joining its own net's kept trace
as a short against "Net 0" and the run exited non-zero on copper that has no
short.

The board is fully synthetic: three 2-pin through-hole parts, nets ``/A`` and
``/B``, and one pre-existing ``/A`` trace joining two of its three pads.
:func:`_board` writes it in either dialect; it is the committed fixture.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import kicad_tools.cli.route_cmd as route_cmd
from kicad_tools.router.io import load_pcb_for_routing, validate_routes
from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer.pcb import parse_segments
from kicad_tools.router.primitives import Route, Segment

PARTS = (("U1", 110), ("U2", 130), ("U3", 150))
NET_IDS = {"/A": 1, "/B": 2}
KEPT_A = '(segment (start 110 110) (end 130 110) (width 0.25) (layer "F.Cu") {net} (uuid "00000000-0000-4000-8000-000000000101"))'
# An empty inline name is KiCad's no-net reference: copper that belongs to no net.
ANONYMOUS = '(segment (start 105 122) (end 108 122) (width 0.25) (layer "F.Cu") (net "") (uuid "00000000-0000-4000-8000-000000000102"))'


def _net(name: str, *, name_only: bool, with_name: bool = True) -> str:
    if name_only:
        return f'(net "{name}")'
    return f'(net {NET_IDS[name]} "{name}")' if with_name else f"(net {NET_IDS[name]})"


def _footprint(index: int, ref: str, x: int, *, name_only: bool) -> str:
    pad = '(pad "{pin}" thru_hole circle (at 0 {y}) (size 2 2) (drill 0.9) (layers "*.Cu" "*.Mask") {net})'
    return "\n".join(
        [
            f'  (footprint "Synthetic:TH2_{ref}" (layer "F.Cu") (uuid "00000000-0000-4000-8000-{index:012d}") (at {x} 110)',
            f'    (property "Reference" "{ref}" (at 2.5 -2.5 0) (layer "F.SilkS"))',
            '    (property "Value" "syn" (at 2.5 2.5 0) (layer "F.Fab"))',
            "    " + pad.format(pin=1, y=0, net=_net("/A", name_only=name_only)),
            "    " + pad.format(pin=2, y=5.08, net=_net("/B", name_only=name_only)) + ")",
        ]
    )


def _board(tmp_path: Path, *, name_only: bool, anonymous: bool = False) -> Path:
    """Write the synthetic board in the numeric or the name-only dialect."""
    table = "" if name_only else '  (net 0 "") (net 1 "/A") (net 2 "/B")\n'
    version = "20260206" if name_only else "20240108"
    footprints = "\n".join(
        _footprint(i + 1, ref, x, name_only=name_only) for i, (ref, x) in enumerate(PARTS)
    )
    kept = "  " + KEPT_A.format(net=_net("/A", name_only=name_only, with_name=False)) + "\n"
    if anonymous:
        kept += f"  {ANONYMOUS}\n"
    path = tmp_path / f"{'name_only' if name_only else 'numeric'}.kicad_pcb"
    path.write_text(
        f'(kicad_pcb (version {version}) (generator "synthetic-8658") (generator_version "10.0")\n'
        "  (general (thickness 1.6))\n"
        '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
        "  (setup (pad_to_mask_clearance 0))\n"
        f"{table}"
        "  (gr_rect (start 100 100) (end 160 125) (stroke (width 0.1) (type default))"
        ' (fill none) (layer "Edge.Cuts"))\n'
        f"{footprints}\n"
        f"{kept}"
        ")\n"
    )
    return path


def _complete(board: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, Path]:
    out = board.with_name(f"out_{board.name}")
    rc = route_cmd.main([str(board), "-o", str(out), "--complete", "--skip-drc", "--no-cache"])
    captured = capsys.readouterr()
    return rc, captured.out + captured.err, out


def test_numeric_board_completes_without_shorts(tmp_path, capsys):
    rc, text, _ = _complete(_board(tmp_path, name_only=False), capsys)
    assert rc == 0, text
    assert "routing short" not in text


def test_name_only_board_completes_without_phantom_shorts(tmp_path, capsys):
    rc, text, out = _complete(_board(tmp_path, name_only=True), capsys)
    assert "vs Net 0" not in text
    assert "routing short" not in text
    assert rc == 0, text
    written = out.read_text()
    # --complete keeps the copper it joins, and the board stays name-only.
    kept = [s for s in parse_segments(written)["/A"] if s.width == 0.25]
    assert [(s.x1, s.y1, s.x2, s.y2) for s in kept] == [(110.0, 110.0, 130.0, 110.0)]
    assert re.search(r"\(net\s+\d", written) is None


@pytest.mark.parametrize("name_only", [False, True])
def test_capture_uses_the_loader_net_ids(tmp_path, name_only):
    board = _board(tmp_path, name_only=name_only)
    captured = route_cmd._capture_preserved_routes(board)
    router, net_map = load_pcb_for_routing(str(board), load_existing_routes=True)

    assert [(r.net, r.net_name) for r in captured] == [(net_map["/A"], "/A")]
    assert {r.net_name: r.net for r in captured} == {
        r.net_name: r.net for r in router.existing_routes
    }
    assert {s.net for r in captured for s in r.segments} == {net_map["/A"]}
    if not name_only:
        assert net_map["/A"] == NET_IDS["/A"]


def test_anonymous_kept_copper_stays_net_zero(tmp_path):
    captured = route_cmd._capture_preserved_routes(_board(tmp_path, name_only=True, anonymous=True))
    by_id = {r.net: r for r in captured}
    assert set(by_id) == {0, 1}
    assert [(s.x1, s.y1) for s in by_id[0].segments] == [(105.0, 122.0)]
    assert {s.net for s in by_id[0].segments} == {0}


def _shorts_against_kept(board: Path, routed_net: str):
    """Short findings for a routed trace laid across the kept ``/A`` trace."""
    router, net_map = load_pcb_for_routing(str(board), load_existing_routes=True)
    router._emitted_preserved_routes = route_cmd._capture_preserved_routes(board)
    net = net_map[routed_net]
    crossing = Segment(120.0, 105.0, 120.0, 115.0, 0.25, Layer.F_CU, net=net, net_name=routed_net)
    router.routes = [Route(net=net, net_name=routed_net, segments=[crossing])]
    return [
        v for v in validate_routes(router) if v.obstacle_type == "segment" and v.distance <= 0.0
    ]


def test_name_only_real_short_against_kept_copper_is_still_reported(tmp_path):
    """A ``/B`` trace across the kept ``/A`` trace is a short between those two nets."""
    board = _board(tmp_path, name_only=True)
    shorts = _shorts_against_kept(board, "/B")
    assert shorts, "a different-net trace across kept copper must be reported"
    assert {(v.net_name, v.obstacle_net_name) for v in shorts} == {("/B", "/A")}
    assert all(v.obstacle_net != 0 and v.obstacle_net != v.net for v in shorts)


def test_name_only_same_net_contact_with_kept_copper_is_not_a_short(tmp_path):
    assert _shorts_against_kept(_board(tmp_path, name_only=True), "/A") == []
