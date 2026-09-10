"""Headless KiCad initialization preserves user configuration and fails closed."""

import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "init_kicad_libraries",
    Path(__file__).resolve().parents[1] / "scripts/ci/init_kicad_libraries.py",
)
assert SPEC and SPEC.loader
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


def test_existing_config_is_preserved_without_template(monkeypatch, tmp_path):
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(helper.subprocess, "check_output", lambda *a, **kw: "10.0.5\n")
    target = tmp_path / "10.0/fp-lib-table"
    target.parent.mkdir()
    target.write_text("user libraries")
    monkeypatch.setattr(Path, "is_file", lambda path: False)
    assert helper.initialize() == target
    assert target.read_text() == "user libraries"


def test_missing_template_fails_without_creating_table(monkeypatch, tmp_path):
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(helper.subprocess, "check_output", lambda *a, **kw: "10.0.5\n")
    monkeypatch.setattr(Path, "is_file", lambda path: False)
    with pytest.raises(FileNotFoundError, match="stock footprint table is unavailable"):
        helper.initialize()
    assert not (tmp_path / "10.0/fp-lib-table").exists()


def test_stock_table_installed_in_versioned_config(monkeypatch, tmp_path):
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(helper.subprocess, "check_output", lambda *a, **kw: "10.0.5\n")
    template = Path("/usr/share/kicad/template/fp-lib-table")
    data = '(fp_lib_table (lib (uri "${KICAD10_FOOTPRINT_DIR}/Capacitor_SMD.pretty")))'
    read_text = Path.read_text
    monkeypatch.setattr(Path, "is_file", lambda path: path == template)
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda path, *a, **kw: data if path == template else read_text(path, *a, **kw),
    )
    target = helper.initialize()
    assert target == tmp_path / "10.0/fp-lib-table"
    assert target.read_text() == data
