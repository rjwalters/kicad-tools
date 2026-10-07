"""Drill export passes only flags kicad-cli accepts, and fails loudly (Issue #6167).

``GerberExporter._export_drill`` used to pass ``--merge-npth`` and
``--minimal-header``, neither of which ``kicad-cli pcb export drill`` has.
The OSH Park preset (``merge_pth_npth=True``) therefore always failed, and
``merge_pth_npth=False`` was silently ignored because kicad-cli merges PTH and
NPTH by default.  These tests pin the mapping onto the real flags
(``--excellon-separate-th``, ``--excellon-min-header``, ``--excellon-units``,
``--excellon-zeros-format``) and the post-run check that drill files exist.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.cli.runner import find_kicad_cli
from kicad_tools.exceptions import ConfigurationError, ExportError
from kicad_tools.export.gerber import MANUFACTURER_PRESETS, GerberConfig, GerberExporter


@pytest.fixture
def exporter(tmp_path):
    pcb_path = tmp_path / "board.kicad_pcb"
    pcb_path.write_text("(kicad_pcb)")
    exp = GerberExporter.__new__(GerberExporter)
    exp.pcb_path = pcb_path
    exp.kicad_cli = Path("/usr/bin/kicad-cli")
    return exp


def _cmd(exporter, tmp_path, **kwargs):
    config = GerberConfig(use_aux_origin=False, **kwargs)
    return exporter._drill_command(config, tmp_path / "out")


def _value(cmd, flag):
    return cmd[cmd.index(flag) + 1]


def test_separate_is_the_default_and_uses_the_real_flag(exporter, tmp_path):
    cmd = _cmd(exporter, tmp_path)
    assert "--excellon-separate-th" in cmd
    assert "--merge-npth" not in cmd
    assert "--minimal-header" not in cmd
    assert "--excellon-min-header" not in cmd
    assert _value(cmd, "--format") == "excellon"
    assert _value(cmd, "--excellon-units") == "mm"
    assert _value(cmd, "--excellon-zeros-format") == "decimal"


def test_merge_omits_separate_flag(exporter, tmp_path):
    cmd = _cmd(exporter, tmp_path, merge_pth_npth=True)
    assert "--excellon-separate-th" not in cmd
    assert "--merge-npth" not in cmd


def test_minimal_header_maps_to_excellon_min_header(exporter, tmp_path):
    cmd = _cmd(exporter, tmp_path, minimal_header=True)
    assert "--excellon-min-header" in cmd
    assert "--minimal-header" not in cmd


@pytest.mark.parametrize(("units", "flag"), [("mm", "mm"), ("in", "in"), ("inch", "in")])
def test_units(exporter, tmp_path, units, flag):
    assert _value(_cmd(exporter, tmp_path, drill_units=units), "--excellon-units") == flag


def test_zeros_format(exporter, tmp_path):
    cmd = _cmd(exporter, tmp_path, drill_zeros_format="suppressleading")
    assert _value(cmd, "--excellon-zeros-format") == "suppressleading"


@pytest.mark.parametrize("fmt", ["gerber", "gerber_x2"])
def test_gerber_format_maps_to_kicad_cli_value_and_drops_excellon_flags(exporter, tmp_path, fmt):
    cmd = _cmd(exporter, tmp_path, drill_format=fmt)
    assert _value(cmd, "--format") == "gerber"
    assert not [a for a in cmd if a.startswith("--excellon-")]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"drill_format": "odb"},
        {"drill_units": "mil"},
        {"drill_zeros_format": "trailing"},
        {"drill_format": "gerber", "merge_pth_npth": True},
    ],
)
def test_values_kicad_cli_would_reject_raise_before_running(exporter, tmp_path, kwargs):
    with patch("kicad_tools.export.gerber.subprocess.run") as run:
        with pytest.raises(ConfigurationError):
            exporter._export_drill(GerberConfig(use_aux_origin=False, **kwargs), tmp_path)
    run.assert_not_called()


def test_kicad_cli_failure_raises(exporter, tmp_path):
    err = subprocess.CalledProcessError(1, ["kicad-cli"], output="Unknown argument: --x", stderr="")
    with patch("kicad_tools.export.gerber.subprocess.run", side_effect=err):
        with pytest.raises(ExportError, match="Drill export failed"):
            exporter._export_drill(GerberConfig(use_aux_origin=False), tmp_path)


@pytest.mark.parametrize("merge", [False, True])
def test_clean_exit_without_drill_files_raises(exporter, tmp_path, merge):
    ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="Done.", stderr="")
    with patch("kicad_tools.export.gerber.subprocess.run", return_value=ok):
        with pytest.raises(ExportError, match="wrote no drill file"):
            exporter._export_drill(
                GerberConfig(use_aux_origin=False, merge_pth_npth=merge), tmp_path
            )


def test_stale_drill_files_from_the_other_mode_are_removed(exporter, tmp_path):
    (tmp_path / "board.drl").write_text("stale merged")

    def fake_run(cmd, **_):
        (tmp_path / "board-PTH.drl").write_text("M48")
        (tmp_path / "board-NPTH.drl").write_text("M48")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    with patch("kicad_tools.export.gerber.subprocess.run", side_effect=fake_run):
        exporter._export_drill(GerberConfig(use_aux_origin=False), tmp_path)

    assert sorted(p.name for p in tmp_path.glob("*.drl")) == ["board-NPTH.drl", "board-PTH.drl"]


def _drill_help_flags() -> set[str]:
    kicad_cli = find_kicad_cli()
    assert kicad_cli is not None
    out = subprocess.run(
        [str(kicad_cli), "pcb", "export", "drill", "--help"],
        capture_output=True,
        text=True,
    )
    return set(re.findall(r"--[a-z][a-z0-9-]*", out.stdout + out.stderr))


@pytest.mark.skipif(find_kicad_cli() is None, reason="kicad-cli not installed")
@pytest.mark.parametrize(
    "config",
    [
        *(p.config for p in MANUFACTURER_PRESETS.values()),
        GerberConfig(merge_pth_npth=True, minimal_header=True, drill_units="in"),
        GerberConfig(drill_format="gerber"),
    ],
)
def test_every_drill_flag_is_one_kicad_cli_lists(exporter, tmp_path, config):
    exporter.kicad_cli = find_kicad_cli()
    flags = {a for a in exporter._drill_command(config, tmp_path) if a.startswith("--")}
    unknown = flags - _drill_help_flags()
    assert not unknown, f"kicad-cli pcb export drill does not accept {sorted(unknown)}"
