"""Regression tests for Issue #9351: recipe loader sibling shadowing.

``load_board_recipe_module`` registers the *top* recipe module under a
per-gate, per-board unique name, but the recipe's bare sibling imports
(``import generate_pcb`` / ``import generate_schematic``) used to cache
in ``sys.modules`` under those plain names and stay there.  The first
board loaded in a process owned the keys; every later board silently
received the wrong sibling modules (board 07 audited against board 06's
declarations, reporting success instead of an error).

The loaders now evict stale sibling entries before the import and
restore ``sys.modules`` afterwards (symmetric with their existing
``sys.path`` restore).  These tests load two boards in one process
through each loader copy and assert each recipe bound its *own*
siblings, plus the ``sys.modules`` symmetry guarantees.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_DIR = REPO_ROOT / "scripts" / "ci"
BOARD_06 = REPO_ROOT / "boards" / "06-diffpair-test"
BOARD_07 = REPO_ROOT / "boards" / "07-matchgroup-test"

# Discriminating declarations: board 06 has USB2_D+ but not TMDS_D2_P,
# board 07 the reverse, so a shadowed sibling flips these assertions.
BOARD_06_ONLY_PAIR = "USB2_D+"
BOARD_07_ONLY_PAIR = "TMDS_D2_P"

_LOADER_MODULE_NAMES = (
    "net_class_map_resolver",
    "check_matchgroup_coverage",
)


def _load_ci_module(name: str):
    """Import a ``scripts/ci`` module the way the existing gate tests do."""
    spec = importlib.util.spec_from_file_location(f"_test_issue_9351_{name}", CI_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(params=_LOADER_MODULE_NAMES, ids=lambda name: name)
def loader(request):
    """Each loader copy, parametrized so both stay sibling-safe."""
    return _load_ci_module(request.param).load_board_recipe_module


@pytest.fixture
def clean_sibling_keys():
    """Hide bare sibling keys; restore any pre-existing ones afterwards."""
    keys = ("generate_pcb", "generate_schematic")
    saved = {key: sys.modules.pop(key) for key in keys if key in sys.modules}
    yield
    for key in keys:
        sys.modules.pop(key, None)
    sys.modules.update(saved)


def _assert_board_07_has_own_siblings(recipe):
    assert recipe.generate_pcb.__file__ == str(BOARD_07 / "generate_pcb.py")
    assert BOARD_07_ONLY_PAIR in recipe.generate_pcb.DIFFPAIRS
    assert BOARD_06_ONLY_PAIR not in recipe.generate_pcb.DIFFPAIRS
    assert recipe.generate_schematic.__file__ == str(BOARD_07 / "generate_schematic.py")


def _assert_board_06_has_own_siblings(recipe):
    assert recipe.generate_pcb.__file__ == str(BOARD_06 / "generate_pcb.py")
    assert BOARD_06_ONLY_PAIR in recipe.generate_pcb.DIFFPAIRS
    assert BOARD_07_ONLY_PAIR not in recipe.generate_pcb.DIFFPAIRS
    assert recipe.generate_schematic.__file__ == str(BOARD_06 / "generate_schematic.py")


class TestTwoBoardLoadOrder:
    """The issue's core scenario: two boards' recipes in one process."""

    def test_board06_then_board07(self, loader, clean_sibling_keys):
        recipe_06 = loader(BOARD_06)
        recipe_07 = loader(BOARD_07)
        _assert_board_06_has_own_siblings(recipe_06)
        _assert_board_07_has_own_siblings(recipe_07)

    def test_board07_then_board06(self, loader, clean_sibling_keys):
        recipe_07 = loader(BOARD_07)
        recipe_06 = loader(BOARD_06)
        _assert_board_07_has_own_siblings(recipe_07)
        _assert_board_06_has_own_siblings(recipe_06)

    def test_same_board_loaded_twice(self, loader, clean_sibling_keys):
        first = loader(BOARD_07)
        second = loader(BOARD_07)
        _assert_board_07_has_own_siblings(first)
        _assert_board_07_has_own_siblings(second)


class TestSysModulesSymmetry:
    """The loader must leave ``sys.modules`` as it found it."""

    def test_no_bare_sibling_keys_left_behind(self, loader, clean_sibling_keys):
        loader(BOARD_06)
        loader(BOARD_07)
        assert "generate_pcb" not in sys.modules
        assert "generate_schematic" not in sys.modules

    def test_preexisting_sibling_modules_restored(self, loader, clean_sibling_keys):
        sentinel_pcb = types.ModuleType("generate_pcb")
        sentinel_schematic = types.ModuleType("generate_schematic")
        sys.modules["generate_pcb"] = sentinel_pcb
        sys.modules["generate_schematic"] = sentinel_schematic

        loader(BOARD_06)
        loader(BOARD_07)

        assert sys.modules["generate_pcb"] is sentinel_pcb
        assert sys.modules["generate_schematic"] is sentinel_schematic

    def test_failed_recipe_import_still_restores_sys_modules(
        self, loader, clean_sibling_keys, tmp_path
    ):
        board = tmp_path / "broken-board"
        board.mkdir()
        (board / "generate_design.py").write_text(
            "import generate_pcb\nraise RuntimeError('boom during recipe import')\n",
            encoding="utf-8",
        )
        (board / "generate_pcb.py").write_text("DIFFPAIRS = {}\n", encoding="utf-8")

        with pytest.raises(RuntimeError, match="boom"):
            loader(board)

        assert "generate_pcb" not in sys.modules
        assert "generate_schematic" not in sys.modules


class TestNoSiblingBoards:
    """A board dir with no sibling modules must still load cleanly."""

    def test_board_dir_without_sibling_modules(self, loader, clean_sibling_keys, tmp_path):
        board = tmp_path / "solo-board"
        board.mkdir()
        (board / "generate_design.py").write_text("CONTRACT = 'solo'\n", encoding="utf-8")

        mod = loader(board)

        assert mod is not None
        assert mod.CONTRACT == "solo"
        assert "generate_design" not in sys.modules
