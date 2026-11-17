"""Tests for the report footprint census and off-board assemblies (issue #3531).

(a) The placement census previously read "0 smd / 0 tht / N other" for
    boards whose recipe emitters never wrote ``(attr ...)`` tokens; the
    collector now falls back to pad-type classification.
(b) Off-board assemblies declared in the project spec (``off_board: true``
    interfaces) surface as a report section and hand-solder/DNP BOM rows.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from kicad_tools.report.collector import ReportDataCollector
from kicad_tools.report.generator import ReportGenerator
from kicad_tools.report.models import ReportData

# ---------------------------------------------------------------------------
# (a) Footprint mount-type classification
# ---------------------------------------------------------------------------


@dataclass
class _FakePad:
    type: str


@dataclass
class _FakeFootprint:
    attr: str = ""
    pads: list = field(default_factory=list)


class TestFootprintMountType:
    def test_explicit_attr_wins(self):
        fp = _FakeFootprint(attr="smd", pads=[_FakePad("thru_hole")])
        assert ReportDataCollector._footprint_mount_type(fp) == "smd"

    def test_fallback_thru_hole_pads(self):
        fp = _FakeFootprint(pads=[_FakePad("thru_hole"), _FakePad("thru_hole")])
        assert ReportDataCollector._footprint_mount_type(fp) == "through_hole"

    def test_fallback_smd_pads(self):
        fp = _FakeFootprint(pads=[_FakePad("smd")])
        assert ReportDataCollector._footprint_mount_type(fp) == "smd"

    def test_mixed_pads_classify_as_tht(self):
        fp = _FakeFootprint(pads=[_FakePad("smd"), _FakePad("thru_hole")])
        assert ReportDataCollector._footprint_mount_type(fp) == "through_hole"

    def test_np_thru_hole_only_is_other(self):
        # Mounting holes have only non-plated holes — not a placed part
        fp = _FakeFootprint(pads=[_FakePad("np_thru_hole")])
        assert ReportDataCollector._footprint_mount_type(fp) == "other"

    def test_no_pads_is_other(self):
        assert ReportDataCollector._footprint_mount_type(_FakeFootprint()) == "other"


# ---------------------------------------------------------------------------
# (b) Off-board assemblies from the project spec
# ---------------------------------------------------------------------------


_SPEC_WITH_OFF_BOARD = textwrap.dedent(
    """\
    kct_version: "1.0"
    project:
      name: "Off-board test"
      revision: "B"
    intent:
      summary: "Test design with off-board supercap banks"
      interfaces:
        - name: SUPERCAP_BANK_POS
          type: energy_storage
          description: "Positive bank (off-board, 30S string)"
          voltage: "81V nominal"
          capacitance: "0.4F"
          off_board: true
          connector: "J3"
          part: "Tecate TPLH-2R7/12WR10X30"
          qty: 30
          assembly: "hand_solder"
          wiring: "J3 pin 1 -> SCAP_POS+; J3 pin 2 -> SCAP_POS_GND"
        - name: SWD
          type: debug
          description: "On-board debug header"
    """
)


@pytest.fixture
def spec_dir(tmp_path: Path) -> Path:
    (tmp_path / "project.kct").write_text(_SPEC_WITH_OFF_BOARD)
    return tmp_path


class TestCollectOffBoard:
    def test_collects_only_off_board_interfaces(self, spec_dir: Path):
        collector = ReportDataCollector(spec_dir / "board.kicad_pcb")
        result = collector.collect_off_board()
        assert result is not None
        assemblies = result["assemblies"]
        assert len(assemblies) == 1
        asm = assemblies[0]
        assert asm["name"] == "SUPERCAP_BANK_POS"
        assert asm["connector"] == "J3"
        assert asm["part"] == "Tecate TPLH-2R7/12WR10X30"
        assert asm["qty"] == 30
        assert asm["assembly"] == "hand_solder"
        assert "SCAP_POS+" in asm["wiring"]

    def test_no_spec_returns_none(self, tmp_path: Path):
        # Guard against walking up into the repository's own project.kct:
        # a .git boundary stops the spec search.
        (tmp_path / ".git").mkdir()
        collector = ReportDataCollector(tmp_path / "board.kicad_pcb")
        assert collector.collect_off_board() is None

    def test_spec_without_off_board_interfaces_returns_none(self, tmp_path: Path):
        (tmp_path / "project.kct").write_text(
            textwrap.dedent(
                """\
                kct_version: "1.0"
                project:
                  name: "No off-board"
                intent:
                  summary: "All parts on the PCB"
                  interfaces:
                    - name: SWD
                      type: debug
                """
            )
        )
        collector = ReportDataCollector(tmp_path / "board.kicad_pcb")
        assert collector.collect_off_board() is None

    def test_bom_groups_marked_hand_solder_dnp(self, spec_dir: Path):
        collector = ReportDataCollector(spec_dir / "board.kicad_pcb")
        groups = collector._off_board_bom_groups()
        assert len(groups) == 1
        row = groups[0]
        assert row["qty"] == 30
        assert row["off_board"] is True
        assert row["mpn"] == "Tecate TPLH-2R7/12WR10X30"
        assert "hand solder" in row["footprint"]
        assert "DNP" in row["footprint"]
        assert "J3" in row["footprint"]


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------


class TestOffBoardTemplateSection:
    def _render(self, off_board: dict | None) -> str:
        data = ReportData(
            project_name="t",
            revision="B",
            date="2026-06-11",
            manufacturer="jlcpcb-tier1",
            off_board=off_board,
        )
        return ReportGenerator()._render(data)

    def test_section_rendered(self):
        rendered = self._render(
            {
                "assemblies": [
                    {
                        "name": "SUPERCAP_BANK_POS",
                        "description": "Positive bank",
                        "connector": "J3",
                        "part": "Tecate TPLH-2R7/12WR10X30",
                        "qty": 30,
                        "voltage": "81V nominal",
                        "capacitance": "0.4F",
                        "assembly": "hand_solder",
                        "wiring": "J3 pin 1 -> SCAP_POS+",
                    }
                ]
            }
        )
        assert "## Off-board Assemblies" in rendered
        assert "SUPERCAP_BANK_POS" in rendered
        assert "| Board connector | J3 |" in rendered
        assert "| Quantity | 30 |" in rendered
        assert "hand solder (DNP for fab assembly)" in rendered
        assert "**Wiring**: J3 pin 1 -> SCAP_POS+" in rendered

    def test_section_omitted_without_data(self):
        assert "Off-board Assemblies" not in self._render(None)
        assert "Off-board Assemblies" not in self._render({"assemblies": []})
