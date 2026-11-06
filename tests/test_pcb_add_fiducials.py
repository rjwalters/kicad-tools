"""Tests for ``kct pcb add-fiducials`` (pcb_add_fiducials module)."""

import json

from kicad_tools.cli.pcb_add_fiducials import _collinear, run_add_fiducials
from kicad_tools.schema.pcb import PCB


def _board(width=60, height=40, origin=(0, 0), extra="", back_part=False) -> str:
    ox, oy = origin
    x0, y0, x1, y1 = ox, oy, ox + width, oy + height
    edge = "".join(
        f'(gr_line (start {a} {b}) (end {c} {d}) (layer "Edge.Cuts") (stroke (width 0.1) (type default)))\n'
        for a, b, c, d in [
            (x0, y0, x1, y0),
            (x1, y0, x1, y1),
            (x1, y1, x0, y1),
            (x0, y1, x0, y0),
        ]
    )
    cx, cy = ox + width / 2, oy + height / 2
    layer = "B.Cu" if back_part else "F.Cu"
    return f"""(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "")
  {edge}
  (footprint "Resistor_SMD:R_0402"
    (layer "{layer}")
    (uuid "fp-r1")
    (at {cx} {cy})
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "{layer}") (net 0 ""))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "{layer}") (net 0 ""))
  )
  {extra}
)
"""


def _write(tmp_path, text):
    p = tmp_path / "b.kicad_pcb"
    p.write_text(text)
    return p


def _fids(path):
    return [fp for fp in PCB.load(path).footprints if fp.reference.startswith("FID")]


class TestAddFiducials:
    def test_places_three_noncollinear_per_side(self, tmp_path):
        p = _write(tmp_path, _board())
        assert run_add_fiducials(p) == 0
        fids = _fids(p)
        assert len(fids) == 6
        for layer in ("F.Cu", "B.Cu"):
            pts = [f.position for f in fids if f.layer == layer]
            assert len(pts) == 3
            assert not _collinear(pts)

    def test_edge_margin_respected(self, tmp_path):
        p = _write(tmp_path, _board(origin=(100, 80)))
        assert run_add_fiducials(p, edge_margin=4.0) == 0
        for f in _fids(p):
            x, y = f.position
            assert 4 - 1e-6 <= x <= 56 + 1e-6  # PCB.load positions are board-relative
            assert 4 - 1e-6 <= y <= 36 + 1e-6

    def test_flags_exclude_from_bom_and_pos(self, tmp_path):
        p = _write(tmp_path, _board())
        assert run_add_fiducials(p, sides="front") == 0
        text = p.read_text()
        assert "board_only" in text
        for f in _fids(p):
            assert f.exclude_from_bom and f.exclude_from_pos_files
            assert "board_only" in f.attr_flags

    def test_dry_run_writes_nothing(self, tmp_path, capsys):
        p = _write(tmp_path, _board())
        before = p.read_text()
        assert run_add_fiducials(p, dry_run=True, output_format="json") == 0
        assert p.read_text() == before
        out = json.loads(capsys.readouterr().out)
        assert len(out["fiducials"]) == 6 and out["written"] is False

    def test_populated_sides_only_back(self, tmp_path):
        p = _write(tmp_path, _board(back_part=True))
        assert run_add_fiducials(p, sides="populated") == 0
        assert {f.layer for f in _fids(p)} == {"B.Cu"}

    def test_avoids_courtyards_and_copper(self, tmp_path):
        # Track hugging the top-left corner forces the site elsewhere.
        trk = '(segment (start 3 3) (end 12 3) (width 0.25) (layer "F.Cu") (net 0) (uuid "t1"))'
        p = _write(tmp_path, _board(extra=trk))
        assert run_add_fiducials(p, sides="front") == 0
        for f in _fids(p):
            x, y = f.position
            # distance from the segment (3,3)-(12,3) must exceed keepout + half width
            dx = max(3 - x, 0, x - 12)
            assert (dx**2 + (y - 3) ** 2) ** 0.5 >= 2.0

    def test_no_free_site_fails_without_write(self, tmp_path, capsys):
        p = _write(tmp_path, _board(width=8, height=8))
        before = p.read_text()
        assert run_add_fiducials(p) == 1
        assert p.read_text() == before
        assert "free fiducial sites" in capsys.readouterr().err

    def test_second_run_uses_fresh_refs(self, tmp_path):
        p = _write(tmp_path, _board(width=120, height=80))
        assert run_add_fiducials(p, sides="front") == 0
        assert run_add_fiducials(p, sides="front") == 0
        refs = [f.reference for f in _fids(p)]
        assert len(refs) == len(set(refs)) == 6

    def test_bad_sides(self, tmp_path):
        p = _write(tmp_path, _board())
        assert run_add_fiducials(p, sides="top") == 1


def test_parser_has_add_fiducials():
    from kicad_tools.cli.parser import create_parser

    args = create_parser().parse_args(
        ["pcb", "add-fiducials", "x.kicad_pcb", "--sides", "front", "--count", "4", "--dry-run"]
    )
    assert args.pcb_command == "add-fiducials"
    assert args.count == 4 and args.dry_run and args.edge_margin == 3.0


def test_nonzero_origin_avoids_track(tmp_path):
    trk = '(segment (start 103 83) (end 112 83) (width 0.25) (layer "F.Cu") (net 0) (uuid "t1"))'
    p = _write(tmp_path, _board(origin=(100, 80), extra=trk))
    assert run_add_fiducials(p, sides="front") == 0
    for f in _fids(p):
        x, y = f.position
        dx = max(3 - x, 0, x - 12)
        assert (dx**2 + (y - 3) ** 2) ** 0.5 >= 2.0
