"""Offline transport/state tests for the JLCPCB Gerber upload slice (#5145).

No test in this file performs a real network request, and no test constructs
or reads a real credential -- every credential here is obviously fake. Every
transport is injected, so the *real* signing, MD5, multipart-field and
ledger/state-machine logic is exercised against a fake HTTP layer.

A mocked protocol success is never a factory receipt: the fake transports
below declare ``live=False`` unless a test is specifically checking the
labeling difference, and the assertions below pin that distinction.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re

import pytest

from kicad_tools.export import submission_plan as sp
from kicad_tools.export import submission_review as sr
from kicad_tools.manufacturers import jlc_upload as ju
from kicad_tools.parts import jlcpcb_api

FAKE_APP_ID = "fake-app-id"
FAKE_ACCESS_KEY = "fake-access-key"
FAKE_SECRET_KEY = "fake-secret-key-do-not-use"

REQUIRED = ("gerber-visually-inspected", "bom-cross-checked", "quantities-confirmed")

AUTH_FIELD = re.compile(r'(\w+)="([^"]*)"')


def fake_credentials() -> jlcpcb_api.JLCCredentials:
    return jlcpcb_api.JLCCredentials(FAKE_APP_ID, FAKE_ACCESS_KEY, FAKE_SECRET_KEY)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def csv_bytes(headers, rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    writer.writerow(headers)
    writer.writerows(rows)
    return b"\xef\xbb\xbf" + stream.getvalue().encode()


def response(status=200, body=None, headers=None, content=None):
    payload = content if content is not None else json.dumps(body).encode()
    return ju.TransportResponse(status, dict(headers or {}), payload)


def ok_body(key="file-key-0001"):
    return {"code": 200, "success": True, "message": "success", "data": key}


class FakeTransport:
    """Records every call; never touches a socket.

    ``live`` defaults to ``False`` -- a mock protocol response must never be
    labeled a real factory receipt.
    """

    def __init__(self, *, responses=None, error=None, live=False, name="fake-mock-protocol"):
        self.identity = ju.TransportIdentity(name, live)
        self.multipart_calls: list[dict] = []
        self.json_calls: list[dict] = []
        self._responses = list(responses or [])
        self._error = error
        self.before_call = None

    def _next(self):
        if self._error is not None:
            raise self._error
        if not self._responses:
            raise AssertionError("FakeTransport ran out of scripted responses")
        return self._responses.pop(0)

    def post_multipart(self, url, *, headers, fields, files):
        if self.before_call is not None:
            self.before_call()
        self.multipart_calls.append(
            {"url": url, "headers": dict(headers), "fields": dict(fields), "files": dict(files)}
        )
        return self._next()

    def post_json(self, url, *, headers, body):
        self.json_calls.append({"url": url, "headers": dict(headers), "body": body})
        return self._next()


@pytest.fixture
def handoff(tmp_path):
    """A published plan + valid review record, ready to upload."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    source = tmp_path / "source"
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
    gerber = b"PK\x03\x04\x00synthetic-exact-zip-bytes\xff\r\n"
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
        destination=tmp_path / "handoff",
    )
    record = sr.submit_review(
        plan,
        reviewer="alice@example.invalid",
        reviewed_at="2026-01-01T00:00:00Z",
        policy_version="policy-v1",
        checklist=dict.fromkeys(REQUIRED, True),
        required_items=REQUIRED,
        destination=tmp_path / "review.json",
    )
    ledger = ju.UploadLedger.open(tmp_path / "uploads.jsonl")
    return {
        "tmp": tmp_path,
        "plan": plan,
        "record": record,
        "ledger": ledger,
        "gerber": gerber,
        "source": source,
        "bundle": bundle,
    }


def upload(handoff, transport, **kwargs):
    kwargs.setdefault("requested_at", "2026-01-02T00:00:00Z")
    return ju.upload_gerber(
        handoff["plan"],
        kwargs.pop("record_override", handoff["record"]),
        ledger=kwargs.pop("ledger", handoff["ledger"]),
        transport=transport,
        credentials=kwargs.pop("credentials", fake_credentials()),
        **kwargs,
    )


def auth_fields(headers):
    value = headers["Authorization"]
    scheme, _, rest = value.partition(" ")
    return scheme, dict(AUTH_FIELD.findall(rest))


# ---------------------------------------------------------------------------
# Transport-layer wire format
# ---------------------------------------------------------------------------


def test_upload_sends_exact_file_bytes_and_plan_upload_name(handoff):
    transport = FakeTransport(responses=[response(200, ok_body())])
    receipt = upload(handoff, transport)

    assert len(transport.multipart_calls) == 1
    call = transport.multipart_calls[0]
    assert call["url"] == "https://open.jlcpcb.com/overseas/openapi/pcb/uploadGerber"
    filename, content, content_type = call["files"]["file"]
    # Exact bytes of the published handoff's own Gerber output, byte for byte.
    assert content == handoff["gerber"]
    assert content == (handoff["plan"].directory / "gerbers.zip").read_bytes()
    assert content_type == "application/octet-stream"
    # The upload filename comes from the plan, never guessed here.
    assert filename == "gerbers.zip"
    assert receipt.file_sha256 == digest(handoff["gerber"])
    assert receipt.file_key == "file-key-0001"


def test_metadata_json_field_is_signed_not_the_multipart_body(handoff):
    transport = FakeTransport(responses=[response(200, ok_body())])
    upload(handoff, transport)

    call = transport.multipart_calls[0]
    assert call["fields"] == {"meta": "{}"}
    scheme, fields = auth_fields(call["headers"])
    assert scheme == jlcpcb_api.AUTH_SCHEME
    assert fields["appid"] == FAKE_APP_ID
    assert fields["accesskey"] == FAKE_ACCESS_KEY
    assert re.fullmatch(r"[0-9a-f]{32}", fields["nonce"])
    assert re.fullmatch(r"[0-9]{10,}", fields["timestamp"])

    # Independently recompute the signature over the METADATA JSON string.
    string_to_sign = (
        f"POST\n{ju.UPLOAD_GERBER_PATH}\n{fields['timestamp']}\n{fields['nonce']}\n" + "{}\n"
    )
    expected = jlcpcb_api._sign(FAKE_SECRET_KEY, string_to_sign)
    assert fields["signature"] == expected

    # ... and NOT over the file bytes or an encoded multipart body.
    file_body_sig = jlcpcb_api._sign(
        FAKE_SECRET_KEY,
        f"POST\n{ju.UPLOAD_GERBER_PATH}\n{fields['timestamp']}\n{fields['nonce']}\n"
        + handoff["gerber"].decode("latin-1")
        + "\n",
    )
    assert fields["signature"] != file_body_sig

    # The secret key itself is never transmitted.
    assert FAKE_SECRET_KEY not in json.dumps(call["headers"])


def test_content_md5_is_lowercase_hex_of_raw_file_bytes(handoff):
    transport = FakeTransport(responses=[response(200, ok_body())])
    upload(handoff, transport)

    header = transport.multipart_calls[0]["headers"]["Content-MD5"]
    expected = hashlib.md5(handoff["gerber"], usedforsecurity=False).hexdigest()
    assert header == expected
    assert header == header.lower()
    assert re.fullmatch(r"[0-9a-f]{32}", header)


def test_module_never_hand_rolls_a_multipart_boundary(handoff):
    transport = FakeTransport(responses=[response(200, ok_body())])
    upload(handoff, transport)

    call = transport.multipart_calls[0]
    # No Content-Type header at all: the HTTP library must generate the
    # multipart content type (and therefore its own boundary).
    assert not [name for name in call["headers"] if name.lower() == "content-type"]
    rendered = json.dumps({"headers": call["headers"], "fields": call["fields"]}).lower()
    assert "boundary" not in rendered
    assert "multipart" not in rendered
    # Structural: the module passes discrete fields/files, never a body blob.
    assert set(call["files"]) == {"file"}
    assert set(call["fields"]) == {"meta"}


def test_requests_transport_delegates_multipart_encoding_to_the_library():
    requests = pytest.importorskip("requests")

    class FakeSession:
        def __init__(self):
            self.calls = []

        def close(self):
            self.closed = True

        def post(self, url, **kwargs):
            self.calls.append({"url": url, **kwargs})

            class Resp:
                status_code = 200
                headers = {"J-Trace-ID": "trace-1"}
                content = json.dumps(ok_body()).encode()

            return Resp()

    session = FakeSession()
    transport = ju.RequestsUploadTransport(session=session)
    assert transport.identity.live is True

    transport.post_multipart(
        "https://example.invalid/x",
        headers={"Content-MD5": "abc"},
        fields={"meta": "{}"},
        files={"file": ("gerbers.zip", b"bytes\x00\xff", "application/octet-stream")},
    )
    call = session.calls[0]
    # requests' own encoder is handed the discrete parts (so it generates the
    # boundary); the transport never builds a body string itself.
    assert call["data"] == {"meta": "{}"}
    assert call["files"] == {"file": ("gerbers.zip", b"bytes\x00\xff", "application/octet-stream")}
    assert not [name for name in call["headers"] if name.lower() == "content-type"]

    # Prove the library-generated encoding carries the exact bytes.
    prepared = requests.Request(
        "POST", "https://example.invalid/x", data=call["data"], files=call["files"]
    ).prepare()
    assert b"boundary=" in prepared.headers["Content-Type"].encode()
    assert b"bytes\x00\xff" in prepared.body

    # Closing must never leave the transport able to resurrect a LIVE session
    # behind an injected one.
    with transport as entered:
        assert entered is transport
    assert session.closed is True
    with pytest.raises(ju.TransportError) as closed:
        transport.post_json("https://example.invalid/x", headers={}, body=b"{}")
    assert closed.value.request_sent is False


# ---------------------------------------------------------------------------
# Pre-network gates
# ---------------------------------------------------------------------------


def test_invalid_review_blocks_any_network_call(handoff, tmp_path):
    other = sr.submit_review(
        handoff["plan"],
        reviewer="mallory@example.invalid",
        reviewed_at="2026-01-01T00:00:00Z",
        policy_version="policy-v1",
        checklist=dict.fromkeys(REQUIRED, True),
        required_items=REQUIRED,
        destination=tmp_path / "review-2.json",
    )
    # Hand-edit the record so its bound plan hash no longer matches.
    tampered = json.loads(other.record_bytes)
    tampered["plan_sha256"] = "0" * 64
    forged = sr.ReviewRecord(other.path, json.dumps(tampered).encode())

    transport = FakeTransport(responses=[response(200, ok_body())])
    with pytest.raises(ju.UploadGateError):
        upload(handoff, transport, record_override=forged)
    assert transport.multipart_calls == []
    assert handoff["ledger"].events() == []


def test_changed_source_bytes_block_any_network_call(handoff):
    (handoff["source"] / "board.kicad_pcb").write_bytes(b"edited after review\r\n")
    transport = FakeTransport(responses=[response(200, ok_body())])
    with pytest.raises(ju.UploadGateError):
        upload(handoff, transport, source_root=handoff["source"])
    assert transport.multipart_calls == []
    assert handoff["ledger"].events() == []


def test_mutated_published_gerber_blocks_any_network_call(handoff):
    published = handoff["plan"].directory
    published.chmod(0o755)
    (published / "gerbers.zip").chmod(0o644)
    (published / "gerbers.zip").write_bytes(b"tampered bytes")

    transport = FakeTransport(responses=[response(200, ok_body())])
    with pytest.raises(ju.UploadGateError):
        upload(handoff, transport)
    assert transport.multipart_calls == []
    assert handoff["ledger"].events() == []


def test_intent_is_persisted_before_the_request_is_sent(handoff):
    seen: list[list[dict]] = []
    transport = FakeTransport(responses=[response(200, ok_body())])
    transport.before_call = lambda: seen.append(handoff["ledger"].events())
    upload(handoff, transport)

    assert len(seen) == 1
    (intent,) = seen[0]
    assert intent["kind"] == "intent"
    assert intent["file"]["sha256"] == digest(handoff["gerber"])
    assert intent["app_identity"] == FAKE_APP_ID
    assert intent["endpoint"].endswith(ju.UPLOAD_GERBER_PATH)


# ---------------------------------------------------------------------------
# Response classification
# ---------------------------------------------------------------------------


def test_http_200_with_business_error_is_a_failure(handoff):
    body = {"code": 4001, "success": False, "message": "gerber parse failed", "data": None}
    transport = FakeTransport(responses=[response(200, body)])
    with pytest.raises(ju.UploadRejectedError) as excinfo:
        upload(handoff, transport)
    assert "gerber parse failed" in str(excinfo.value)

    events = handoff["ledger"].events()
    assert [e["kind"] for e in events] == ["intent", "failure"]
    assert events[1]["http_status"] == 200
    assert events[1]["code"] == 4001
    assert (
        ju.upload_state(
            handoff["ledger"],
            file_sha256=digest(handoff["gerber"]),
            app_identity=FAKE_APP_ID,
            endpoint=jlcpcb_api.JLC_OPENAPI_BASE + ju.UPLOAD_GERBER_PATH,
        )
        == ju.STATE_FAILED
    )
    assert (
        ju.find_receipt(
            handoff["ledger"],
            file_sha256=digest(handoff["gerber"]),
            app_identity=FAKE_APP_ID,
            endpoint=jlcpcb_api.JLC_OPENAPI_BASE + ju.UPLOAD_GERBER_PATH,
        )
        is None
    )


def test_http_200_success_flag_without_code_200_is_a_failure(handoff):
    transport = FakeTransport(responses=[response(200, {"success": True, "data": "k"})])
    with pytest.raises(ju.UploadRejectedError):
        upload(handoff, transport)
    assert [e["kind"] for e in handoff["ledger"].events()] == ["intent", "failure"]


def test_http_403_preserves_only_safe_reason_and_trace_id(handoff):
    leaky = (
        "Forbidden: IP not in whitelist. echoed-authorization="
        f'JOP appid="{FAKE_APP_ID}", accesskey="{FAKE_ACCESS_KEY}" '
        f"secret={FAKE_SECRET_KEY} <html><body>raw-payload-marker</body></html>"
    )
    body = {"code": 403, "success": False, "message": leaky}
    transport = FakeTransport(
        responses=[response(403, body, headers={"J-Trace-ID": "abc-123_TRACE"})]
    )
    with pytest.raises(ju.UploadRejectedError) as excinfo:
        upload(handoff, transport)

    text = str(excinfo.value)
    assert "abc-123_TRACE" in text
    assert excinfo.value.trace_id == "abc-123_TRACE"
    assert excinfo.value.classification == "ip-not-whitelisted"
    for secret in (FAKE_SECRET_KEY, FAKE_ACCESS_KEY):
        assert secret not in text
    assert "raw-payload-marker" not in text

    ledger_text = handoff["ledger"].path.read_text()
    for secret in (FAKE_SECRET_KEY, FAKE_ACCESS_KEY):
        assert secret not in ledger_text
    assert "raw-payload-marker" not in ledger_text
    assert "abc-123_TRACE" in ledger_text
    # The whole server reason is withheld once redaction fired -- the fixed
    # classification word is what survives, not the server's prose.
    assert "withheld" in ledger_text
    assert '"ip-not-whitelisted"' in ledger_text


def test_unsafe_trace_id_header_is_discarded(handoff):
    transport = FakeTransport(
        responses=[
            response(
                403,
                {"code": 403, "success": False, "message": "denied"},
                headers={"J-Trace-ID": "<script>alert(1)</script>" + "x" * 400},
            )
        ]
    )
    with pytest.raises(ju.UploadRejectedError) as excinfo:
        upload(handoff, transport)
    assert excinfo.value.trace_id is None
    assert "script" not in handoff["ledger"].path.read_text()


def test_non_json_error_body_is_a_failure_without_echoing_the_body(handoff):
    transport = FakeTransport(responses=[response(502, content=b"<html>raw-payload-marker</html>")])
    with pytest.raises(ju.UploadRejectedError):
        upload(handoff, transport)
    assert "raw-payload-marker" not in handoff["ledger"].path.read_text()


@pytest.mark.parametrize(
    "body",
    [
        {"code": 200, "success": True, "data": ""},
        {"code": 200, "success": True, "data": 12345},
        {"code": 200, "success": True, "data": "key with\nnewline"},
        {"code": 200, "success": True},
    ],
)
def test_unusable_file_key_fails_closed(handoff, body):
    transport = FakeTransport(responses=[response(200, body)])
    with pytest.raises(ju.UploadRejectedError):
        upload(handoff, transport)
    assert [e["kind"] for e in handoff["ledger"].events()] == ["intent", "failure"]


# ---------------------------------------------------------------------------
# App / file identity binding
# ---------------------------------------------------------------------------


def test_app_identity_mismatch_fails_closed(handoff):
    body = dict(ok_body(), appId="someone-elses-app")
    transport = FakeTransport(responses=[response(200, body)])
    with pytest.raises(ju.UploadIdentityError):
        upload(handoff, transport)
    assert [e["kind"] for e in handoff["ledger"].events()] == ["intent", "failure"]


def test_file_md5_mismatch_fails_closed(handoff):
    body = dict(ok_body(), fileMd5="0" * 32)
    transport = FakeTransport(responses=[response(200, body)])
    with pytest.raises(ju.UploadIdentityError):
        upload(handoff, transport)


def test_echoed_matching_identity_is_accepted(handoff):
    body = dict(
        ok_body(),
        appId=FAKE_APP_ID,
        fileMd5=hashlib.md5(handoff["gerber"], usedforsecurity=False).hexdigest().upper(),
    )
    transport = FakeTransport(responses=[response(200, body)])
    receipt = upload(handoff, transport)
    assert receipt.file_key == "file-key-0001"


# ---------------------------------------------------------------------------
# File-ID reuse binding
# ---------------------------------------------------------------------------


def test_repeat_upload_of_the_same_file_hash_reuses_the_file_id(handoff):
    transport = FakeTransport(responses=[response(200, ok_body())])
    first = upload(handoff, transport)
    assert first.reused is False

    second = upload(handoff, transport, requested_at="2026-01-03T00:00:00Z")
    assert second.reused is True
    assert second.file_key == first.file_key
    assert second.file_sha256 == first.file_sha256
    # No second network call, and no second intent in the ledger.
    assert len(transport.multipart_calls) == 1
    assert [e["kind"] for e in handoff["ledger"].events()] == ["intent", "success"]


def test_file_id_is_not_reused_for_different_bytes_with_the_same_name(handoff, tmp_path):
    transport = FakeTransport(responses=[response(200, ok_body("key-a"))])
    upload(handoff, transport)

    # Same upload filename, different bytes -> a different plan and hash.
    bundle = handoff["bundle"]
    (bundle / "gerbers.zip").write_bytes(b"PK\x03\x04different-exact-bytes\n")
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
            name,
            digest((handoff["source"] / name).read_bytes()),
            (handoff["source"] / name).stat().st_size,
        )
        for role, name in [("pcb", "board.kicad_pcb"), ("schematic", "board.kicad_sch")]
    }
    plan2 = sp.prepare_submission(
        bundle_root=bundle,
        source_root=handoff["source"],
        source_evidence=evidence,
        bundle_files=("gerbers.zip", "bom.csv", "cpl.csv"),
        artifacts={"gerber": "gerbers.zip", "bom": "bom.csv", "cpl": "cpl.csv"},
        board_quantity=5,
        settings=sp.FactorySettings("jlcpcb", 4, ("top",)),
        destination=tmp_path / "handoff-2",
    )
    record2 = sr.submit_review(
        plan2,
        reviewer="alice@example.invalid",
        reviewed_at="2026-01-01T00:00:00Z",
        policy_version="policy-v1",
        checklist=dict.fromkeys(REQUIRED, True),
        required_items=REQUIRED,
        destination=tmp_path / "review-plan2.json",
    )
    transport._responses.append(response(200, ok_body("key-b")))
    second = ju.upload_gerber(
        plan2,
        record2,
        ledger=handoff["ledger"],
        transport=transport,
        credentials=fake_credentials(),
        requested_at="2026-01-04T00:00:00Z",
    )
    assert second.reused is False
    assert second.file_key == "key-b"
    assert len(transport.multipart_calls) == 2


def test_file_id_is_not_reused_across_app_identities(handoff):
    transport = FakeTransport(
        responses=[response(200, ok_body("key-a")), response(200, ok_body("key-b"))]
    )
    upload(handoff, transport)
    other = jlcpcb_api.JLCCredentials("other-app-id", FAKE_ACCESS_KEY, FAKE_SECRET_KEY)
    second = upload(handoff, transport, credentials=other, requested_at="2026-01-05T00:00:00Z")
    assert second.reused is False
    assert second.app_identity == "other-app-id"
    assert len(transport.multipart_calls) == 2


def test_file_id_is_not_reused_across_endpoints(handoff):
    transport = FakeTransport(
        responses=[response(200, ok_body("key-a")), response(200, ok_body("key-b"))]
    )
    upload(handoff, transport)
    second = upload(
        handoff,
        transport,
        base_url="https://staging.example.invalid",
        requested_at="2026-01-06T00:00:00Z",
    )
    assert second.reused is False
    assert len(transport.multipart_calls) == 2


def test_a_definite_failure_does_not_block_a_later_attempt(handoff):
    transport = FakeTransport(
        responses=[
            response(200, {"code": 4001, "success": False, "message": "parse failed"}),
            response(200, ok_body("key-after-retry")),
        ]
    )
    with pytest.raises(ju.UploadRejectedError):
        upload(handoff, transport)
    receipt = upload(handoff, transport, requested_at="2026-01-07T00:00:00Z")
    assert receipt.file_key == "key-after-retry"
    assert len(transport.multipart_calls) == 2


# ---------------------------------------------------------------------------
# Uncertain outcomes and reconciliation
# ---------------------------------------------------------------------------


def binding(handoff, gerber=None):
    return {
        "file_sha256": digest(gerber if gerber is not None else handoff["gerber"]),
        "app_identity": FAKE_APP_ID,
        "endpoint": jlcpcb_api.JLC_OPENAPI_BASE + ju.UPLOAD_GERBER_PATH,
    }


def test_timeout_after_send_records_uncertain_and_blocks_auto_retry(handoff):
    transport = FakeTransport(error=ju.TransportError("read timed out", request_sent=True))
    with pytest.raises(ju.UploadUncertainError):
        upload(handoff, transport)

    events = handoff["ledger"].events()
    assert [e["kind"] for e in events] == ["intent", "uncertain"]
    assert ju.upload_state(handoff["ledger"], **binding(handoff)) == ju.STATE_UNCERTAIN

    # A second call must NOT re-upload: it is blocked pending reconciliation.
    retry = FakeTransport(responses=[response(200, ok_body())])
    with pytest.raises(ju.UploadBlockedError):
        upload(handoff, retry, requested_at="2026-01-08T00:00:00Z")
    assert retry.multipart_calls == []
    assert [e["kind"] for e in handoff["ledger"].events()] == ["intent", "uncertain"]


def test_unknown_transport_failure_is_uncertain_not_failed(handoff):
    transport = FakeTransport(error=ju.TransportError("connection reset"))
    with pytest.raises(ju.UploadUncertainError):
        upload(handoff, transport)
    assert ju.upload_state(handoff["ledger"], **binding(handoff)) == ju.STATE_UNCERTAIN


def test_proven_unsent_request_is_a_plain_failure(handoff):
    transport = FakeTransport(
        error=ju.TransportError("could not connect to host", request_sent=False)
    )
    with pytest.raises(ju.UploadRejectedError):
        upload(handoff, transport)
    assert ju.upload_state(handoff["ledger"], **binding(handoff)) == ju.STATE_FAILED


def test_unexpected_transport_exception_is_uncertain(handoff):
    class Boom(Exception):
        pass

    transport = FakeTransport(error=Boom("something else entirely"))
    with pytest.raises(ju.UploadUncertainError):
        upload(handoff, transport)
    assert ju.upload_state(handoff["ledger"], **binding(handoff)) == ju.STATE_UNCERTAIN


def test_crash_after_intent_leaves_an_uncertain_dangling_attempt(handoff):
    # KeyboardInterrupt is a BaseException: it models the process dying after
    # the intent was persisted, with no outcome ever recorded.
    transport = FakeTransport(error=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        upload(handoff, transport)

    assert [e["kind"] for e in handoff["ledger"].events()] == ["intent"]
    # A fresh ledger object re-reading the same file (a new process) agrees.
    reopened = ju.UploadLedger.open(handoff["ledger"].path)
    assert ju.upload_state(reopened, **binding(handoff)) == ju.STATE_UNCERTAIN

    retry = FakeTransport(responses=[response(200, ok_body())])
    with pytest.raises(ju.UploadBlockedError):
        upload(handoff, retry)
    assert retry.multipart_calls == []


def test_pending_reconciliations_names_the_unresolved_attempt(handoff):
    transport = FakeTransport(error=ju.TransportError("read timed out", request_sent=True))
    with pytest.raises(ju.UploadUncertainError):
        upload(handoff, transport)

    pending = ju.pending_reconciliations(handoff["ledger"])
    assert len(pending) == 1
    assert pending[0]["file"]["sha256"] == digest(handoff["gerber"])
    assert pending[0]["app_identity"] == FAKE_APP_ID


def test_reconciling_as_succeeded_binds_the_operator_supplied_file_key(handoff):
    transport = FakeTransport(error=ju.TransportError("read timed out", request_sent=True))
    with pytest.raises(ju.UploadUncertainError):
        upload(handoff, transport)
    attempt_id = ju.pending_reconciliations(handoff["ledger"])[0]["attempt_id"]

    state = ju.reconcile_upload(
        handoff["ledger"],
        attempt_id=attempt_id,
        resolution="succeeded",
        file_key="key-confirmed-by-human",
        reconciled_by="alice@example.invalid",
        reconciled_at="2026-01-09T00:00:00Z",
        evidence="Checked the JLCPCB portal by hand; the file is present once.",
    )
    assert state == ju.STATE_SUCCEEDED

    retry = FakeTransport(responses=[response(200, ok_body())])
    receipt = upload(handoff, retry, requested_at="2026-01-10T00:00:00Z")
    assert retry.multipart_calls == []
    assert receipt.reused is True
    assert receipt.file_key == "key-confirmed-by-human"
    # A human attestation is not a protocol receipt from the factory.
    assert receipt.evidence == "human-reconciliation"
    assert receipt.is_factory_receipt is False


def test_reconciling_as_failed_unblocks_a_fresh_upload(handoff):
    transport = FakeTransport(error=ju.TransportError("read timed out", request_sent=True))
    with pytest.raises(ju.UploadUncertainError):
        upload(handoff, transport)
    attempt_id = ju.pending_reconciliations(handoff["ledger"])[0]["attempt_id"]

    state = ju.reconcile_upload(
        handoff["ledger"],
        attempt_id=attempt_id,
        resolution="failed",
        reconciled_by="alice@example.invalid",
        reconciled_at="2026-01-09T00:00:00Z",
        evidence="Portal shows no uploaded file for this hash.",
    )
    assert state == ju.STATE_FAILED

    retry = FakeTransport(responses=[response(200, ok_body("fresh-key"))])
    receipt = upload(handoff, retry, requested_at="2026-01-10T00:00:00Z")
    assert len(retry.multipart_calls) == 1
    assert receipt.file_key == "fresh-key"
    assert receipt.reused is False


def test_reconciliation_requires_an_uncertain_attempt_and_full_evidence(handoff):
    transport = FakeTransport(responses=[response(200, ok_body())])
    upload(handoff, transport)
    done = handoff["ledger"].events()[0]["attempt_id"]

    # Already resolved: not reconcilable.
    with pytest.raises(ju.LedgerError):
        ju.reconcile_upload(
            handoff["ledger"],
            attempt_id=done,
            resolution="failed",
            reconciled_by="a",
            reconciled_at="t",
            evidence="e",
        )
    # Unknown attempt.
    with pytest.raises(ju.LedgerError):
        ju.reconcile_upload(
            handoff["ledger"],
            attempt_id="0" * 32,
            resolution="failed",
            reconciled_by="a",
            reconciled_at="t",
            evidence="e",
        )


def test_reconciliation_field_validation(handoff):
    transport = FakeTransport(error=ju.TransportError("read timed out", request_sent=True))
    with pytest.raises(ju.UploadUncertainError):
        upload(handoff, transport)
    attempt_id = ju.pending_reconciliations(handoff["ledger"])[0]["attempt_id"]

    common = {
        "attempt_id": attempt_id,
        "reconciled_by": "alice@example.invalid",
        "reconciled_at": "2026-01-09T00:00:00Z",
        "evidence": "checked by hand",
    }
    # succeeded without a file key
    with pytest.raises(ju.LedgerError):
        ju.reconcile_upload(handoff["ledger"], resolution="succeeded", **common)
    # failed WITH a file key
    with pytest.raises(ju.LedgerError):
        ju.reconcile_upload(handoff["ledger"], resolution="failed", file_key="k", **common)
    # unknown resolution
    with pytest.raises(ju.LedgerError):
        ju.reconcile_upload(handoff["ledger"], resolution="maybe", **common)
    # missing human identity / evidence
    with pytest.raises(ju.LedgerError):
        ju.reconcile_upload(
            handoff["ledger"],
            attempt_id=attempt_id,
            resolution="failed",
            reconciled_by="  ",
            reconciled_at="2026-01-09T00:00:00Z",
            evidence="checked",
        )
    with pytest.raises(ju.LedgerError):
        ju.reconcile_upload(
            handoff["ledger"],
            attempt_id=attempt_id,
            resolution="failed",
            reconciled_by="alice",
            reconciled_at="2026-01-09T00:00:00Z",
            evidence="   ",
        )
    # Nothing was appended by any of the rejected calls.
    assert [e["kind"] for e in handoff["ledger"].events()] == ["intent", "uncertain"]


def test_reconciliation_cannot_be_applied_twice(handoff):
    transport = FakeTransport(error=ju.TransportError("read timed out", request_sent=True))
    with pytest.raises(ju.UploadUncertainError):
        upload(handoff, transport)
    attempt_id = ju.pending_reconciliations(handoff["ledger"])[0]["attempt_id"]
    args = {
        "attempt_id": attempt_id,
        "resolution": "failed",
        "reconciled_by": "alice",
        "reconciled_at": "t",
        "evidence": "checked",
    }
    ju.reconcile_upload(handoff["ledger"], **args)
    with pytest.raises(ju.LedgerError):
        ju.reconcile_upload(handoff["ledger"], **args)


def test_an_uncertain_attempt_blocks_even_when_another_attempt_succeeded(handoff):
    """Fail-closed precedence: unresolved uncertainty dominates a later success."""
    transport = FakeTransport(responses=[response(200, ok_body())])
    upload(handoff, transport)
    # Append a hand-made dangling intent for the same binding (a crashed peer).
    events = handoff["ledger"].events()
    intent = dict(events[0], attempt_id="f" * 32)
    handoff["ledger"]._append(intent)

    assert ju.upload_state(handoff["ledger"], **binding(handoff)) == ju.STATE_UNCERTAIN
    retry = FakeTransport(responses=[response(200, ok_body())])
    with pytest.raises(ju.UploadBlockedError):
        upload(handoff, retry, requested_at="2026-01-11T00:00:00Z")
    assert retry.multipart_calls == []


def test_a_concurrent_upload_against_the_same_ledger_is_blocked(handoff):
    ledger_path = handoff["ledger"].path
    lock = ledger_path.parent / ("." + ledger_path.name + ".upload-lock")
    lock.write_bytes(b"")  # a peer process is mid-upload

    transport = FakeTransport(responses=[response(200, ok_body())])
    with pytest.raises(ju.UploadBlockedError):
        upload(handoff, transport)
    assert transport.multipart_calls == []
    assert handoff["ledger"].events() == []

    lock.unlink()
    receipt = upload(handoff, transport)
    assert receipt.file_key == "file-key-0001"
    # The lock is released again once the attempt finishes.
    assert not lock.exists()


# ---------------------------------------------------------------------------
# Mock-vs-factory labeling
# ---------------------------------------------------------------------------


def test_mock_transport_success_is_never_labeled_a_factory_receipt(handoff):
    transport = FakeTransport(responses=[response(200, ok_body())])
    receipt = upload(handoff, transport)
    assert transport.identity.live is False
    assert receipt.is_factory_receipt is False
    assert receipt.evidence == "mock-protocol-only"
    assert receipt.states["upload"] == "mock-protocol-only"
    assert receipt.states["order"] == "not-performed"
    assert receipt.states["factory_matching"] == "not-observed"
    assert receipt.states["human_approval"] == "reviewed"
    text = handoff["ledger"].path.read_text()
    assert "mock-protocol-only" in text
    assert '"uploaded"' not in text


def test_live_transport_success_is_labeled_a_factory_receipt(handoff):
    transport = FakeTransport(responses=[response(200, ok_body())], live=True, name="live-fake")
    receipt = upload(handoff, transport)
    assert receipt.is_factory_receipt is True
    assert receipt.evidence == "live-factory-response"
    assert receipt.states["upload"] == "uploaded"


# ---------------------------------------------------------------------------
# Preview retrieval is a separate call
# ---------------------------------------------------------------------------


def test_upload_never_performs_the_preview_call(handoff):
    transport = FakeTransport(responses=[response(200, ok_body())])
    upload(handoff, transport)
    assert transport.json_calls == []
    assert all(e["kind"] != "preview" for e in handoff["ledger"].events())


def test_preview_is_a_separate_signed_call_with_its_own_bound_record(handoff):
    transport = FakeTransport(
        responses=[
            response(200, ok_body("file-key-0001")),
            response(200, {"code": 200, "success": True, "data": {"stencilLayer": 4}}),
        ]
    )
    receipt = upload(handoff, transport)
    preview = ju.fetch_preview(
        receipt,
        ledger=handoff["ledger"],
        transport=transport,
        credentials=fake_credentials(),
        requested_at="2026-01-12T00:00:00Z",
    )
    assert preview.data == {"stencilLayer": 4}
    call = transport.json_calls[0]
    assert call["url"].endswith(ju.PREVIEW_AUDIT_PATH)
    assert call["body"] == b'{"key":"file-key-0001"}'
    assert call["headers"]["Content-Type"] == "application/json"
    scheme, fields = auth_fields(call["headers"])
    expected = jlcpcb_api._sign(
        FAKE_SECRET_KEY,
        f"POST\n{ju.PREVIEW_AUDIT_PATH}\n{fields['timestamp']}\n{fields['nonce']}\n"
        '{"key":"file-key-0001"}\n',
    )
    assert fields["signature"] == expected

    events = handoff["ledger"].events()
    assert [e["kind"] for e in events] == ["intent", "success", "preview"]
    assert events[2]["file_key"] == "file-key-0001"
    assert events[2]["file"]["sha256"] == digest(handoff["gerber"])
    assert events[2]["payload_sha256"] == preview.payload_sha256


def test_preview_requires_a_recorded_successful_upload(handoff):
    transport = FakeTransport(responses=[response(200, ok_body())])
    receipt = upload(handoff, transport)
    forged = ju.UploadReceipt(**{**receipt.__dict__, "file_key": "not-in-the-ledger"})
    with pytest.raises(ju.UploadGateError):
        ju.fetch_preview(
            forged,
            ledger=handoff["ledger"],
            transport=transport,
            credentials=fake_credentials(),
            requested_at="2026-01-12T00:00:00Z",
        )
    assert transport.json_calls == []


def test_preview_business_error_is_not_persisted_as_a_result(handoff):
    transport = FakeTransport(
        responses=[
            response(200, ok_body()),
            response(200, {"code": 5000, "success": False, "message": "not ready"}),
        ]
    )
    receipt = upload(handoff, transport)
    with pytest.raises(ju.UploadRejectedError):
        ju.fetch_preview(
            receipt,
            ledger=handoff["ledger"],
            transport=transport,
            credentials=fake_credentials(),
            requested_at="2026-01-12T00:00:00Z",
        )
    assert [e["kind"] for e in handoff["ledger"].events()] == ["intent", "success"]


# ---------------------------------------------------------------------------
# Ledger integrity
# ---------------------------------------------------------------------------


def test_torn_ledger_tail_is_rejected(handoff):
    transport = FakeTransport(responses=[response(200, ok_body())])
    upload(handoff, transport)
    with handoff["ledger"].path.open("ab") as stream:
        stream.write(b'{"schema_version":1,"kind":"succ')
    with pytest.raises(ju.LedgerError):
        handoff["ledger"].events()


def test_noncanonical_ledger_line_is_rejected(handoff):
    with handoff["ledger"].path.open("ab") as stream:
        stream.write(b'{ "kind": "intent", "schema_version": 1 }\n')
    with pytest.raises(ju.LedgerError):
        handoff["ledger"].events()


def test_unknown_ledger_event_kind_is_rejected(handoff):
    handoff["ledger"]._append({"schema_version": 1, "kind": "ordered", "attempt_id": "a" * 32})
    with pytest.raises(ju.LedgerError):
        handoff["ledger"].events()


def test_duplicate_attempt_id_is_rejected(handoff):
    transport = FakeTransport(responses=[response(200, ok_body())])
    upload(handoff, transport)
    handoff["ledger"]._append(handoff["ledger"].events()[0])
    with pytest.raises(ju.LedgerError):
        ju.upload_state(handoff["ledger"], **binding(handoff))


def test_outcome_without_an_intent_is_rejected(handoff):
    handoff["ledger"]._append(
        {
            "schema_version": 1,
            "kind": "success",
            "attempt_id": "b" * 32,
            "file_key": "k",
            "http_status": 200,
            "trace_id": None,
        }
    )
    with pytest.raises(ju.LedgerError):
        ju.upload_state(handoff["ledger"], **binding(handoff))


def test_two_outcomes_for_one_attempt_are_rejected(handoff):
    transport = FakeTransport(responses=[response(200, ok_body())])
    upload(handoff, transport)
    events = handoff["ledger"].events()
    handoff["ledger"]._append(events[1])
    with pytest.raises(ju.LedgerError):
        ju.upload_state(handoff["ledger"], **binding(handoff))


def test_ledger_state_is_re_read_from_disk_each_call(handoff):
    transport = FakeTransport(responses=[response(200, ok_body())])
    upload(handoff, transport)
    fresh = ju.UploadLedger(handoff["ledger"].path)
    assert ju.upload_state(fresh, **binding(handoff)) == ju.STATE_SUCCEEDED


def test_ledger_must_live_outside_the_published_handoff(handoff):
    inside = ju.UploadLedger(handoff["plan"].directory / "uploads.jsonl")
    transport = FakeTransport(responses=[response(200, ok_body())])
    with pytest.raises(ju.UploadGateError):
        upload(handoff, transport, ledger=inside)
    assert transport.multipart_calls == []


# ---------------------------------------------------------------------------
# Scope guards: no order/pay/quote surface anywhere in this slice
# ---------------------------------------------------------------------------


def test_module_exposes_exactly_two_endpoint_paths():
    paths = {
        value
        for name, value in vars(ju).items()
        if name.endswith("_PATH") and isinstance(value, str)
    }
    assert paths == {ju.UPLOAD_GERBER_PATH, ju.PREVIEW_AUDIT_PATH}
    assert ju.UPLOAD_GERBER_PATH == "/overseas/openapi/pcb/uploadGerber"
    assert ju.PREVIEW_AUDIT_PATH == "/overseas/openapi/pcb/audit/get"


def test_pcba_handoff_is_an_explicit_manual_website_step():
    text = ju.PCBA_WEBSITE_HANDOFF.lower()
    assert "website" in text
    assert "does not" in text and "automate" in text


def test_module_source_contains_no_order_pay_or_quote_endpoint():
    from pathlib import Path

    source = Path(ju.__file__).read_text()
    # Every URL-path-shaped string literal in the module, whatever its name.
    literals = set(re.findall(r"\"(/[A-Za-z0-9/_.-]+)\"", source))
    assert literals == {ju.UPLOAD_GERBER_PATH, ju.PREVIEW_AUDIT_PATH}
    lowered = source.lower()
    for forbidden in (
        "calculate",
        "createorder",
        "placeorder",
        "orderpay",
        "uploadbom",
        "uploadcpl",
        "submitorder",
    ):
        assert forbidden not in lowered, f"out-of-scope endpoint surface: {forbidden}"
