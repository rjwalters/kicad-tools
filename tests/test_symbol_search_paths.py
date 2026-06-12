"""Regression tests for KICAD_SYMBOL_DIR symbol library discovery.

Issue #3606: `kicad-tools lib list --symbols` ignored KICAD_SYMBOL_DIR
because the CLI consumed a hardcoded module constant instead of reading
the environment variable at call time (the footprint side already read
KICAD_FOOTPRINT_DIR at call time via detect_kicad_library_path()).

These tests pin the unified resolver, get_symbol_search_paths(), and
its consumers (lib list, symbol-info library lookup, list_libraries),
including the reporter's flat-directory-of-.kicad_sym-files layout
(the standard KiCad/NixOS layout).
"""

import json

from kicad_tools.schematic.grid import get_symbol_search_paths
from kicad_tools.schematic.library import list_libraries

MINIMAL_LIB = '(kicad_symbol_lib\n\t(symbol "R0"\n\t\t(property "Reference" "R")\n\t)\n)\n'


def _make_symbol_dir(tmp_path, names=("Device", "Audio")):
    """Create a flat directory of .kicad_sym files (NixOS/KiCad layout)."""
    sym_dir = tmp_path / "symbols"
    sym_dir.mkdir()
    for name in names:
        (sym_dir / f"{name}.kicad_sym").write_text(MINIMAL_LIB)
    return sym_dir


class TestGetSymbolSearchPaths:
    """Tests for the unified call-time symbol path resolver."""

    def test_env_var_dir_is_first(self, tmp_path, monkeypatch):
        """KICAD_SYMBOL_DIR takes priority over platform defaults."""
        sym_dir = _make_symbol_dir(tmp_path)
        monkeypatch.setenv("KICAD_SYMBOL_DIR", str(sym_dir))

        paths = get_symbol_search_paths()
        assert paths, "env-var directory should be discovered"
        assert paths[0] == sym_dir

    def test_env_var_read_at_call_time(self, tmp_path, monkeypatch):
        """Setting the env var after import is honored (no import-time constant)."""
        monkeypatch.delenv("KICAD_SYMBOL_DIR", raising=False)
        before = get_symbol_search_paths()

        sym_dir = _make_symbol_dir(tmp_path)
        monkeypatch.setenv("KICAD_SYMBOL_DIR", str(sym_dir))
        after = get_symbol_search_paths()

        assert sym_dir not in before
        assert after[0] == sym_dir

    def test_unset_env_var_platform_defaults_only(self, monkeypatch):
        """Without the env var, only platform defaults are returned (unchanged)."""
        monkeypatch.delenv("KICAD_SYMBOL_DIR", raising=False)
        paths = get_symbol_search_paths()
        # All returned paths exist and none came from the (unset) env var
        for p in paths:
            assert p.exists()

    def test_nonexistent_env_dir_filtered(self, tmp_path, monkeypatch):
        """A KICAD_SYMBOL_DIR pointing at a missing path falls through silently
        (matching KICAD_FOOTPRINT_DIR behavior)."""
        missing = tmp_path / "does-not-exist"
        monkeypatch.setenv("KICAD_SYMBOL_DIR", str(missing))
        assert missing not in get_symbol_search_paths()

    def test_registry_delegates_to_shared_resolver(self, tmp_path, monkeypatch):
        """registry._default_symbol_paths is a thin alias of the shared resolver."""
        from kicad_tools.schematic.registry import _default_symbol_paths

        sym_dir = _make_symbol_dir(tmp_path)
        monkeypatch.setenv("KICAD_SYMBOL_DIR", str(sym_dir))
        assert _default_symbol_paths() == get_symbol_search_paths()


class TestListLibrariesEnvVar:
    """list_libraries() honors KICAD_SYMBOL_DIR (flat .kicad_sym layout)."""

    def test_finds_env_var_libraries(self, tmp_path, monkeypatch):
        sym_dir = _make_symbol_dir(tmp_path, names=("Device", "Audio", "MCU_Module"))
        monkeypatch.setenv("KICAD_SYMBOL_DIR", str(sym_dir))

        libs = list_libraries()
        for name in ("Device", "Audio", "MCU_Module"):
            assert name in libs

    def test_unset_env_var_does_not_find_temp_libs(self, tmp_path, monkeypatch):
        sym_dir = _make_symbol_dir(tmp_path, names=("Issue3606Sentinel",))
        monkeypatch.delenv("KICAD_SYMBOL_DIR", raising=False)
        assert "Issue3606Sentinel" not in list_libraries()
        # Sanity: setting it makes the sentinel appear
        monkeypatch.setenv("KICAD_SYMBOL_DIR", str(sym_dir))
        assert "Issue3606Sentinel" in list_libraries()


class TestCliLibListEnvVar:
    """The reporter's exact case: `kicad-tools lib list --symbols`."""

    def test_lib_list_symbols_honors_env_var(self, tmp_path, monkeypatch, capsys):
        from kicad_tools.cli import main

        sym_dir = _make_symbol_dir(tmp_path, names=("Issue3606Lib",))
        monkeypatch.setenv("KICAD_SYMBOL_DIR", str(sym_dir))

        result = main(["lib", "list", "--symbols"])
        assert result == 0

        captured = capsys.readouterr()
        assert "Issue3606Lib" in captured.out
        assert "No symbol libraries found" not in captured.out

    def test_lib_list_symbols_json_count(self, tmp_path, monkeypatch, capsys):
        from kicad_tools.cli import main

        sym_dir = _make_symbol_dir(tmp_path, names=("A3606", "B3606"))
        monkeypatch.setenv("KICAD_SYMBOL_DIR", str(sym_dir))

        result = main(["lib", "list", "--symbols", "--format", "json"])
        assert result == 0

        data = json.loads(capsys.readouterr().out)
        assert data["symbols"]["count"] >= 2
        assert "A3606" in data["symbols"]["libraries"]
        assert "B3606" in data["symbols"]["libraries"]


class TestFindSymbolLibraryEnvVar:
    """`lib symbol-info` library lookup honors KICAD_SYMBOL_DIR."""

    def test_find_symbol_library_in_env_dir(self, tmp_path, monkeypatch):
        from kicad_tools.cli.lib import _find_symbol_library

        sym_dir = _make_symbol_dir(tmp_path, names=("Issue3606Lib",))
        monkeypatch.setenv("KICAD_SYMBOL_DIR", str(sym_dir))

        found = _find_symbol_library("Issue3606Lib")
        assert found == sym_dir / "Issue3606Lib.kicad_sym"
