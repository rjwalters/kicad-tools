"""Bundle-integrity guards for manufacturing manifests (issue #3529).

``kct export`` used to write ``bom_jlcpcb.csv`` / ``cpl_jlcpcb.csv`` with
CRLF line endings (csv.writer's default) and hash THAT content into
``manifest.json``.  Git text normalization stores LF, so ``sha256sum`` on
a fresh checkout mismatched the manifest for those two files -- found
while shipping softstart rev B (softstart#5).

Two layers of defense:

1. Formatter-level: all CSV exporters must emit LF-only content so that
   disk == git == manifest on every platform.
2. Bundle-level: every committed ``output/**/manifest.json`` under
   ``boards/`` must verify against the files actually on disk.  This is
   the check that would have caught both the CRLF mismatch and the
   stale-manifest-entry class (issue #3529 scope addition from the
   PR #3536 judge).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from kicad_tools.export import verify_manifest
from kicad_tools.export.bom_formats import BOMExportConfig, JLCPCBBOMFormatter
from kicad_tools.export.pnp import JLCPCBPnPFormatter, PlacementData, PnPExportConfig
from kicad_tools.schema.bom import BOMItem

REPO_ROOT = Path(__file__).resolve().parent.parent

# All committed manufacturing bundles: boards/*/output and
# boards/external/*/output (softstart etc.).
COMMITTED_MANIFESTS = sorted(REPO_ROOT.glob("boards/**/output/**/manifest.json"))


def _manifest_id(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


# ---------------------------------------------------------------------------
# Layer 1: CSV exporters must emit LF-only content
# ---------------------------------------------------------------------------


class TestCSVLineEndings:
    def test_jlcpcb_bom_has_no_crlf(self):
        items = [
            BOMItem(
                reference="R1",
                value="10k",
                footprint="R_0603_1608Metric",
                lib_id="Device:R",
                lcsc="C25804",
            ),
            BOMItem(
                reference="C1",
                value="100nF",
                footprint="C_0603_1608Metric",
                lib_id="Device:C",
                lcsc="C14663",
            ),
        ]
        csv_text = JLCPCBBOMFormatter(BOMExportConfig()).format(items)
        assert "\r" not in csv_text, "BOM CSV must use LF line endings (issue #3529)"
        assert csv_text.endswith("\n")
        assert len(csv_text.splitlines()) == 3  # header + 2 rows

    def test_jlcpcb_cpl_has_no_crlf(self):
        placements = [
            PlacementData(
                reference="R1",
                value="10k",
                footprint="R_0603_1608Metric",
                x=10.0,
                y=20.0,
                rotation=90.0,
                layer="F.Cu",
            ),
        ]
        csv_text = JLCPCBPnPFormatter(PnPExportConfig()).format(placements)
        assert "\r" not in csv_text, "CPL CSV must use LF line endings (issue #3529)"
        assert csv_text.endswith("\n")


# ---------------------------------------------------------------------------
# Layer 2a: verify_manifest unit behaviour
# ---------------------------------------------------------------------------


def _write_bundle(tmp_path: Path) -> Path:
    """Create a minimal bundle with a correct manifest; return its path."""
    bom = tmp_path / "bom_jlcpcb.csv"
    bom.write_text("Comment,Designator,Footprint,LCSC Part #\n10k,R1,R_0603,C25804\n")
    gerber_dir = tmp_path / "gerbers"
    gerber_dir.mkdir()
    zip_file = gerber_dir / "gerbers.zip"
    zip_file.write_bytes(b"PK\x05\x06" + b"\x00" * 18)  # empty zip

    files = {}
    for p in (bom, zip_file):
        files[p.name] = {
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
            "size": p.stat().st_size,
        }
    manifest_path = tmp_path / "manifest.json"
    files[manifest_path.name] = {"sha256": "self", "size": 0}  # must be skipped
    manifest_path.write_text(json.dumps({"version": "1.0", "files": files}, indent=2))
    return manifest_path


class TestVerifyManifest:
    def test_clean_bundle_passes(self, tmp_path):
        manifest_path = _write_bundle(tmp_path)
        assert verify_manifest(manifest_path) == []

    def test_resolves_files_in_subdirectories(self, tmp_path):
        # gerbers.zip lives in gerbers/ but the manifest stores the bare
        # name; verification must still find and check it.
        manifest_path = _write_bundle(tmp_path)
        problems = verify_manifest(manifest_path)
        assert not any("gerbers.zip" in p for p in problems)

    def test_detects_modified_content(self, tmp_path):
        manifest_path = _write_bundle(tmp_path)
        # Simulate the CRLF normalization mismatch: same logical content,
        # different bytes on disk.
        bom = tmp_path / "bom_jlcpcb.csv"
        bom.write_bytes(bom.read_bytes().replace(b"\n", b"\r\n"))
        problems = verify_manifest(manifest_path)
        assert any("bom_jlcpcb.csv" in p and "sha256 mismatch" in p for p in problems)
        assert any("bom_jlcpcb.csv" in p and "size mismatch" in p for p in problems)

    def test_detects_missing_file(self, tmp_path):
        manifest_path = _write_bundle(tmp_path)
        (tmp_path / "bom_jlcpcb.csv").unlink()
        problems = verify_manifest(manifest_path)
        assert any("bom_jlcpcb.csv" in p and "not found" in p for p in problems)


# ---------------------------------------------------------------------------
# Layer 2b: every committed bundle must verify (would have caught #3529)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("manifest_path", COMMITTED_MANIFESTS, ids=_manifest_id)
def test_committed_bundle_manifest_verifies(manifest_path: Path):
    problems = verify_manifest(manifest_path)
    assert problems == [], (
        f"{_manifest_id(manifest_path)} is stale relative to the files on disk. "
        "Regenerate the bundle (kct export) or recompute the listed hashes. "
        f"Mismatches: {problems}"
    )


def test_committed_bundles_discovered():
    """Guard the glob itself -- if bundle layout moves, fail loudly instead
    of silently parametrizing over nothing."""
    assert COMMITTED_MANIFESTS, "expected committed manifest.json bundles under boards/"
