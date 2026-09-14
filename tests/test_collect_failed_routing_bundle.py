"""Tests for scripts/ci/collect_failed_routing_bundle.py (Issue #5067).

The four long-running board-routing CI jobs (``diffpair-routing-regression``,
``matchgroup-routing-regression``, ``board-06-end-to-end``,
``board-07-end-to-end``) discard their generated PCB/schematic/DRC output on
failure, making failures hard to reproduce (#5044 / PR #5045). This module's
tests pin the collection script's core contract:

* Present sources are copied byte-for-byte with a recorded SHA-256 hash.
* Missing sources are recorded (not fatal) -- partial output is the expected
  case for a job that failed mid-route or mid-validation.
* The manifest carries reproduction metadata (commit, seed, command, tool
  versions) plus a per-file inventory.
* The CLI entry point never raises/exits non-zero on a missing source, and
  never copies anything outside the caller's explicit ``--source`` list.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "ci" / "collect_failed_routing_bundle.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("collect_failed_routing_bundle", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["collect_failed_routing_bundle"] = module
    spec.loader.exec_module(module)
    return module


collect_failed_routing_bundle = _load_module()


class TestParseSourceArg:
    def test_plain_path_derives_label_from_basename(self) -> None:
        label, path = collect_failed_routing_bundle.parse_source_arg(
            "boards/06-diffpair-test/regression-output"
        )
        assert label == "regression-output"
        assert path == Path("boards/06-diffpair-test/regression-output")

    def test_explicit_label_overrides_basename(self) -> None:
        label, path = collect_failed_routing_bundle.parse_source_arg("board06-ci=/tmp/board06-ci")
        assert label == "board06-ci"
        assert path == Path("/tmp/board06-ci")

    def test_empty_label_before_equals_raises(self) -> None:
        with pytest.raises(ValueError):
            collect_failed_routing_bundle.parse_source_arg("=/tmp/board06-ci")


class TestCollectSource:
    def test_missing_source_is_recorded_not_fatal(self, tmp_path: Path) -> None:
        dest_root = tmp_path / "dest"
        dest_root.mkdir()
        missing = tmp_path / "does-not-exist"

        result = collect_failed_routing_bundle.collect_source("missing-thing", missing, dest_root)

        assert result.present is False
        assert result.dest_relpath is None
        assert result.files == []
        # Nothing was created under dest for a missing source.
        assert not (dest_root / "missing-thing").exists()

    def test_directory_source_copies_recursively_with_hashes(self, tmp_path: Path) -> None:
        source = tmp_path / "regression-output"
        source.mkdir()
        (source / "diffpair_test_routed.kicad_pcb").write_text("pcb-bytes")
        nested = source / "sub"
        nested.mkdir()
        (nested / "net_class_map.json").write_text('{"a": 1}')

        dest_root = tmp_path / "dest"
        dest_root.mkdir()

        result = collect_failed_routing_bundle.collect_source(
            "regression-output", source, dest_root
        )

        assert result.present is True
        assert result.dest_relpath == "regression-output"
        collected_paths = {Path(f["path"]).name for f in result.files}
        assert collected_paths == {"diffpair_test_routed.kicad_pcb", "net_class_map.json"}

        pcb_copy = dest_root / "regression-output" / "diffpair_test_routed.kicad_pcb"
        assert pcb_copy.is_file()
        assert pcb_copy.read_text() == "pcb-bytes"

        expected_hash = hashlib.sha256(b"pcb-bytes").hexdigest()
        pcb_entry = next(
            f for f in result.files if Path(f["path"]).name == "diffpair_test_routed.kicad_pcb"
        )
        assert pcb_entry["sha256"] == expected_hash
        assert pcb_entry["size_bytes"] == len(b"pcb-bytes")

    def test_file_source_is_copied_under_label(self, tmp_path: Path) -> None:
        source = tmp_path / "lvs.json"
        source.write_text('{"clean": true}')
        dest_root = tmp_path / "dest"
        dest_root.mkdir()

        result = collect_failed_routing_bundle.collect_source("lvs-report", source, dest_root)

        assert result.present is True
        copy = dest_root / "lvs-report" / "lvs.json"
        assert copy.is_file()
        assert copy.read_text() == '{"clean": true}'

    def test_partial_output_mixes_present_and_missing(self, tmp_path: Path) -> None:
        """The board-06/07 end-to-end jobs may fail before every sidecar
        exists (e.g. schematic present, routed PCB absent) -- collection
        must handle a mix of present and missing sources in one call."""
        present = tmp_path / "board06-ci"
        present.mkdir()
        (present / "diffpair_test.kicad_sch").write_text("sch-bytes")
        missing = tmp_path / "board06-copper.json"  # never written this run

        dest_root = tmp_path / "dest"
        dest_root.mkdir()

        present_result = collect_failed_routing_bundle.collect_source(
            "board06-ci", present, dest_root
        )
        missing_result = collect_failed_routing_bundle.collect_source(
            "board06-copper", missing, dest_root
        )

        assert present_result.present is True
        assert missing_result.present is False


class TestBuildManifest:
    def test_manifest_carries_reproduction_metadata(self) -> None:
        collected = [
            collect_failed_routing_bundle.CollectedSource(
                label="regression-output",
                source="boards/06-diffpair-test/regression-output",
                present=True,
                dest_relpath="regression-output",
                files=[
                    {"path": "dest/regression-output/x.kicad_pcb", "size_bytes": 3, "sha256": "abc"}
                ],
            ),
            collect_failed_routing_bundle.CollectedSource(
                label="missing-thing",
                source="/tmp/nope",
                present=False,
                dest_relpath=None,
                files=[],
            ),
        ]

        manifest = collect_failed_routing_bundle.build_manifest(
            job="diffpair-routing-regression",
            board="boards/06-diffpair-test",
            commit="deadbeef",
            seed="42",
            command="python generate_design.py ... --seed 42",
            python_version="Python 3.12.0",
            kicad_version="10.0.0",
            collected=collected,
        )

        assert manifest["job"] == "diffpair-routing-regression"
        assert manifest["board"] == "boards/06-diffpair-test"
        assert manifest["commit"] == "deadbeef"
        assert manifest["seed"] == "42"
        assert manifest["command"].startswith("python generate_design.py")
        assert manifest["python_version"] == "Python 3.12.0"
        assert manifest["kicad_version"] == "10.0.0"
        assert manifest["total_files_collected"] == 1
        assert len(manifest["sources"]) == 2
        present_entry = next(s for s in manifest["sources"] if s["label"] == "regression-output")
        assert present_entry["present"] is True
        missing_entry = next(s for s in manifest["sources"] if s["label"] == "missing-thing")
        assert missing_entry["present"] is False


class TestMainCLI:
    def test_main_writes_manifest_and_never_fails_on_missing_source(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        present_source = tmp_path / "regression-output"
        present_source.mkdir()
        (present_source / "diffpair_test_routed.kicad_pcb").write_text("pcb-bytes")

        dest = tmp_path / "staging"
        argv = [
            str(dest),
            "--source",
            f"regression-output={present_source}",
            "--source",
            f"missing-sidecar={tmp_path / 'does-not-exist'}",
            "--job",
            "diffpair-routing-regression",
            "--board",
            "boards/06-diffpair-test",
            "--commit",
            "cafef00d",
            "--seed",
            "42",
            "--command",
            "python boards/06-diffpair-test/generate_design.py ... --seed 42",
            "--python-version",
            "Python 3.12.0",
            "--kicad-version",
            "10.0.0",
        ]

        rc = collect_failed_routing_bundle.main(argv)

        assert rc == 0
        manifest_path = dest / "manifest.json"
        assert manifest_path.is_file()
        manifest = json.loads(manifest_path.read_text())
        assert manifest["commit"] == "cafef00d"
        assert manifest["total_files_collected"] == 1
        labels_present = {s["label"]: s["present"] for s in manifest["sources"]}
        assert labels_present == {"regression-output": True, "missing-sidecar": False}

        copied_pcb = dest / "regression-output" / "diffpair_test_routed.kicad_pcb"
        assert copied_pcb.is_file()
        assert copied_pcb.read_text() == "pcb-bytes"

    def test_main_creates_dest_even_with_zero_sources(self, tmp_path: Path) -> None:
        dest = tmp_path / "staging"
        rc = collect_failed_routing_bundle.main(
            [
                str(dest),
                "--job",
                "board-06-end-to-end",
                "--board",
                "boards/06-diffpair-test",
                "--commit",
                "cafef00d",
                "--command",
                "uv run python boards/06-diffpair-test/generate_design.py ...",
            ]
        )
        assert rc == 0
        assert dest.is_dir()
        manifest = json.loads((dest / "manifest.json").read_text())
        assert manifest["total_files_collected"] == 0
        assert manifest["sources"] == []
