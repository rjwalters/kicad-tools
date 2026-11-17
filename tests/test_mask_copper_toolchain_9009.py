"""Issue #9009: ``mask_to_copper`` must be able to complete under KCT_KICAD_DOCKER=1.

Hermetic -- every native process is faked, so this runs on any host without
KiCad or docker. Three defects kept the check incomplete after #8946:

* the full-layer export used the host ``kicad-cli`` while the per-object
  worker used the pinned image, so the two passes ran different KiCad
  releases (``Native object/export versions differ``);
* the unqualified-version refusal named no way forward;
* KiCad returns zone fills fractured (holes bridged to the outline), which
  shapely calls invalid, so every board with a filled zone reported
  ``invalid native object polygon`` and a full-layer material mismatch.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from pathlib import Path

import pytest
from shapely.geometry import Polygon

from tests.test_mask_export_geometry import _board

_PAD_BODY = """(footprint "P1" (layer "F.Cu")
      (pad "1" thru_hole circle (at 0 0) (size 1.7 1.7) (drill 1.0)
        (layers "*.Cu" "*.Mask")))"""
#: ``_board`` numbers UUIDs in source order: footprint 1, pad 2.
_PAD_UUID = str(uuid.UUID(int=2))


def _docker_env(monkeypatch, tmp_path):
    """Pretend KCT_KICAD_DOCKER=1 with docker on PATH; return the config home."""
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setattr("kicad_tools.cli.kicad_toolchain.docker_fallback_enabled", lambda: True)
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/docker" if name == "docker" else None
    )
    monkeypatch.setattr("kicad_tools.cli.kicad_toolchain.process_config_home", lambda: cfg)
    monkeypatch.setattr("kicad_tools.cli.kicad_toolchain._synced_model_dir", lambda: None)
    monkeypatch.setattr(
        "kicad_tools.cli.kicad_toolchain.docker_image", lambda: "kicad/kicad:pinned"
    )
    return cfg


def _forbid_host_cli(monkeypatch):
    def fail():
        raise AssertionError("host kicad-cli discovery must not run in Docker mode")

    monkeypatch.setattr("kicad_tools.cli.runner.find_kicad_cli", fail)


def _image_and_entrypoint(argv):
    entry = argv.index("--entrypoint")
    return argv[entry + 1], argv[entry + 2]


# ---------------------------------------------------------------------------
# (A) one KiCad for both passes
# ---------------------------------------------------------------------------


def test_export_resolver_uses_pinned_image_kicad_cli(monkeypatch, tmp_path):
    from kicad_tools.validate.mask_export_geometry import _resolve_native_kicad_cli_command

    cfg = _docker_env(monkeypatch, tmp_path)
    command = _resolve_native_kicad_cli_command(tmp_path / "scratch")
    assert command[:2] == ["docker", "run"]
    assert _image_and_entrypoint(command) == ("kicad-cli", "kicad/kicad:pinned")
    scratch = tmp_path / "scratch"
    assert f"{scratch}:{scratch}" in command
    assert f"KICAD_CONFIG_HOME={cfg}" in command


def test_export_resolver_mounts_system_temp_without_scratch_dir(monkeypatch, tmp_path):
    import tempfile

    from kicad_tools.validate.mask_export_geometry import _resolve_native_kicad_cli_command

    _docker_env(monkeypatch, tmp_path)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systmp"))
    command = _resolve_native_kicad_cli_command(None)
    assert f"{tmp_path / 'systmp'}:{tmp_path / 'systmp'}" in command


def test_export_resolver_is_inert_outside_docker_mode(monkeypatch, tmp_path):
    from kicad_tools.validate.mask_export_geometry import _resolve_native_kicad_cli_command

    monkeypatch.setattr("kicad_tools.cli.kicad_toolchain.docker_fallback_enabled", lambda: False)
    assert _resolve_native_kicad_cli_command(tmp_path) is None
    # Opted in but no docker binary: fall back to host discovery, as the
    # per-object resolver does.
    monkeypatch.setattr("kicad_tools.cli.kicad_toolchain.docker_fallback_enabled", lambda: True)
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert _resolve_native_kicad_cli_command(tmp_path) is None


def _fake_native(monkeypatch, *, version="10.0.6", objects=None, worker_version=None):
    """Fake every native subprocess both passes start; record each argv."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        cmd = list(cmd)
        calls.append(cmd)
        if cmd[-1] == "--version":
            return subprocess.CompletedProcess(cmd, 0, version + "\n", "")
        if "export" in cmd and "gerbers" in cmd:
            # No layers written: the export side reports missing layers, which
            # is irrelevant to which command it ran.
            return subprocess.CompletedProcess(cmd, 0, "", "")
        board, output = Path(cmd[-3]), Path(cmd[-1])
        output.write_text(
            json.dumps(
                {
                    "schema": "kct.native-mask-objects.v1",
                    "source_sha256": hashlib.sha256(board.read_bytes()).hexdigest(),
                    "native_version": worker_version or version,
                    "errors": [],
                    "objects": objects or {},
                    "max_error_mm": 0.0,
                }
            )
        )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_both_native_passes_run_the_same_pinned_image(monkeypatch, tmp_path):
    """Under KCT_KICAD_DOCKER=1 export and worker resolve one image and config home."""
    from kicad_tools.validate.mask_copper_geometry import inspect_attributed_mask_geometry

    cfg = _docker_env(monkeypatch, tmp_path)
    _forbid_host_cli(monkeypatch)
    calls = _fake_native(monkeypatch)
    board = _board(tmp_path / "board.kicad_pcb", _PAD_BODY)
    result = inspect_attributed_mask_geometry(board, scratch_dir=tmp_path)

    version_call = next(c for c in calls if c[-1] == "--version")
    export_call = next(c for c in calls if "gerbers" in c)
    worker_call = next(
        c for c in calls if len(c) >= 4 and c[-4].endswith("_native_mask_objects.py")
    )
    assert _image_and_entrypoint(version_call) == ("kicad-cli", "kicad/kicad:pinned")
    assert _image_and_entrypoint(export_call) == ("kicad-cli", "kicad/kicad:pinned")
    assert _image_and_entrypoint(worker_call) == ("python3", "kicad/kicad:pinned")
    for argv in (export_call, worker_call):
        assert f"KICAD_CONFIG_HOME={cfg}" in argv
    assert "Native object/export versions differ" not in result.errors
    assert result.exported.native_version == "10.0.6"


def test_explicit_native_command_still_wins_in_docker_mode(monkeypatch, tmp_path):
    from kicad_tools.validate.mask_copper_geometry import inspect_attributed_mask_geometry

    _docker_env(monkeypatch, tmp_path)
    _forbid_host_cli(monkeypatch)
    calls = _fake_native(monkeypatch)
    board = _board(tmp_path / "board.kicad_pcb", _PAD_BODY)
    inspect_attributed_mask_geometry(
        board, native_command=["/explicit/kicad-cli"], scratch_dir=tmp_path
    )
    export_call = next(c for c in calls if "gerbers" in c)
    assert export_call[0] == "/explicit/kicad-cli"


def test_host_mode_export_still_uses_discovered_kicad_cli(monkeypatch, tmp_path):
    from kicad_tools.validate.mask_copper_geometry import inspect_attributed_mask_geometry

    monkeypatch.setattr("kicad_tools.cli.kicad_toolchain.docker_fallback_enabled", lambda: False)
    monkeypatch.setattr("kicad_tools.cli.runner.find_kicad_cli", lambda: Path("/host/kicad-cli"))
    calls = _fake_native(monkeypatch)
    board = _board(tmp_path / "board.kicad_pcb", _PAD_BODY)
    inspect_attributed_mask_geometry(
        board, native_python_command=["/host/python3"], scratch_dir=tmp_path
    )
    export_call = next(c for c in calls if "gerbers" in c)
    assert export_call[0] == "/host/kicad-cli"


# ---------------------------------------------------------------------------
# (B) the unqualified-host refusal names a way forward
# ---------------------------------------------------------------------------


def test_unqualified_host_kicad_is_still_refused_with_a_remediation(monkeypatch, tmp_path):
    from kicad_tools.validate.mask_copper_geometry import (
        QUALIFIED_NATIVE_VERSIONS,
        inspect_attributed_mask_geometry,
    )

    assert "10.0.8" not in QUALIFIED_NATIVE_VERSIONS
    monkeypatch.setattr("kicad_tools.cli.kicad_toolchain.docker_fallback_enabled", lambda: False)
    monkeypatch.setattr("kicad_tools.cli.runner.find_kicad_cli", lambda: Path("/host/kicad-cli"))
    _fake_native(monkeypatch, version="10.0.8")
    board = _board(tmp_path / "board.kicad_pcb", _PAD_BODY)
    result = inspect_attributed_mask_geometry(
        board, native_python_command=["/host/python3"], scratch_dir=tmp_path
    )
    refusal = [e for e in result.errors if "qualified KiCad plot profile" in e]
    assert len(refusal) == 1
    assert "got 10.0.8" in refusal[0]
    assert "KCT_KICAD_DOCKER=1" in refusal[0]
    assert "install a qualified KiCad release" in refusal[0]
    # Refused means no attribution was produced.
    assert result.objects == {}


# ---------------------------------------------------------------------------
# Fractured zone fills are repaired, ambiguous polygons still refused
# ---------------------------------------------------------------------------

#: A 4x4 square with a 2x2 hole, in KiCad's fractured form: the hole is joined
#: to the outline by a zero-width bridge along y=0..1 at x=1, so the single
#: ring touches itself. Area 16 - 4 = 12.
_FRACTURED = [
    [0, 0],
    [1, 0],
    [1, 1],
    [1, 3],
    [3, 3],
    [3, 1],
    [1, 1],
    [1, 0],
    [4, 0],
    [4, 4],
    [0, 4],
]
_BOW_TIE = [[0, 0], [1, 1], [1, 0], [0, 1]]


def test_shared_repair_accepts_fractured_fill_and_rejects_bow_tie():
    from kicad_tools.validate.gerber_geometry import repair_self_touching_polygon

    fractured = Polygon(_FRACTURED)
    assert not fractured.is_valid
    repaired = repair_self_touching_polygon(fractured)
    assert repaired is not None and repaired.is_valid
    assert repaired.area == pytest.approx(12.0)
    assert repair_self_touching_polygon(Polygon(_BOW_TIE)) is None


@pytest.mark.parametrize(
    "shell,accepted",
    [(_FRACTURED, True), (_BOW_TIE, False)],
    ids=["fractured-zone-fill", "bow-tie"],
)
def test_native_object_reader_repairs_only_area_preserving_polygons(
    monkeypatch, tmp_path, shell, accepted
):
    from kicad_tools.validate.mask_copper_geometry import inspect_attributed_mask_geometry

    monkeypatch.setattr("kicad_tools.cli.kicad_toolchain.docker_fallback_enabled", lambda: False)
    monkeypatch.setattr("kicad_tools.cli.runner.find_kicad_cli", lambda: Path("/host/kicad-cli"))
    objects = {
        _PAD_UUID: {
            "kind": "PAD",
            "net": "",
            "layers": {"F.Cu": [{"shell": shell, "holes": []}]},
            "margin_mm": {},
        }
    }
    _fake_native(monkeypatch, objects=objects)
    board = _board(tmp_path / "board.kicad_pcb", _PAD_BODY)
    result = inspect_attributed_mask_geometry(
        board, native_python_command=["/host/python3"], scratch_dir=tmp_path
    )
    invalid = [e for e in result.errors if "invalid native object polygon" in e]
    if accepted:
        assert invalid == []
        geometry = result.objects[_PAD_UUID]["layers_geometry"]["F.Cu"]
        assert geometry.is_valid
        assert geometry.area == pytest.approx(12.0)
    else:
        assert invalid == [f"{_PAD_UUID}/F.Cu: invalid native object polygon"]
        assert "F.Cu" not in result.objects[_PAD_UUID]["layers_geometry"]


# ---------------------------------------------------------------------------
# A missing object is reported once, not twice
# ---------------------------------------------------------------------------


def _worker_payload(errors):
    return json.dumps(
        {
            "schema": "kct.native-mask-objects.v1",
            "native_version": "10.0.6",
            "errors": list(errors),
            "objects": {},
            "max_error_mm": 0.0,
        }
    )


def test_missing_object_the_worker_already_named_is_not_reported_twice():
    from kicad_tools.validate.mask_copper_geometry import _read_worker_output

    named, silent = "uuid-named", "uuid-silent"
    data = _read_worker_output(
        _worker_payload([f"{named}: missing/ambiguous native object"]), {named, silent}
    )
    assert data["errors"] == [
        f"{named}: missing/ambiguous native object",
        f"Missing native object: {silent}",
    ]
