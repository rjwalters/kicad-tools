"""Regression tests for softstart report-quality fixes (issues #3530/#3531).

Pins the committed softstart artifacts:
- the schematic declares a paper size that actually contains its content
  (was: A4 declared with content extending past x=849 mm), and
- the PCB footprints carry ``(attr smd)`` / ``(attr through_hole)``
  tokens so the report census reads 56 SMD / 18 THT / 4 other instead of
  "0 smd / 0 tht / 78 other".
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_SOFTSTART_OUT = (
    Path(__file__).resolve().parents[1] / "boards" / "external" / "softstart" / "output"
)

pytestmark = pytest.mark.skipif(
    not _SOFTSTART_OUT.exists(), reason="softstart board outputs not present"
)


def _attr_census(pcb_path: Path) -> tuple[int, int, int]:
    text = pcb_path.read_text()
    total = len(re.findall(r"\(footprint ", text))
    smd = len(re.findall(r"\(attr smd\)", text))
    tht = len(re.findall(r"\(attr through_hole\)", text))
    return total, smd, tht


@pytest.mark.parametrize("filename", ["softstart.kicad_pcb", "softstart_routed.kicad_pcb"])
def test_softstart_footprints_carry_mount_attrs(filename: str):
    total, smd, tht = _attr_census(_SOFTSTART_OUT / filename)
    assert total == 78
    assert smd == 56
    assert tht == 18
    # The remaining 4 are the M3 mounting holes (np_thru_hole only,
    # excluded from BOM/CPL) — legitimately "other".
    assert total - smd - tht == 4


def test_softstart_schematic_paper_contains_content():
    """The declared sheet must contain the placed content (issue #3530)."""
    from kicad_tools.schematic.models.paper import paper_dimensions

    text = (_SOFTSTART_OUT / "softstart.kicad_sch").read_text()
    paper_match = re.search(r'\(paper "([^"]+)"\)', text)
    assert paper_match is not None
    dims = paper_dimensions(paper_match.group(1))
    assert dims is not None, f"unmodeled paper size {paper_match.group(1)!r}"
    width, height = dims

    # Strip the lib_symbols block (its coordinates are symbol-relative).
    lib_start = text.find("(lib_symbols")
    if lib_start >= 0:
        depth = 0
        i = lib_start
        while i < len(text):
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        text = text[:lib_start] + text[i + 1 :]

    xs: list[float] = []
    ys: list[float] = []
    for m in re.finditer(r"\((?:at|xy) (-?[\d.]+) (-?[\d.]+)", text):
        xs.append(float(m.group(1)))
        ys.append(float(m.group(2)))

    assert xs, "no placed content found"
    assert max(xs) <= width, f"content x={max(xs):.1f} exceeds {width} mm sheet"
    assert max(ys) <= height, f"content y={max(ys):.1f} exceeds {height} mm sheet"


def test_softstart_report_census_and_off_board_sections():
    """The shipped report documents the census and off-board banks."""
    report = _SOFTSTART_OUT / "manufacturing" / "report.md"
    if not report.exists():
        pytest.skip("manufacturing bundle not present")
    text = report.read_text()
    assert "78 (56 SMD, 18 THT, 4 other)" in text
    assert "## Off-board Assemblies" in text
    assert "Tecate TPLH-2R7/12WR10X30" in text
    assert "| Board connector | J3 |" in text
    assert "| Board connector | J4 |" in text
