"""Retuning twice must not reissue UUIDs already on the board (Issue #8321)."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from kicad_tools.router.pcb_retune import retune_pcb
from kicad_tools.router.rules import NetClassRouting
from tests.test_pcb_retune import ADDR, board

DATA = {"D0": (30, 5, 45), "D1": (34, 5, 41), "D2": (38, 5, 39.2), "D3": (42, 5, 38)}
UUID_RE = re.compile(r'\(uuid "([0-9a-f-]{36})"\)|\(uuid "([0-9a-f-]{36})"')


def _class_map() -> dict[str, NetClassRouting]:
    out: dict[str, NetClassRouting] = {}
    for group, nets in (("ADDR_BUS", ADDR), ("DATA_BUS", DATA)):
        nc = NetClassRouting(
            name=group,
            length_match_group=group,
            length_match_tolerance_mm=0.5,
            length_critical=True,
        )
        out.update(dict.fromkeys(nets, nc))
    return out


def _uuids(path: Path) -> list[str]:
    return [a or b for a, b in UUID_RE.findall(path.read_text())]


def _retune(src: Path, dst: Path, group: str):
    return retune_pcb(src, dst, groups=[group], net_class_map=_class_map())


def test_second_retune_on_other_group_has_unique_uuids(tmp_path: Path) -> None:
    src = tmp_path / "in.kicad_pcb"
    src.write_text(board(ADDR | DATA))
    first = tmp_path / "first.kicad_pcb"
    final = tmp_path / "final.kicad_pcb"
    assert _retune(src, first, "ADDR_BUS").status == "tuned"
    assert _retune(first, final, "DATA_BUS").status == "tuned"

    dupes = {u: n for u, n in Counter(_uuids(final)).items() if n > 1}
    assert not dupes

    # Identical input + selection is still byte-identical, with a derived seed.
    again = tmp_path / "again.kicad_pcb"
    _retune(first, again, "DATA_BUS")
    assert again.read_bytes() == final.read_bytes()
    seeds = {
        json.loads(Path(f"{p}.retune.json").read_text())["settings"]["uuid_seed"]
        for p in (first, final)
    }
    assert len(seeds) == 2
