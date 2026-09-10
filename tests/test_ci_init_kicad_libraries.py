"""Headless KiCad initialization preserves configuration and fails closed."""

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


@pytest.fixture
def config(monkeypatch, tmp_path):
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(helper.subprocess, "check_output", lambda *a, **kw: "10.0.5\n")
    return tmp_path / "10.0"


def templates(monkeypatch, files):
    read_text = Path.read_text
    monkeypatch.setattr(Path, "is_file", lambda path: str(path) in files)
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda path, *a, **kw: (
            files[str(path)] if str(path) in files else read_text(path, *a, **kw)
        ),
    )


def test_existing_config_is_preserved_without_template(monkeypatch, config):
    config.mkdir()
    targets = (config / "fp-lib-table", config / "sym-lib-table")
    for target in targets:
        target.write_text("user libraries")
    templates(monkeypatch, {})
    assert helper.initialize() == targets
    assert all(target.read_text() == "user libraries" for target in targets)


@pytest.mark.parametrize("existing", [None, "fp-lib-table", "sym-lib-table"])
def test_missing_template_fails_without_creating_table(monkeypatch, config, existing):
    if existing:
        config.mkdir()
        (config / existing).write_text("user libraries")
    templates(monkeypatch, {})
    with pytest.raises(FileNotFoundError, match="stock .* table is unavailable"):
        helper.initialize()
    assert sorted(p.name for p in config.glob("*")) == ([existing] if existing else [])


@pytest.mark.parametrize("existing", [None, "fp-lib-table", "sym-lib-table"])
def test_missing_stock_tables_installed_preserving_other_table(monkeypatch, config, existing):
    names = {"fp-lib-table": "FOOTPRINT", "sym-lib-table": "SYMBOL"}
    data = {n: f'(table (uri "${{KICAD10_{v}_DIR}}/stock"))' for n, v in names.items()}
    if existing:
        config.mkdir()
        (config / existing).write_text("custom table")
    templates(monkeypatch, {f"/usr/share/kicad/template/{n}": text for n, text in data.items()})
    assert helper.initialize() == (config / "fp-lib-table", config / "sym-lib-table")
    for name in names:
        assert (config / name).read_text() == ("custom table" if name == existing else data[name])


def test_wrong_version_symbol_template_fails_before_writing(monkeypatch, config):
    templates(
        monkeypatch,
        {
            "/usr/share/kicad/template/fp-lib-table": "${KICAD10_FOOTPRINT_DIR}",
            "/usr/share/kicad/template/sym-lib-table": "${KICAD9_SYMBOL_DIR}",
        },
    )
    with pytest.raises(RuntimeError, match="symbol tables .* do not match KiCad"):
        helper.initialize()
    assert not config.exists()


def test_matching_version_template_selected_after_stale_candidate(monkeypatch, config):
    templates(
        monkeypatch,
        {
            "/usr/share/kicad/template/fp-lib-table": "${KICAD9_FOOTPRINT_DIR}",
            "/usr/local/share/kicad/template/fp-lib-table": "${KICAD10_FOOTPRINT_DIR}",
            "/usr/share/kicad/template/sym-lib-table": "${KICAD10_SYMBOL_DIR}",
        },
    )
    fp, sym = helper.initialize()
    assert fp.read_text() == "${KICAD10_FOOTPRINT_DIR}"
    assert sym.read_text() == "${KICAD10_SYMBOL_DIR}"
