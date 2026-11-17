"""PCB.export_gerbers honours layers/include_drill/create_zip on presets (Issue #7023).

Before the fix the jlcpcb (default), pcbway and oshpark paths dropped all
three arguments: ``create_zip=False`` still returned a zip, ``include_drill``
was ignored, explicit ``layers`` never reached kicad-cli, and
``export_gerbers_zip`` produced a zip holding a nested ``gerbers.zip``.

kicad-cli is faked: ``subprocess.run`` records each command and writes
dummy output files so the exporter's own zip / drill checks run for real.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.export.gerber import GerberExporter
from kicad_tools.schema.pcb import PCB

PRESETS = ["jlcpcb", "pcbway", "oshpark"]

MINIMAL_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
)
"""


class FakeKicadCli:
    """Stand-in for ``subprocess.run`` that mimics kicad-cli's outputs."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, cmd, *args, **kwargs):
        import subprocess

        if list(cmd[1:3]) != ["pcb", "export"]:
            # e.g. a ``kicad-cli version`` probe
            return subprocess.CompletedProcess(cmd, 0, stdout="10.0.6\n", stderr="")
        self.commands.append(list(cmd))
        out_dir = Path(cmd[cmd.index("--output") + 1])
        stem = Path(cmd[4]).stem
        if cmd[3] == "gerbers":
            (out_dir / f"{stem}-F_Cu.gtl").write_text("G04 dummy*\n")
            (out_dir / f"{stem}-Edge_Cuts.gm1").write_text("G04 dummy*\n")
        elif cmd[3] == "drill":
            for name in (f"{stem}.drl", f"{stem}-PTH.drl", f"{stem}-NPTH.drl"):
                (out_dir / name).write_text("M48\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def subcommands(self) -> list[str]:
        return [c[3] for c in self.commands]

    def gerber_layers(self) -> str:
        (cmd,) = [c for c in self.commands if c[3] == "gerbers"]
        return cmd[cmd.index("--layers") + 1]


@pytest.fixture
def pcb_file(tmp_path: Path) -> Path:
    path = tmp_path / "board.kicad_pcb"
    path.write_text(MINIMAL_PCB)
    return path


@pytest.fixture
def fake_cli():
    fake = FakeKicadCli()
    with (
        patch("kicad_tools.export.gerber.find_kicad_cli", return_value=Path("/usr/bin/kicad-cli")),
        patch("kicad_tools.export.gerber.subprocess.run", side_effect=fake),
    ):
        yield fake


def _has_drill(names) -> bool:
    return any(str(n).endswith(".drl") for n in names)


@pytest.mark.parametrize("manufacturer", PRESETS)
class TestPresetOverrides:
    def test_default_still_returns_zip(self, pcb_file, tmp_path, fake_cli, manufacturer):
        out = tmp_path / "out"
        result = PCB.load(pcb_file).export_gerbers(out, manufacturer=manufacturer)
        assert result.suffix == ".zip" and result.is_file()
        with zipfile.ZipFile(result) as zf:
            assert _has_drill(zf.namelist())
        assert "drill" in fake_cli.subcommands()

    def test_create_zip_false_returns_directory(self, pcb_file, tmp_path, fake_cli, manufacturer):
        out = tmp_path / "out"
        result = PCB.load(pcb_file).export_gerbers(out, manufacturer=manufacturer, create_zip=False)
        assert result == out and result.is_dir()
        names = [p.name for p in out.iterdir()]
        assert "gerbers.zip" not in names
        assert any(n.endswith(".gtl") for n in names)
        assert _has_drill(names)

    def test_create_zip_true_returns_zip(self, pcb_file, tmp_path, fake_cli, manufacturer):
        result = PCB.load(pcb_file).export_gerbers(
            tmp_path / "out", manufacturer=manufacturer, create_zip=True
        )
        assert result.suffix == ".zip" and result.is_file()

    def test_include_drill_false_skips_drill(self, pcb_file, tmp_path, fake_cli, manufacturer):
        out = tmp_path / "out"
        PCB.load(pcb_file).export_gerbers(
            out, manufacturer=manufacturer, include_drill=False, create_zip=False
        )
        assert "drill" not in fake_cli.subcommands()
        assert not _has_drill(p.name for p in out.iterdir())

    def test_include_drill_true_produces_drill(self, pcb_file, tmp_path, fake_cli, manufacturer):
        PCB.load(pcb_file).export_gerbers(
            tmp_path / "out", manufacturer=manufacturer, include_drill=True
        )
        assert "drill" in fake_cli.subcommands()

    def test_explicit_layers_reach_kicad_cli(self, pcb_file, tmp_path, fake_cli, manufacturer):
        PCB.load(pcb_file).export_gerbers(
            tmp_path / "out", manufacturer=manufacturer, layers=["F.Cu", "B.Cu", "Edge.Cuts"]
        )
        assert fake_cli.gerber_layers() == "F.Cu,B.Cu,Edge.Cuts"

    def test_omitted_layers_use_preset_default(self, pcb_file, tmp_path, fake_cli, manufacturer):
        PCB.load(pcb_file).export_gerbers(tmp_path / "out", manufacturer=manufacturer)
        layers = fake_cli.gerber_layers().split(",")
        assert {"F.Cu", "B.Cu", "Edge.Cuts", "F.Mask", "B.Mask"} <= set(layers)


def test_layers_without_edge_cuts_warns(pcb_file, tmp_path, fake_cli, caplog):
    with caplog.at_level("WARNING", logger="kicad_tools.export.gerber"):
        PCB.load(pcb_file).export_gerbers(tmp_path / "out", layers=["F.Cu"])
    assert fake_cli.gerber_layers() == "F.Cu"
    assert "Edge.Cuts" in caplog.text


class TestGenericPathUnchanged:
    def test_omitted_args_return_directory_with_drill(self, pcb_file, tmp_path, fake_cli):
        out = tmp_path / "out"
        result = PCB.load(pcb_file).export_gerbers(out, manufacturer="generic")
        assert result == out and result.is_dir()
        assert "drill" in fake_cli.subcommands()

    def test_explicit_args_honoured(self, pcb_file, tmp_path, fake_cli):
        result = PCB.load(pcb_file).export_gerbers(
            tmp_path / "out", manufacturer="generic", include_drill=False, create_zip=True
        )
        assert result.suffix == ".zip"
        assert "drill" not in fake_cli.subcommands()


class TestExportForManufacturerOverrides:
    """``None`` keeps the preset field; any other value replaces it."""

    def _config(self, pcb_file, **kwargs):
        exporter = GerberExporter(pcb_file)
        with patch.object(GerberExporter, "export", autospec=True) as export:
            exporter.export_for_manufacturer("jlcpcb", "out", **kwargs)
        return export.call_args.args[1]

    def test_none_keeps_preset(self, pcb_file, fake_cli):
        from kicad_tools.export.gerber import JLCPCB_PRESET

        config = self._config(pcb_file)
        assert config == JLCPCB_PRESET.config

    def test_values_override_preset(self, pcb_file, fake_cli):
        config = self._config(
            pcb_file, layers=["F.Cu", "Edge.Cuts"], generate_drill=False, create_zip=False
        )
        assert config.layers == ["F.Cu", "Edge.Cuts"]
        assert config.generate_drill is False
        assert config.create_zip is False

    def test_preset_config_not_mutated(self, pcb_file, fake_cli):
        from kicad_tools.export.gerber import JLCPCB_PRESET

        self._config(pcb_file, layers=["F.Cu"], generate_drill=False, create_zip=False)
        assert JLCPCB_PRESET.config.layers == []
        assert JLCPCB_PRESET.config.generate_drill is True
        assert JLCPCB_PRESET.config.create_zip is True


@pytest.mark.parametrize("manufacturer", PRESETS)
class TestExportGerbersZip:
    def test_no_nested_zip(self, pcb_file, tmp_path, fake_cli, manufacturer):
        output = tmp_path / "fab.zip"
        result = PCB.load(pcb_file).export_gerbers_zip(output, manufacturer=manufacturer)
        assert result == output
        with zipfile.ZipFile(output) as zf:
            names = zf.namelist()
        assert "gerbers.zip" not in names
        assert not any(n.endswith(".zip") for n in names)
        assert any(n.endswith(".gtl") for n in names)
        assert _has_drill(names)

    def test_include_drill_false(self, pcb_file, tmp_path, fake_cli, manufacturer):
        output = tmp_path / "fab.zip"
        PCB.load(pcb_file).export_gerbers_zip(
            output, manufacturer=manufacturer, include_drill=False
        )
        with zipfile.ZipFile(output) as zf:
            names = zf.namelist()
        assert not _has_drill(names)
        assert "drill" not in fake_cli.subcommands()
