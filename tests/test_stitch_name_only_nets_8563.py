"""Issue #8563: stitch writers use the board's own net dialect.

A KiCad-10 name-only board has no net table; ``(net 8)`` there reads as
"no net".  ``add_via_to_pcb`` / ``add_trace_to_pcb`` (and so ``run_stitch``,
hence the pour-link closer's weld) must write ``(net "NAME")`` on such boards
and stay byte-identical on numeric-table boards.
"""

from __future__ import annotations

import re
from pathlib import Path

from kicad_tools.cli.stitch_cmd import run_stitch

from .test_stitch_mfr import TWO_LAYER_GND_ZONE_PCB

_NUMERIC_NET = re.compile(r'\(net\s+\d+(?:\s+"[^"]*")?\)')


def _name_only_text() -> str:
    text = re.sub(r'\n\s*\(net 0 ""\)', "", TWO_LAYER_GND_ZONE_PCB)
    text = re.sub(r'\(net \d+ ("[^"]*")\)', r"(net \1)", text)
    return re.sub(r'\(zone \(net \d+\) \(net_name ("[^"]*")\)', r"(zone (net \1)", text)


def _stitch(pcb: Path) -> None:
    result = run_stitch(
        pcb,
        ["GND"],
        via_size=0.6,
        drill=0.3,
        clearance=0.2,
        trace_width=0.2,
        preferred_sites={"C1.1": [(109.49, 112.0)]},
    )
    assert result.vias_added


def test_name_only_board_gets_name_only_copper(tmp_path: Path) -> None:
    text = _name_only_text()
    assert not _NUMERIC_NET.search(text)
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(text)
    _stitch(pcb)
    out = pcb.read_text()
    assert "(via" in out and "(segment" in out
    assert not _NUMERIC_NET.search(out), _NUMERIC_NET.findall(out)


def test_numeric_table_board_stays_numeric(tmp_path: Path) -> None:
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(TWO_LAYER_GND_ZONE_PCB)
    _stitch(pcb)
    out = pcb.read_text()
    assert re.search(r"\(via[^\n]*\n?[^\n]*\(net 1\)", out) or "(net 1)" in out
    assert '(net "GND")' not in out.split("(via", 1)[1]
