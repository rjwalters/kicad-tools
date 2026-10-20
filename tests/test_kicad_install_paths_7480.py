"""Windows KiCad 9/10 install detection (issue #7480)."""

from __future__ import annotations

import platform
import sys

import pytest

from kicad_tools.cli import runner
from kicad_tools.footprints.library_path import detect_kicad_library_path
from kicad_tools.kicad_install import windows_kicad_install_roots


@pytest.fixture
def win_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "x86"))
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.delenv("KICAD_FOOTPRINT_DIR", raising=False)
    monkeypatch.setattr(runner.shutil, "which", lambda _name: None)
    return tmp_path


def _install(root, version):
    (root / "KiCad" / version / "bin").mkdir(parents=True)
    exe = root / "KiCad" / version / "bin" / "kicad-cli.exe"
    exe.write_text("")
    (root / "KiCad" / version / "share" / "kicad" / "footprints").mkdir(parents=True)
    return exe


def test_kicad_10_cli_and_footprints_found(win_env):
    exe = _install(win_env, "10.0")
    assert exe in runner._kicad_cli_candidates()
    paths = detect_kicad_library_path()
    assert paths.source == "auto"
    assert paths.footprints_path == win_env / "KiCad" / "10.0" / "share" / "kicad" / "footprints"


def test_numeric_sort_newest_first(win_env):
    for v in ("8.0", "9.0", "10.0"):
        _install(win_env, v)
    names = [p.name for p in windows_kicad_install_roots()]
    assert names == ["10.0", "9.0", "8.0"]
    ours = [c for c in runner._kicad_cli_candidates() if str(win_env) in str(c)]
    assert [c.parent.parent.name for c in ours] == ["10.0", "9.0", "8.0"]


def test_non_version_dirs_ignored_and_unversioned_fallback(win_env):
    (win_env / "KiCad" / "docs").mkdir(parents=True)
    (win_env / "KiCad" / "share" / "kicad" / "footprints").mkdir(parents=True)
    assert windows_kicad_install_roots() == []
    paths = detect_kicad_library_path()
    assert paths.footprints_path is not None
    assert paths.footprints_path.parts[-4:] == ("KiCad", "share", "kicad", "footprints")


def test_env_override_beats_versioned(win_env, monkeypatch):
    _install(win_env, "10.0")
    custom = win_env / "custom"
    custom.mkdir()
    monkeypatch.setenv("KICAD_FOOTPRINT_DIR", str(custom))
    assert detect_kicad_library_path().source == "env"


def test_path_precedence_in_cli_candidates(win_env, monkeypatch):
    _install(win_env, "10.0")
    on_path = win_env / "onpath" / "kicad-cli.exe"
    on_path.parent.mkdir()
    on_path.write_text("")
    monkeypatch.setattr(runner.shutil, "which", lambda _name: str(on_path))
    assert runner._kicad_cli_candidates()[0] == on_path


def test_no_windows_probing_elsewhere(win_env, monkeypatch):
    _install(win_env, "10.0")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    assert not any("KiCad" in str(c) and "10.0" in str(c) for c in runner._kicad_cli_candidates())
