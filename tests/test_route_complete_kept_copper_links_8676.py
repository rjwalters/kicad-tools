"""``--complete`` requests only the joins kept copper leaves missing (#8676).

Synthetic board from the #8658 fixture: nets ``/A`` (U1.1, U2.1, U3.1) and
``/B`` (U1.2, U2.2, U3.2); the kept ``/A`` trace joins U1.1 and U2.1.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.router.io import load_pcb_for_routing
from tests.test_route_complete_name_only_kept_copper_8658 import KEPT_B, _board


def _links(board: Path, *, complete: bool = True) -> dict[str, list[frozenset[str]]]:
    router, net_map = load_pcb_for_routing(str(board), load_existing_routes=True)
    router._preserve_listed_copper = complete
    _coupled, connections = router._lattice_netset_connections()
    names = {v: k for k, v in net_map.items()}
    out: dict[str, list[frozenset[str]]] = {}
    for (net, _seq), a, b, _cls in connections:
        out.setdefault(names[net], []).append(frozenset({f"{a.ref}.{a.pin}", f"{b.ref}.{b.pin}"}))
    return out


@pytest.mark.parametrize("name_only", [False, True])
def test_partly_joined_net_requests_only_the_missing_link(tmp_path, name_only):
    links = _links(_board(tmp_path, name_only=name_only))
    # /A: 3 pads, 2 already joined -> exactly 1 link, to the stranded U3.1.
    assert len(links["/A"]) == 1
    assert any(p.startswith("U3.") for p in next(iter(links["/A"])))
    # /B carries no kept copper -> its plain 2-link star.
    assert len(links["/B"]) == 2


def test_without_complete_the_star_is_unchanged(tmp_path):
    links = _links(_board(tmp_path, name_only=False), complete=False)
    assert len(links["/A"]) == 2


def test_fully_joined_net_requests_no_links(tmp_path):
    board = _board(tmp_path, name_only=False)
    second = (
        '  (segment (start 130 110) (end 150 110) (width 0.25) (layer "F.Cu") (net 1) '
        '(uuid "00000000-0000-4000-8000-000000000301"))\n'
    )
    board.write_text(board.read_text().rstrip()[:-1] + second + ")\n")
    assert "/A" not in _links(board)


def test_name_only_drifted_ids_still_one_link(tmp_path):
    board = _board(tmp_path, name_only=True)
    text = board.read_text()
    board.write_text(text.replace("  (gr_rect", f"  {KEPT_B}\n  (gr_rect", 1))
    links = _links(board)
    assert len(links["/A"]) == 1
    assert len(links["/B"]) == 1


def test_complete_budgets_and_reports_the_reduced_count(tmp_path, capsys):
    import kicad_tools.cli.route_cmd as route_cmd

    board = _board(tmp_path, name_only=False)
    out = tmp_path / "out.kicad_pcb"
    rc = route_cmd.main([str(board), "-o", str(out), "--complete", "--skip-drc", "--no-cache"])
    text = (lambda c: c.out + c.err)(capsys.readouterr())
    assert rc == 0, text
    # /A needs 1 link (kept trace joins two pads), /B needs 2.
    assert "x 3 links" in text
    assert "redundant_copper" not in text


def test_kept_copper_anchors_are_collected_for_pathfinder_9636(tmp_path):
    """The kept /A trace becomes start anchors on its pads (issue #9636)."""
    router, _net_map = load_pcb_for_routing(
        str(_board(tmp_path, name_only=False)), load_existing_routes=True
    )
    router._preserve_listed_copper = True
    router._lattice_netset_connections()
    anchors = router._lattice_kept_anchors
    # The trace joins U1.1 and U2.1: each sees the other pad's end as an anchor;
    # the stranded U3.1 and the copper-less /B pads carry none.
    assert set(anchors) == {("U1", "1"), ("U2", "1")}
    # Without --complete nothing is kept, so nothing is anchored.
    router._preserve_listed_copper = False
    router._lattice_netset_connections()
    assert router._lattice_kept_anchors == {}
