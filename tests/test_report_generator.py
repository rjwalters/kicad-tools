"""Tests for the report generation module."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

from kicad_tools.report.generator import ReportGenerator
from kicad_tools.report.models import ReportData

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _full_data(**overrides) -> ReportData:
    """Return a fully-populated ``ReportData`` instance."""
    defaults: dict = {
        "project_name": "TestBoard",
        "revision": "A",
        "date": "2026-04-12",
        "manufacturer": "jlcpcb",
        "board_stats": {
            "layer_count": 4,
            "layer_names": ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
            "footprint_count": 134,
            "footprint_smd": 60,
            "footprint_tht": 70,
            "footprint_other": 4,
            "net_count": 80,
            "segment_count": 320,
            "via_count": 45,
            "board_width_mm": 200.0,
            "board_height_mm": 120.0,
        },
        "bom_groups": [
            {
                "value": "100nF",
                "footprint": "0402",
                "qty": 10,
                "refs": "C1-C10",
                "mpn": "CL05B104KO5NNNC",
                "lcsc": "C1525",
            },
            {
                "value": "10k",
                "footprint": "0402",
                "qty": 5,
                "refs": "R1-R5",
                "mpn": "RC0402FR-0710KL",
                "lcsc": "C25744",
            },
        ],
        "drc": {
            "error_count": 0,
            "warning_count": 2,
            "blocking_count": 0,
            "passed": True,
        },
        "erc": {
            "error_count": 0,
            "warning_count": 1,
            "passed": True,
            "details": "",
        },
        "audit": {
            "verdict": "ready",
            "action_items": [
                {
                    "priority": 1,
                    "description": "Complete routing: 3 nets incomplete",
                    "command": "kct validate connectivity board.kicad_pcb",
                },
                {
                    "priority": 3,
                    "description": "Review silkscreen placement",
                    "command": None,
                },
            ],
        },
        "net_status": {
            "total_nets": 61,
            "complete_count": 58,
            "completion_percent": 95.5,
            "incomplete_count": 3,
            "unrouted_count": 0,
            "total_unconnected_pads": 5,
            "incomplete_net_names": ["GND", "SDA", "VCC"],
        },
        "cost": {
            "per_unit": 2.50,
            "batch_qty": 100,
            "batch_total": 250.00,
            "currency": "USD",
        },
        "schematic_sheets": [
            {"name": "Main Sheet", "figure_path": "figures/main_sheet.png"},
            {"name": "Power", "figure_path": "figures/power.png"},
        ],
        "pcb_figures": {
            "front": "figures/pcb_front.png",
            "back": "figures/pcb_back.png",
            "copper": "figures/pcb_copper.png",
        },
        "notes": "This is a prototype build.",
        "tool_version": "0.11.0",
        "git_hash": "abc1234",
    }
    defaults.update(overrides)
    return ReportData(**defaults)


# ---------------------------------------------------------------------------
# TestReportData
# ---------------------------------------------------------------------------


class TestReportData:
    """Test the ReportData dataclass."""

    def test_full_instantiation(self) -> None:
        data = _full_data()
        assert data.project_name == "TestBoard"
        assert data.revision == "A"
        assert data.date == "2026-04-12"
        assert data.manufacturer == "jlcpcb"
        assert data.board_stats is not None
        assert data.bom_groups is not None
        assert data.drc is not None
        assert data.erc is not None
        assert data.audit is not None
        assert data.net_status is not None
        assert data.cost is not None
        assert data.schematic_sheets is not None
        assert data.pcb_figures is not None
        assert data.notes == "This is a prototype build."
        assert data.tool_version == "0.11.0"
        assert data.git_hash == "abc1234"

    def test_defaults(self) -> None:
        data = ReportData(
            project_name="Minimal",
            revision="1",
            date="2026-01-01",
            manufacturer="pcbway",
        )
        assert data.board_stats is None
        assert data.bom_groups is None
        assert data.notes == ""
        assert data.tool_version == ""
        assert data.git_hash == ""


# ---------------------------------------------------------------------------
# TestReportGenerator
# ---------------------------------------------------------------------------


class TestReportGenerator:
    """Test the ReportGenerator class."""

    def test_full_render(self, tmp_path: Path) -> None:
        data = _full_data()
        gen = ReportGenerator()
        report_path = gen.generate(data, tmp_path)

        assert report_path.exists()
        content = report_path.read_text(encoding="utf-8")

        # All 11 section headings must appear
        assert "# TestBoard - Design Report" in content
        assert "## Board Summary" in content
        assert "## ERC Status" in content
        assert "## Schematic Overview" in content
        assert "## PCB Layout" in content
        assert "## Bill of Materials" in content
        assert "## DRC Status" in content
        assert "## Manufacturing Readiness" in content
        assert "## Routing Status" in content
        assert "## Cost Estimate" in content
        assert "## Notes" in content

        # No None literals
        assert "None" not in content

        # Verify some data rendered
        assert "jlcpcb" in content
        assert "100nF" in content
        assert "PASS" in content
        assert "READY" in content
        assert "95.5%" in content
        assert "abc1234" in content

    def test_partial_data_omits_sections(self, tmp_path: Path) -> None:
        data = ReportData(
            project_name="Sparse",
            revision="1",
            date="2026-01-01",
            manufacturer="pcbway",
        )
        gen = ReportGenerator()
        report_path = gen.generate(data, tmp_path)

        content = report_path.read_text(encoding="utf-8")

        # Header is always present
        assert "# Sparse - Design Report" in content

        # Optional sections must be absent
        assert "## Board Summary" not in content
        assert "## ERC Status" not in content
        assert "## Schematic Overview" not in content
        assert "## PCB Layout" not in content
        assert "## Bill of Materials" not in content
        assert "## DRC Status" not in content
        assert "## Manufacturing Readiness" not in content
        assert "## Routing Status" not in content
        assert "## Cost Estimate" not in content
        assert "## Notes" not in content

        # No None literals
        assert "None" not in content

    def test_immutability_guard(self, tmp_path: Path) -> None:
        """If the computed next version directory already contains report.md,
        generate() must raise FileExistsError.

        Simulates a race condition by monkey-patching the version scanner
        to return a directory that already holds a report.
        """
        data = _full_data()
        gen = ReportGenerator()

        # Generate v1 normally
        path1 = gen.generate(data, tmp_path)
        assert path1.exists()

        # Monkey-patch next_version_dir to always return v1 (already has report.md)
        gen.next_version_dir = staticmethod(lambda output_dir: tmp_path / "v1")  # type: ignore[assignment]

        with pytest.raises(FileExistsError, match="must not be overwritten"):
            gen.generate(data, tmp_path)

    def test_metadata_written(self, tmp_path: Path) -> None:
        data = _full_data()
        gen = ReportGenerator()
        report_path = gen.generate(data, tmp_path)

        metadata_path = report_path.parent / "metadata.json"
        assert metadata_path.exists()

        meta = json.loads(metadata_path.read_text(encoding="utf-8"))
        assert "timestamp" in meta
        assert "kicad_tools_version" in meta
        assert "git_hash" in meta
        assert "template_sha256" in meta

        # template_sha256 must be a valid hex string
        sha = meta["template_sha256"]
        assert len(sha) == 64
        assert all(c in "0123456789abcdef" for c in sha)

        # Version and hash from data
        assert meta["kicad_tools_version"] == "0.11.0"
        assert meta["git_hash"] == "abc1234"

    def test_auto_version_increment(self, tmp_path: Path) -> None:
        data = _full_data()
        gen = ReportGenerator()

        p1 = gen.generate(data, tmp_path)
        p2 = gen.generate(data, tmp_path)
        p3 = gen.generate(data, tmp_path)

        assert p1.parent.name == "v1"
        assert p2.parent.name == "v2"
        assert p3.parent.name == "v3"

    def test_custom_template(self, tmp_path: Path) -> None:
        template_dir = tmp_path / "custom_templates"
        template_dir.mkdir()
        custom_template = template_dir / "custom.md.j2"
        custom_template.write_text("# {{ project_name }} custom report\n")

        data = _full_data()
        output_dir = tmp_path / "output"
        gen = ReportGenerator(template_path=custom_template)
        report_path = gen.generate(data, output_dir)

        content = report_path.read_text(encoding="utf-8")
        assert "# TestBoard custom report" in content

    def test_figure_paths_not_validated(self, tmp_path: Path) -> None:
        """Figure paths are strings in the output, not validated on disk."""
        data = _full_data(
            pcb_figures={
                "front": "figures/nonexistent.png",
                "back": "figures/also_missing.png",
            },
            schematic_sheets=[
                {"name": "Ghost", "figure_path": "figures/ghost.png"},
            ],
        )
        gen = ReportGenerator()
        report_path = gen.generate(data, tmp_path)

        content = report_path.read_text(encoding="utf-8")
        assert "figures/nonexistent.png" in content
        assert "figures/ghost.png" in content

    def test_empty_notes_omitted(self, tmp_path: Path) -> None:
        """Empty notes string means no Notes section."""
        data = _full_data(notes="")
        gen = ReportGenerator()
        report_path = gen.generate(data, tmp_path)

        content = report_path.read_text(encoding="utf-8")
        assert "## Notes" not in content

    def test_nonempty_notes_included(self, tmp_path: Path) -> None:
        data = _full_data(notes="Ship by Friday.")
        gen = ReportGenerator()
        report_path = gen.generate(data, tmp_path)

        content = report_path.read_text(encoding="utf-8")
        assert "## Notes" in content
        assert "Ship by Friday." in content

    def test_drc_fail_status(self, tmp_path: Path) -> None:
        data = _full_data(
            drc={
                "error_count": 3,
                "warning_count": 1,
                "blocking_count": 2,
                "passed": False,
            }
        )
        gen = ReportGenerator()
        report_path = gen.generate(data, tmp_path)

        content = report_path.read_text(encoding="utf-8")
        assert "FAIL" in content

    def test_erc_pass_status(self, tmp_path: Path) -> None:
        """ERC section renders PASS when passed is True."""
        data = _full_data(
            erc={
                "error_count": 0,
                "warning_count": 0,
                "passed": True,
                "details": "",
            }
        )
        gen = ReportGenerator()
        report_path = gen.generate(data, tmp_path)

        content = report_path.read_text(encoding="utf-8")
        assert "## ERC Status" in content
        assert "| Errors | 0 |" in content
        assert "| Warnings | 0 |" in content

    def test_erc_fail_status(self, tmp_path: Path) -> None:
        """ERC section renders FAIL when passed is False."""
        data = _full_data(
            erc={
                "error_count": 3,
                "warning_count": 1,
                "passed": False,
                "details": "pin_not_connected (2x), power_pin_not_driven (1x)",
            }
        )
        gen = ReportGenerator()
        report_path = gen.generate(data, tmp_path)

        content = report_path.read_text(encoding="utf-8")
        assert "## ERC Status" in content
        assert "| Errors | 3 |" in content
        assert "| Warnings | 1 |" in content
        assert "FAIL" in content
        assert "pin_not_connected (2x), power_pin_not_driven (1x)" in content

    def test_erc_omitted_when_none(self, tmp_path: Path) -> None:
        """ERC section is absent when erc is None."""
        data = _full_data(erc=None)
        gen = ReportGenerator()
        report_path = gen.generate(data, tmp_path)

        content = report_path.read_text(encoding="utf-8")
        assert "## ERC Status" not in content

    def test_erc_details_omitted_when_empty(self, tmp_path: Path) -> None:
        """ERC details line is not rendered when details is empty."""
        data = _full_data(
            erc={
                "error_count": 0,
                "warning_count": 0,
                "passed": True,
                "details": "",
            }
        )
        gen = ReportGenerator()
        report_path = gen.generate(data, tmp_path)

        content = report_path.read_text(encoding="utf-8")
        assert "## ERC Status" in content
        assert "**Details**" not in content

    def test_board_summary_full_render(self, tmp_path: Path) -> None:
        """Board summary section renders all collector-produced fields."""
        data = _full_data(
            board_stats={
                "layer_count": 2,
                "layer_names": ["F.Cu", "B.Cu"],
                "footprint_count": 134,
                "footprint_smd": 60,
                "footprint_tht": 70,
                "footprint_other": 4,
                "net_count": 62,
                "segment_count": 0,
                "via_count": 0,
                "board_width_mm": 200.0,
                "board_height_mm": 120.0,
            }
        )
        gen = ReportGenerator()
        report_path = gen.generate(data, tmp_path)
        content = report_path.read_text(encoding="utf-8")

        assert "## Board Summary" in content
        # Layers row: count + comma-joined names
        assert "2 copper (F.Cu, B.Cu)" in content
        # Footprints row: total with breakdown
        assert "134 (60 SMD, 70 THT, 4 other)" in content
        # Nets row
        assert "| Nets | 62 |" in content
        # Traces row
        assert "0 segments" in content
        # Vias row
        assert "| Vias | 0 |" in content
        # Board Size row
        assert "200.0 x 120.0 mm" in content
        # No None literals
        assert "None" not in content

    def test_board_summary_partial_fields(self, tmp_path: Path) -> None:
        """Omitting footprint breakdown fields renders only total count."""
        data = _full_data(
            board_stats={
                "footprint_count": 42,
                "net_count": 10,
            }
        )
        gen = ReportGenerator()
        report_path = gen.generate(data, tmp_path)
        content = report_path.read_text(encoding="utf-8")

        assert "## Board Summary" in content
        # Footprint total appears without breakdown
        assert "| Footprints | 42 |" in content
        # No SMD/THT breakdown since those fields are absent
        assert "SMD" not in content
        assert "THT" not in content
        # Layers row absent (no layer_count provided)
        assert "| Layers" not in content
        # Board Size row absent (no dimensions)
        assert "Board Size" not in content
        # No None literals
        assert "None" not in content

    def test_board_summary_only_layer_count(self, tmp_path: Path) -> None:
        """board_stats with only layer_count renders just the Layers row."""
        data = _full_data(
            board_stats={
                "layer_count": 6,
            }
        )
        gen = ReportGenerator()
        report_path = gen.generate(data, tmp_path)
        content = report_path.read_text(encoding="utf-8")

        assert "## Board Summary" in content
        assert "6 copper" in content
        # No other Board Summary rows (use table-row prefix to avoid
        # matching text from other sections like Routing Status)
        assert "| Footprints" not in content
        assert "| Nets |" not in content
        assert "| Traces" not in content
        assert "| Vias" not in content
        assert "| Board Size" not in content
        # No None literals
        assert "None" not in content

    def test_board_summary_none_omits_section(self, tmp_path: Path) -> None:
        """board_stats=None means no Board Summary section."""
        data = _full_data(board_stats=None)
        gen = ReportGenerator()
        report_path = gen.generate(data, tmp_path)
        content = report_path.read_text(encoding="utf-8")

        assert "## Board Summary" not in content

    def test_action_items_render_with_priority_labels(self, tmp_path: Path) -> None:
        """Action items render with bold priority labels, not raw dict strings."""
        data = _full_data(
            audit={
                "verdict": "not_ready",
                "action_items": [
                    {
                        "priority": 1,
                        "description": "Complete routing: 5 nets incomplete",
                        "command": "kct validate connectivity board.kicad_pcb",
                    },
                    {
                        "priority": 2,
                        "description": "Fix DRC errors before manufacturing",
                        "command": "kct drc board.kicad_pcb",
                    },
                    {
                        "priority": 3,
                        "description": "Review silkscreen placement",
                        "command": None,
                    },
                ],
            }
        )
        gen = ReportGenerator()
        report_path = gen.generate(data, tmp_path)
        content = report_path.read_text(encoding="utf-8")

        # Priority labels must appear as bold Markdown tags
        assert "**[CRITICAL]** Complete routing: 5 nets incomplete" in content
        assert "**[IMPORTANT]** Fix DRC errors before manufacturing" in content
        assert "**[OPTIONAL]** Review silkscreen placement" in content

        # Command must appear as inline code for items that have one
        assert "`kct validate connectivity board.kicad_pcb`" in content
        assert "`kct drc board.kicad_pcb`" in content

        # Raw dict syntax must NOT appear
        assert "{'priority'" not in content
        assert "'description'" not in content

        # No None literals (command=None must not render)
        assert "None" not in content

    def test_action_items_unknown_priority_renders_info(self, tmp_path: Path) -> None:
        """An action item with an unrecognized priority renders as INFO."""
        data = _full_data(
            audit={
                "verdict": "ready",
                "action_items": [
                    {
                        "priority": 99,
                        "description": "Some informational note",
                        "command": None,
                    },
                ],
            }
        )
        gen = ReportGenerator()
        report_path = gen.generate(data, tmp_path)
        content = report_path.read_text(encoding="utf-8")

        assert "**[INFO]** Some informational note" in content

    def test_empty_action_items_omits_subsection(self, tmp_path: Path) -> None:
        """An empty action_items list means no Action Items subsection."""
        data = _full_data(
            audit={
                "verdict": "ready",
                "action_items": [],
            }
        )
        gen = ReportGenerator()
        report_path = gen.generate(data, tmp_path)
        content = report_path.read_text(encoding="utf-8")

        assert "## Manufacturing Readiness" in content
        assert "### Action Items" not in content


# ---------------------------------------------------------------------------
# TestImportError
# ---------------------------------------------------------------------------


class TestReportImportError:
    """Test graceful degradation when jinja2 is absent."""

    def test_import_error_message(self) -> None:
        """Verify that when jinja2 is absent the module still imports but
        ReportData and ReportGenerator are not exported."""
        # Save and remove the real jinja2 module
        saved = {}
        for key in list(sys.modules.keys()):
            if key == "jinja2" or key.startswith("jinja2."):
                saved[key] = sys.modules.pop(key)

        # Also remove cached report modules so they re-import
        for key in list(sys.modules.keys()):
            if "kicad_tools.report" in key:
                saved[key] = sys.modules.pop(key)

        try:
            with mock.patch.dict(sys.modules, {"jinja2": None}):
                import importlib

                mod = importlib.import_module("kicad_tools.report")
                # Core figure/render exports must always be available
                assert hasattr(mod, "FigureEntry")
                assert hasattr(mod, "ReportFigureGenerator")
                assert hasattr(mod, "render_html")
                assert hasattr(mod, "render_pdf")
                # Jinja2-dependent exports must be absent
                assert not hasattr(mod, "ReportData")
                assert not hasattr(mod, "ReportGenerator")
        finally:
            # Restore all saved modules
            sys.modules.update(saved)


# ---------------------------------------------------------------------------
# TestReportCLI
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TestUnwrapEnvelope
# ---------------------------------------------------------------------------


class TestUnwrapEnvelope:
    """Test the _unwrap_envelope helper."""

    def test_unwraps_standard_envelope(self) -> None:
        from kicad_tools.cli.report_cmd import _unwrap_envelope

        payload = {
            "schema_version": 1,
            "generated_at": "2026-04-12T00:00:00+00:00",
            "pcb_path": "board.kicad_pcb",
            "data": {"layer_count": 4},
        }
        assert _unwrap_envelope(payload) == {"layer_count": 4}

    def test_returns_flat_dict_unchanged(self) -> None:
        from kicad_tools.cli.report_cmd import _unwrap_envelope

        flat = {"layer_count": 4, "net_count": 80}
        assert _unwrap_envelope(flat) == flat

    def test_returns_none_for_null_data(self) -> None:
        from kicad_tools.cli.report_cmd import _unwrap_envelope

        payload = {
            "schema_version": 1,
            "generated_at": "2026-04-12T00:00:00+00:00",
            "data": None,
        }
        assert _unwrap_envelope(payload) is None


# ---------------------------------------------------------------------------
# TestLoadDataDir
# ---------------------------------------------------------------------------


class TestLoadDataDir:
    """Test the _load_data_dir helper directly."""

    def test_filename_mapping_board_summary(self, tmp_path: Path) -> None:
        """board_summary.json maps to board_stats field."""
        from kicad_tools.cli.report_cmd import _load_data_dir

        (tmp_path / "board_summary.json").write_text(
            json.dumps({"layer_count": 2, "footprint_count": 5})
        )
        result = _load_data_dir(str(tmp_path))
        assert "board_stats" in result
        # footprint_count is preserved as-is (template uses footprint_count directly)
        assert result["board_stats"]["footprint_count"] == 5

    def test_filename_mapping_drc_summary(self, tmp_path: Path) -> None:
        """drc_summary.json maps to drc field."""
        from kicad_tools.cli.report_cmd import _load_data_dir

        (tmp_path / "drc_summary.json").write_text(json.dumps({"error_count": 0, "passed": True}))
        result = _load_data_dir(str(tmp_path))
        assert "drc" in result
        assert result["drc"]["passed"] is True

    def test_filename_mapping_erc_summary(self, tmp_path: Path) -> None:
        """erc_summary.json maps to erc field."""
        from kicad_tools.cli.report_cmd import _load_data_dir

        (tmp_path / "erc_summary.json").write_text(
            json.dumps({"error_count": 2, "warning_count": 1, "passed": False, "details": "test"})
        )
        result = _load_data_dir(str(tmp_path))
        assert "erc" in result
        assert result["erc"]["passed"] is False
        assert result["erc"]["error_count"] == 2

    def test_bom_groups_extraction(self, tmp_path: Path) -> None:
        """BOM groups list is extracted from the groups key."""
        from kicad_tools.cli.report_cmd import _load_data_dir

        (tmp_path / "bom.json").write_text(
            json.dumps(
                {
                    "total_components": 3,
                    "unique_parts": 1,
                    "dnp_count": 0,
                    "groups": [{"value": "10k", "qty": 3}],
                }
            )
        )
        result = _load_data_dir(str(tmp_path))
        assert isinstance(result["bom_groups"], list)
        assert result["bom_groups"][0]["value"] == "10k"

    def test_completion_percent_passed_through(self, tmp_path: Path) -> None:
        """completion_percent emitted by the collector is passed through as-is."""
        from kicad_tools.cli.report_cmd import _load_data_dir

        (tmp_path / "net_status.json").write_text(
            json.dumps({"completion_percent": 95.0, "incomplete_count": 2})
        )
        result = _load_data_dir(str(tmp_path))
        ns = result["net_status"]
        assert "completion_percent" in ns
        assert ns["completion_percent"] == 95.0

    def test_envelope_unwrapping(self, tmp_path: Path) -> None:
        """Enveloped JSON files are unwrapped to their data payload."""
        from kicad_tools.cli.report_cmd import _load_data_dir

        (tmp_path / "audit.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generated_at": "2026-04-12T00:00:00+00:00",
                    "pcb_path": "board.kicad_pcb",
                    "data": {"verdict": "ready"},
                }
            )
        )
        result = _load_data_dir(str(tmp_path))
        assert result["audit"]["verdict"] == "ready"
        # Envelope keys must not leak through
        assert "schema_version" not in result["audit"]

    def test_missing_files_produce_empty_result(self, tmp_path: Path) -> None:
        """A data directory with no recognized JSON files returns empty dict."""
        from kicad_tools.cli.report_cmd import _load_data_dir

        result = _load_data_dir(str(tmp_path))
        assert result == {}

    def test_null_envelope_skips_section(self, tmp_path: Path) -> None:
        """An envelope with data=null is skipped (section omitted)."""
        from kicad_tools.cli.report_cmd import _load_data_dir

        (tmp_path / "board_summary.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generated_at": "2026-04-12T00:00:00+00:00",
                    "data": None,
                }
            )
        )
        result = _load_data_dir(str(tmp_path))
        assert "board_stats" not in result


# ---------------------------------------------------------------------------
# TestReportCLI
# ---------------------------------------------------------------------------


class TestReportCLI:
    """Test the CLI entry point."""

    def test_help(self) -> None:
        """kct report generate --help must exit 0."""
        from kicad_tools.cli.report_cmd import main as report_main

        with pytest.raises(SystemExit) as exc_info:
            report_main(["generate", "--help"])
        assert exc_info.value.code == 0

    def test_report_parser_registered(self) -> None:
        """The report subcommand must be recognized by the main parser."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(["report", "generate", "test.kicad_pro", "--mfr", "jlcpcb"])
        assert args.command == "report"
        assert args.report_command == "generate"
        assert args.report_input == "test.kicad_pro"
        assert args.report_mfr == "jlcpcb"

    def test_generate_skeleton(self, tmp_path: Path) -> None:
        """Calling generate with --skip-collect should produce a skeleton report."""
        from kicad_tools.cli.report_cmd import main as report_main

        output_dir = tmp_path / "reports"
        result = report_main(
            [
                "generate",
                "test.kicad_pro",
                "--mfr",
                "testmfr",
                "-o",
                str(output_dir),
                "--skip-collect",
            ]
        )
        assert result == 0

        report_path = output_dir / "v1" / "report.md"
        assert report_path.exists()

        content = report_path.read_text(encoding="utf-8")
        assert "# test - Design Report" in content
        assert "testmfr" in content

    def test_generate_with_data_dir(self, tmp_path: Path) -> None:
        """Calling generate with --data-dir loads flat JSON files.

        Uses the correct filenames that the collector writes
        (``board_summary.json``, ``drc_summary.json``) and flat (no envelope)
        data.  This verifies backward-compatibility with non-enveloped JSON.
        """
        from kicad_tools.cli.report_cmd import main as report_main

        data_dir = tmp_path / "data"
        data_dir.mkdir()

        # Filenames must match what the collector writes, with collector field names
        (data_dir / "board_summary.json").write_text(
            json.dumps({"layer_count": 2, "layer_names": ["F.Cu", "B.Cu"], "net_count": 10})
        )
        (data_dir / "drc_summary.json").write_text(
            json.dumps({"error_count": 0, "warning_count": 0, "blocking_count": 0, "passed": True})
        )

        output_dir = tmp_path / "reports"
        result = report_main(
            [
                "generate",
                "board.kicad_pcb",
                "--mfr",
                "pcbway",
                "-o",
                str(output_dir),
                "--data-dir",
                str(data_dir),
            ]
        )
        assert result == 0

        content = (output_dir / "v1" / "report.md").read_text(encoding="utf-8")
        assert "## Board Summary" in content
        assert "## DRC Status" in content
        assert "PASS" in content

    def test_generate_with_data_dir_envelope(self, tmp_path: Path) -> None:
        """Envelope-wrapped JSON snapshots render all report sections.

        Writes JSON files using the same envelope format the collector
        produces (``{schema_version, generated_at, pcb_path, data: {...}}``),
        verifies that every section renders with the correct values, and
        exercises the data-dir loading bug fixes from issue #1321:

        1. Envelope unwrapping (all sections)
        2. Filename mapping (board_summary.json -> board_stats,
           drc_summary.json -> drc)
        3. BOM groups extraction (data.groups -> bom_groups list)
        """
        from kicad_tools.cli.report_cmd import main as report_main

        data_dir = tmp_path / "data"
        data_dir.mkdir()

        def _envelope(data):
            return {
                "schema_version": 1,
                "generated_at": "2026-04-12T00:00:00+00:00",
                "pcb_path": "board.kicad_pcb",
                "data": data,
            }

        # Bug 2: board_summary.json with collector field names
        (data_dir / "board_summary.json").write_text(
            json.dumps(
                _envelope(
                    {
                        "layer_count": 4,
                        "footprint_count": 42,
                        "net_count": 80,
                        "segment_count": 200,
                        "via_count": 15,
                        "board_width_mm": 50.0,
                        "board_height_mm": 30.0,
                    }
                )
            )
        )

        # Bug 2: drc_summary.json (not drc.json)
        (data_dir / "drc_summary.json").write_text(
            json.dumps(
                _envelope(
                    {
                        "error_count": 1,
                        "warning_count": 2,
                        "blocking_count": 0,
                        "passed": False,
                    }
                )
            )
        )

        # Bug 3: BOM groups nested under "groups" key
        (data_dir / "bom.json").write_text(
            json.dumps(
                _envelope(
                    {
                        "total_components": 10,
                        "unique_parts": 2,
                        "dnp_count": 0,
                        "groups": [
                            {
                                "value": "100nF",
                                "footprint": "0402",
                                "qty": 10,
                                "refs": "C1-C10",
                                "mpn": "CL05B104KO5NNNC",
                                "lcsc": "C1525",
                            },
                        ],
                    }
                )
            )
        )

        # Bug 1: audit with envelope
        (data_dir / "audit.json").write_text(
            json.dumps(
                _envelope(
                    {
                        "verdict": "ready",
                        "action_items": [
                            {
                                "priority": 3,
                                "description": "Review silkscreen",
                                "command": None,
                            },
                        ],
                    }
                )
            )
        )

        # net_status: collector now emits completion_percent directly
        (data_dir / "net_status.json").write_text(
            json.dumps(
                _envelope(
                    {
                        "total_nets": 80,
                        "complete_count": 76,
                        "incomplete_count": 4,
                        "unrouted_count": 2,
                        "total_unconnected_pads": 5,
                        "completion_percent": 95.0,
                        "incomplete_net_names": ["CLK", "MISO", "MOSI", "RST"],
                    }
                )
            )
        )

        output_dir = tmp_path / "reports"
        result = report_main(
            [
                "generate",
                "board.kicad_pcb",
                "--mfr",
                "jlcpcb",
                "-o",
                str(output_dir),
                "--data-dir",
                str(data_dir),
            ]
        )
        assert result == 0

        content = (output_dir / "v1" / "report.md").read_text(encoding="utf-8")

        # Section headings present
        assert "## Board Summary" in content
        assert "## Bill of Materials" in content
        assert "## DRC Status" in content
        assert "## Manufacturing Readiness" in content
        assert "## Routing Status" in content

        # Bug 2: footprint_count renders directly (template uses footprint_count)
        assert "| Footprints | 42 |" in content

        # Bug 1: board_stats values render (layer_count from envelope)
        assert "| Layers | 4 copper |" in content

        # Bug 2: DRC values render from drc_summary.json
        assert "| Errors | 1 |" in content
        assert "FAIL" in content

        # Bug 3: BOM table rows populated
        assert "100nF" in content
        assert "C1525" in content

        # Bug 1: audit renders
        assert "READY" in content
        assert "Review silkscreen" in content

        # Routing status: completion_percent renders directly from collector
        assert "95.0%" in content
        # New rows in the routing status table
        assert "| Complete Nets | 76 / 80 |" in content
        assert "| Incomplete Nets | 4 |" in content
        assert "| Unconnected Pads | 5 |" in content
        # Incomplete net names list rendered
        assert "### Incomplete Nets" in content
        assert "- CLK" in content
        assert "- MOSI" in content

    def test_generate_with_data_dir_null_envelope(self, tmp_path: Path) -> None:
        """An envelope with ``data: null`` (collector failure) omits the section."""
        from kicad_tools.cli.report_cmd import main as report_main

        data_dir = tmp_path / "data"
        data_dir.mkdir()

        (data_dir / "board_summary.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generated_at": "2026-04-12T00:00:00+00:00",
                    "pcb_path": "board.kicad_pcb",
                    "data": None,
                }
            )
        )

        output_dir = tmp_path / "reports"
        result = report_main(
            [
                "generate",
                "board.kicad_pcb",
                "--mfr",
                "jlcpcb",
                "-o",
                str(output_dir),
                "--data-dir",
                str(data_dir),
            ]
        )
        assert result == 0

        content = (output_dir / "v1" / "report.md").read_text(encoding="utf-8")
        # Section should be omitted, not crash
        assert "## Board Summary" not in content

    def test_data_dir_bypasses_collector(self, tmp_path: Path) -> None:
        """When --data-dir is provided, ReportDataCollector must not be instantiated."""
        from kicad_tools.cli.report_cmd import main as report_main

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "board_stats.json").write_text(
            json.dumps({"layer_count": 2, "component_count": 10})
        )

        output_dir = tmp_path / "reports"
        with mock.patch(
            "kicad_tools.cli.report_cmd._auto_collect",
            side_effect=AssertionError("_auto_collect should not be called"),
        ):
            result = report_main(
                [
                    "generate",
                    "board.kicad_pcb",
                    "--mfr",
                    "jlcpcb",
                    "-o",
                    str(output_dir),
                    "--data-dir",
                    str(data_dir),
                ]
            )
        assert result == 0

    def test_auto_collect_writes_data_and_report_same_version(self, tmp_path: Path) -> None:
        """Auto-collect should write data into vN/data/ and the report into the same vN/."""
        from kicad_tools.cli.report_cmd import main as report_main

        output_dir = tmp_path / "reports"

        # Mock _auto_collect to simulate writing data into the correct version dir
        def fake_auto_collect(pcb_path, output_dir, manufacturer, quantity, skip_erc):
            from kicad_tools.report.generator import ReportGenerator

            version_dir = ReportGenerator.next_version_dir(output_dir)
            data_dir = version_dir / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / "board_stats.json").write_text(
                json.dumps({"layer_count": 4, "component_count": 42})
            )
            from kicad_tools.cli.report_cmd import _load_data_dir

            data_kwargs = _load_data_dir(str(data_dir))
            return version_dir, data_kwargs

        with mock.patch(
            "kicad_tools.cli.report_cmd._auto_collect",
            side_effect=fake_auto_collect,
        ):
            result = report_main(
                [
                    "generate",
                    "board.kicad_pcb",
                    "--mfr",
                    "jlcpcb",
                    "-o",
                    str(output_dir),
                ]
            )

        assert result == 0

        # Data and report must both be under v1
        assert (output_dir / "v1" / "data" / "board_stats.json").exists()
        assert (output_dir / "v1" / "report.md").exists()

        # No v2 directory should exist (no version-numbering race)
        assert not (output_dir / "v2").exists()

    def test_auto_collect_collector_failure_non_fatal(self, tmp_path: Path) -> None:
        """If collect_all produces no data, the CLI should still return 0."""
        from kicad_tools.cli.report_cmd import main as report_main

        output_dir = tmp_path / "reports"

        # Mock _auto_collect to simulate a collector that produces no data
        def empty_auto_collect(pcb_path, output_dir, manufacturer, quantity, skip_erc):
            from kicad_tools.report.generator import ReportGenerator

            version_dir = ReportGenerator.next_version_dir(output_dir)
            data_dir = version_dir / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            return version_dir, {}

        with mock.patch(
            "kicad_tools.cli.report_cmd._auto_collect",
            side_effect=empty_auto_collect,
        ):
            result = report_main(
                [
                    "generate",
                    "board.kicad_pcb",
                    "--mfr",
                    "jlcpcb",
                    "-o",
                    str(output_dir),
                ]
            )

        assert result == 0
        assert (output_dir / "v1" / "report.md").exists()

    def test_new_flags_forwarded_to_collector(self, tmp_path: Path) -> None:
        """--quantity and --skip-erc must reach _auto_collect correctly."""
        from kicad_tools.cli.report_cmd import main as report_main

        output_dir = tmp_path / "reports"
        captured = {}

        def capturing_auto_collect(pcb_path, output_dir, manufacturer, quantity, skip_erc):
            captured["quantity"] = quantity
            captured["skip_erc"] = skip_erc
            from kicad_tools.report.generator import ReportGenerator

            version_dir = ReportGenerator.next_version_dir(output_dir)
            data_dir = version_dir / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            return version_dir, {}

        with mock.patch(
            "kicad_tools.cli.report_cmd._auto_collect",
            side_effect=capturing_auto_collect,
        ):
            result = report_main(
                [
                    "generate",
                    "board.kicad_pcb",
                    "--mfr",
                    "jlcpcb",
                    "-o",
                    str(output_dir),
                    "--quantity",
                    "50",
                    "--skip-erc",
                ]
            )

        assert result == 0
        assert captured["quantity"] == 50
        assert captured["skip_erc"] is True

    def test_skip_collect_produces_skeleton(self, tmp_path: Path) -> None:
        """--skip-collect must produce a skeleton report without invoking the collector."""
        from kicad_tools.cli.report_cmd import main as report_main

        output_dir = tmp_path / "reports"

        with mock.patch(
            "kicad_tools.cli.report_cmd._auto_collect",
            side_effect=AssertionError("_auto_collect should not be called"),
        ):
            result = report_main(
                [
                    "generate",
                    "board.kicad_pcb",
                    "--mfr",
                    "jlcpcb",
                    "-o",
                    str(output_dir),
                    "--skip-collect",
                ]
            )

        assert result == 0
        report_path = output_dir / "v1" / "report.md"
        assert report_path.exists()

        content = report_path.read_text(encoding="utf-8")
        # Skeleton: header present, optional data sections absent
        assert "# board - Design Report" in content
        assert "## Board Summary" not in content

    def test_version_dir_parameter_in_generator(self, tmp_path: Path) -> None:
        """ReportGenerator.generate() with version_dir writes to the specified directory."""
        data = _full_data()
        gen = ReportGenerator()

        custom_dir = tmp_path / "v99"
        report_path = gen.generate(data, tmp_path, version_dir=custom_dir)

        assert report_path.parent.name == "v99"
        assert report_path.exists()

        content = report_path.read_text(encoding="utf-8")
        assert "# TestBoard - Design Report" in content

    def test_version_dir_none_auto_increments(self, tmp_path: Path) -> None:
        """When version_dir is None, generate() auto-increments as before."""
        data = _full_data()
        gen = ReportGenerator()

        p1 = gen.generate(data, tmp_path, version_dir=None)
        p2 = gen.generate(data, tmp_path, version_dir=None)

        assert p1.parent.name == "v1"
        assert p2.parent.name == "v2"


# ---------------------------------------------------------------------------
# TestReportAutoCollect
# ---------------------------------------------------------------------------


class TestReportAutoCollect:
    """Tests for the _auto_collect helper function."""

    def test_auto_collect_returns_version_dir_and_data(self, tmp_path: Path) -> None:
        """_auto_collect should return a (version_dir, data_kwargs) tuple."""
        from kicad_tools.cli.report_cmd import _auto_collect
        from kicad_tools.report.collector import ReportDataCollector

        def stub_collect_all(self_collector, data_dir):
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / "board_summary.json").write_text(
                json.dumps({"layer_count": 2, "footprint_count": 5})
            )
            return {"board_summary": data_dir / "board_summary.json"}

        with mock.patch.object(ReportDataCollector, "collect_all", stub_collect_all):
            version_dir, data_kwargs = _auto_collect(
                pcb_path=Path("test.kicad_pcb"),
                output_dir=tmp_path / "reports",
                manufacturer="jlcpcb",
                quantity=10,
                skip_erc=True,
            )

        assert version_dir == tmp_path / "reports" / "v1"
        assert "board_stats" in data_kwargs
        assert data_kwargs["board_stats"]["layer_count"] == 2

    def test_auto_collect_next_version_increments(self, tmp_path: Path) -> None:
        """If v1 already exists, _auto_collect should use v2."""
        from kicad_tools.cli.report_cmd import _auto_collect
        from kicad_tools.report.collector import ReportDataCollector

        output_dir = tmp_path / "reports"
        (output_dir / "v1").mkdir(parents=True)

        def stub_collect_all(self_collector, data_dir):
            data_dir.mkdir(parents=True, exist_ok=True)
            return {}

        with mock.patch.object(ReportDataCollector, "collect_all", stub_collect_all):
            version_dir, _ = _auto_collect(
                pcb_path=Path("test.kicad_pcb"),
                output_dir=output_dir,
                manufacturer="jlcpcb",
                quantity=5,
                skip_erc=False,
            )

        assert version_dir == output_dir / "v2"
