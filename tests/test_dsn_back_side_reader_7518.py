"""Back-side padstack layers against a recorded real Specctra reader (#7518).

Nothing here models the reader. The sessions in
``tests/fixtures/dsn_back_side_7518/`` are what Freerouting v2.5.0 (jar sha256
``f6f51bb02245e8e717f9359bd260cc9c5c0b1bc0acc8b7cb2cd5b8ffeb5de3c7``) wrote on
the operator's Mac on 2026-10-27, for DSNs exported from the committed board;
``reader_run.json`` records the host, versions, commands and observed result.

* ``before.dsn`` / ``before.ses``: back images named their pads' copper on
  B.Cu. The reader routed to every back-side SMD pad on F.Cu (a double flip:
  it swaps a back-placed image's padstack layers), and pinned kicad-cli DRC of
  the imported board showed the F.Cu track ends unconnected to the B.Cu pads.
* ``after.dsn`` / ``after.ses``: back images name that copper on the mirror
  layer (F.Cu). The reader routed to every back-side SMD pad on B.Cu, and DRC
  showed no unconnected or dangling copper.

The tests pin both observations to the recorded bytes, and pin today's
exporter to the exact DSN that produced the passing run.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from kicad_tools.export.dsn import KiCadToDSNExporter
from kicad_tools.export.ses import SESToKiCadImporter

FIXTURE = Path(__file__).parent / "fixtures" / "dsn_back_side_7518"
BOARD = "back_side_board"
BACK_SMD_PADS = {("U2", "1"), ("U2", "2"), ("U3", "1"), ("U3", "2")}
TOL_MM = 1e-3


@pytest.fixture
def board(tmp_path: Path) -> Path:
    for suffix in (".kicad_pcb", ".kicad_pro"):
        shutil.copyfile(FIXTURE / f"{BOARD}{suffix}", tmp_path / f"{BOARD}{suffix}")
    return tmp_path / f"{BOARD}.kicad_pcb"


def _smd_pads(pcb: Path) -> dict[tuple[str, str], tuple[float, float, str]]:
    """``(ref, pad) -> (x mm, y mm, copper side)`` for every SMD pad, from KiCad's file."""
    exporter = KiCadToDSNExporter(str(pcb))
    exporter.export()
    out = {}
    for fp in exporter.footprints:
        for pad in fp.pads:
            if pad.pad_type != "smd":
                continue
            c = KiCadToDSNExporter._pad_center(fp, pad)
            out[(fp.reference, pad.number)] = (c.x, c.y, fp.layer)
    return out


def _wire_ends_on_pads(ses: Path, pads) -> dict[tuple[str, str], set[str]]:
    """Layers of the session's wire ends that sit on each SMD pad centre."""
    importer = SESToKiCadImporter(ses)
    importer.parse()
    scale = importer.mm_per_unit
    hits: dict[tuple[str, str], set[str]] = {}
    for wire in importer.wires:
        for x, y in (wire.points[0], wire.points[-1]):
            for key, (px, py, _side) in pads.items():
                if abs(x * scale - px) < TOL_MM and abs(y * scale - py) < TOL_MM:
                    hits.setdefault(key, set()).add(wire.layer)
    return hits


def _strip_name(dsn: str) -> str:
    return re.sub(r"^\(pcb \S+", "(pcb", dsn, count=1)


class TestRecordedReaderRun:
    def test_exporter_writes_the_dsn_of_the_passing_run(self, board):
        exported = KiCadToDSNExporter(str(board)).export()
        assert _strip_name(exported) == _strip_name((FIXTURE / "after.dsn").read_text())

    def test_recorded_dsns_differ_only_in_back_image_padstack_layers(self):
        before = (FIXTURE / "before.dsn").read_text()
        after = (FIXTURE / "after.dsn").read_text()
        assert "Rect[B]Pad_1475.0x600.0_um" in before
        assert "Rect[B]" not in after
        assert (
            before.replace("Rect[B]", "Rect[T]").replace(
                "    (padstack Rect[T]Pad_1475.0x600.0_um\n"
                "      (shape (rect B.Cu -737.5 -300.0 737.5 300.0))\n"
                "      (attach off)\n"
                "    )\n",
                "",
            )
            == after
        )

    def test_reader_swaps_back_image_layers(self, board):
        """before.ses: B.Cu-named back pads were reached on F.Cu (the double flip)."""
        pads = _smd_pads(board)
        hits = _wire_ends_on_pads(FIXTURE / "before.ses", pads)
        assert {k: hits.get(k) for k in BACK_SMD_PADS} == {k: {"F.Cu"} for k in BACK_SMD_PADS}

    def test_back_side_smd_copper_is_reached_on_b_cu(self, board):
        """after.ses: every SMD pad is reached only on its own copper layer."""
        pads = _smd_pads(board)
        hits = _wire_ends_on_pads(FIXTURE / "after.ses", pads)
        assert hits.keys() >= BACK_SMD_PADS
        for key, layers in hits.items():
            assert layers == {pads[key][2]}, key
        assert {k: hits[k] for k in BACK_SMD_PADS} == {k: {"B.Cu"} for k in BACK_SMD_PADS}

    def test_imported_session_puts_back_pad_tracks_on_b_cu(self, board, tmp_path):
        pads = _smd_pads(board)
        merged = SESToKiCadImporter(FIXTURE / "after.ses").merge_into(
            board, tmp_path / "routed.kicad_pcb"
        )
        ends: dict[tuple[str, str], set[str]] = {}
        for m in re.finditer(
            r"\(segment\s+\(start ([-\d.]+) ([-\d.]+)\)\s+\(end ([-\d.]+) ([-\d.]+)\)"
            r".*?\(layer \"?([^\"\s)]+)\"?\)",
            merged,
            re.S,
        ):
            x1, y1, x2, y2, layer = m.groups()
            for x, y in ((float(x1), float(y1)), (float(x2), float(y2))):
                for key, (px, py, _side) in pads.items():
                    if abs(x - px) < TOL_MM and abs(y - py) < TOL_MM:
                        ends.setdefault(key, set()).add(layer)
        assert {k: ends.get(k) for k in BACK_SMD_PADS} == {k: {"B.Cu"} for k in BACK_SMD_PADS}
