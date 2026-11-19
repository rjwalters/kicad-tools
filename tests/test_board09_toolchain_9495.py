"""Board 09's producers run on the pinned KiCad toolchain (Issue #9495).

``check_design.py`` calls a bare ``kicad-cli`` and ``generate_design.py``
resolves footprints from the installed KiCad library, so both must refuse a
host whose KiCad is not ``PINNED_KICAD_VERSION`` (exit 2, nothing written),
exactly like the ``scripts/boards/*.py`` producers (Issue #6962).  No real
KiCad is needed: the version probe is mocked.
"""

from __future__ import annotations

import importlib.util
import runpy
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path

import pytest

from kicad_tools.cli import kicad_toolchain

BOARD = Path(__file__).resolve().parents[1] / "boards" / "09-usbc-pd-power"
SCRIPTS = ["check_design.py", "generate_design.py"]
OFF_PIN = "10.0.7"
assert OFF_PIN != kicad_toolchain.PINNED_KICAD_VERSION


@pytest.fixture
def host_with_other_kicad(monkeypatch):
    """A host whose ``kicad-cli`` reports a release other than the pin."""
    monkeypatch.delenv(kicad_toolchain.KICAD_DOCKER_ENV, raising=False)
    monkeypatch.setattr(kicad_toolchain, "_locate_kicad_cli", lambda: "/fake/kicad-cli")
    monkeypatch.setattr(
        kicad_toolchain.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=f"{OFF_PIN}\n", stderr=""),
    )
    monkeypatch.syspath_prepend(str(BOARD))  # check_design imports engineering.calculate


def _load(script, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        f"board09_{Path(script).stem}_9495", BOARD / script
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("script", SCRIPTS)
def test_entry_point_refuses_unpinned_kicad_and_writes_nothing(
    script, host_with_other_kicad, monkeypatch, tmp_path, capsys
):
    out = tmp_path / "out"
    monkeypatch.setattr(sys, "argv", [script, str(out)])
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(BOARD / script), run_name="__main__")
    assert exit_info.value.code == 2
    err = capsys.readouterr().err
    assert "REFUSED (KiCad environment, Issue #6962)" in err
    assert OFF_PIN in err
    assert not out.exists()


@pytest.mark.parametrize("script", SCRIPTS)
def test_entry_point_runs_main_inside_the_toolchain(script, monkeypatch, tmp_path):
    """On a matching toolchain ``__main__`` hands ``main`` to the wrapper, with
    the repo root mounted so the Docker shim can see the board files."""
    seen = {}

    def fake_wrapper(main, mounts=()):
        seen["main"], seen["mounts"] = main, list(mounts)
        return 0

    monkeypatch.setattr(kicad_toolchain, "run_on_evidence_toolchain", fake_wrapper)
    monkeypatch.syspath_prepend(str(BOARD))
    monkeypatch.setattr(sys, "argv", [script, str(tmp_path / "out")])
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(BOARD / script), run_name="__main__")
    assert exit_info.value.code == 0
    assert callable(seen["main"])
    assert seen["mounts"] == [BOARD.parents[1]]


def test_check_design_main_passes_through_its_own_exit_status(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(BOARD))
    module = _load("check_design.py", monkeypatch)
    monkeypatch.setattr(kicad_toolchain, "evidence_toolchain", lambda mounts=(): nullcontext())

    def run():
        return kicad_toolchain.run_on_evidence_toolchain(lambda: module.main([str(tmp_path)]))

    # A failing check is exit 1 (not the wrapper's refusal code 2), a pass is 0.
    monkeypatch.setattr(module, "check", lambda output: False)
    assert run() == 1
    monkeypatch.setattr(module, "check", lambda output: True)
    assert run() == 0


def test_generate_design_main_returns_zero(monkeypatch, tmp_path):
    module = _load("generate_design.py", monkeypatch)
    seen = []
    monkeypatch.setattr(module, "generate", lambda output: seen.append(output))
    assert module.main([str(tmp_path / "o")]) == 0
    assert seen == [tmp_path / "o"]
