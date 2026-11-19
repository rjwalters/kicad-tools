"""Footprint-library identity recorded in evidence provenance (Issue #9526).

Native evidence gates only the ``kicad-cli`` *version*; the footprint library
``PCB.add_footprint`` reads has no version file and can move independently of
the CLI (split ``kicad``/``kicad-footprints`` packages, or ``KICAD_FOOTPRINT_DIR``
pointing anywhere).  So the evidence toolchain records where host-side reads
resolve (:attr:`EvidenceToolchain.footprint_library`) and
:func:`kicad_tools.cli.kicad_toolchain.evidence_provenance` turns the consumed
``.kicad_mod`` files into a ``kicad_version`` + content-hash record -- recorded,
not gated.  No real KiCad or Docker is needed: the version probe and the
``docker`` binary are faked and the libraries are tmp directories.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from kicad_tools.cli import kicad_toolchain, runner
from kicad_tools.footprints.library_path import (
    detect_kicad_library_path,
    reset_library_reads,
)

BOARD = Path(__file__).resolve().parents[1] / "boards" / "09-usbc-pd-power"

FP_TEXT = (
    '(footprint "C_0805_2012Metric" (version 20240108) (generator pcbnew) (layer "F.Cu")\n'
    "  (attr smd)\n"
    '  (pad "1" smd rect (at -0.9375 0) (size 1.05 1.35) (layers "F.Cu" "F.Mask"))\n'
    '  (pad "2" smd rect (at 0.9375 0) (size 1.05 1.35) (layers "F.Cu" "F.Mask"))\n'
    ")\n"
)
FP_NAME = "Capacitor_SMD.pretty/C_0805_2012Metric.kicad_mod"


@pytest.fixture(autouse=True)
def clean_reads():
    """Read recording is process state; every test starts and ends clean."""
    reset_library_reads()
    yield
    reset_library_reads()


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


def make_library(root: Path, *, pad1_x: str = "-0.9375") -> Path:
    """A one-footprint tmp library; *pad1_x* moves pad 1 (a geometry change)."""
    lib = root / "Capacitor_SMD.pretty"
    lib.mkdir(parents=True)
    (lib / "C_0805_2012Metric.kicad_mod").write_text(FP_TEXT.replace("-0.9375", pad1_x))
    return root


def read_one() -> Path | None:
    """The producer's read, through the same funnel ``add_footprint`` uses."""
    return detect_kicad_library_path().get_footprint_file("Capacitor_SMD", "C_0805_2012Metric")


def identity_of(monkeypatch, root: Path) -> dict:
    """Identity recorded for one ``read_one()`` against *root*."""
    reset_library_reads()
    monkeypatch.setenv(kicad_toolchain.KICAD_FOOTPRINT_DIR_ENV, str(root))
    with kicad_toolchain.evidence_toolchain():
        assert read_one() is not None
        return kicad_toolchain.evidence_provenance()["footprint_library"]


def test_provenance_records_version_and_identity(host_with_pinned_kicad, monkeypatch, tmp_path):
    root = make_library(tmp_path / "host-footprints")
    monkeypatch.setenv(kicad_toolchain.KICAD_FOOTPRINT_DIR_ENV, str(root))
    with kicad_toolchain.evidence_toolchain() as chain:
        resolved = read_one()
        assert resolved is not None
        prov = kicad_toolchain.evidence_provenance()
        resolution = chain.footprint_library
    assert prov["kicad_version"] == kicad_toolchain.PINNED_KICAD_VERSION
    identity = prov["footprint_library"]
    assert identity["source"] == "env"
    assert identity["files"] == [FP_NAME]
    per_file = hashlib.sha256(resolved.read_bytes()).hexdigest()
    expected = hashlib.sha256(f"{FP_NAME}\0{per_file}\n".encode()).hexdigest()
    assert identity["sha256"] == expected
    assert resolution is not None
    assert resolution.source == "env"
    assert resolution.root == root


def test_one_changed_footprint_changes_identity(host_with_pinned_kicad, monkeypatch, tmp_path):
    plain = identity_of(monkeypatch, make_library(tmp_path / "lib-a"))
    tampered = identity_of(monkeypatch, make_library(tmp_path / "lib-b", pad1_x="-1.23456"))
    assert plain["files"] == tampered["files"] == [FP_NAME]
    assert plain["sha256"] != tampered["sha256"]


def test_identical_libraries_yield_identical_identity(
    host_with_pinned_kicad, monkeypatch, tmp_path
):
    """The identity hashes content and library-relative names -- not host paths."""
    first = identity_of(monkeypatch, make_library(tmp_path / "lib-a"))
    second = identity_of(monkeypatch, make_library(tmp_path / "somewhere-else"))
    assert first["sha256"] == second["sha256"]
    assert first["files"] == second["files"]


def test_no_reads_record_version_only(host_with_pinned_kicad):
    with kicad_toolchain.evidence_toolchain():
        prov = kicad_toolchain.evidence_provenance()
    assert prov == {
        "kicad_version": kicad_toolchain.PINNED_KICAD_VERSION,
        "footprint_library": None,
    }


def test_provenance_is_none_outside_the_toolchain(host_with_pinned_kicad):
    assert kicad_toolchain.evidence_provenance() is None
    with kicad_toolchain.evidence_toolchain():
        pass
    assert kicad_toolchain.evidence_provenance() is None


@pytest.fixture
def extracting_docker(monkeypatch, tmp_path):
    """A ``docker`` stand-in whose ``cp`` exports a stub image footprint library.

    Real subprocess execution (unlike ``host_with_pinned_kicad``'s faked
    ``subprocess.run``): the version probe answers through the stub's ``run``,
    so ``docker cp`` actually copies *image_library* out."""
    bindir = tmp_path / "docker-bin"
    bindir.mkdir()
    image_library = tmp_path / "stub-image-footprints"
    (image_library / "Capacitor_SMD.pretty").mkdir(parents=True)
    (image_library / "Capacitor_SMD.pretty" / "C_0805_2012Metric.kicad_mod").write_text(
        FP_TEXT.replace("-0.9375", "-9.8765")
    )
    docker = bindir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        f"  run) echo {kicad_toolchain.PINNED_KICAD_VERSION} ;;\n"
        "  create) echo stub-container ;;\n"
        f'  cp) cp -r "{image_library}" "$3" ;;\n'
        "  rm) ;;\n"
        "  *) ;;\n"
        "esac\n"
        "exit 0\n"
    )
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join([str(bindir), "/usr/bin", "/bin"]))
    monkeypatch.setattr(runner, "_kicad_cli_candidates", lambda: [Path(shutil.which("kicad-cli"))])
    runner.find_kicad_cli.cache_clear()
    yield
    runner.find_kicad_cli.cache_clear()


def test_docker_mode_records_the_image_library_identity(monkeypatch, tmp_path, extracting_docker):
    """Under ``KCT_KICAD_DOCKER=1`` the recorded identity is the *image's*
    library (source ``env``, extracted root), not the host's."""
    monkeypatch.setenv(kicad_toolchain.KICAD_DOCKER_ENV, "1")
    with kicad_toolchain.evidence_toolchain(pin_host_footprints=True) as chain:
        assert read_one() is not None
        prov = kicad_toolchain.evidence_provenance()
        resolution = chain.footprint_library
    identity = prov["footprint_library"]
    assert identity["source"] == "env"
    assert identity["files"] == [FP_NAME]
    assert prov["kicad_version"] == kicad_toolchain.PINNED_KICAD_VERSION
    assert resolution is not None
    assert resolution.source == "env"
    # The extracted root is a per-run scratch directory; it is gone with the
    # context, and only the source + digest (stable) go into evidence.
    assert not resolution.root.exists()


def _load_generator(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "board09_generate_9526", BOARD / "generate_design.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def _write_tmp_standard_library(root: Path, module, tmp_path: Path) -> Path:
    """Every ``Lib:Name`` footprint the board's parts list, with the pads the
    matching symbol defs use (``generate()`` asserts land patterns match pins).

    Pin numbers are read off a throwaway ``Schematic`` built the same way
    ``generate()`` builds its symbol side.
    """
    lib_input = tmp_path / "lib-input"
    lib_input.mkdir(parents=True)
    sch = module.Schematic(
        title="pin lookup",
        paper="A1",
        local_symbol_libs=[module.write_library(lib_input)],
    )
    needed: dict[tuple[str, str], set[str]] = {}
    for part in module.parts():
        if ":" not in part.footprint:
            continue
        symbol = sch.add_symbol(
            part.symbol,
            x=10.0,
            y=10.0,
            ref=part.ref,
            value=part.value,
            footprint=part.footprint,
        )
        lib, name = part.footprint.split(":", 1)
        needed.setdefault((lib, name), set()).update(p.number for p in symbol.symbol_def.pins)
    for (lib, name), pins in needed.items():
        lib_dir = root / f"{lib}.pretty"
        lib_dir.mkdir(parents=True, exist_ok=True)
        pads = []
        for index, pin in enumerate(sorted(pins)):
            pads.append(
                f'  (pad "{pin}" smd rect (at {index % 5} {index // 5}) '
                '(size 1.0 1.0) (layers "F.Cu" "F.Mask"))\n'
            )
        (lib_dir / f"{name}.kicad_mod").write_text(
            f'(footprint "{name}" (version 20240108) (generator pcbnew) (layer "F.Cu")\n'
            "  (attr smd)\n" + "".join(pads) + ")\n"
        )
    return root


def test_generate_emits_toolchain_record(host_with_pinned_kicad, monkeypatch, tmp_path):
    """Board 09's emitted ``circuit.json`` carries ``kicad_version`` alongside
    the footprint-library identity of what the run actually read -- and the
    identity is stable across two runs."""
    module = _load_generator(monkeypatch)
    lib_root = _write_tmp_standard_library(
        tmp_path / "standard-footprints", module, tmp_path / "pin-lookup"
    )
    monkeypatch.setenv(kicad_toolchain.KICAD_FOOTPRINT_DIR_ENV, str(lib_root))
    with kicad_toolchain.evidence_toolchain():
        result = module.generate(tmp_path / "out-a")
    record = result["toolchain"]
    assert record["kicad_version"] == kicad_toolchain.PINNED_KICAD_VERSION
    emitted = record["footprint_library"]
    assert emitted["source"] == "env"
    files = emitted["files"]
    assert "Capacitor_SMD.pretty/C_0603_1608Metric.kicad_mod" in files
    assert "Package_DFN_QFN.pretty/QFN-24-1EP_4x4mm_P0.5mm_EP2.6x2.6mm.kicad_mod" in files
    assert (tmp_path / "out-a" / "circuit.json").read_text().count('"toolchain"') == 1
    # Second run, same library: identical identity (content hash, not paths).
    reset_library_reads()
    with kicad_toolchain.evidence_toolchain():
        result2 = module.generate(tmp_path / "out-b")
    assert result2["toolchain"] == record
