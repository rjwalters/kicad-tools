"""Board 09's producers run on the pinned KiCad toolchain (Issue #9495).

``check_design.py`` calls a ``kicad-cli`` and ``generate_design.py`` resolves
footprints from the installed KiCad library, so both must refuse a host whose
KiCad is not ``PINNED_KICAD_VERSION`` (exit 2, nothing written), exactly like
the ``scripts/boards/*.py`` producers (Issue #6962).  No real KiCad or Docker
is needed: the version probe and the ``docker`` binary are faked.  The review
round on PR #9502 added two more requirements covered here:

* ERC/DRC must invoke the *qualified* executable the wrapper validated -- it
  can live outside ``PATH`` (e.g. a macOS app bundle), so re-resolving the
  bare name loses it.
* Under ``KCT_KICAD_DOCKER=1`` the Python generation step still runs on the
  host, so its footprint reads must be pinned to the image's libraries --
  a host library must not leak its geometry into the committed board.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.cli import kicad_toolchain, runner
from kicad_tools.exceptions import FileNotFoundError as KicadFileNotFoundError
from kicad_tools.footprints.library_path import detect_kicad_library_path

BOARD = Path(__file__).resolve().parents[1] / "boards" / "09-usbc-pd-power"
SCRIPTS = ["check_design.py", "generate_design.py"]
OFF_PIN = "10.0.7"
assert OFF_PIN != kicad_toolchain.PINNED_KICAD_VERSION

PINNED_FP_TEXT = (
    '(footprint "C_0805_2012Metric" (version 20240108) (generator pcbnew) (layer "F.Cu")\n'
    "  (attr smd)\n"
    '  (desc "pinned-image geometry")\n'
    '  (pad "1" smd rect (at -0.9375 0) (size 1.05 1.35) (layers "F.Cu" "F.Mask"))\n'
    '  (pad "2" smd rect (at 0.9375 0) (size 1.05 1.35) (layers "F.Cu" "F.Mask"))\n'
    ")\n"
)
TAMPERED_FP_TEXT = PINNED_FP_TEXT.replace("-0.9375", "-1.23456").replace(
    "pinned-image geometry", "host-tampered geometry"
)


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


@pytest.fixture
def host_with_pinned_kicad(monkeypatch):
    """A host whose ``kicad-cli`` reports exactly the pinned release."""
    monkeypatch.delenv(kicad_toolchain.KICAD_DOCKER_ENV, raising=False)
    monkeypatch.setattr(kicad_toolchain, "_locate_kicad_cli", lambda: "/fake/kicad-cli")
    monkeypatch.setattr(
        kicad_toolchain.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 0, stdout=f"{kicad_toolchain.PINNED_KICAD_VERSION}\n", stderr=""
        ),
    )


def _load(script, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        f"board09_{Path(script).stem}_9495", BOARD / script
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def _install_fake_docker(monkeypatch, tmp_path, *, copy_exit=0, image_library=None):
    """A ``docker`` stand-in on PATH whose ``run`` probes the pinned version.

    ``create``/``rm`` succeed; ``cp`` exports *image_library* to the requested
    destination (or exits *copy_exit*).  kicad-cli discovery is pinned to the
    shim ``evidence_toolchain`` puts at the front of PATH.
    """
    bindir = tmp_path / "docker-bin"
    bindir.mkdir()
    calls = tmp_path / "docker-calls.log"
    lines = [
        "#!/bin/sh\n",
        f'printf "%s\\n" "$@" >> {calls}\n',
        'case "$1" in\n',
        f"  run) echo {kicad_toolchain.PINNED_KICAD_VERSION} ;;\n",
        "  create) echo stub-container ;;\n",
    ]
    if image_library is not None:
        lines.append(f'  cp) cp -r "{image_library}" "$3" ;;\n')
    else:
        lines.append(f"  cp) exit {copy_exit} ;;\n")
    lines += ["  rm) ;;\n", "  *) ;;\n", "esac\n", "exit 0\n"]
    docker = bindir / "docker"
    docker.write_text("".join(lines))
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join([str(bindir), "/usr/bin", "/bin"]))
    monkeypatch.setattr(runner, "_kicad_cli_candidates", lambda: [Path(shutil.which("kicad-cli"))])
    runner.find_kicad_cli.cache_clear()
    yield calls
    runner.find_kicad_cli.cache_clear()


@pytest.fixture
def extracting_docker(monkeypatch, tmp_path):
    """A docker stand-in whose ``cp`` exports a stub image footprint library."""
    image_library = tmp_path / "stub-image-footprints"
    (image_library / "Capacitor_SMD.pretty").mkdir(parents=True)
    (image_library / "Capacitor_SMD.pretty" / "C_0805_2012Metric.kicad_mod").write_text(
        PINNED_FP_TEXT
    )
    yield from _install_fake_docker(monkeypatch, tmp_path, image_library=image_library)


@pytest.fixture
def failing_docker(monkeypatch, tmp_path):
    """A docker stand-in whose ``cp`` always fails."""
    yield from _install_fake_docker(monkeypatch, tmp_path, copy_exit=1)


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


def test_generate_entry_wraps_main_with_pinned_image_libraries(monkeypatch, tmp_path):
    """``generate_design.__main__`` hands ``main`` to the wrapper with the repo
    root mounted and the image's footprint libraries pinned for host reads."""
    seen = {}

    def fake_wrapper(main, mounts=(), pin_host_footprints=False):
        seen["main"], seen["mounts"], seen["pin"] = main, list(mounts), pin_host_footprints
        return 0

    monkeypatch.setattr(kicad_toolchain, "run_on_evidence_toolchain", fake_wrapper)
    monkeypatch.syspath_prepend(str(BOARD))
    monkeypatch.setattr(sys, "argv", ["generate_design.py", str(tmp_path / "out")])
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(BOARD / "generate_design.py"), run_name="__main__")
    assert exit_info.value.code == 0
    assert callable(seen["main"])
    assert seen["mounts"] == [BOARD.parents[1]]
    assert seen["pin"] is True


def test_check_entry_threads_the_qualified_cli_into_main(monkeypatch, tmp_path):
    """``check_design.__main__`` threads the toolchain's qualified ``kicad-cli``
    through ``main()`` into the native subprocess calls."""

    @contextlib.contextmanager
    def fake_chain(mounts=(), **kwargs):
        yield SimpleNamespace(kicad_cli="/qualified/kicad-cli")

    seen = []

    def fake_run(cmd, *args, **kwargs):
        seen.append(list(cmd))
        raise FileNotFoundError("stop check() once the ERC call is recorded")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(kicad_toolchain, "evidence_toolchain", fake_chain)
    monkeypatch.syspath_prepend(str(BOARD))
    monkeypatch.setattr(sys, "argv", ["check_design.py", str(tmp_path / "out")])
    with pytest.raises(FileNotFoundError):
        runpy.run_path(str(BOARD / "check_design.py"), run_name="__main__")
    assert seen, "ERC never ran"
    assert seen[0][:3] == ["/qualified/kicad-cli", "sch", "erc"]


def test_check_design_main_passes_through_its_own_exit_status(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(BOARD))
    module = _load("check_design.py", monkeypatch)
    monkeypatch.setattr(
        kicad_toolchain, "evidence_toolchain", lambda mounts=(), **kwargs: contextlib.nullcontext()
    )

    def run():
        return kicad_toolchain.run_on_evidence_toolchain(lambda: module.main([str(tmp_path)]))

    # A failing check is exit 1 (not the wrapper's refusal code 2), a pass is 0.
    monkeypatch.setattr(module, "check", lambda output, kicad_cli="kicad-cli": False)
    assert run() == 1
    monkeypatch.setattr(module, "check", lambda output, kicad_cli="kicad-cli": True)
    assert run() == 0


def test_generate_design_main_returns_zero(monkeypatch, tmp_path):
    module = _load("generate_design.py", monkeypatch)
    seen = []
    monkeypatch.setattr(module, "generate", lambda output: seen.append(output))
    assert module.main([str(tmp_path / "o")]) == 0
    assert seen == [tmp_path / "o"]


def test_erc_and_drc_invoke_the_qualified_executable(monkeypatch, tmp_path):
    """``check()`` must invoke the qualified binary it is given: the wrapper
    can validate one outside ``PATH`` (macOS app bundle), and re-resolving the
    bare name there loses it (Issue #9502 review)."""
    monkeypatch.syspath_prepend(str(BOARD))
    module = _load("check_design.py", monkeypatch)
    bundle = tmp_path / "KiCad.app" / "Contents" / "MacOS" / "kicad-cli"
    bundle.parent.mkdir(parents=True)
    bundle.write_text("#!/bin/sh\nexit 0\n")
    bundle.chmod(0o755)
    calls = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        report = cmd[cmd.index("--output") + 1]
        if cmd[1] == "sch":
            Path(report).write_text('{"violations": []}')
        else:
            Path(report).write_text('{"violations": [], "unconnected_items": []}')
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    with pytest.raises(KicadFileNotFoundError):
        # No board inputs exist under tmp; check() gets past both native calls
        # and fails on the missing PCB file (kicad_tools' FileNotFoundError is
        # not an OSError) -- both calls are recorded by then.
        module.check(out_dir, kicad_cli=str(bundle))
    assert calls[0][:3] == [str(bundle), "sch", "erc"]
    assert calls[1][:3] == [str(bundle), "pcb", "drc"]
    assert all(call[0] != "kicad-cli" for call in calls)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell fake")
def test_docker_mode_pins_host_side_footprint_reads(monkeypatch, tmp_path, extracting_docker):
    """Under ``KCT_KICAD_DOCKER=1``, ``pin_host_footprints`` points host-side
    footprint reads (``PCB.add_footprint``) at the image's libraries -- even
    when the host library carries different geometry (Issue #9502 review)."""
    host_lib = tmp_path / "host-footprints"
    (host_lib / "Capacitor_SMD.pretty").mkdir(parents=True)
    (host_lib / "Capacitor_SMD.pretty" / "C_0805_2012Metric.kicad_mod").write_text(TAMPERED_FP_TEXT)
    monkeypatch.setenv(kicad_toolchain.KICAD_FOOTPRINT_DIR_ENV, str(host_lib))
    monkeypatch.setenv(kicad_toolchain.KICAD_DOCKER_ENV, "1")
    with kicad_toolchain.evidence_toolchain(pin_host_footprints=True) as chain:
        assert chain.docker_image == kicad_toolchain.pinned_kicad_image()
        resolved = detect_kicad_library_path()
        assert resolved.source == "env"
        exported = Path(os.environ[kicad_toolchain.KICAD_FOOTPRINT_DIR_ENV])
        assert exported == resolved.footprints_path
        assert exported != host_lib
        pinned_file = resolved.get_footprint_file("Capacitor_SMD", "C_0805_2012Metric")
        assert pinned_file is not None
        assert "pinned-image geometry" in Path(pinned_file).read_text()
        assert "host-tampered geometry" not in Path(pinned_file).read_text()
    # The host environment is restored and the extraction is gone with the
    # toolchain's scratch directory.
    assert os.environ[kicad_toolchain.KICAD_FOOTPRINT_DIR_ENV] == str(host_lib)
    assert not exported.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell fake")
def test_docker_extraction_failure_refuses(monkeypatch, tmp_path, failing_docker, capsys):
    """A failed library extraction is an environment refusal: fail closed with
    exit 2 before ``main`` runs, never fall back to the host library."""
    monkeypatch.setenv(kicad_toolchain.KICAD_DOCKER_ENV, "1")
    ran = []
    rc = kicad_toolchain.run_on_evidence_toolchain(
        lambda: ran.append("main") or 0, pin_host_footprints=True
    )
    assert rc == 2
    assert not ran
    err = capsys.readouterr().err
    assert "REFUSED (KiCad environment, Issue #6962)" in err
    assert "docker cp" in err


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell fake")
def test_native_mode_ignores_pin_host_footprints(monkeypatch, tmp_path, host_with_pinned_kicad):
    """Without Docker mode the flag is a no-op: the version gate already
    required the pinned native install, and ``KICAD_FOOTPRINT_DIR`` is left
    untouched."""
    monkeypatch.delenv(kicad_toolchain.KICAD_FOOTPRINT_DIR_ENV, raising=False)
    with kicad_toolchain.evidence_toolchain(pin_host_footprints=True) as chain:
        assert chain.docker_image is None
        assert kicad_toolchain.KICAD_FOOTPRINT_DIR_ENV not in os.environ
    assert kicad_toolchain.KICAD_FOOTPRINT_DIR_ENV not in os.environ
