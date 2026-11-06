"""Offline tests for the submit-jlcpcb workflow; any network contact fails the test."""

import hashlib
import json
from datetime import datetime, timezone

import pytest

from kicad_tools.export import submission_workflow as wf
from kicad_tools.parts.jlcpcb_api import ComponentInventory
from tests.test_submission_plan import csv_bytes

PCB = (
    '(kicad_pcb\n  (layers\n    (0 "F.Cu" signal)\n    (1 "In1.Cu" signal)\n'
    '    (2 "In2.Cu" signal)\n    (31 "B.Cu" signal)\n    (44 "Edge.Cuts" user)\n  )\n)\n'
)


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Source:
    def __init__(self, stock):
        self.stock = stock
        self.calls = 0

    def get_component_inventory(self, codes):
        self.calls += 1
        rows = tuple(
            {"componentCode": c, "stockCount": self.stock[c]} for c in codes if c in self.stock
        )
        return ComponentInventory(rows, datetime.now(timezone.utc))


class Boom:
    def get_component_inventory(self, codes):
        pytest.fail("network contacted")


@pytest.fixture
def env(tmp_path):
    bundle, src = tmp_path / "bundle", tmp_path / "src"
    bundle.mkdir()
    src.mkdir()
    (src / "b.kicad_pcb").write_text(PCB)
    (src / "b.kicad_sch").write_text("(kicad_sch)\n")
    (bundle / "bom.csv").write_bytes(
        csv_bytes(
            ["Comment", "Designator", "Footprint", "LCSC Part #", "Quantity"],
            [["10k", "R1,R2", "R_0402", "C1", 2], ["1u", "C1", "C_0402", "C2", 1]],
        )
    )
    (bundle / "cpl.csv").write_bytes(
        csv_bytes(
            ["Designator", "Val", "Package", "Mid X", "Mid Y", "Rotation", "Layer"],
            [
                ["R1", "10k", "R_0402", "1mm", "1mm", "0", "Top"],
                ["R2", "10k", "R_0402", "2mm", "1mm", "0", "Top"],
                ["C1", "1u", "C_0402", "3mm", "1mm", "0", "Top"],
            ],
        )
    )
    (bundle / "gerbers.zip").write_bytes(b"PK\x03\x04gerber")
    return {"bundle": bundle, "src": src, "out": tmp_path / "out"}


def write_manifest(env, signoff=True, board_sha=None):
    files = {
        p.name: {"sha256": sha(p.read_bytes()), "size": p.stat().st_size}
        for p in env["bundle"].iterdir()
        if p.name != "manifest.json"
    }
    tapeout = {"board_sha256": board_sha or sha((env["src"] / "b.kicad_pcb").read_bytes())}
    if signoff:
        tapeout["human_signoff"] = {"reviewer": "Ann", "signed_at": "2026-11-01T00:00:00Z"}
    (env["bundle"] / "manifest.json").write_text(json.dumps({"files": files, "tapeout": tapeout}))


def run(env, source=None, **kw):
    return wf.run_submission(
        bundle_root=env["bundle"],
        board=env["src"] / "b.kicad_pcb",
        schematic=env["src"] / "b.kicad_sch",
        quantity=kw.pop("quantity", 10),
        output_dir=env["out"],
        inventory_source=source,
        **kw,
    )


@pytest.mark.parametrize("fault", ["tamper", "nosignoff", "boardsha"])
def test_preflight_refusals_make_no_network_calls(env, fault):
    write_manifest(
        env, signoff=fault != "nosignoff", board_sha="0" * 64 if fault == "boardsha" else None
    )
    if fault == "tamper":
        (env["bundle"] / "bom.csv").write_bytes(b"changed")
    with pytest.raises(wf.WorkflowError):
        run(env, Boom(), dry_run=True)
    assert not (env["out"] / "handoff").exists()


def test_settings_come_from_board_and_cpl(env):
    write_manifest(env)
    facts = wf.preflight(env["bundle"], env["src"] / "b.kicad_pcb")
    assert facts.board_layers == 4
    assert facts.assembly_sides == ("top",)


def test_dry_run_passes_and_writes_review_page(env):
    write_manifest(env)
    source = Source({"C1": 100, "C2": 100})
    result = run(env, source, dry_run=True)
    assert not result.stock.blocked
    assert result.states == {
        "prepared": "prepared",
        "stock_check": "stock-checked",
        "upload": "not-performed",
    }
    index = (result.page.directory / "index.md").read_text()
    assert "## Stock check" in index and "ordered" not in index.lower().replace("order: not", "")
    plan = json.loads(result.plan.plan_bytes)
    assert plan["settings"]["board_layers"] == 4
    assert plan["board_quantity"] == 10


def test_short_and_unknown_stock_block_and_are_listed(env):
    write_manifest(env)
    result = run(env, Source({"C1": 5}), dry_run=True)  # C1 needs 20, C2 unreturned
    assert result.stock.blocked
    failures = wf.format_stock_failures(result.stock)
    assert any("C1: need 20, stock 5" in f for f in failures)
    assert any("C2: need 10, stock unknown" in f for f in failures)
    assert result.states["stock_check"] == "blocked"


def test_no_credentials_means_unknown_blocks(env):
    write_manifest(env)
    result = run(env, None, dry_run=True)
    assert result.stock.blocked
    assert {line.status for line in result.stock.lines} == {"unknown"}


def test_inventory_covered_line_is_acknowledged(env):
    write_manifest(env)
    result = run(env, Source({"C1": 100}), dry_run=True, inventory_covered=["C2"])
    assert not result.stock.blocked
    assert {line.catalog_id: line.status for line in result.stock.lines} == {
        "C1": "ok",
        "C2": "covered",
    }


def test_upload_refused_when_blocked_or_dry_run(env):
    write_manifest(env)
    with pytest.raises(wf.WorkflowError):
        run(env, Source({}), dry_run=True, upload=True, reviewer="A", credentials=object())
    with pytest.raises(wf.WorkflowError, match="Stock gate blocked"):
        run(env, Source({}), dry_run=False, upload=True, reviewer="A", credentials=object())


def test_upload_leg_records_receipt_via_mock_transport(env):
    from kicad_tools.manufacturers import jlc_upload

    write_manifest(env)
    creds = wf.JLCCredentials("app", "ak", "sk")
    result = run(
        env,
        Source({"C1": 100, "C2": 100}),
        dry_run=False,
        upload=True,
        reviewer="Ann",
        credentials=creds,
        transport=MockTransport(),
    )
    assert result.upload["state"] != "not-performed"
    assert result.upload["factory_receipt"] is False
    ledger = jlc_upload.UploadLedger.open(env["out"] / "upload-ledger.jsonl")
    assert len(ledger.attempts()) == 1
    assert "ordered" not in result.states.values()


class MockTransport:
    """Scripted protocol responses; never touches a socket."""

    def __init__(self):
        from kicad_tools.manufacturers import jlc_upload as ju

        self.identity = ju.TransportIdentity("fake-mock-protocol", False)
        self._responses = [
            ju.TransportResponse(
                200, {}, json.dumps({"code": 200, "success": True, "data": "key-1"}).encode()
            ),
            ju.TransportResponse(
                200,
                {},
                json.dumps({"code": 200, "success": True, "data": {"layers": 4}}).encode(),
            ),
        ]
        self.calls = 0

    def _next(self):
        self.calls += 1
        return self._responses.pop(0)

    def post_multipart(self, url, *, headers, fields, files):
        return self._next()

    def post_json(self, url, *, headers, body):
        return self._next()


def test_review_page_renders_site_handoff_without_order_state(env):
    from kicad_tools.export.submission_review import render_review_page

    write_manifest(env)
    result = run(env, Source({"C1": 100, "C2": 100}), dry_run=True)
    site = {
        "state": "in-cart",
        "driver_version": "jlc-site-driver-1",
        "cart_url": "https://cart.jlcpcb.com/x",
        "quote_total": "$42.10",
        "lead_time": "5 days",
        "uploaded_hashes": {"bom.csv": "ab" * 32},
        "steps": [{"name": "upload-bom", "screenshot": "01-upload-bom.png", "detail": {}}],
        "match_diff": [],
        "remaining_human_steps": ["Place and pay for the order yourself"],
    }
    page = render_review_page(result.plan, destination=env["out"] / "cart-page", site=site)
    text = (page.directory / "index.md").read_text()
    assert "https://cart.jlcpcb.com/x" in text and "$42.10" in text and "5 days" in text
    assert "- cart: in-cart" in text and "- factory_matching: factory-matched" in text
    assert "order: ordered" not in text
    assert "- [ ] Place and pay for the order yourself" in text


# --- Issue #8537: sign-off producer, split preflight errors, stock export shape ---


def export_shaped_manifest(env):
    """Manifest as ``kct export`` writes it: files only, no ``tapeout`` block."""
    bundle = env["bundle"]
    files = {
        p.relative_to(bundle).as_posix(): {"sha256": sha(p.read_bytes()), "size": p.stat().st_size}
        for p in bundle.rglob("*")
        if p.is_file() and p.name != "manifest.json"
    }
    (bundle / "manifest.json").write_text(json.dumps({"version": "1.0", "files": files}))


def test_export_bundle_plus_signoff_passes_preflight_and_dry_run(env):
    (env["bundle"] / "gerbers").mkdir()
    (env["bundle"] / "gerbers" / "gerbers.zip").write_bytes(b"PK\x03\x04gerber")
    (env["bundle"] / "gerbers.zip").unlink()
    (env["bundle"] / "kicad_project.zip").write_bytes(b"PK\x03\x04project")
    export_shaped_manifest(env)
    board = env["src"] / "b.kicad_pcb"
    with pytest.raises(wf.WorkflowError, match="human sign-off"):
        wf.preflight(env["bundle"], board)
    wf.sign_off_bundle(env["bundle"], board, "Ann")
    facts = wf.preflight(env["bundle"], board)
    assert facts.artifacts["gerber"] == "gerbers/gerbers.zip"
    result = run(env, Source({"C1": 100, "C2": 100}), dry_run=True)
    assert not result.stock.blocked


def test_signoff_is_idempotent_and_updates_signed_at(env):
    export_shaped_manifest(env)
    board = env["src"] / "b.kicad_pcb"
    wf.sign_off_bundle(env["bundle"], board, "Ann", signed_at="2026-01-01T00:00:00Z")
    rec = wf.sign_off_bundle(env["bundle"], board, "Bob", signed_at="2026-02-01T00:00:00Z")
    assert rec["human_signoff"] == {"reviewer": "Bob", "signed_at": "2026-02-01T00:00:00Z"}
    wf.preflight(env["bundle"], board)


@pytest.mark.parametrize("reviewer", ["", "   "])
def test_signoff_refuses_blank_reviewer(env, reviewer):
    export_shaped_manifest(env)
    with pytest.raises(wf.WorkflowError, match="reviewer"):
        wf.sign_off_bundle(env["bundle"], env["src"] / "b.kicad_pcb", reviewer)
    assert "tapeout" not in json.loads((env["bundle"] / "manifest.json").read_text())


def test_signoff_refuses_extra_or_missing_bundle_file(env):
    export_shaped_manifest(env)
    board = env["src"] / "b.kicad_pcb"
    (env["bundle"] / "extra.txt").write_text("x")
    with pytest.raises(wf.WorkflowError, match="extra.txt"):
        wf.sign_off_bundle(env["bundle"], board, "Ann")
    (env["bundle"] / "extra.txt").unlink()
    (env["bundle"] / "cpl.csv").unlink()
    with pytest.raises(wf.WorkflowError, match="cpl.csv"):
        wf.sign_off_bundle(env["bundle"], board, "Ann")


def test_board_changed_after_signoff_refuses(env):
    export_shaped_manifest(env)
    board = env["src"] / "b.kicad_pcb"
    wf.sign_off_bundle(env["bundle"], board, "Ann")
    board.write_text(PCB + "; edited\n")
    with pytest.raises(wf.WorkflowError, match="board_sha256"):
        wf.preflight(env["bundle"], board)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda m: m["files"].__setitem__("bom.csv", "oops"), "bom.csv is not an object"),
        (lambda m: m["files"]["bom.csv"].pop("size"), "bom.csv is missing required key 'size'"),
        (lambda m: m["files"]["bom.csv"].pop("sha256"), "bom.csv is missing required key 'sha256'"),
        (
            lambda m: m["files"]["bom.csv"].__setitem__("sha256", "0" * 64),
            "sha256 mismatch for bom.csv",
        ),
        (lambda m: m["files"]["bom.csv"].__setitem__("size", 1), "size mismatch for bom.csv"),
    ],
)
def test_preflight_distinct_record_errors(env, mutate, message):
    write_manifest(env)
    path = env["bundle"] / "manifest.json"
    manifest = json.loads(path.read_text())
    mutate(manifest)
    path.write_text(json.dumps(manifest))
    with pytest.raises(wf.WorkflowError) as err:
        wf.preflight(env["bundle"], env["src"] / "b.kicad_pcb")
    assert message.replace(" is not an object", "") in str(err.value) or message in str(err.value)
