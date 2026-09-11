"""Synthetic offline review records and pages; no supplier responses or private data."""

import csv
import hashlib
import io
import json

import pytest

from kicad_tools.export import submission_plan as sp
from kicad_tools.export import submission_review as sr

REQUIRED = ("gerber-visually-inspected", "bom-cross-checked", "quantities-confirmed")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def csv_bytes(headers, rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    writer.writerow(headers)
    writer.writerows(rows)
    return b"\xef\xbb\xbf" + stream.getvalue().encode()


def refresh_manifest(args):
    bundle = args["bundle_root"]
    files = {
        name: {
            "sha256": digest((bundle / name).read_bytes()),
            "size": (bundle / name).stat().st_size,
        }
        for name in args["bundle_files"]
    }
    (bundle / "manifest.json").write_text(json.dumps({"version": "1.0", "files": files}))


@pytest.fixture
def plan_inputs(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    rows = [
        ["10k", "R1,R2", "R_0402", "C1", "2"],
        ["1k", "R3", "R_0402", "C2", "1"],
    ]
    placements = [
        ["R1", "10k", "R_0402", "10mm", "20mm", "0", "Top"],
        ["R2", "10k", "R_0402", "11mm", "20mm", "0", "Top"],
        ["R3", "1k", "R_0402", "12mm", "20mm", "0", "Top"],
    ]
    (bundle / "bom.csv").write_bytes(
        csv_bytes(["Comment", "Designator", "Footprint", "LCSC Part #", "Quantity"], rows)
    )
    (bundle / "cpl.csv").write_bytes(
        csv_bytes(
            ["Designator", "Val", "Package", "Mid X", "Mid Y", "Rotation", "Layer"], placements
        )
    )
    (bundle / "gerbers.zip").write_bytes(b"PK\x03\x04\x00synthetic-exact-zip-bytes\xff\r\n")
    (source / "board.kicad_pcb").write_bytes(b"synthetic PCB\r\n")
    (source / "board.kicad_sch").write_bytes(b"synthetic schematic\n")
    sources = {
        role: sp.SourceEvidence(
            name, digest((source / name).read_bytes()), (source / name).stat().st_size
        )
        for role, name in [("pcb", "board.kicad_pcb"), ("schematic", "board.kicad_sch")]
    }
    args = {
        "bundle_root": bundle,
        "source_root": source,
        "source_evidence": sources,
        "bundle_files": ("gerbers.zip", "bom.csv", "cpl.csv"),
        "artifacts": {"gerber": "gerbers.zip", "bom": "bom.csv", "cpl": "cpl.csv"},
        "board_quantity": 1,
        "settings": sp.FactorySettings("jlcpcb", 4, ("top",)),
        "destination": tmp_path / "handoff",
    }
    refresh_manifest(args)
    return args


@pytest.fixture
def plan(plan_inputs):
    return sp.prepare_submission(**plan_inputs)


def full_checklist():
    return dict.fromkeys(REQUIRED, True)


def submit(plan, tmp_path, *, name="review.json", checklist=None, **overrides):
    kwargs = {
        "plan": plan,
        "reviewer": "alice@example.com",
        "reviewed_at": "2026-01-01T00:00:00Z",
        "policy_version": "policy-v1",
        "checklist": full_checklist() if checklist is None else checklist,
        "required_items": REQUIRED,
        "destination": tmp_path / name,
    }
    kwargs.update(overrides)
    return sr.submit_review(**kwargs)


def test_submit_review_binds_exact_hashes(plan, tmp_path):
    record = submit(plan, tmp_path)
    assert record.sha256 == digest(record.record_bytes)
    core = json.loads(record.record_bytes)
    assert core["schema_version"] == 1
    assert core["reviewer"] == "alice@example.com"
    assert core["policy_version"] == "policy-v1"
    assert core["checklist"] == full_checklist()
    assert core["plan_sha256"] == plan.sha256
    plan_core = json.loads(plan.plan_bytes)
    assert core["bundle_files"] == plan_core["bundle_files"]
    assert core["sources"] == plan_core["sources"]
    assert (
        core["artifact_outputs"]["gerber"]["sha256"]
        == plan_core["outputs"]["gerbers.zip"]["sha256"]
    )
    assert core["artifact_outputs"]["bom"]["sha256"] == plan_core["outputs"]["bom.csv"]["sha256"]
    assert core["artifact_outputs"]["cpl"]["sha256"] == plan_core["outputs"]["cpl.csv"]["sha256"]
    assert core["states"]["human_approval"] == "reviewed"
    assert core["states"]["order"] == "not-performed"
    assert core["states"]["upload"] == "not-performed"
    assert core["states"]["factory_matching"] == "not-observed"
    assert not record.path.stat().st_mode & 0o222
    assert sr.verify_review(record, plan) == core


@pytest.mark.parametrize(
    "checklist",
    [
        {},
        dict.fromkeys(REQUIRED[:-1], True),  # missing a required item
        {**dict.fromkeys(REQUIRED, True), REQUIRED[0]: False},  # explicit decline
        {**dict.fromkeys(REQUIRED, True), "extra-unrecognized-item": True},  # unknown extra item
        dict.fromkeys(REQUIRED, "yes"),  # truthy but not exactly True
    ],
)
def test_incomplete_checklist_never_writes_a_record(plan, tmp_path, checklist):
    with pytest.raises(sr.ReviewError):
        submit(plan, tmp_path, checklist=checklist)
    assert not (tmp_path / "review.json").exists()


def test_missing_checklist_is_invalid_never_an_implicit_pass(plan, tmp_path):
    with pytest.raises(sr.ReviewError, match="checklist"):
        sr.submit_review(
            plan=plan,
            reviewer="alice",
            reviewed_at="t0",
            policy_version="v1",
            checklist={},
            required_items=REQUIRED,
            destination=tmp_path / "review.json",
        )
    assert not (tmp_path / "review.json").exists()


@pytest.mark.parametrize(
    "overrides",
    [
        {"reviewer": ""},
        {"reviewer": "   "},
        {"reviewed_at": ""},
        {"policy_version": ""},
        {"required_items": ()},
        {"required_items": ("",)},
    ],
)
def test_explicit_identity_and_policy_fields_are_required(plan, tmp_path, overrides):
    with pytest.raises(sr.ReviewError):
        submit(plan, tmp_path, **overrides)
    assert not (tmp_path / "review.json").exists()


def test_review_record_destination_must_not_exist(plan, tmp_path):
    dest = tmp_path / "review.json"
    dest.write_bytes(b"peer owned review record")
    with pytest.raises(sr.ReviewError, match="exists"):
        submit(plan, tmp_path)
    assert dest.read_bytes() == b"peer owned review record"


def test_review_record_cannot_be_written_inside_the_handoff(plan):
    with pytest.raises(sr.ReviewError):
        sr.submit_review(
            plan=plan,
            reviewer="alice",
            reviewed_at="t0",
            policy_version="v1",
            checklist=full_checklist(),
            required_items=REQUIRED,
            destination=plan.directory / "review.json",
        )


def test_mixed_revision_bundle_change_invalidates_review(plan_inputs, tmp_path):
    plan = sp.prepare_submission(**plan_inputs)
    record = submit(plan, tmp_path)
    assert sr.verify_review(record, plan) is not None

    (plan_inputs["bundle_root"] / "gerbers.zip").write_bytes(b"PK\x03\x04 changed-bytes\r\n")
    refresh_manifest(plan_inputs)
    new_plan = sp.prepare_submission(**(plan_inputs | {"destination": tmp_path / "handoff-2"}))
    assert new_plan.sha256 != plan.sha256
    with pytest.raises(sr.ReviewError, match="different plan"):
        sr.verify_review(record, new_plan)


def test_source_change_after_review_invalidates_only_when_rechecked(plan_inputs, tmp_path):
    plan = sp.prepare_submission(**plan_inputs)
    record = submit(plan, tmp_path)
    assert sr.verify_review(record, plan, source_root=plan_inputs["source_root"]) is not None

    (plan_inputs["source_root"] / "board.kicad_pcb").write_bytes(b"mutated pcb bytes on disk")
    # The plan/bundle-level hashes alone were fixed at prepare time and are unaffected...
    assert sr.verify_review(record, plan) is not None
    # ...but explicitly rereading the current source catches the drift.
    with pytest.raises(sr.ReviewError, match="source"):
        sr.verify_review(record, plan, source_root=plan_inputs["source_root"])


def test_verify_review_rejects_hand_edited_checklist(plan, tmp_path):
    record = submit(plan, tmp_path)
    record.path.chmod(0o644)
    core = json.loads(record.path.read_bytes())
    core["checklist"][REQUIRED[0]] = False
    tampered_bytes = (json.dumps(core) + "\n").encode()
    record.path.write_bytes(tampered_bytes)
    tampered = sr.ReviewRecord(record.path, tampered_bytes)
    with pytest.raises(sr.ReviewError):
        sr.verify_review(tampered, plan)


def test_render_page_without_record_is_explicitly_not_reviewed(plan, tmp_path):
    page = sr.render_review_page(plan, destination=tmp_path / "page")
    index = (page.directory / "index.md").read_text()
    assert "NOT REVIEWED" in index
    assert "human_approval: not-established" in index
    manifest = json.loads(page.manifest_bytes)
    assert manifest["review_sha256"] is None
    assert manifest["plan_sha256"] == plan.sha256
    assert (page.directory / "uploads" / "gerbers.zip").read_bytes() == (
        plan.directory / "gerbers.zip"
    ).read_bytes()
    assert (page.directory / "inventory-report.txt").exists()
    assert not (page.directory / "index.md").stat().st_mode & 0o222


def test_render_page_with_valid_record_shows_reviewer_and_state(plan, tmp_path):
    record = submit(plan, tmp_path)
    page = sr.render_review_page(plan, destination=tmp_path / "page", record=record)
    index = (page.directory / "index.md").read_text()
    assert "alice@example.com" in index
    assert "policy-v1" in index
    assert "human_approval: reviewed" in index
    manifest = json.loads(page.manifest_bytes)
    assert manifest["review_sha256"] == record.sha256


def test_render_page_rejects_stale_record(plan_inputs, tmp_path):
    plan = sp.prepare_submission(**plan_inputs)
    record = submit(plan, tmp_path)
    (plan_inputs["bundle_root"] / "gerbers.zip").write_bytes(b"PK\x03\x04 changed-again\r\n")
    refresh_manifest(plan_inputs)
    new_plan = sp.prepare_submission(**(plan_inputs | {"destination": tmp_path / "handoff-2"}))
    with pytest.raises(sr.ReviewError):
        sr.render_review_page(new_plan, destination=tmp_path / "page", record=record)
    assert not (tmp_path / "page").exists()


def _asset(path, data, kind):
    return sr.ReviewAsset(path, digest(data), len(data), kind)


def test_layer_views_and_synthetic_test_evidence_are_kept_separate(plan, tmp_path):
    assets_root = tmp_path / "assets"
    assets_root.mkdir()
    top_view = b"fake-png-bytes-top-copper-layer"
    headless_capture = b"fake-headless-browser-screenshot-bytes"
    (assets_root / "top-copper.png").write_bytes(top_view)
    (assets_root / "headless-screenshot.png").write_bytes(headless_capture)
    assets = {
        "top-copper.png": _asset("top-copper.png", top_view, "manufacturing"),
        "headless-screenshot.png": _asset(
            "headless-screenshot.png", headless_capture, "synthetic-test-evidence"
        ),
    }
    page = sr.render_review_page(
        plan, destination=tmp_path / "page", assets=assets, assets_root=assets_root
    )
    assert (page.directory / "layer-views" / "top-copper.png").read_bytes() == top_view
    assert (page.directory / "test-evidence" / "headless-screenshot.png").read_bytes() == (
        headless_capture
    )
    index = (page.directory / "index.md").read_text()
    assert "NOT MANUFACTURING EVIDENCE" in index
    assert "layer-views/top-copper.png" in index
    assert "test-evidence/headless-screenshot.png" in index
    manifest = json.loads(page.manifest_bytes)
    assert "layer-views/top-copper.png" in manifest["files"]
    assert "test-evidence/headless-screenshot.png" in manifest["files"]


def test_synthetic_test_evidence_alone_never_appears_as_manufacturing(plan, tmp_path):
    assets_root = tmp_path / "assets"
    assets_root.mkdir()
    capture = b"only a headless test capture, nothing manufactured"
    (assets_root / "capture.png").write_bytes(capture)
    assets = {"capture.png": _asset("capture.png", capture, "synthetic-test-evidence")}
    page = sr.render_review_page(
        plan, destination=tmp_path / "page", assets=assets, assets_root=assets_root
    )
    assert not (page.directory / "layer-views").exists()
    index = (page.directory / "index.md").read_text()
    layer_section = index.split("## Layer views")[1].split("## Synthetic")[0]
    assert "(none supplied)" in layer_section
    assert "capture.png" not in layer_section


def test_asset_bytes_must_match_declared_hash(plan, tmp_path):
    assets_root = tmp_path / "assets"
    assets_root.mkdir()
    (assets_root / "view.png").write_bytes(b"real-bytes")
    bad = sr.ReviewAsset("view.png", digest(b"different-bytes"), 10, "manufacturing")
    with pytest.raises(sr.ReviewError):
        sr.render_review_page(
            plan,
            destination=tmp_path / "page",
            assets={"view.png": bad},
            assets_root=assets_root,
        )
    assert not (tmp_path / "page").exists()


def test_unknown_asset_kind_rejected(plan, tmp_path):
    assets_root = tmp_path / "assets"
    assets_root.mkdir()
    (assets_root / "view.png").write_bytes(b"real-bytes")
    weird = sr.ReviewAsset(
        "view.png", digest(b"real-bytes"), len(b"real-bytes"), "customer-provided-photo"
    )
    with pytest.raises(sr.ReviewError):
        sr.render_review_page(
            plan,
            destination=tmp_path / "page",
            assets={"view.png": weird},
            assets_root=assets_root,
        )


def test_assets_require_explicit_assets_root(plan, tmp_path):
    good = sr.ReviewAsset("view.png", digest(b"x"), 1, "manufacturing")
    with pytest.raises(sr.ReviewError):
        sr.render_review_page(plan, destination=tmp_path / "page", assets={"view.png": good})


def test_review_page_is_portable_no_secrets_or_ambient_paths(plan, tmp_path, monkeypatch):
    monkeypatch.setenv("JLCPCB_API_KEY", "must-not-appear-in-review-page")
    record = submit(plan, tmp_path)
    page = sr.render_review_page(plan, destination=tmp_path / "page", record=record)
    tmp_path_marker = str(tmp_path).encode()
    for path in page.directory.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert tmp_path_marker not in data
            assert b"must-not-appear-in-review-page" not in data
            assert b"JLCPCB_API_KEY" not in data


def test_review_page_contains_only_expected_files(plan, tmp_path):
    record = submit(plan, tmp_path)
    page = sr.render_review_page(plan, destination=tmp_path / "page", record=record)
    manifest = json.loads(page.manifest_bytes)
    found = {
        str(p.relative_to(page.directory))
        for p in page.directory.rglob("*")
        if p.is_file() and p.name != "review-page-manifest.json"
    }
    assert found == set(manifest["files"])
    plan_core = json.loads(plan.plan_bytes)
    assert set(plan_core["outputs"]) == {
        name.removeprefix("uploads/") for name in found if name.startswith("uploads/")
    }
