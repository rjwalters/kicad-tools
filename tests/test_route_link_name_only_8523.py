"""route-link writes name-only net refs and merged geometry (Issue #8523)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("shapely")

from kicad_tools.router.link_router import (  # noqa: E402
    LinkRoute,
    append_link_routes,
    collapse_link_route,
)

NUMERIC = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (net 0 "")
  (net 1 "SIG")
)
"""
NAME_ONLY = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (footprint "R" (layer "F.Cu") (at 10 10)
    (pad "1" smd rect (at 0 0) (size 0.8 0.8) (layers "F.Cu") (net "SIG")))
)
"""
BARE = re.compile(r"\(net \d+\)")


def _route() -> LinkRoute:
    return LinkRoute(
        net_number=93,
        net_name="SIG",
        width=0.3,
        via_size=0.6,
        via_drill=0.3,
        segments=[(0, 0, 5, 0, "F.Cu"), (5, 0, 10, 0, "F.Cu")],
        vias=[(10, 0)],
    )


def _write(tmp_path: Path, text: str) -> str:
    p = tmp_path / "b.kicad_pcb"
    p.write_text(text)
    append_link_routes(p, [_route()], (0.0, 0.0))
    return p.read_text()


def test_name_only_board_gets_named_refs(tmp_path):
    out = _write(tmp_path, NAME_ONLY)
    assert not BARE.search(out)
    assert out.count('(net "SIG")') >= 4  # pad + 2 segments + via


def test_numeric_board_keeps_numeric_refs(tmp_path):
    out = _write(tmp_path, NUMERIC)
    assert len(BARE.findall(out)) == 3  # 2 segments + via


def test_collapse_merges_collinear():
    c = collapse_link_route(_route())
    assert c.segments == [(0, 0, 10, 0, "F.Cu")]
    assert c.vias == [(10, 0)]


def test_collapse_diagonal_staircase():
    s = 0.05
    segs = []
    x = y = 0.0
    for _ in range(10):
        segs.append((x, y, x + s, y, "F.Cu"))
        segs.append((x + s, y, x + 2 * s, y + s, "F.Cu"))
        x, y = x + 2 * s, y + s
    r = LinkRoute(93, "SIG", 0.3, 0.6, 0.3, segments=segs)
    c = collapse_link_route(r)
    assert len(c.segments) < len(segs)
    assert c.segments[0][:2] == (0, 0)
    assert c.segments[-1][2:4] == pytest.approx((x, y))


def test_collapse_single_segment_and_layers_kept():
    r = LinkRoute(1, "SIG", 0.3, 0.6, 0.3, segments=[(0, 0, 1, 0, "F.Cu"), (1, 0, 1, 1, "B.Cu")])
    assert collapse_link_route(r).segments == r.segments
