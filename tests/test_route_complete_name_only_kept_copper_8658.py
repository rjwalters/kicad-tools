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
from dataclasses import replace
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


# --- Dedupe of kept copper against re-routed nets (name-only dialect) -------

# A trace on a net no pad carries: nothing can re-route it.
PADLESS_C = '(segment (start 103 121) (end 108 121) (width 0.3) (layer "F.Cu") (net "/C") (uuid "00000000-0000-4000-8000-000000000103"))'
# A kept ``/B`` trace, for the board whose copper precedes its footprints.
KEPT_B = '(segment (start 110 115.08) (end 130 115.08) (width 0.25) (layer "F.Cu") (net "/B") (uuid "00000000-0000-4000-8000-000000000201"))'
KEPT_A_XY = (110.0, 110.0, 130.0, 110.0)
KEPT_B_XY = (110.0, 115.08, 130.0, 115.08)
PADLESS_C_XY = (103.0, 121.0, 108.0, 121.0)
ANONYMOUS_XY = (105.0, 122.0, 108.0, 122.0)


def _kept_board(tmp_path: Path) -> Path:
    """Name-only board with the kept ``/A`` trace, a pad-less and an anonymous one."""
    board = _board(tmp_path, name_only=True, anonymous=True)
    board.write_text(board.read_text().rstrip()[:-1] + f"  {PADLESS_C}\n)\n")
    return board


def _drift_board(tmp_path: Path) -> Path:
    """Name-only board whose kept ``/B`` trace is written ahead of the footprints.

    ``/B`` is then the first net name in the file, so it gets synthetic id 1.
    A kct rewrite moves all copper behind the footprints, where ``/A`` is seen
    first: the same two names swap ids between the input and the output.
    """
    board = _board(tmp_path, name_only=True)
    text = board.read_text()
    assert text.count("  (gr_rect") == 1
    board.write_text(text.replace("  (gr_rect", f"  {KEPT_B}\n  (gr_rect"))
    return board


def _route(board: Path, capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, Path]:
    out = board.with_name(f"out_{board.name}")
    rc = route_cmd.main([str(board), "-o", str(out), "--skip-drc", "--no-cache", *args])
    captured = capsys.readouterr()
    return rc, captured.out + captured.err, out


def _kept_xy(text: str) -> set[tuple[float, float, float, float]]:
    """Coordinates of the input's kept traces that ``text`` still carries."""
    known = {KEPT_A_XY, KEPT_B_XY, PADLESS_C_XY, ANONYMOUS_XY}
    found = {
        (s.x1, s.y1, s.x2, s.y2)
        for segs in parse_segments(text).values()
        for s in segs
        if s.width in (0.25, 0.3)
    }
    return found & known


def _blocks_xy(sexp: str) -> set[tuple[float, float, float, float]]:
    return _kept_xy(f"(kicad_pcb\n{sexp}\n)")


def test_plain_preserve_existing_name_only_drops_only_the_rerouted_nets_copper(tmp_path, capsys):
    """The disclosed behaviour change: stale kept copper of a re-routed net goes."""
    rc, text, out = _route(_kept_board(tmp_path), capsys, "--preserve-existing")
    assert rc == 0, text
    written = out.read_text()
    # /A is re-routed, so its stale kept trace is replaced by the routed copy.
    assert _kept_xy(written) == {PADLESS_C_XY, ANONYMOUS_XY}
    assert parse_segments(written)["/A"], "the re-routed net must still have copper"
    assert re.search(r"\(net\s+\d", written) is None


def test_plain_preserve_existing_name_only_keeps_copper_of_nets_it_does_not_route(tmp_path, capsys):
    rc, text, out = _route(_kept_board(tmp_path), capsys, "--preserve-existing", "--nets", "/B")
    assert rc == 0, text
    assert _kept_xy(out.read_text()) == {KEPT_A_XY, PADLESS_C_XY, ANONYMOUS_XY}


def test_dedupe_preserved_sexp_name_only(tmp_path):
    """The checkpoint / partial-save dedupe, on name-only kept copper."""
    board = _kept_board(tmp_path)
    captured = route_cmd._capture_preserved_routes(board)
    sexp = route_cmd._serialize_preserved_routes(captured, name_only=True)
    router, net_map = load_pcb_for_routing(str(board), load_existing_routes=True)
    assert _blocks_xy(sexp) == {KEPT_A_XY, PADLESS_C_XY, ANONYMOUS_XY}

    def dedupe(routed_names, **kwargs):
        routed = [Route(net=net_map[n], net_name=n) for n in routed_names]
        return _blocks_xy(
            route_cmd._dedupe_preserved_sexp(
                sexp,
                captured,
                routed,
                name_only=True,
                board_net_ids=router.board_net_ids,
                **kwargs,
            )
        )

    # Copper of the re-routed net goes; every other net's copper stays.
    assert dedupe(["/A"]) == {PADLESS_C_XY, ANONYMOUS_XY}
    assert dedupe(["/B"]) == {KEPT_A_XY, PADLESS_C_XY, ANONYMOUS_XY}
    assert dedupe(["/A", "/B"]) == {PADLESS_C_XY, ANONYMOUS_XY}
    # A stub net keeps its copper although it is re-routed ...
    assert dedupe(["/A"], stub_net_ids={net_map["/A"]}) == {
        KEPT_A_XY,
        PADLESS_C_XY,
        ANONYMOUS_XY,
    }
    # ... and so does every net with kept copper under --complete.
    router._preserve_listed_copper = True
    assert net_map["/A"] in route_cmd._kept_preserved_net_ids(router)
    assert dedupe(["/A"], stub_net_ids=route_cmd._kept_preserved_net_ids(router)) == {
        KEPT_A_XY,
        PADLESS_C_XY,
        ANONYMOUS_XY,
    }


def _second_attempt(tmp_path, capsys):
    """Capture from the pristine board; router loaded from kct's rewrite of it.

    This is what a later layer-escalation attempt holds: the capture is taken
    once from the pristine staged file, and the attempt's router re-reads the
    file after a kct write.  The net ids of the two texts differ.
    """
    board = _drift_board(tmp_path)
    captured = route_cmd._capture_preserved_routes(board)
    rc, text, rewritten = _route(board, capsys, "--preserve-existing", "--nets", "/A")
    assert rc == 0, text
    router, net_map = load_pcb_for_routing(str(rewritten), load_existing_routes=True)
    captured_ids = {r.net_name: r.net for r in captured}
    # The shape under test: the same names, different ids in the two texts.
    assert captured_ids == {"/B": 1, "/A": 2}
    assert {name: net_map[name] for name in ("/A", "/B")} == {"/A": 1, "/B": 2}
    return captured, router, net_map, rewritten


def test_drifted_ids_do_not_drop_kept_copper_of_a_net_that_was_not_rerouted(tmp_path, capsys):
    captured, router, net_map, _ = _second_attempt(tmp_path, capsys)
    sexp = route_cmd._serialize_preserved_routes(captured, name_only=True)
    assert _blocks_xy(sexp) == {KEPT_A_XY, KEPT_B_XY}
    # Only /A is re-routed.  Its id in this router is the CAPTURED id of /B.
    routed = [Route(net=net_map["/A"], net_name="/A")]
    assert routed[0].net == next(r.net for r in captured if r.net_name == "/B")

    checkpoint = route_cmd._dedupe_preserved_sexp(
        sexp,
        captured,
        routed,
        stub_net_ids=route_cmd._kept_preserved_net_ids(router),
        name_only=True,
        board_net_ids=router.board_net_ids,
    )
    assert _blocks_xy(checkpoint) == {KEPT_B_XY}

    final = route_cmd._serialize_preserved_routes(
        captured,
        exclude_net_ids={r.net for r in routed},
        name_only=True,
        board_net_ids=router.board_net_ids,
    )
    assert _blocks_xy(final) == {KEPT_B_XY}


def test_drifted_ids_finalize_writes_and_records_the_right_kept_copper(tmp_path, capsys):
    """``_finalize_routes`` on a second-attempt router with a pristine capture."""
    captured, router, net_map, rewritten = _second_attempt(tmp_path, capsys)
    net_a = net_map["/A"]
    new_a = Segment(110.0, 110.0, 150.0, 110.0, 0.2, Layer.F_CU, net=net_a, net_name="/A")
    router.routes = [Route(net=net_a, net_name="/A", segments=[new_a])]

    route_sexp, _, _ = route_cmd._finalize_routes(
        router,
        {net_a},
        1,
        quiet=True,
        preserve_existing=True,
        preserved_routes=captured,
        pcb_path=rewritten,
    )

    assert _blocks_xy(route_sexp) == {KEPT_B_XY}
    # The gate audits the kept copper under THIS router's ids, not the capture's.
    emitted = router._emitted_preserved_routes
    assert [(r.net_name, r.net) for r in emitted] == [("/B", net_map["/B"])]
    assert {s.net for r in emitted for s in r.segments} == {net_map["/B"]}
    # The capture itself is not mutated: a later attempt re-keys it afresh.
    assert {r.net_name: r.net for r in captured} == {"/B": 1, "/A": 2}


def test_kept_copper_the_current_router_cannot_name_is_never_dropped():
    """Anonymous copper, and a name absent from the router's map, match nothing."""
    seg = Segment(1.0, 1.0, 2.0, 1.0, 0.25, Layer.F_CU, net=0, net_name="")
    anonymous = Route(net=0, net_name="", segments=[seg])
    unknown = Route(net=1, net_name="/GONE", segments=[replace(seg, net=1, net_name="/GONE")])
    routed_ids = {0, 1}
    board_net_ids = {"/A": 1, "/B": 2}
    for route in (anonymous, unknown):
        assert not route_cmd._preserved_route_is_rerouted(route, routed_ids, board_net_ids)
    kept = route_cmd._serialize_preserved_routes(
        [anonymous, unknown],
        exclude_net_ids=routed_ids,
        name_only=True,
        board_net_ids=board_net_ids,
    )
    assert kept.count("(segment") == 2
