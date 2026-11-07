"""Tapeout sign-off must not stale a readiness receipt (Issue #8580)."""

import hashlib
import json

from kicad_tools.cli.board_readiness import read_readiness, readiness_input_hash

from .test_board_readiness import report_fixture, save

MANIFEST = "output/manufacturing/manifest.json"


def _ready_board(root, manifest):
    report = report_fixture(root)
    path = root / MANIFEST
    path.write_text(json.dumps(manifest))
    report["inputs"][MANIFEST] = readiness_input_hash(MANIFEST, path.read_bytes())
    save(root, report)
    return path


def test_tapeout_key_does_not_stale_receipt(tmp_path):
    path = _ready_board(tmp_path, {"files": {"a": {"sha256": "x"}}})
    assert read_readiness(tmp_path)["status"] == "ready"
    path.write_text(json.dumps({"tapeout": {"board_sha256": "y"}, "files": {"a": {"sha256": "x"}}}))
    assert read_readiness(tmp_path)["status"] == "ready"


def test_key_order_is_irrelevant(tmp_path):
    path = _ready_board(tmp_path, {"a": 1, "files": {}})
    path.write_text('{ "files": {}, "a": 1 }')
    assert read_readiness(tmp_path)["status"] == "ready"


def test_other_manifest_changes_still_stale(tmp_path):
    path = _ready_board(tmp_path, {"files": {"a": {"sha256": "x"}}})
    for changed in (
        {"files": {"a": {"sha256": "z"}}},
        {"files": {"a": {"sha256": "x"}}, "extra": 1},
    ):
        path.write_text(json.dumps(changed))
        result = read_readiness(tmp_path)
        assert result["status"] == "unverified"
        assert f"Evidence is stale: {MANIFEST} changed" in result["blockers"][0]


def test_invalid_json_falls_back_to_raw_hash(tmp_path):
    path = _ready_board(tmp_path, {"files": {}})
    path.write_text("not json")
    assert readiness_input_hash(MANIFEST, b"not json") == hashlib.sha256(b"not json").hexdigest()
    assert read_readiness(tmp_path)["status"] == "unverified"
    report = json.loads((tmp_path / "output/readiness.json").read_text())
    report["inputs"][MANIFEST] = hashlib.sha256(b"not json").hexdigest()
    save(tmp_path, report)
    assert read_readiness(tmp_path)["status"] == "ready"
    path.write_text("edited")
    assert read_readiness(tmp_path)["status"] == "unverified"


def test_legacy_raw_hash_receipt_still_valid(tmp_path):
    path = _ready_board(tmp_path, {"files": {}})
    report = json.loads((tmp_path / "output/readiness.json").read_text())
    report["inputs"][MANIFEST] = hashlib.sha256(path.read_bytes()).hexdigest()
    save(tmp_path, report)
    assert read_readiness(tmp_path)["status"] == "ready"
