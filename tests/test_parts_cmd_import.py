"""``kct parts import`` is reachable from the top-level CLI (Issue #7065)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.cli import main
from kicad_tools.parts.importer import PartImporter


@pytest.fixture
def mocked_importer():
    from kicad_tools.datasheet.models import Datasheet, DatasheetResult
    from kicad_tools.datasheet.pins import ExtractedPin, PinTable

    found = DatasheetResult(
        part_number="STM32F103C8T6",
        manufacturer="ST",
        description="MCU",
        datasheet_url="https://example.com/d.pdf",
        source="lcsc",
        confidence=0.95,
    )
    sheet = Datasheet(
        part_number="STM32F103C8T6",
        manufacturer="ST",
        local_path=Path("/tmp/test.pdf"),
        source_url="https://example.com/d.pdf",
        source="lcsc",
        downloaded_at=datetime.now(),
        file_size=1,
    )
    table = PinTable(
        pins=[ExtractedPin(number="1", name="VBAT", type="power_in")], package="LQFP48"
    )
    with (
        patch.object(PartImporter, "_search_datasheet", return_value=found),
        patch.object(PartImporter, "_download_datasheet", return_value=sheet),
        patch.object(PartImporter, "_parse_datasheet", return_value=table),
        patch.object(PartImporter, "_match_footprint", return_value=("Package_QFP:LQFP-48", 0.9)),
        patch.object(PartImporter, "_generate_symbol", return_value="STM32F103C8T6"),
        patch.object(PartImporter, "_save_symbol"),
    ):
        yield


def test_help_exits_zero_and_lists_options(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["parts", "import", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for flag in ("--package", "--layout", "--dry-run", "--overwrite", "--format"):
        assert flag in out


def test_dry_run_end_to_end(mocked_importer, tmp_path, capsys):
    lib = tmp_path / "out.kicad_sym"
    rc = main(["parts", "import", "STM32F103C8T6", "--library", str(lib), "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "STM32F103C8T6" in out


def test_interactive_warns_on_stderr_stdout_unchanged(mocked_importer, tmp_path, capsys):
    lib = str(tmp_path / "out.kicad_sym")
    argv = ["parts", "import", "STM32F103C8T6", "--library", lib, "--dry-run", "--format", "json"]
    assert main(argv) == 0
    base = capsys.readouterr()
    assert "deprecated" not in base.err

    assert main([*argv, "-i"]) == 0
    with_i = capsys.readouterr()
    assert with_i.err.count("deprecated") == 1
    assert with_i.out == base.out
