"""Exercise real routed and checkpoint copper across placement exclusions."""

import pytest

from kicad_tools.cli import route_cmd
from kicad_tools.placement.routing import analyze_routing_placement
from kicad_tools.router.io import load_pcb_for_routing
from kicad_tools.schema.pcb import PCB
from tests.test_routing_placement_disposition import board_text


@pytest.mark.parametrize("name_only", [False, True])
@pytest.mark.parametrize("preservation", ["none", "captured", "loader"])
def test_final_output_keeps_exact_excluded_blocks(tmp_path, name_only, preservation):
    board = tmp_path / "source.kicad_pcb"
    text = board_text(name_only=name_only).replace(
        "(segment (start 105 103)",
        '(segment (uuid "00000000-0000-0000-0000-000000000123") (locked yes) (start 105 103)',
    )
    board.write_text(text)
    original = board.read_bytes()
    disposition = analyze_routing_placement(board)
    router, nets = load_pcb_for_routing(
        str(board),
        placement_disposition=disposition,
        force_python=True,
        load_existing_routes=preservation != "none",
    )
    router.routes = router.route_net(nets["GOOD"])
    assert router.routes and any(r.segments for r in router.routes)
    captured = route_cmd._capture_preserved_routes(board) if preservation == "captured" else None
    sexp, _, _ = route_cmd._finalize_routes(
        router,
        {nets["GOOD"]},
        1,
        quiet=True,
        preserve_existing=preservation != "none",
        preserved_routes=captured,
        pcb_path=board,
    )
    output = tmp_path / "out.kicad_pcb"
    route_cmd._write_routed_pcb(board, output, sexp)
    for block in router.placement_preserved_copper.splitlines():
        assert output.read_text().count(block) == 1
    pcb = PCB.load(output)
    assert len([s for s in pcb.segments if s.net_name == "BAD"]) == 1
    assert len([v for v in pcb.vias if v.net_name == "BAD"]) == 1
    assert any(s.net_name == "GOOD" for s in pcb.segments)
    assert board.read_bytes() == original


@pytest.mark.parametrize("name_only", [False, True])
@pytest.mark.parametrize("preserve", [False, True])
def test_checkpoint_preserves_excluded_copper_before_reload(tmp_path, name_only, preserve):
    from types import SimpleNamespace

    board = tmp_path / "source.kicad_pcb"
    board.write_text(board_text(name_only=name_only))
    original = board.read_bytes()
    disposition = analyze_routing_placement(board)
    router, nets = load_pcb_for_routing(
        str(board),
        placement_disposition=disposition,
        force_python=True,
    )
    routes = router.route_net(nets["GOOD"])
    args = SimpleNamespace(preserve_existing=preserve, _placement_disposition=disposition)
    _, preserved = route_cmd._capture_attempt_preserved_copper(board, args)
    output = tmp_path / "checkpoint.kicad_pcb"
    callback = route_cmd._make_checkpoint_callback(
        board,
        output,
        interval=1,
        quiet=True,
        preserved_sexp=preserved,
    )
    callback(routes, {})
    for block in router.placement_preserved_copper.splitlines():
        assert output.read_text().count(block) == 1
    reloaded, reloaded_nets = load_pcb_for_routing(
        str(output),
        placement_disposition=disposition,
        force_python=True,
    )
    assert reloaded_nets["BAD"] not in reloaded.nets
    assert len(reloaded.placement_preserved_routes) == 1
    assert board.read_bytes() == original
