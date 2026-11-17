"""CI's KiCad init writes the pinned tables into an isolated config home (#5860, #6962)."""

import importlib.util
import shlex
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "init_kicad_libraries",
    Path(__file__).resolve().parents[1] / "scripts/ci/init_kicad_libraries.py",
)
assert SPEC and SPEC.loader
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)

TABLES = Path(__file__).resolve().parents[1] / "src/kicad_tools/data/kicad-config/10.0"


@pytest.fixture
def kicad(monkeypatch, tmp_path):
    """A KiCad 10.0 kicad-cli, no GitHub env, and a user config that must stay untouched."""
    user = tmp_path / "user" / "10.0"
    user.mkdir(parents=True)
    (user / "fp-lib-table").write_text("user libraries")
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(tmp_path / "user"))
    monkeypatch.delenv("GITHUB_ENV", raising=False)
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path / "runner"))
    (tmp_path / "runner").mkdir()
    monkeypatch.setattr(helper.subprocess, "check_output", lambda *a, **kw: "10.0.6\n")
    return user


def test_writes_pinned_tables_into_fresh_isolated_home(kicad, tmp_path):
    home, tables = helper.initialize()
    assert home.parent == tmp_path / "runner"
    assert tables == (home / "10.0" / "fp-lib-table", home / "10.0" / "sym-lib-table")
    for table in tables:
        assert table.read_bytes() == (TABLES / table.name).read_bytes()
    # The user's own config is neither read nor modified.
    assert (kicad / "fp-lib-table").read_text() == "user libraries"
    assert helper.os.environ["KICAD_CONFIG_HOME"] == str(home)


def test_github_env_receives_the_config_home(kicad, tmp_path, monkeypatch):
    github_env = tmp_path / "github_env"
    monkeypatch.setenv("GITHUB_ENV", str(github_env))
    home, _ = helper.initialize(tmp_path / "chosen")
    assert home == tmp_path / "chosen"
    assert github_env.read_text() == f"KICAD_CONFIG_HOME={home}\n"


def test_shell_mode_prints_an_eval_able_export(kicad, tmp_path, capsys):
    assert helper.main(["--shell", "--config-home", str(tmp_path / "with space")]) == 0
    out = capsys.readouterr().out.strip()
    assert out == f"export KICAD_CONFIG_HOME={shlex.quote(str(tmp_path / 'with space'))}"


def test_wrong_kicad_release_fails_before_writing(kicad, tmp_path, monkeypatch):
    monkeypatch.setattr(helper.subprocess, "check_output", lambda *a, **kw: "9.0.1\n")
    with pytest.raises(
        RuntimeError, match=r"KiCad 9\.0.*pinned library tables are for KiCad 10\.0"
    ):
        helper.initialize(tmp_path / "home")
    assert not (tmp_path / "home").exists()
    assert helper.main(["--config-home", str(tmp_path / "home")]) == 1
