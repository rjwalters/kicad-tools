"""Pinned, digest-verified 3D model subset (issue #7683)."""

from __future__ import annotations

import hashlib
import json
from argparse import Namespace
from pathlib import Path

import pytest

from kicad_tools.cli.kicad_toolchain import PINNED_KICAD_VERSION, docker_kicad_cli_prefix
from kicad_tools.pcb import model_audit, models3d_pinned
from kicad_tools.pcb.models3d_pinned import (
    PINNED_3DMODEL_TAG,
    Manifest,
    load_manifest,
    pinned_model_dir,
    sync_models,
    verify_cache,
)

PAYLOADS = {
    "Resistor_SMD.3dshapes/R_0603_1608Metric.step": b"resistor-body",
    "Capacitor_SMD.3dshapes/C_0402_1005Metric.step": b"capacitor,body",
}


def _manifest(payloads=PAYLOADS) -> Manifest:
    return Manifest(
        tag="10.0.6",
        commit="0" * 40,
        files={rel: hashlib.sha256(data).hexdigest() for rel, data in payloads.items()},
    )


class _Fetcher:
    """Stub network: counts calls, serves PAYLOADS keyed by manifest path."""

    def __init__(self, payloads=PAYLOADS):
        self.calls: list[str] = []
        self.payloads = payloads

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        from urllib.parse import unquote

        for rel, data in self.payloads.items():
            if unquote(url).endswith(rel):
                return data
        raise OSError("404")


def test_pinned_tag_tracks_pinned_image_version():
    assert PINNED_3DMODEL_TAG == PINNED_KICAD_VERSION
    assert load_manifest().tag == PINNED_3DMODEL_TAG


def test_committed_manifest_is_well_formed():
    manifest = load_manifest()
    assert len(manifest) > 0
    assert len(manifest.commit) == 40
    # The peeled commit of the annotated tag, not the tag object (125f0268...).
    assert manifest.commit == "e62ed1fc7862da83f789bd562671b5e4b82afcdf"
    assert all(rel.endswith(".step") for rel in manifest.files)
    assert models3d_pinned.ATTRIBUTION_PATH.is_file()


def test_sync_downloads_then_second_run_makes_no_network_calls(tmp_path):
    manifest, fetcher = _manifest(), _Fetcher()
    first = sync_models(manifest, tmp_path, fetcher)
    assert first.ok and sorted(first.downloaded) == sorted(PAYLOADS)
    assert len(fetcher.calls) == len(PAYLOADS)
    assert all("/-/raw/10.0.6/" in url for url in fetcher.calls)

    second = sync_models(manifest, tmp_path, fetcher)
    assert second.ok and not second.downloaded and len(second.cached) == len(PAYLOADS)
    assert len(fetcher.calls) == len(PAYLOADS)  # unchanged: zero new calls
    assert verify_cache(manifest, tmp_path) == []


def test_digest_mismatch_deletes_file_and_names_it(tmp_path):
    manifest = _manifest()
    bad = "Resistor_SMD.3dshapes/R_0603_1608Metric.step"
    result = sync_models(manifest, tmp_path, _Fetcher({**PAYLOADS, bad: b"tampered"}))
    assert not result.ok
    assert list(result.failed) == [bad] and "sha256 mismatch" in result.failed[bad]
    assert not (tmp_path / bad).exists()
    assert not list(tmp_path.rglob(".part-*"))


def test_corrupt_cached_file_is_refetched(tmp_path):
    manifest, fetcher = _manifest(), _Fetcher()
    sync_models(manifest, tmp_path, fetcher)
    victim = next(iter(PAYLOADS))
    (tmp_path / victim).write_bytes(b"bitrot")
    fetcher.calls.clear()
    assert sync_models(manifest, tmp_path, fetcher).ok
    assert len(fetcher.calls) == 1 and verify_cache(manifest, tmp_path) == []


def test_missing_upstream_file_is_a_named_failure(tmp_path):
    result = sync_models(_manifest(), tmp_path, _Fetcher({}))
    assert sorted(result.failed) == sorted(PAYLOADS)


def test_pinned_model_dir_requires_every_file(tmp_path):
    manifest = _manifest()
    assert pinned_model_dir(manifest, tmp_path) is None  # empty cache
    rel = next(iter(PAYLOADS))
    (tmp_path / rel).parent.mkdir(parents=True)
    (tmp_path / rel).write_bytes(PAYLOADS[rel])
    assert pinned_model_dir(manifest, tmp_path) is None  # partial cache
    sync_models(manifest, tmp_path, _Fetcher())
    assert pinned_model_dir(manifest, tmp_path) == tmp_path


@pytest.mark.parametrize("bad", ["/etc/passwd", "../escape.step"])
def test_manifest_rejects_unsafe_paths(tmp_path, bad):
    path = tmp_path / "m.json"
    path.write_text(json.dumps({"tag": "t", "commit": "c", "files": {bad: "0" * 64}}))
    with pytest.raises(models3d_pinned.ModelSyncError):
        load_manifest(path)


@pytest.mark.parametrize(
    "digest",
    ["0" * 63, "0" * 65, "g" * 64, "A" * 64, " " + "0" * 63, "0" * 63 + "\n"],
)
def test_manifest_rejects_non_hex_digests(tmp_path, digest):
    """Digests must be exactly 64 lowercase hex characters (sha256 hexdigest)."""
    path = tmp_path / "m.json"
    path.write_text(json.dumps({"tag": "t", "commit": "c", "files": {"a.step": digest}}))
    with pytest.raises(models3d_pinned.ModelSyncError, match="digest"):
        load_manifest(path)


def test_manifest_accepts_lowercase_hex_digest(tmp_path):
    path = tmp_path / "m.json"
    good = hashlib.sha256(b"x").hexdigest()
    path.write_text(json.dumps({"tag": "t", "commit": "c", "files": {"a.step": good}}))
    assert load_manifest(path).files == {"a.step": good}


@pytest.mark.parametrize(
    "bad",
    ["lcsc/C12345.step", "C2040.step", "Foo.3dshapes/C99.wrl", "easyeda/x.step", "lcsc-3d/a.step"],
)
def test_manifest_rejects_lcsc_easyeda_bodies(tmp_path, bad):
    """The pinned subset is KiCad stock models only; LCSC bodies never enter it (#7686)."""
    path = tmp_path / "m.json"
    path.write_text(json.dumps({"tag": "t", "commit": "c", "files": {bad: "0" * 64}}))
    with pytest.raises(models3d_pinned.ModelSyncError, match="LCSC"):
        load_manifest(path)


def test_pinned_subset_is_distinct_from_lcsc_cache(tmp_path, monkeypatch):
    """Synced pinned files are not LCSC content and never serve an LCSC ref."""
    from kicad_tools.pcb import lcsc_guard
    from kicad_tools.pcb.lcsc_models import DEFAULT_CACHE_ENV_VAR, lcsc_cache_dir

    monkeypatch.delenv(DEFAULT_CACHE_ENV_VAR, raising=False)
    monkeypatch.delenv(models3d_pinned.CACHE_ENV_VAR, raising=False)
    lcsc_default, pinned_default = lcsc_cache_dir(), models3d_pinned.cache_dir()
    assert lcsc_default not in pinned_default.parents and lcsc_default != pinned_default
    assert pinned_default not in lcsc_default.parents

    cache = tmp_path / "pinned"
    monkeypatch.setenv(models3d_pinned.CACHE_ENV_VAR, str(cache))
    manifest = _manifest()
    monkeypatch.setattr(models3d_pinned, "load_manifest", lambda path=None: manifest)
    root = models3d_pinned.cache_dir("10.0.6")
    sync_models(manifest, root, _Fetcher())

    # An LCSC body placed inside the pinned cache must not resolve via the pinned env.
    (root / "lcsc").mkdir()
    (root / "lcsc" / "C12345.step").write_bytes(b"ISO-10303-21; lcsc")
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(
        '(kicad_pcb (version 20240108) (generator "t")\n'
        '  (footprint "Resistor_SMD:R_0603_1608Metric" (layer "F.Cu") (at 0 0)\n'
        '    (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS"))\n'
        '    (model "${KCT_LCSC_3D_DIR}/C12345.step"))\n'
        ")\n"
    )
    assert model_audit.audit_pcb_models(pcb, env="pinned").covered == 0

    # The guard does not mistake pinned KiCad models for LCSC bodies.
    lcsc = tmp_path / "lcsc-cache"
    lcsc.mkdir()
    (lcsc / "C777.step").write_bytes(b"ISO-10303-21; lcsc body")
    digests = lcsc_guard.lcsc_cache_digests(lcsc)
    pinned_files = [root / rel for rel in manifest.files]
    assert lcsc_guard.find_lcsc_step_violations(pinned_files, digests) == []


def _pcb(tmp_path: Path) -> Path:
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(
        '(kicad_pcb (version 20240108) (generator "t")\n'
        '  (footprint "Resistor_SMD:R_0603_1608Metric" (layer "F.Cu") (at 0 0)\n'
        '    (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS"))\n'
        '    (model "${KICAD10_3DMODEL_DIR}/Resistor_SMD.3dshapes/R_0603_1608Metric.step"))\n'
        ")\n"
    )
    return pcb


def test_pinned_audit_resolves_from_synced_cache(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    monkeypatch.setenv(models3d_pinned.CACHE_ENV_VAR, str(cache))
    manifest = _manifest({k: v for k, v in PAYLOADS.items() if k.startswith("Resistor")})
    monkeypatch.setattr(models3d_pinned, "load_manifest", lambda path=None: manifest)
    pcb = _pcb(tmp_path)

    # Not synced: degrades to the sentinel, nothing resolves, no crash.
    assert model_audit.resolve_pinned_model_dir() is None
    assert model_audit.audit_pcb_models(pcb, env="pinned").covered == 0

    sync_models(manifest, models3d_pinned.cache_dir("10.0.6"), _Fetcher())
    assert model_audit.resolve_pinned_model_dir() == models3d_pinned.cache_dir("10.0.6")
    report = model_audit.audit_pcb_models(pcb, env="pinned")
    assert (report.covered, report.total) == (1, 1)


def test_cli_pinned_audit_reports_not_synced(tmp_path, monkeypatch, capsys):
    from kicad_tools.cli.commands.pcb import _run_model_audit_command

    monkeypatch.setenv(models3d_pinned.CACHE_ENV_VAR, str(tmp_path / "empty"))
    args = Namespace(env="pinned", model_dir=None, format="text")
    assert _run_model_audit_command(args, _pcb(tmp_path)) == 0
    assert "pinned model set not synced" in capsys.readouterr().out


def test_cli_pinned_audit_json_reports_synced_flag(tmp_path, monkeypatch, capsys):
    from kicad_tools.cli.commands.pcb import _run_model_audit_command

    cache = tmp_path / "cache"
    monkeypatch.setenv(models3d_pinned.CACHE_ENV_VAR, str(cache))
    manifest = _manifest({k: v for k, v in PAYLOADS.items() if k.startswith("Resistor")})
    monkeypatch.setattr(models3d_pinned, "load_manifest", lambda path=None: manifest)
    pcb = _pcb(tmp_path)
    args = Namespace(env="pinned", model_dir=None, format="json")

    assert _run_model_audit_command(args, pcb) == 0
    assert json.loads(capsys.readouterr().out)["synced"] is False

    sync_models(manifest, models3d_pinned.cache_dir("10.0.6"), _Fetcher())
    assert _run_model_audit_command(args, pcb) == 0
    assert json.loads(capsys.readouterr().out)["synced"] is True

    # An explicit --model-dir bypasses the synced cache: the key is absent.
    explicit = Namespace(env="pinned", model_dir=str(tmp_path), format="json")
    assert _run_model_audit_command(explicit, pcb) == 0
    assert "synced" not in json.loads(capsys.readouterr().out)


def test_models_command_sync_and_status(tmp_path, monkeypatch, capsys):
    from kicad_tools.cli.commands.models import run_models_command

    monkeypatch.setenv(models3d_pinned.CACHE_ENV_VAR, str(tmp_path / "cache"))
    manifest = _manifest()
    monkeypatch.setattr(models3d_pinned, "load_manifest", lambda path=None: manifest)
    fetcher = _Fetcher()
    monkeypatch.setattr(models3d_pinned, "default_fetcher", fetcher)

    status = Namespace(models_command="status", models_format="text")
    assert run_models_command(status) == 1
    sync = Namespace(models_command="sync", models_format="text", sparse=True)
    assert run_models_command(sync) == 0
    assert run_models_command(status) == 0
    assert (models3d_pinned.cache_dir("10.0.6") / "ATTRIBUTION.md").is_file()
    calls = len(fetcher.calls)
    assert run_models_command(sync) == 0 and len(fetcher.calls) == calls
    capsys.readouterr()


def test_docker_prefix_mounts_models_only_when_given(tmp_path):
    base = docker_kicad_cli_prefix([tmp_path], tmp_path / "cfg", model_dir=None)
    models = tmp_path / "models"
    models.mkdir()
    withm = docker_kicad_cli_prefix([tmp_path], tmp_path / "cfg", model_dir=models)
    assert f"KICAD10_3DMODEL_DIR={models}" in withm
    assert f"{models}:{models}:ro" in withm
    assert not any(a.startswith("KICAD10_3DMODEL_DIR") for a in base)
