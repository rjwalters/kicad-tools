"""Regression tests for ``boards/02-charlieplex-led/generate_pcb.py`` output-path handling.

Issue #3981: ``kct build boards/02-charlieplex-led -o <dir>`` passes the
**absolute output directory** as ``sys.argv[1]`` to the board's generator
scripts (``build_cmd._run_step_pcb`` / ``_run_step_route`` both call
``_run_python_script(..., script_args=[str(ctx.output_dir)])``). The old
``generate_pcb.main()`` treated ``sys.argv[1]`` as a *file* path and did
``Path(__file__).parent / sys.argv[1]``; for an absolute directory this
resolves to the directory itself, so ``write_text()`` raised
``IsADirectoryError``.

These tests pin the directory-aware contract (mirroring
``generate_schematic.py`` and ``generate_design.py`` in the same board
directory): a directory argument yields ``<dir>/charlieplex_3x3.kicad_pcb``,
while a plain file path is honored as-is.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "02-charlieplex-led"
GENERATE_PCB_SCRIPT = BOARD_DIR / "generate_pcb.py"
ROUTE_DEMO_SCRIPT = BOARD_DIR / "route_demo.py"

PCB_FILENAME = "charlieplex_3x3.kicad_pcb"


def _load_generate_pcb() -> ModuleType:
    """Import ``generate_pcb.py`` as a standalone module.

    The board directory is added to ``sys.path`` because the script imports
    sibling modules (its shared design spec) by bare name.
    """
    if str(BOARD_DIR) not in sys.path:
        sys.path.insert(0, str(BOARD_DIR))
    spec = importlib.util.spec_from_file_location("board02_generate_pcb", GENERATE_PCB_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_directory_arg_writes_pcb_inside_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory argument (the ``kct build -o`` contract) must not crash.

    Regression for #3981: previously raised ``IsADirectoryError``.
    """
    module = _load_generate_pcb()
    monkeypatch.setattr(sys, "argv", ["generate_pcb.py", str(tmp_path)])

    module.main()  # Must not raise IsADirectoryError.

    expected = tmp_path / PCB_FILENAME
    assert expected.is_file(), f"expected {expected} to be written"
    assert expected.read_text().startswith("(kicad_pcb")


def test_directory_arg_created_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing but empty output directory works; the file lands inside it."""
    out_dir = tmp_path / "nested" / "out"
    out_dir.mkdir(parents=True)
    module = _load_generate_pcb()
    monkeypatch.setattr(sys, "argv", ["generate_pcb.py", str(out_dir)])

    module.main()

    assert (out_dir / PCB_FILENAME).is_file()


def test_explicit_file_path_is_honored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-directory path is treated as the target file, parents created."""
    target = tmp_path / "sub" / "custom.kicad_pcb"
    module = _load_generate_pcb()
    monkeypatch.setattr(sys, "argv", ["generate_pcb.py", str(target)])

    module.main()

    assert target.is_file()
    assert target.read_text().startswith("(kicad_pcb")


def test_route_demo_handles_directory_arg() -> None:
    """``route_demo.py`` main() must interpret a directory arg as the output dir.

    Fast source-level guard (the end-to-end route is covered by
    ``test_board02_route_demo_recipe.py``). Fixing ``generate_pcb.py`` alone
    unmasked the identical ``IsADirectoryError`` in ``route_demo.py`` when
    ``kct build -o <dir>`` reaches the ROUTE step, so pin the directory
    branch here.
    """
    src = ROUTE_DEMO_SCRIPT.read_text(encoding="utf-8")
    assert "Path(sys.argv[1]).is_dir()" in src, (
        "route_demo.py must special-case a directory argument so "
        "`kct build -o <dir>` does not raise IsADirectoryError"
    )
    assert "output_path.parent.mkdir(parents=True, exist_ok=True)" in src
