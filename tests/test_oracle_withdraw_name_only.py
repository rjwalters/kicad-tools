"""Issue #8563: a withdrawn pour weld leaves no copper on a name-only board.

``PourLinkCloser._weld_pads`` records one capsule per placed via and stub leg;
``withdraw`` must remove every one of them again, and nothing it writes or
leaves behind may carry a legacy numeric ``(net <n>)`` clause (KiCad 10 reads
that as no net).
"""

from __future__ import annotations

import re
from pathlib import Path

from kicad_tools.router.oracle_completion import ClosureAttempt, PourLinkCloser

from .test_stitch_mfr import TWO_LAYER_GND_ZONE_PCB

_NUMERIC_NET = re.compile(r'\(net\s+\d+(?:\s+"[^"]*")?\)')
_COPPER = re.compile(r"\((?:via|segment)\b")


def _name_only_text() -> str:
    text = re.sub(r'\n\s*\(net 0 ""\)', "", TWO_LAYER_GND_ZONE_PCB)
    text = re.sub(r'\(net \d+ ("[^"]*")\)', r"(net \1)", text)
    return re.sub(r'\(zone \(net \d+\) \(net_name ("[^"]*")\)', r"(zone (net \1)", text)


def _copper_net_clauses(text: str) -> list[str]:
    """Net clauses that sit on a via / segment node (pads and zones excluded)."""
    out: list[str] = []
    for m in _COPPER.finditer(text):
        end = text.find("\n", m.start())
        out.extend(_NUMERIC_NET.findall(text[m.start() : end if end > 0 else None]))
    return out


def test_withdrawn_weld_leaves_no_copper_and_no_numeric_net(tmp_path: Path) -> None:
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(_name_only_text())
    before = pcb.read_bytes()
    assert not _COPPER.search(before.decode())

    closer = PourLinkCloser(via_size=0.6, via_drill=0.3, clearance=0.2, trace_width=0.2)
    attempt = ClosureAttempt()
    closer._weld_pads(pcb, {"GND": {"C1.1"}}, attempt)

    placed = pcb.read_text()
    assert attempt.footprints, "the weld placed nothing; the fixture is vacuous"
    assert "(via" in placed and "(segment" in placed, "expected a via and a stub segment"
    assert not _copper_net_clauses(placed), _copper_net_clauses(placed)

    removed = closer.withdraw(pcb, before, attempt.footprints)

    left = pcb.read_text()
    assert removed == len(_COPPER.findall(placed))
    assert not _COPPER.findall(left), "withdrawn weld left copper behind"
    assert not _NUMERIC_NET.search(left)
