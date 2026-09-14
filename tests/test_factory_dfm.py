"""Synthetic DFM report/transcription binding tests (#5146).

Every DFM report, transcription and hash below is a synthetic fixture built
locally -- none of it is a real factory report, and none of it is fetched or
reconstructed from any real-world report hash (including the parent issue's
user-reported hash, which this file intentionally never touches). No test
performs a network call or claims a real credential; upload receipts are
produced the same offline, injected-transport way as
``tests/manufacturers/test_jlc_upload.py``.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json

import pytest

from kicad_tools.export import factory_dfm as fd
from kicad_tools.export import submission_plan as sp
from kicad_tools.export import submission_review as sr
from kicad_tools.manufacturers import jlc_upload as ju
from kicad_tools.parts import jlcpcb_api

FAKE_APP_ID = "fake-app-id"
FAKE_ACCESS_KEY = "fake-access-key"
FAKE_SECRET_KEY = "fake-secret-key-do-not-use"

REQUIRED = ("gerber-visually-inspected", "bom-cross-checked", "quantities-confirmed")

SAMPLE_REPORT = b"%PDF-1.4 synthetic raster-only DFM report bytes, not a real factory report\n"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def csv_bytes(headers, rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    writer.writerow(headers)
    writer.writerows(rows)
    return b"\xef\xbb\xbf" + stream.getvalue().encode()


def fake_credentials() -> jlcpcb_api.JLCCredentials:
    return jlcpcb_api.JLCCredentials(FAKE_APP_ID, FAKE_ACCESS_KEY, FAKE_SECRET_KEY)


class FakeTransport:
    """Records every call; never touches a socket. ``live`` is always False."""

    def __init__(self, *, responses=None):
        self.identity = ju.TransportIdentity("fake-mock-protocol", False)
        self.multipart_calls: list[dict] = []
        self._responses = list(responses or [])

    def post_multipart(self, url, *, headers, fields, files):
        self.multipart_calls.append(
            {"url": url, "headers": dict(headers), "fields": dict(fields), "files": dict(files)}
        )
        return self._responses.pop(0)

    def post_json(self, url, *, headers, body):  # pragma: no cover -- unused here
        raise AssertionError("factory_dfm tests never fetch a live preview")


def ok_response(key="file-key-0001"):
    body = json.dumps({"code": 200, "success": True, "message": "success", "data": key}).encode()
    return ju.TransportResponse(200, {}, body)


def build_handoff(tmp_path, *, suffix=""):
    """A published plan + valid review record, ready to upload (synthetic)."""
    bundle = tmp_path / f"bundle{suffix}"
    bundle.mkdir()
    source = tmp_path / f"source{suffix}"
    source.mkdir()
    rows = [["10k", "R1,R2", "R_0402", "C1", "2"], ["1k", "R3", "R_0402", "C2", "1"]]
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
    gerber = b"PK\x03\x04\x00synthetic-exact-zip-bytes" + suffix.encode() + b"\xff\r\n"
    (bundle / "gerbers.zip").write_bytes(gerber)
    (source / "board.kicad_pcb").write_bytes(b"synthetic PCB\r\n")
    (source / "board.kicad_sch").write_bytes(b"synthetic schematic\n")
    files = {
        name: {
            "sha256": digest((bundle / name).read_bytes()),
            "size": (bundle / name).stat().st_size,
        }
        for name in ("gerbers.zip", "bom.csv", "cpl.csv")
    }
    (bundle / "manifest.json").write_text(json.dumps({"version": "1.0", "files": files}))
    evidence = {
        role: sp.SourceEvidence(
            name, digest((source / name).read_bytes()), (source / name).stat().st_size
        )
        for role, name in [("pcb", "board.kicad_pcb"), ("schematic", "board.kicad_sch")]
    }
    plan = sp.prepare_submission(
        bundle_root=bundle,
        source_root=source,
        source_evidence=evidence,
        bundle_files=("gerbers.zip", "bom.csv", "cpl.csv"),
        artifacts={"gerber": "gerbers.zip", "bom": "bom.csv", "cpl": "cpl.csv"},
        board_quantity=5,
        settings=sp.FactorySettings("jlcpcb", 4, ("top",)),
        destination=tmp_path / f"handoff{suffix}",
    )
    record = sr.submit_review(
        plan,
        reviewer="alice@example.invalid",
        reviewed_at="2026-01-01T00:00:00Z",
        policy_version="policy-v1",
        checklist=dict.fromkeys(REQUIRED, True),
        required_items=REQUIRED,
        destination=tmp_path / f"review{suffix}.json",
    )
    return {"plan": plan, "record": record, "gerber": gerber}


@pytest.fixture
def bound(tmp_path):
    """A plan/review with a real (offline, mock-protocol) bound upload receipt."""
    handoff = build_handoff(tmp_path)
    ledger = ju.UploadLedger.open(tmp_path / "uploads.jsonl")
    transport = FakeTransport(responses=[ok_response()])
    receipt = ju.upload_gerber(
        handoff["plan"],
        handoff["record"],
        ledger=ledger,
        transport=transport,
        credentials=fake_credentials(),
        requested_at="2026-01-02T00:00:00Z",
    )
    return {**handoff, "ledger": ledger, "receipt": receipt}


def good_category(**kwargs):
    defaults = {
        "category": "silkscreen-clearance",
        "count": 0,
        "limit": 10,
        "capped": False,
        "coordinates_available": True,
        "threshold": "0.1 mm",
    }
    defaults.update(kwargs)
    return fd.CategoryFinding(**defaults)


def good_transcription(**kwargs):
    defaults = {
        "provenance": "manual",
        "performed_by": "alice@example.invalid",
        "confidence": 0.95,
        "extracted_rows": 1,
        "ocr_failed": False,
        "image_only_source": True,
        "categories": (good_category(),),
        "modules": (
            fd.ModuleCoverage("pcb_fabrication", True),
            fd.ModuleCoverage("smt_assembly", True),
        ),
    }
    defaults.update(kwargs)
    return fd.TranscriptionEvidence(**defaults)


def attach(handoff, **kwargs):
    kwargs.setdefault("report_bytes", SAMPLE_REPORT)
    kwargs.setdefault("report_revision", "v27")
    kwargs.setdefault("checker_time", "2026-01-03T00:00:00Z")
    kwargs.setdefault("gerber_sha256", digest(handoff["gerber"]))
    kwargs.setdefault("app_identity", FAKE_APP_ID)
    kwargs.setdefault("endpoint", "https://open.jlcpcb.com" + ju.UPLOAD_GERBER_PATH)
    kwargs.setdefault("transcription", good_transcription())
    return fd.attach_dfm_report(**kwargs)


# ---------------------------------------------------------------------------
# Byte preservation
# ---------------------------------------------------------------------------


def test_report_bytes_preserved_and_verifiable(bound):
    attachment = attach(bound)
    assert attachment.report_sha256 == digest(SAMPLE_REPORT)
    assert attachment.report_size == len(SAMPLE_REPORT)
    fd.verify_dfm_attachment(attachment, SAMPLE_REPORT)  # does not raise


def test_tampered_report_bytes_fail_reverification(bound):
    attachment = attach(bound)
    with pytest.raises(fd.DFMError, match="no longer match"):
        fd.verify_dfm_attachment(attachment, SAMPLE_REPORT + b"tampered")


def test_report_label_is_cosmetic_never_identity(bound):
    """A filename/label alone must never stand in for hash-bound identity."""
    same_label_different_bundle = attach(
        bound, report_label="DFM_Report.pdf", gerber_sha256="a" * 64
    )
    same_label_original_bundle = attach(bound, report_label="DFM_Report.pdf")
    assert same_label_different_bundle.gerber_sha256 != same_label_original_bundle.gerber_sha256
    assert same_label_different_bundle.sha256 != same_label_original_bundle.sha256


# ---------------------------------------------------------------------------
# Stale / mismatched Gerber and receipt binding
# ---------------------------------------------------------------------------


def test_stale_v25_report_against_v27_gerber_hash_rejected(bound):
    stale_hash = digest(b"the v25 gerber bundle, not the current one")
    with pytest.raises(fd.DFMError, match="stale"):
        attach(bound, report_claimed_gerber_sha256=stale_hash)


def test_report_claimed_hash_matching_current_bundle_is_accepted(bound):
    attachment = attach(bound, report_claimed_gerber_sha256=digest(bound["gerber"]))
    assert attachment.gerber_sha256 == digest(bound["gerber"])


def test_offline_creation_has_explicit_unknown_upload_binding(bound):
    attachment = attach(bound)  # no ledger supplied at all
    assert attachment.upload_binding == "unknown"
    assert attachment.receipt_sha256 is None
    assert not attachment.readiness_eligible  # never promotable while unknown


def test_bound_receipt_matching_exact_identity_is_recorded(bound):
    attachment = attach(bound, ledger=bound["ledger"])
    assert attachment.upload_binding == "unknown"
    assert attachment.receipt_sha256 == bound["receipt"].sha256


def test_readiness_requires_both_pass_and_bound(synthetic_factory_receipt):
    bound = synthetic_factory_receipt
    passing = attach(bound, ledger=bound["ledger"], plan=bound["plan"], review=bound["record"])
    assert passing.dfm_status == "pass"
    assert passing.readiness_eligible

    offline_pass = attach(bound)  # same evidence, no ledger
    assert offline_pass.dfm_status == "pass"
    assert not offline_pass.readiness_eligible


def test_mismatched_receipt_case_report_bound_to_wrong_upload(tmp_path):
    """A ledger receipt for a *different* Gerber bundle must never bind here."""
    handoff_a = build_handoff(tmp_path, suffix="-a")
    handoff_b = build_handoff(tmp_path, suffix="-b")
    ledger = ju.UploadLedger.open(tmp_path / "uploads.jsonl")
    ju.upload_gerber(
        handoff_a["plan"],
        handoff_a["record"],
        ledger=ledger,
        transport=FakeTransport(responses=[ok_response()]),
        credentials=fake_credentials(),
        requested_at="2026-01-02T00:00:00Z",
    )
    # Attach a DFM report against bundle B's Gerber hash; the ledger only has
    # a receipt for bundle A -- this must never be silently "borrowed".
    attachment = attach(handoff_b, ledger=ledger)
    assert attachment.upload_binding == "unknown"
    assert attachment.receipt_sha256 is None


def test_receipt_stale_relative_to_current_plan_rejected(tmp_path):
    handoff_a = build_handoff(tmp_path, suffix="-a")
    handoff_b = build_handoff(tmp_path, suffix="-b")
    ledger = ju.UploadLedger.open(tmp_path / "uploads.jsonl")
    ju.upload_gerber(
        handoff_a["plan"],
        handoff_a["record"],
        ledger=ledger,
        transport=FakeTransport(responses=[ok_response()]),
        credentials=fake_credentials(),
        requested_at="2026-01-02T00:00:00Z",
    )
    # Reuse handoff A's exact Gerber hash so find_receipt() matches, but
    # supply handoff B's (different) plan as the "current" plan.
    with pytest.raises(fd.DFMError, match="stale relative to the current submission plan"):
        attach(
            handoff_a,
            ledger=ledger,
            plan=handoff_b["plan"],
        )


def test_receipt_stale_relative_to_current_review_rejected(tmp_path):
    handoff = build_handoff(tmp_path)
    ledger = ju.UploadLedger.open(tmp_path / "uploads.jsonl")
    ju.upload_gerber(
        handoff["plan"],
        handoff["record"],
        ledger=ledger,
        transport=FakeTransport(responses=[ok_response()]),
        credentials=fake_credentials(),
        requested_at="2026-01-02T00:00:00Z",
    )
    other_review = sr.submit_review(
        handoff["plan"],
        reviewer="mallory@example.invalid",
        reviewed_at="2026-01-05T00:00:00Z",
        policy_version="policy-v2",
        checklist=dict.fromkeys(REQUIRED, True),
        required_items=REQUIRED,
        destination=ledger.path.parent / "other-review.json",
    )
    with pytest.raises(fd.DFMError, match="stale relative to the current review record"):
        attach(handoff, ledger=ledger, review=other_review)


# ---------------------------------------------------------------------------
# DFM verdict: never an implicit pass
# ---------------------------------------------------------------------------


def test_raster_only_single_page_with_successful_manual_transcription_can_pass(bound):
    attachment = attach(
        bound, transcription=good_transcription(image_only_source=True, extracted_rows=3)
    )
    assert attachment.dfm_status == "pass"


def test_zero_extracted_rows_cannot_pass(bound):
    attachment = attach(bound, transcription=good_transcription(extracted_rows=0))
    assert attachment.dfm_status == "unresolved"


def test_ocr_failure_cannot_pass(bound):
    attachment = attach(
        bound,
        transcription=good_transcription(
            provenance="ocr", extracted_rows=0, ocr_failed=True, confidence=None
        ),
    )
    assert attachment.dfm_status == "unresolved"


def test_image_only_pdf_with_no_successful_transcription_cannot_pass(bound):
    attachment = attach(
        bound,
        transcription=good_transcription(
            provenance="unknown",
            performed_by="unknown",
            confidence=None,
            extracted_rows=0,
            image_only_source=True,
        ),
    )
    assert attachment.dfm_status == "unresolved"


def test_missing_smt_analysis_entirely_recorded_as_unknown_not_fabricated(bound):
    transcription = good_transcription(modules=(fd.ModuleCoverage("pcb_fabrication", True),))
    attachment = attach(bound, transcription=transcription)
    coverage = {module.module: module.covered for module in attachment.modules}
    assert coverage == {"pcb_fabrication": True, "smt_assembly": None}


def test_absent_per_category_details_cannot_pass(bound):
    attachment = attach(bound, transcription=good_transcription(categories=()))
    assert attachment.dfm_status == "unresolved"


def test_count_cap_truncation_flag_blocks_pass(bound):
    capped = good_category(category="hole-size", count=50, limit=50, capped=True)
    attachment = attach(bound, transcription=good_transcription(categories=(capped,)))
    assert attachment.dfm_status == "unresolved"


def test_category_exceeding_its_limit_is_a_definite_fail(bound):
    over_limit = good_category(category="trace-width", count=12, limit=10, capped=False)
    attachment = attach(bound, transcription=good_transcription(categories=(over_limit,)))
    assert attachment.dfm_status == "fail"


def test_category_missing_count_or_limit_is_unresolved_not_pass(bound):
    unknown_count = good_category(category="annular-ring", count=None, limit=5)
    attachment = attach(bound, transcription=good_transcription(categories=(unknown_count,)))
    assert attachment.dfm_status == "unresolved"


# ---------------------------------------------------------------------------
# Hash binding and determinism
# ---------------------------------------------------------------------------


def test_attachment_hash_is_deterministic_and_content_bound(bound):
    first = attach(bound)
    second = attach(bound)
    assert first.sha256 == second.sha256
    assert first.attachment_bytes == second.attachment_bytes

    different = attach(bound, checker_time="2026-01-04T00:00:00Z")
    assert different.sha256 != first.sha256


def test_states_property_reflects_dfm_and_binding(bound):
    attachment = attach(bound, ledger=bound["ledger"])
    assert attachment.states == {"dfm": "pass", "upload_binding": "unknown"}


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_empty_report_bytes_rejected(bound):
    with pytest.raises(fd.DFMError):
        attach(bound, report_bytes=b"")


def test_blank_revision_rejected(bound):
    with pytest.raises(fd.DFMError):
        attach(bound, report_revision="  ")


def test_blank_checker_time_rejected(bound):
    with pytest.raises(fd.DFMError):
        attach(bound, checker_time="")


def test_malformed_gerber_hash_rejected(bound):
    with pytest.raises(fd.DFMError):
        attach(bound, gerber_sha256="not-a-hash")


def test_blank_app_identity_rejected(bound):
    with pytest.raises(fd.DFMError):
        attach(bound, app_identity="")


def test_blank_endpoint_rejected(bound):
    with pytest.raises(fd.DFMError):
        attach(bound, endpoint="")


def test_blank_report_label_rejected_when_supplied(bound):
    with pytest.raises(fd.DFMError):
        attach(bound, report_label="   ")


@pytest.mark.parametrize("provenance", ["", "guessed", "OCR", None])
def test_invalid_provenance_rejected(bound, provenance):
    with pytest.raises(fd.DFMError):
        attach(bound, transcription=good_transcription(provenance=provenance))


def test_blank_performed_by_rejected(bound):
    with pytest.raises(fd.DFMError):
        attach(bound, transcription=good_transcription(performed_by=""))


@pytest.mark.parametrize("confidence", [-0.1, 1.1, "high"])
def test_invalid_confidence_rejected(bound, confidence):
    with pytest.raises(fd.DFMError):
        attach(bound, transcription=good_transcription(confidence=confidence))


def test_negative_extracted_rows_rejected(bound):
    with pytest.raises(fd.DFMError):
        attach(bound, transcription=good_transcription(extracted_rows=-1))


def test_non_bool_ocr_failed_rejected(bound):
    with pytest.raises(fd.DFMError):
        attach(bound, transcription=good_transcription(ocr_failed=1))


def test_duplicate_category_name_rejected(bound):
    duplicate = (good_category(), good_category())
    with pytest.raises(fd.DFMError, match="Duplicate DFM category"):
        attach(bound, transcription=good_transcription(categories=duplicate))


def test_duplicate_module_entry_rejected(bound):
    duplicate = (
        fd.ModuleCoverage("pcb_fabrication", True),
        fd.ModuleCoverage("pcb_fabrication", False),
    )
    with pytest.raises(fd.DFMError, match="Duplicate DFM module"):
        attach(bound, transcription=good_transcription(modules=duplicate))


def test_unrecognized_module_name_rejected(bound):
    bad = (fd.ModuleCoverage("solder-paste-inspection", True),)
    with pytest.raises(fd.DFMError):
        attach(bound, transcription=good_transcription(modules=bad))


def test_capped_true_without_observed_count_rejected(bound):
    bad = good_category(category="capped-but-unknown", count=None, capped=True)
    with pytest.raises(fd.DFMError, match="observed count"):
        attach(bound, transcription=good_transcription(categories=(bad,)))


def test_negative_category_count_rejected(bound):
    bad = good_category(count=-1)
    with pytest.raises(fd.DFMError):
        attach(bound, transcription=good_transcription(categories=(bad,)))


def test_blank_category_threshold_rejected_when_supplied(bound):
    bad = good_category(threshold="   ")
    with pytest.raises(fd.DFMError):
        attach(bound, transcription=good_transcription(categories=(bad,)))


@pytest.mark.parametrize("evidence", [ju.EVIDENCE_MOCK, ju.EVIDENCE_RECONCILED])
def test_nonfactory_receipts_never_allow_readiness(bound, monkeypatch, evidence):
    from dataclasses import replace

    receipt = replace(bound["receipt"], evidence=evidence)
    monkeypatch.setattr(ju, "find_receipt", lambda *args, **kwargs: receipt)
    result = attach(bound, ledger=bound["ledger"], plan=bound["plan"], review=bound["record"])
    assert not result.readiness_eligible
    assert result.upload_binding == "unknown"
    assert result.receipt_evidence == evidence
    assert result.receipt_sha256 == receipt.sha256


@pytest.fixture
def synthetic_factory_receipt(bound, monkeypatch):
    """Model the future receipt consumer only; never call/enable live transport."""
    from dataclasses import replace

    receipt = replace(bound["receipt"], evidence=ju.EVIDENCE_LIVE)
    monkeypatch.setattr(ju, "find_receipt", lambda *args, **kwargs: receipt)
    return bound


def test_factory_receipt_requires_current_verified_review(synthetic_factory_receipt):
    handoff = synthetic_factory_receipt
    assert not attach(handoff, ledger=handoff["ledger"]).readiness_eligible
    result = attach(
        handoff, ledger=handoff["ledger"], plan=handoff["plan"], review=handoff["record"]
    )
    assert result.readiness_eligible
    (handoff["plan"].source_root / "board.kicad_pcb").write_bytes(b"changed after approval")
    assert not result.readiness_eligible
    with pytest.raises(fd.DFMError, match="review|source"):
        attach(handoff, ledger=handoff["ledger"], plan=handoff["plan"], review=handoff["record"])


def test_changed_published_bundle_rejects_readiness(synthetic_factory_receipt):
    handoff = synthetic_factory_receipt
    result = attach(
        handoff, ledger=handoff["ledger"], plan=handoff["plan"], review=handoff["record"]
    )
    core = json.loads(handoff["plan"].plan_bytes)
    output = handoff["plan"].directory / core["artifacts"]["gerber"]["output_name"]
    output.chmod(0o600)
    output.write_bytes(b"changed")
    assert not result.readiness_eligible
    with pytest.raises(fd.DFMError, match="handoff"):
        attach(handoff, ledger=handoff["ledger"], plan=handoff["plan"], review=handoff["record"])


def test_coordinate_evidence_preserved_in_attachment_identity(bound):
    from dataclasses import replace

    coordinate = fd.FindingCoordinate(x="12.340", y="-2.5", units="mm", context="F.SilkS R1")
    category = good_category(coordinates=(coordinate,))
    result = attach(bound, transcription=good_transcription(categories=(category,)))
    encoded = json.loads(result.attachment_bytes)["transcription"]["categories"][0]
    assert encoded["coordinates"] == [
        {"x": "12.340", "y": "-2.5", "units": "mm", "context": "F.SilkS R1"}
    ]
    changed = replace(category, coordinates=(replace(coordinate, x="12.341"),))
    assert (
        attach(bound, transcription=good_transcription(categories=(changed,))).sha256
        != result.sha256
    )
    unknown = good_category(coordinates_available=None, coordinates=None, threshold=None)
    unknown_encoded = json.loads(
        attach(bound, transcription=good_transcription(categories=(unknown,))).attachment_bytes
    )["transcription"]["categories"][0]
    assert unknown_encoded["coordinates"] is None
    assert unknown_encoded["coordinates_available"] is None
    assert unknown_encoded["threshold"] is None


@pytest.mark.parametrize("missing", ["plan", "review"])
def test_partial_review_chain_is_not_ready(synthetic_factory_receipt, missing):
    handoff = synthetic_factory_receipt
    options = {"ledger": handoff["ledger"], "plan": handoff["plan"], "review": handoff["record"]}
    options.pop(missing)
    assert not attach(handoff, **options).readiness_eligible


def test_review_content_is_verified_even_when_receipt_hash_matches(
    synthetic_factory_receipt, monkeypatch
):
    from dataclasses import replace

    handoff = synthetic_factory_receipt
    core = json.loads(handoff["record"].record_bytes)
    core["checklist"][REQUIRED[0]] = False
    review = replace(handoff["record"], record_bytes=sp._json(core))
    receipt = replace(handoff["receipt"], evidence=ju.EVIDENCE_LIVE, review_sha256=review.sha256)
    monkeypatch.setattr(ju, "find_receipt", lambda *args, **kwargs: receipt)
    with pytest.raises(fd.DFMError, match="review"):
        attach(handoff, ledger=handoff["ledger"], plan=handoff["plan"], review=review)


def test_receipt_rechecked_after_attachment(synthetic_factory_receipt, monkeypatch):
    handoff = synthetic_factory_receipt
    result = attach(
        handoff, ledger=handoff["ledger"], plan=handoff["plan"], review=handoff["record"]
    )
    assert result.readiness_eligible
    monkeypatch.setattr(ju, "find_receipt", lambda *args, **kwargs: None)
    assert not result.readiness_eligible


@pytest.mark.parametrize(
    "coordinate", [fd.FindingCoordinate("", "1", "mm"), fd.FindingCoordinate("1", "2", 1)]
)
def test_invalid_coordinate_text_rejected(bound, coordinate):
    with pytest.raises(fd.DFMError, match="Coordinate"):
        attach(
            bound,
            transcription=good_transcription(
                categories=(good_category(coordinates=(coordinate,)),)
            ),
        )
