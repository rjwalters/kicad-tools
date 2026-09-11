"""Resumable exact-byte Gerber upload intents and hash-bound receipts (#5145).

Offline implementation toward #5056's third slice (#5142 -> #5143 -> #5145
-> #5146): durable offline protocol records for an **already prepared and
already human-reviewed** Gerber bundle. Live calls
remain disabled pending the complete first-party endpoint contract.
Clean-room Python; no copied SDK code or runtime Java/Windows dependency.

What this module never does
---------------------------
* **No create/pay/order operation of any kind exists here.** There is no
  quote, no order creation, no payment, and no fabrication-parameter mapping
  (the parent issue records that the inspected SDK exposes field names but not
  enough enum/unit meaning to map fabrication settings safely, so none are
  guessed).
* **No BOM/CPL upload or PCBA component-submission call.** Neither the
  published API list nor the inspected SDK contains one; rather than pretend to
  automate it, the workflow falls back to the explicit website hand-off
  described by :data:`PCBA_WEBSITE_HANDOFF`.
* **No wall clock is ever invented for a persisted record.** Every durable
  record carries a caller-supplied timestamp. The only clock this module reads
  is the request-signature timestamp, which is a wire value and is never
  persisted.
* **No mocked protocol success is ever labeled a real factory receipt.** The
  injected transport must declare a :class:`TransportIdentity`; a receipt is
  only marked ``live-factory-response`` when the transport that produced it
  declared ``live=True``, and a human reconciliation is recorded as a human
  attestation (``human-reconciliation``), never as a protocol receipt.

Protocol provenance -- partially verified, live calls disabled
--------------------------------------------------------------
Public first-party documentation retrieved 2026-09-11 confirms multipart
uploads, signing the metadata JSON, five newline-terminated signature fields,
Base64 HMAC-SHA256, and application-scoped API keys. The API list describes
Gerber upload returning a file ID and preview accepting that ID and language:

* https://api.jlcpcb.com/docs/start
* https://api.jlcpcb.com/docs/api-request-signature
* https://api.jlcpcb.com/docs/configure-api-key
* https://api.jlcpcb.com/docs/create-an-application
* https://api.jlcpcb.com/docs/api-list

The exact upload/preview paths, multipart part names, metadata schema and
hexadecimal MD5 encoding remain unconfirmed. Constants below preserve #5056's
user-reported SDK observations for OFFLINE fixtures only. Declared-live
upload/preview calls and direct RequestsUploadTransport sends raise
UploadGateError before session creation or request/ledger writes. There is no
caller flag that substitutes for first-party verification. Protocol
confirmation and live enablement remain outstanding under #5145.

The provisional upload uses POST /overseas/openapi/pcb/uploadGerber, fields
meta (the JSON string {}) and file, and lowercase-hex Content-MD5 of raw bytes.
The provisional preview path is /overseas/openapi/pcb/audit/get. These details
must not be described as confirmed. prepare_multipart_request exercises the
library's encoder without opening a session or sending a request. Injected
non-live transports retain the durable mock protocol workflow. No credentials
are read from the environment by this module.

The durable upload state machine
--------------------------------
Upload outcomes are recorded in an append-only :class:`UploadLedger` (a JSONL
sidecar, written outside the read-only handoff directory and distinct from the
#5142 plan and the #5143 review record -- this module reads those, it does not
duplicate their fields). Per attempt:

.. code-block:: text

    intent  --success-------------------> succeeded
            --failure-------------------> failed
            --uncertain-----------------> uncertain --reconciliation--> succeeded|failed
            --(no outcome ever written)-> uncertain --reconciliation--> succeeded|failed

An attempt whose intent was persisted but whose outcome never was (process
killed mid-request) folds to ``uncertain`` by construction -- there is no
boolean "retry me" flag anywhere. For a given (file sha256, app identity,
endpoint) binding, an unresolved ``uncertain`` attempt **dominates**: it blocks
automatic retry until a human (or a dedicated recovery step) resolves it with
:func:`reconcile_upload`, which requires an explicit reconciler identity,
timestamp, and evidence statement. Nothing in this module assumes, in either
direction, whether an uncertain request reached the factory.

A successful file key is reused **only** for the exact same file hash, under
the same app identity and the same endpoint. A filename or revision never
selects a receipt.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import secrets
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, NoReturn, Protocol

from ..export.submission_plan import (
    SubmissionPlan,
    _digest,
    _json,
    _load_json,
    _path,
    _read,
    _root,
    verify_submission,
)
from ..export.submission_review import ReviewRecord, verify_review
from ..parts.jlcpcb_api import (
    JLC_OPENAPI_BASE,
    JLCAPIError,
    JLCAuthError,
    JLCCredentials,
    JLCIncompleteResponseError,
    JLCIPNotWhitelistedError,
    JLCPermissionError,
    JLCQuotaError,
    _build_auth_header,
    _classify_business_error,
    _compact_json,
    _safe_error_reason,
    _sign,
    _string_to_sign,
)

__all__ = [
    "LedgerError",
    "PCBA_WEBSITE_HANDOFF",
    "PREVIEW_AUDIT_PATH",
    "PreviewResult",
    "STATE_FAILED",
    "STATE_NOT_ATTEMPTED",
    "STATE_SUCCEEDED",
    "STATE_UNCERTAIN",
    "RequestsUploadTransport",
    "TransportError",
    "TransportIdentity",
    "TransportResponse",
    "UPLOAD_GERBER_PATH",
    "UploadAttempt",
    "UploadBlockedError",
    "UploadError",
    "UploadGateError",
    "UploadIdentityError",
    "UploadLedger",
    "UploadReceipt",
    "UploadRejectedError",
    "UploadTransport",
    "UploadUncertainError",
    "fetch_preview",
    "find_receipt",
    "pending_reconciliations",
    "prepare_multipart_request",
    "reconcile_upload",
    "upload_gerber",
    "upload_state",
]

# --------------------------------------------------------------------------
# Endpoint and wire-format constants (see "Protocol provenance" above).
#
# Exactly two endpoints exist in this slice. There is intentionally no quote,
# order, payment, or BOM/CPL path here.
# --------------------------------------------------------------------------
UPLOAD_GERBER_PATH = "/overseas/openapi/pcb/uploadGerber"
PREVIEW_AUDIT_PATH = "/overseas/openapi/pcb/audit/get"

UPLOAD_META_FIELD = "meta"
UPLOAD_FILE_FIELD = "file"
#: The exact metadata JSON string that is both signed and transmitted.
UPLOAD_META_JSON = "{}"
UPLOAD_CONTENT_TYPE = "application/octet-stream"
TRACE_HEADER = "J-Trace-ID"

PCBA_WEBSITE_HANDOFF = (
    "JLCPCB has no first-party-verified BOM/CPL upload or PCBA component-selection"
    " API. Continue the assembly submission by hand on the JLCPCB website using the"
    " exact reviewed files published in the handoff directory; this tool does not"
    " automate that step and never claims to have performed it."
)

SCHEMA_VERSION = 1

STATE_NOT_ATTEMPTED = "not-attempted"
STATE_SUCCEEDED = "succeeded"
STATE_FAILED = "failed"
STATE_UNCERTAIN = "uncertain"

EVIDENCE_LIVE = "live-factory-response"
EVIDENCE_MOCK = "mock-protocol-only"
EVIDENCE_RECONCILED = "human-reconciliation"

_INTENT_KIND = "intent"
_OUTCOME_KINDS = ("success", "failure", "uncertain")
_RECONCILE_KIND = "reconciliation"
_PREVIEW_KIND = "preview"
_EVENT_KINDS = frozenset({_INTENT_KIND, *_OUTCOME_KINDS, _RECONCILE_KIND, _PREVIEW_KIND})

_ATTEMPT_ID = re.compile(r"[0-9a-f]{32}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MD5 = re.compile(r"[0-9a-f]{32}\Z")
#: A file key is an opaque server token; only this bounded, printable shape is
#: ever retained, so a hostile payload cannot smuggle structure into a record.
_FILE_KEY = re.compile(r"[A-Za-z0-9._:/+=-]{1,256}\Z")
#: Same idea for the trace id: an explicit whitelist, not "whatever was sent".
_TRACE_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")

#: Error bodies may be HTML or oversized; bound the JSON parse for failures.
_MAX_ERROR_BODY = 65536

#: The only shape of server-supplied prose this module ever surfaces or
#: persists: bounded plain text. Markup, braces, brackets and other
#: raw-payload markers are excluded, so an echoed response body cannot ride
#: out through an error message or a ledger record.
_SAFE_REASON = re.compile(r"""[A-Za-z0-9 .,:;!?()/_'"=+#@-]{1,200}\Z""")

#: Fixed, safe classification vocabulary. The server's own message is only
#: ever surfaced after :func:`_safe_error_reason` sanitization/redaction; this
#: word is what callers and persisted records should branch on.
_CLASSIFICATIONS: dict[type, str] = {
    JLCAuthError: "auth-failed",
    JLCIPNotWhitelistedError: "ip-not-whitelisted",
    JLCPermissionError: "permission-denied",
    JLCQuotaError: "quota-exceeded",
    JLCIncompleteResponseError: "incomplete-response",
    JLCAPIError: "request-failed",
}

#: Response keys that echo the caller's own identity back. Any mismatch fails
#: closed; a missing key is not evidence of anything and is simply absent.
_APP_ECHO_KEYS = ("appId", "appid", "app_id", "applicationId")
_MD5_ECHO_KEYS = ("md5", "fileMd5", "contentMd5", "content_md5", "Content-MD5")
_SHA_ECHO_KEYS = ("sha256", "fileSha256", "file_sha256")


class UploadError(Exception):
    """Base class for every failure in this module."""


class UploadGateError(UploadError):
    """A precondition for uploading failed; no network call was made.

    Raised for an invalid/stale human review, drifted bundle bytes, a
    malformed plan, a ledger inside the published handoff, or an unusable
    caller argument. This is always raised *before* anything is sent.
    """


class UploadBlockedError(UploadError):
    """An unresolved uncertain attempt blocks an automatic retry.

    Resolve it explicitly with :func:`reconcile_upload` (see
    :func:`pending_reconciliations`). Nothing was sent.
    """


class UploadUncertainError(UploadError):
    """The request may or may not have reached the factory.

    The attempt is durably recorded as ``uncertain``; a later attempt for the
    same binding is blocked until it is explicitly reconciled.
    """


class UploadRejectedError(UploadError):
    """The request definitively failed; it is safe to attempt again.

    Carries only safe, bounded diagnostics: a fixed ``classification`` word, a
    sanitized/redacted ``reason``, the HTTP status, the business ``code``, and
    the whitelisted ``J-Trace-ID``. The raw response body is never retained.
    """

    def __init__(
        self,
        message: str,
        *,
        classification: str,
        reason: str,
        http_status: int | None = None,
        code: int | None = None,
        trace_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.classification = classification
        self.reason = reason
        self.http_status = http_status
        self.code = code
        self.trace_id = trace_id


class UploadIdentityError(UploadUncertainError):
    """Response identity is unresolved; an upload needs reconciliation."""

    def __init__(self, message: str, *, classification: str, reason: str) -> None:
        super().__init__(message)
        self.classification = classification
        self.reason = reason


class LedgerError(UploadError):
    """The durable upload ledger is unusable, inconsistent, or misused."""


# --------------------------------------------------------------------------
# Injected transport
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TransportIdentity:
    """What a transport is, and whether it actually talks to the network.

    ``live`` must be ``True`` **only** for a transport that performs real HTTP
    requests against a real endpoint. Every test double declares ``False``, and
    that is what keeps a mocked protocol success from ever being recorded or
    labeled as a real factory receipt.
    """

    name: str
    live: bool


@dataclass(frozen=True)
class TransportResponse:
    """A raw HTTP response. Parsing/classification happens in this module."""

    status_code: int
    headers: Mapping[str, str]
    content: bytes


class TransportError(Exception):
    """A transport-level failure.

    ``request_sent`` is the only thing that separates a definite failure from
    an uncertain one, and it is fail-closed: ``False`` means the transport can
    *prove* nothing left the machine (e.g. the connection was never
    established). ``True`` or ``None`` (unknown) both become ``uncertain``.
    """

    def __init__(self, reason: str, *, request_sent: bool | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.request_sent = request_sent


class UploadTransport(Protocol):
    """The HTTP surface this module needs. Always injected, never constructed.

    Implementations must let their HTTP library encode the multipart body (and
    therefore generate its own boundary) from the discrete ``fields``/``files``
    mappings -- this module deliberately supplies no ``Content-Type`` for the
    upload request.
    """

    identity: TransportIdentity

    def post_multipart(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        fields: Mapping[str, str],
        files: Mapping[str, tuple[str, bytes, str]],
    ) -> TransportResponse: ...

    def post_json(
        self, url: str, *, headers: Mapping[str, str], body: bytes
    ) -> TransportResponse: ...


def _require_offline_transport(transport: UploadTransport) -> TransportIdentity:
    identity = getattr(transport, "identity", None)
    if not isinstance(identity, TransportIdentity) or type(identity.live) is not bool:
        raise UploadGateError("Transport must declare an explicit boolean TransportIdentity")
    if identity.live or isinstance(transport, RequestsUploadTransport):
        _unverified_protocol()
    return identity


def _unverified_protocol() -> NoReturn:
    # No caller flag can stand in for the missing first-party wire contract.
    raise UploadGateError(
        "Live upload/preview protocol is not fully first-party verified; "
        "only offline injected transports are supported"
    )


def prepare_multipart_request(
    url: str,
    *,
    headers: Mapping[str, str],
    fields: Mapping[str, str],
    files: Mapping[str, tuple[str, bytes, str]],
) -> Any:
    """Prepare the provisional wire format offline; never create/send a session.

    requests supplies its multipart encoder and boundary. This is protocol
    fixture support, not evidence that the endpoint accepts the request.
    """
    import requests

    return requests.Request(
        "POST", url, headers=dict(headers), data=dict(fields), files=dict(files)
    ).prepare()


class RequestsUploadTransport:
    """Reserved live transport, disabled until its protocol is verified.

    No session is created and no request is sent, including with an injected
    session. Use prepare_multipart_request for offline wire-format inspection.
    """

    identity = TransportIdentity("requests-live", True)

    def __init__(self, *, session: Any | None = None, timeout: float = 120.0) -> None:
        self._session = session
        self._closed = False
        self.timeout = timeout

    def _post(self, url: str, **kwargs: Any) -> TransportResponse:
        _unverified_protocol()

    def post_multipart(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        fields: Mapping[str, str],
        files: Mapping[str, tuple[str, bytes, str]],
    ) -> TransportResponse:
        return self._post(url, headers=dict(headers), data=dict(fields), files=dict(files))

    def post_json(self, url: str, *, headers: Mapping[str, str], body: bytes) -> TransportResponse:
        return self._post(url, headers=dict(headers), data=body)

    def close(self) -> None:
        self._closed = True
        session, self._session = self._session, None
        if session is not None:
            session.close()

    def __enter__(self) -> RequestsUploadTransport:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> Literal[False]:
        self.close()
        return False


# --------------------------------------------------------------------------
# Durable append-only ledger
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class UploadAttempt:
    """One folded upload attempt: its intent, its outcome, its resolution."""

    attempt_id: str
    intent: dict[str, Any]
    outcome: dict[str, Any] | None = None
    reconciliation: dict[str, Any] | None = None
    previews: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    @property
    def state(self) -> str:
        if self.reconciliation is not None:
            resolution = self.reconciliation["resolution"]
            return STATE_SUCCEEDED if resolution == "succeeded" else STATE_FAILED
        if self.outcome is None:
            # Intent persisted, outcome never was: the process died mid-request.
            return STATE_UNCERTAIN
        kind = self.outcome["kind"]
        if kind == "success":
            return STATE_SUCCEEDED
        if kind == "failure":
            return STATE_FAILED
        return STATE_UNCERTAIN

    @property
    def file_key(self) -> str | None:
        if self.reconciliation is not None:
            key = self.reconciliation.get("file_key")
            return key if isinstance(key, str) else None
        if self.outcome is not None and self.outcome["kind"] == "success":
            key = self.outcome.get("file_key")
            return key if isinstance(key, str) else None
        return None

    @property
    def evidence(self) -> str | None:
        """How the current state was established -- never inferred upward."""
        if self.reconciliation is not None:
            return EVIDENCE_RECONCILED
        if self.outcome is not None and self.outcome["kind"] == "success":
            live = self.intent.get("transport", {}).get("live")
            return EVIDENCE_LIVE if live is True else EVIDENCE_MOCK
        return None

    @property
    def binding(self) -> tuple[str, str, str]:
        return (
            self.intent["file"]["sha256"],
            self.intent["app_identity"],
            self.intent["endpoint"],
        )


class UploadLedger:
    """Append-only JSONL record of upload intents and outcomes.

    Every read re-parses the file from disk: no state computed in an earlier
    call (let alone an earlier process) is ever trusted. Every append is
    ``fsync``-ed before the caller continues, which is what makes "persist the
    intent *before* sending" a real durability guarantee rather than a
    buffered-write hope.

    The file is validated fail-closed on every read: a non-canonical line, a
    duplicate attempt id, an outcome without an intent, two outcomes for one
    attempt, an unknown event kind, or a torn tail (a crash mid-append) all
    raise :class:`LedgerError` rather than being silently skipped.
    """

    __slots__ = ("path",)

    def __init__(self, path: Path) -> None:
        self.path = Path(os.path.abspath(path))

    @classmethod
    def open(cls, path: Path) -> UploadLedger:
        """Return a ledger at ``path``, creating an empty one if needed."""
        ledger = cls(path)
        parent = _root(ledger.path.parent)
        _path(ledger.path.name)
        try:
            fd = os.open(ledger.path, os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        except OSError as exc:
            raise LedgerError("Upload ledger is missing, inaccessible, or symlinked") from exc
        os.close(fd)
        dir_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return ledger

    def _append(self, core: Mapping[str, Any]) -> None:
        line = _json(dict(core))
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
        except OSError as exc:
            raise LedgerError("Upload ledger is missing, inaccessible, or symlinked") from exc
        try:
            written = 0
            while written < len(line):
                written += os.write(fd, line[written:])
            os.fsync(fd)
        except OSError as exc:
            raise LedgerError("Upload ledger append failed") from exc
        finally:
            os.close(fd)

    def events(self) -> list[dict[str, Any]]:
        """Re-read and validate every record from disk."""
        try:
            data = _read(_root(self.path.parent), self.path.name).data
        except ValueError as exc:
            raise LedgerError("Upload ledger is missing, inaccessible, or symlinked") from exc
        parts = data.split(b"\n")
        if parts[-1] != b"":
            raise LedgerError(
                "Upload ledger tail is incomplete (interrupted append); explicit repair required"
            )
        events: list[dict[str, Any]] = []
        for line in parts[:-1]:
            try:
                core = _load_json(line)
            except ValueError as exc:
                raise LedgerError("Upload ledger contains a malformed record") from exc
            if not isinstance(core, dict) or _json(core) != line + b"\n":
                raise LedgerError("Upload ledger contains a noncanonical record")
            if core.get("schema_version") != SCHEMA_VERSION or core.get("kind") not in _EVENT_KINDS:
                raise LedgerError("Upload ledger contains an unsupported record")
            attempt_id = core.get("attempt_id")
            if not isinstance(attempt_id, str) or not _ATTEMPT_ID.fullmatch(attempt_id):
                raise LedgerError("Upload ledger record has no valid attempt id")
            events.append(core)
        return events

    def attempts(self) -> dict[str, UploadAttempt]:
        """Fold the event stream into one :class:`UploadAttempt` per attempt id."""
        intents: dict[str, dict[str, Any]] = {}
        outcomes: dict[str, dict[str, Any]] = {}
        reconciliations: dict[str, dict[str, Any]] = {}
        previews: dict[str, list[dict[str, Any]]] = {}
        for core in self.events():
            attempt_id = core["attempt_id"]
            kind = core["kind"]
            if kind == _INTENT_KIND:
                if attempt_id in intents:
                    raise LedgerError("Upload ledger contains a duplicate attempt id")
                _check_intent(core)
                intents[attempt_id] = core
                previews[attempt_id] = []
                continue
            if attempt_id not in intents:
                raise LedgerError("Upload ledger records an outcome with no persisted intent")
            if kind in _OUTCOME_KINDS:
                if attempt_id in outcomes:
                    raise LedgerError("Upload ledger records two outcomes for one attempt")
                outcomes[attempt_id] = core
            elif kind == _RECONCILE_KIND:
                if attempt_id in reconciliations:
                    raise LedgerError("Upload ledger records two reconciliations for one attempt")
                _check_reconciliation(core)
                reconciliations[attempt_id] = core
            else:
                previews[attempt_id].append(core)
        folded = {}
        for attempt_id, intent in intents.items():
            attempt = UploadAttempt(
                attempt_id,
                intent,
                outcomes.get(attempt_id),
                reconciliations.get(attempt_id),
                tuple(previews[attempt_id]),
            )
            if attempt.reconciliation is not None and (
                attempt.outcome is not None and attempt.outcome["kind"] != "uncertain"
            ):
                raise LedgerError("Upload ledger reconciles an attempt that was not uncertain")
            folded[attempt_id] = attempt
        return folded


def _check_intent(core: Mapping[str, Any]) -> None:
    file_record = core.get("file")
    if (
        not isinstance(file_record, dict)
        or not isinstance(file_record.get("sha256"), str)
        or not _SHA256.fullmatch(file_record["sha256"])
        or not isinstance(file_record.get("md5"), str)
        or not _MD5.fullmatch(file_record["md5"])
        or type(file_record.get("size")) is not int
        or not isinstance(file_record.get("upload_name"), str)
        or not file_record["upload_name"]
        or not isinstance(core.get("app_identity"), str)
        or not isinstance(core.get("endpoint"), str)
        or not isinstance(core.get("requested_at"), str)
        or not isinstance(core.get("transport"), dict)
        or not isinstance(core.get("plan_sha256"), str)
        or not _SHA256.fullmatch(core["plan_sha256"])
        or not isinstance(core.get("review_sha256"), str)
        or not _SHA256.fullmatch(core["review_sha256"])
    ):
        raise LedgerError("Upload ledger intent is missing its bound file/app identity")


def _check_reconciliation(core: Mapping[str, Any]) -> None:
    if (
        core.get("resolution") not in ("succeeded", "failed")
        or not isinstance(core.get("reconciled_by"), str)
        or not core["reconciled_by"].strip()
        or not isinstance(core.get("reconciled_at"), str)
        or not core["reconciled_at"].strip()
        or not isinstance(core.get("evidence"), str)
        or not core["evidence"].strip()
    ):
        raise LedgerError("Upload ledger reconciliation is incomplete")
    key = core.get("file_key")
    if core["resolution"] == "succeeded":
        if not isinstance(key, str) or not _FILE_KEY.fullmatch(key):
            raise LedgerError("A reconciliation to succeeded requires an explicit file key")
    elif key is not None:
        raise LedgerError("A reconciliation to failed cannot carry a file key")


# --------------------------------------------------------------------------
# Receipts
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class UploadReceipt:
    """A hash-bound receipt: file identity + app identity + request state.

    ``is_factory_receipt`` is ``True`` only when a transport that declared
    itself live produced an actual protocol success. A mock-protocol success
    and a human reconciliation both report ``False``.
    """

    attempt_id: str
    endpoint: str
    app_identity: str
    upload_name: str
    file_sha256: str
    file_md5: str
    file_size: int
    plan_sha256: str
    review_sha256: str
    file_key: str
    request_state: str
    evidence: str
    reused: bool

    @property
    def is_factory_receipt(self) -> bool:
        return self.evidence == EVIDENCE_LIVE

    @property
    def states(self) -> dict[str, str]:
        """Stage vocabulary shared with the plan/review records (#5142/#5143)."""
        upload = {
            EVIDENCE_LIVE: "uploaded",
            EVIDENCE_MOCK: EVIDENCE_MOCK,
            EVIDENCE_RECONCILED: "reconciled",
        }[self.evidence]
        return {
            "human_approval": "reviewed",
            "upload": upload,
            "factory_matching": "not-observed",
            "order": "not-performed",
        }

    @property
    def receipt_bytes(self) -> bytes:
        """Canonical JSON of the bound receipt core (stable, sorted, UTF-8)."""
        return _json(
            {
                "schema_version": SCHEMA_VERSION,
                "attempt_id": self.attempt_id,
                "endpoint": self.endpoint,
                "app_identity": self.app_identity,
                "file": {
                    "upload_name": self.upload_name,
                    "sha256": self.file_sha256,
                    "md5": self.file_md5,
                    "size": self.file_size,
                },
                "plan_sha256": self.plan_sha256,
                "review_sha256": self.review_sha256,
                "file_key": self.file_key,
                "request_state": self.request_state,
                "evidence": self.evidence,
                "factory_receipt": self.is_factory_receipt,
                "states": self.states,
            }
        )

    @property
    def sha256(self) -> str:
        return _digest(self.receipt_bytes)


@dataclass(frozen=True)
class PreviewResult:
    """A parsing/preview payload, bound to the file key it was fetched for.

    This slice keeps the preview call structurally separate from the upload
    call and persists the binding plus the payload digest. Interpreting the
    payload (DFM/preview binding proper) is #5146's scope, not this module's.
    """

    attempt_id: str
    endpoint: str
    app_identity: str
    file_sha256: str
    file_key: str
    payload_sha256: str
    payload_size: int
    data: Any
    evidence: str

    @property
    def is_factory_result(self) -> bool:
        return self.evidence == EVIDENCE_LIVE


def _receipt(attempt: UploadAttempt, *, reused: bool) -> UploadReceipt:
    intent = attempt.intent
    key = attempt.file_key
    evidence = attempt.evidence
    if key is None or evidence is None:
        raise LedgerError("Cannot build a receipt from an attempt with no bound file key")
    return UploadReceipt(
        attempt_id=attempt.attempt_id,
        endpoint=intent["endpoint"],
        app_identity=intent["app_identity"],
        upload_name=intent["file"]["upload_name"],
        file_sha256=intent["file"]["sha256"],
        file_md5=intent["file"]["md5"],
        file_size=intent["file"]["size"],
        plan_sha256=intent["plan_sha256"],
        review_sha256=intent["review_sha256"],
        file_key=key,
        request_state=(
            "reconciled-succeeded" if evidence == EVIDENCE_RECONCILED else STATE_SUCCEEDED
        ),
        evidence=evidence,
        reused=reused,
    )


# --------------------------------------------------------------------------
# State queries (always recomputed from the durable record)
# --------------------------------------------------------------------------


def _bound(
    ledger: UploadLedger, *, file_sha256: str, app_identity: str, endpoint: str
) -> list[UploadAttempt]:
    key = (file_sha256, app_identity, endpoint)
    return [a for a in ledger.attempts().values() if a.binding == key]


def upload_state(
    ledger: UploadLedger, *, file_sha256: str, app_identity: str, endpoint: str
) -> str:
    """Return the state of this exact (file hash, app identity, endpoint) binding.

    Unresolved uncertainty dominates: as long as any attempt for the binding is
    ``uncertain``, the binding is ``uncertain`` and automatic retry is blocked,
    regardless of what any other attempt reported.
    """
    states = {
        a.state
        for a in _bound(
            ledger, file_sha256=file_sha256, app_identity=app_identity, endpoint=endpoint
        )
    }
    for state in (STATE_UNCERTAIN, STATE_SUCCEEDED, STATE_FAILED):
        if state in states:
            return state
    return STATE_NOT_ATTEMPTED


def find_receipt(
    ledger: UploadLedger, *, file_sha256: str, app_identity: str, endpoint: str
) -> UploadReceipt | None:
    """Return the reusable receipt for this exact binding, or ``None``.

    A receipt is *never* selected by filename, upload order, or revision -- the
    file hash, app identity and endpoint must all match exactly, and any
    unresolved uncertainty for the binding suppresses reuse entirely.
    """
    attempts = _bound(ledger, file_sha256=file_sha256, app_identity=app_identity, endpoint=endpoint)
    if any(a.state == STATE_UNCERTAIN for a in attempts):
        return None
    for attempt in attempts:
        if attempt.state == STATE_SUCCEEDED:
            return _receipt(attempt, reused=True)
    return None


def pending_reconciliations(ledger: UploadLedger) -> list[dict[str, Any]]:
    """Return each unresolved uncertain attempt's own persisted intent.

    This is the worklist for the explicit recovery step: a human (or a
    dedicated reconciliation command) confirms the true server-side state for
    each entry and resolves it with :func:`reconcile_upload`.
    """
    return [
        dict(attempt.intent)
        for attempt in ledger.attempts().values()
        if attempt.state == STATE_UNCERTAIN
    ]


def reconcile_upload(
    ledger: UploadLedger,
    *,
    attempt_id: str,
    resolution: str,
    reconciled_by: str,
    reconciled_at: str,
    evidence: str,
    file_key: str | None = None,
) -> str:
    """Explicitly resolve one uncertain attempt. Performs no network call.

    ``resolution`` is ``"succeeded"`` (the reconciler confirmed the factory
    holds the file, and supplies the ``file_key`` it was issued) or
    ``"failed"`` (the reconciler confirmed it does not). Both require a
    reconciler identity, a timestamp and a free-text ``evidence`` statement of
    what was actually checked -- this module can never synthesize any of them.

    A reconciliation is recorded as a human attestation, never as a protocol
    receipt: the resulting receipt reports
    ``evidence == "human-reconciliation"`` and ``is_factory_receipt is False``.
    """
    if resolution not in ("succeeded", "failed"):
        raise LedgerError("Reconciliation resolution must be 'succeeded' or 'failed'")
    for name, value in (
        ("reconciler identity", reconciled_by),
        ("reconciliation timestamp", reconciled_at),
        ("reconciliation evidence", evidence),
    ):
        if not isinstance(value, str) or not value.strip():
            raise LedgerError(f"Explicit {name} required")
    if resolution == "succeeded":
        if not isinstance(file_key, str) or not _FILE_KEY.fullmatch(file_key):
            raise LedgerError("A reconciliation to succeeded requires an explicit file key")
    elif file_key is not None:
        raise LedgerError("A reconciliation to failed cannot carry a file key")

    # Fence the check-then-act read of `attempt.state` against the eventual
    # `_append` the same way `upload_gerber` fences its own durable-state
    # decision -- two concurrent reconciliations for the same attempt_id would
    # otherwise both observe "uncertain" and both append a reconciliation
    # event, which permanently poisons `UploadLedger.attempts()` for every
    # binding sharing this ledger file.
    with _upload_lock(ledger):
        attempts = ledger.attempts()
        attempt = attempts.get(attempt_id)
        if attempt is None:
            raise LedgerError("Unknown upload attempt")
        if attempt.state != STATE_UNCERTAIN:
            raise LedgerError("Only an uncertain attempt can be reconciled")

        ledger._append(
            {
                "schema_version": SCHEMA_VERSION,
                "kind": _RECONCILE_KIND,
                "attempt_id": attempt_id,
                "resolution": resolution,
                "file_key": file_key,
                "reconciled_by": reconciled_by,
                "reconciled_at": reconciled_at,
                "evidence": evidence,
            }
        )
        return ledger.attempts()[attempt_id].state


# --------------------------------------------------------------------------
# Request/response helpers
# --------------------------------------------------------------------------


def _redactions(credentials: JLCCredentials, authorization: str, signature: str) -> tuple[str, ...]:
    return (
        credentials.app_id,
        credentials.access_key,
        credentials.secret_key,
        authorization,
        signature,
    )


def _trace_id(response: TransportResponse) -> str | None:
    """Return the whitelisted trace id, or ``None``. Never echoes a raw header."""
    for name, value in response.headers.items():
        if isinstance(name, str) and name.lower() == TRACE_HEADER.lower():
            if isinstance(value, str) and _TRACE_ID.fullmatch(value):
                return value
            return None
    return None


def _classification(error: JLCAPIError) -> str:
    return _CLASSIFICATIONS.get(type(error), "request-failed")


def _signed_headers(
    credentials: JLCCredentials, *, path: str, body: str
) -> tuple[dict[str, str], str]:
    """Sign ``body`` for ``path`` and return (headers, signature).

    ``body`` is the exact string that is signed. For the upload call that is
    the **metadata JSON**, not the multipart body that is actually
    transmitted (see the module docstring's provenance note).
    """
    nonce = secrets.token_hex(16)
    timestamp = str(int(time.time()))
    signature = _sign(credentials.secret_key, _string_to_sign("POST", path, timestamp, nonce, body))
    headers = {
        "Accept": "application/json",
        "Authorization": _build_auth_header(
            credentials, nonce=nonce, timestamp=timestamp, signature=signature
        ),
    }
    return headers, signature


def _envelope(response: TransportResponse) -> dict[str, Any] | None:
    status = response.status_code
    if status >= 400 and len(response.content) > _MAX_ERROR_BODY:
        return None
    try:
        # Strict parse: a duplicate key in a response envelope is treated as
        # unusable rather than silently collapsed to a last-one-wins value.
        parsed = _load_json(response.content)
    except (ValueError, UnicodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _typed_envelope(envelope: dict[str, Any] | None) -> bool:
    return (
        envelope is not None
        and type(envelope.get("code")) is int
        and type(envelope.get("success")) is bool
    )


def _successful_response(response: TransportResponse, envelope: dict[str, Any] | None) -> bool:
    return (
        _typed_envelope(envelope)
        and 200 <= response.status_code < 300
        and envelope is not None
        and envelope["code"] == 200
        and envelope["success"] is True
    )


def _whitelisted_reason(sanitized: str) -> str:
    """Keep the server's own reason only when it passes an explicit whitelist.

    :func:`~kicad_tools.parts.jlcpcb_api._safe_error_reason` already redacts
    known credential material and strips nonprintables, but a server reason is
    still attacker-influenced text. Anything that redaction touched, or that
    contains a character outside the narrow plain-prose whitelist (markup,
    quotes, braces -- i.e. an echoed raw payload), is withheld entirely and
    replaced with a fixed phrase. The classification word is computed from the
    sanitized reason *before* this step, so diagnosis is not lost.
    """
    if "[redacted]" in sanitized:
        return "server reason withheld (contained credential material)"
    if not _SAFE_REASON.fullmatch(sanitized):
        return "server reason withheld (unsafe or nonprose content)"
    return sanitized


def _business_failure(
    response: TransportResponse,
    envelope: dict[str, Any] | None,
    redactions: tuple[str, ...],
) -> tuple[str, str, int | None, str | None]:
    """Return (classification, safe reason, business code, trace id).

    The raw body is never returned, logged, or persisted -- only the fixed
    classification word, a whitelisted reason, and the whitelisted trace id.
    """
    status = response.status_code
    message = envelope.get("message") if envelope is not None else None
    sanitized = _safe_error_reason(message, redactions)
    code = envelope.get("code") if envelope is not None else None
    if type(code) is not int:
        code = None
    error = _classify_business_error(code, sanitized, http_status=status)
    return _classification(error), _whitelisted_reason(sanitized), error.code, _trace_id(response)


def _check_response_identity(
    envelope: Mapping[str, Any], *, app_identity: str, file_md5: str, file_sha256: str
) -> None:
    """Fail closed when a response echoes someone else's app or file identity."""
    for keys, expected, digest in (
        (_APP_ECHO_KEYS, app_identity, False),
        (_MD5_ECHO_KEYS, file_md5, True),
        (_SHA_ECHO_KEYS, file_sha256, True),
    ):
        for key in keys:
            if key not in envelope:
                continue
            value = envelope[key]
            if (
                not isinstance(value, str)
                or not value.strip()
                or (value.lower() if digest else value) != expected
            ):
                raise UploadIdentityError(
                    "JLCPCB response identity is malformed or mismatched; failing closed.",
                    classification="identity-mismatch",
                    reason="response identity does not match the request binding",
                )


# --------------------------------------------------------------------------
# Upload
# --------------------------------------------------------------------------


@contextlib.contextmanager
def _upload_lock(ledger: UploadLedger) -> Iterator[None]:
    """Hold an exclusive cooperative lock beside the ledger for one attempt.

    Two processes that both read "not attempted" and both send would upload the
    same bytes twice; this fences that race the same way
    :func:`~kicad_tools.export.submission_plan.prepare_submission` fences
    concurrent publication. A crash can leave the lock file behind for explicit
    inspection -- which is consistent with what that crash also leaves in the
    ledger: a dangling intent that is already blocking automatic retry.
    """
    lock = ledger.path.parent / ("." + ledger.path.name + ".upload-lock")
    try:
        lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise UploadBlockedError(
            "Another upload is already in progress for this ledger (or a previous"
            f" one crashed and left {lock.name} behind). Not uploading."
        ) from exc
    try:
        yield
    finally:
        os.close(lock_fd)
        with contextlib.suppress(OSError):
            lock.unlink()


def _plan_gerber(plan: SubmissionPlan) -> tuple[str, dict[str, Any]]:
    core = _load_json(plan.plan_bytes)
    if (
        not isinstance(core, dict)
        or core.get("schema_version") != 1
        or not isinstance(core.get("artifacts"), dict)
        or not isinstance(core.get("outputs"), dict)
    ):
        raise UploadGateError("Unsupported or malformed plan")
    artifact = core["artifacts"].get("gerber")
    if not isinstance(artifact, dict) or not isinstance(artifact.get("output_name"), str):
        raise UploadGateError("Plan is missing its bound Gerber artifact identity")
    name = artifact["output_name"]
    evidence = core["outputs"].get(name)
    if (
        not isinstance(evidence, dict)
        or not isinstance(evidence.get("sha256"), str)
        or type(evidence.get("size")) is not int
    ):
        raise UploadGateError("Plan is missing its bound Gerber output hash")
    return name, evidence


def upload_gerber(
    plan: SubmissionPlan,
    record: ReviewRecord,
    *,
    ledger: UploadLedger,
    transport: UploadTransport,
    credentials: JLCCredentials,
    requested_at: str,
    source_root: Path | None = None,
    base_url: str = JLC_OPENAPI_BASE,
    attempt_id: str | None = None,
) -> UploadReceipt:
    """Upload the plan's exact reviewed Gerber bytes, once, and record the outcome.

    Immediately before anything is sent -- never trusting a value computed in
    an earlier call or process -- this:

    1. re-verifies the published handoff against ``plan.sha256``
       (:func:`~kicad_tools.export.submission_plan.verify_submission`
       re-reads and re-hashes every published output and its file set);
    2. re-verifies the human review with
       :func:`~kicad_tools.export.submission_review.verify_review`, which
       re-hashes the record against the *current* plan (pass ``source_root``
       to also catch a source file edited after the review was recorded);
    3. re-reads the Gerber bytes and re-checks them against the plan's own
       bound hash/size;
    4. consults the durable ledger for this exact (file hash, app identity,
       endpoint) binding.

    If the binding already has a successful file key, it is reused and **no
    network call is made**. If it has an unresolved uncertain attempt,
    :class:`UploadBlockedError` is raised and **no network call is made** --
    resolve it with :func:`reconcile_upload` first.

    Otherwise the intent is appended to the ledger and ``fsync``-ed *before*
    the request is built or sent, so a crash mid-request always leaves
    evidence of exactly what was attempted (and folds to ``uncertain``).

    Raises:
        UploadGateError: a precondition failed; nothing was sent.
        UploadBlockedError: an unresolved uncertain attempt blocks retry.
        UploadUncertainError: the outcome is unknown and now needs reconciling.
        UploadRejectedError: a definite failure.
        UploadIdentityError: unresolved response identity, recorded as uncertain.
    """
    if not isinstance(requested_at, str) or not requested_at.strip():
        raise UploadGateError("Explicit request timestamp required")
    if not isinstance(base_url, str) or not base_url.strip():
        raise UploadGateError("Explicit base URL required")
    identity = _require_offline_transport(transport)
    if ledger.path.is_relative_to(plan.directory):
        raise UploadGateError("Upload ledger must live outside the published handoff")

    name, bound = _plan_gerber(plan)

    # (1) The published handoff must still be exactly what the plan says.
    try:
        verified = verify_submission(plan.directory, plan.sha256)
    except ValueError as exc:
        raise UploadGateError(f"Published handoff failed reverification: {exc}") from exc
    if verified.plan_bytes != plan.plan_bytes:
        raise UploadGateError("Published plan bytes differ from the supplied plan")

    # (2) The human review must still be valid for that exact plan/source.
    try:
        verify_review(record, plan, source_root=source_root)
    except ValueError as exc:
        raise UploadGateError(f"Bound human review is not valid: {exc}") from exc

    # (3) Re-read the exact bytes that will go on the wire.
    try:
        content = _read(plan.directory, name).data
    except ValueError as exc:
        raise UploadGateError("Published Gerber output is unreadable") from exc
    file_sha256 = _digest(content)
    if file_sha256 != bound["sha256"] or len(content) != bound["size"]:
        raise UploadGateError("Published Gerber bytes differ from the plan's bound hash")
    # MD5 here is a provisional protocol checksum, never a security
    # property -- integrity is established by the SHA-256 binding above.
    file_md5 = hashlib.md5(content, usedforsecurity=False).hexdigest()

    endpoint = base_url.rstrip("/") + UPLOAD_GERBER_PATH
    app_identity = credentials.app_id
    binding = {
        "file_sha256": file_sha256,
        "app_identity": app_identity,
        "endpoint": endpoint,
    }

    # (4) Serialize concurrent uploaders against the same ledger. This is a
    # cooperative exclusive lock, not a distributed one: it stops two local
    # processes from racing past the same durable state into a duplicate
    # upload of identical bytes.
    with _upload_lock(ledger):
        # (4) Durable state decides whether a request may happen at all.
        state = upload_state(ledger, **binding)
        if state == STATE_UNCERTAIN:
            raise UploadBlockedError(
                "A previous upload of these exact bytes ended with an uncertain outcome."
                " Automatic retry is blocked: confirm the true server-side state and"
                " resolve it with reconcile_upload() before uploading again."
            )
        if state == STATE_SUCCEEDED:
            existing = find_receipt(ledger, **binding)
            if existing is None:  # pragma: no cover -- contradicts upload_state()
                raise LedgerError("Ledger reports a success with no reusable receipt")
            return existing

        if attempt_id is None:
            attempt_id = secrets.token_hex(16)
        elif not isinstance(attempt_id, str) or not _ATTEMPT_ID.fullmatch(attempt_id):
            raise UploadGateError("attempt_id must be 32 lowercase hex characters")
        if attempt_id in ledger.attempts():
            raise UploadGateError("attempt_id is already present in the ledger")

        intent = {
            "schema_version": SCHEMA_VERSION,
            "kind": _INTENT_KIND,
            "attempt_id": attempt_id,
            "endpoint": endpoint,
            "app_identity": app_identity,
            "file": {
                "upload_name": name,
                "sha256": file_sha256,
                "md5": file_md5,
                "size": len(content),
            },
            "plan_sha256": plan.sha256,
            "review_sha256": record.sha256,
            "requested_at": requested_at,
            "transport": {"name": identity.name, "live": bool(identity.live)},
        }
        # Durably persisted BEFORE the request is built or sent.
        ledger._append(intent)

        headers, signature = _signed_headers(
            credentials, path=UPLOAD_GERBER_PATH, body=UPLOAD_META_JSON
        )
        # Lowercase hex MD5 of the raw file bytes. No Content-Type is set: the
        # transport's HTTP library generates the multipart content type and its own
        # boundary.
        headers["Content-MD5"] = file_md5
        redactions = _redactions(credentials, headers["Authorization"], signature)

        try:
            response = transport.post_multipart(
                endpoint,
                headers=headers,
                fields={UPLOAD_META_FIELD: UPLOAD_META_JSON},
                files={UPLOAD_FILE_FIELD: (name, content, UPLOAD_CONTENT_TYPE)},
            )
        except TransportError as exc:
            reason = _whitelisted_reason(_safe_error_reason(exc.reason, redactions))
            if exc.request_sent is False:
                _record_failure(ledger, attempt_id, "transport-unsent", reason, None, None, None)
                raise UploadRejectedError(
                    f"JLCPCB upload request was never sent: {reason}",
                    classification="transport-unsent",
                    reason=reason,
                ) from None
            _record_uncertain(ledger, attempt_id, "transport-uncertain", reason)
            raise UploadUncertainError(
                "JLCPCB upload outcome is UNKNOWN (the request may have been received):"
                f" {reason}. Recorded as uncertain; automatic retry is blocked until"
                " reconcile_upload() resolves it."
            ) from None
        except Exception as exc:  # noqa: BLE001 -- fail closed on an unclassified fault
            reason = _whitelisted_reason(_safe_error_reason(str(exc), redactions))
            _record_uncertain(ledger, attempt_id, "transport-unclassified", reason)
            raise UploadUncertainError(
                "JLCPCB upload outcome is UNKNOWN (unclassified transport fault):"
                f" {reason}. Recorded as uncertain; automatic retry is blocked until"
                " reconcile_upload() resolves it."
            ) from None

        envelope = _envelope(response)
        status = response.status_code
        trace_id = _trace_id(response)
        try:
            if envelope is not None:
                _check_response_identity(
                    envelope, app_identity=app_identity, file_md5=file_md5, file_sha256=file_sha256
                )
        except UploadIdentityError as exc:
            _record_uncertain(ledger, attempt_id, exc.classification, exc.reason)
            raise
        ok = _successful_response(response, envelope)
        explicit_rejection = (
            _typed_envelope(envelope)
            and envelope is not None
            and envelope["success"] is False
            and envelope["code"] != 200
        )
        if (
            not ok
            and not explicit_rejection
            and not (400 <= status < 500 and status != 408 and envelope is None)
        ):
            reason = "response did not establish a successful receipt or explicit rejection"
            _record_uncertain(ledger, attempt_id, "incomplete-response", reason)
            raise UploadUncertainError(
                "Upload outcome is unknown; automatic retry requires reconciliation"
            )
        if not ok:
            classification, reason, code, trace_id = _business_failure(
                response, envelope, redactions
            )
            _record_failure(ledger, attempt_id, classification, reason, status, code, trace_id)
            raise UploadRejectedError(
                f"JLCPCB upload failed [{classification}] (HTTP {status}, code={code},"
                f" {TRACE_HEADER}={trace_id}): {reason}",
                classification=classification,
                reason=reason,
                http_status=status,
                code=code,
                trace_id=trace_id,
            )

        assert envelope is not None
        key = envelope.get("data")
        if not isinstance(key, str) or not _FILE_KEY.fullmatch(key):
            reason = "response carried no usable file key"
            _record_uncertain(ledger, attempt_id, "incomplete-response", reason)
            raise UploadUncertainError(
                "Upload response carried no usable file key; automatic retry requires reconciliation"
            )

        ledger._append(
            {
                "schema_version": SCHEMA_VERSION,
                "kind": "success",
                "attempt_id": attempt_id,
                "file_key": key,
                "http_status": status,
                "trace_id": trace_id,
                # Never "uploaded" for a non-live transport: a mocked protocol
                # success is not a factory receipt.
                "evidence": EVIDENCE_LIVE if identity.live else EVIDENCE_MOCK,
            }
        )
        return _receipt(ledger.attempts()[attempt_id], reused=False)


def _record_failure(
    ledger: UploadLedger,
    attempt_id: str,
    classification: str,
    reason: str,
    http_status: int | None,
    code: int | None,
    trace_id: str | None,
) -> None:
    ledger._append(
        {
            "schema_version": SCHEMA_VERSION,
            "kind": "failure",
            "attempt_id": attempt_id,
            "classification": classification,
            "reason": reason,
            "http_status": http_status,
            "code": code,
            "trace_id": trace_id,
        }
    )


def _record_uncertain(
    ledger: UploadLedger, attempt_id: str, classification: str, reason: str
) -> None:
    ledger._append(
        {
            "schema_version": SCHEMA_VERSION,
            "kind": "uncertain",
            "attempt_id": attempt_id,
            "classification": classification,
            "reason": reason,
            "resolution_required": True,
        }
    )


# --------------------------------------------------------------------------
# Preview / parsing retrieval -- a separate call with its own persisted result
# --------------------------------------------------------------------------


def fetch_preview(
    receipt: UploadReceipt,
    *,
    ledger: UploadLedger,
    transport: UploadTransport,
    credentials: JLCCredentials,
    requested_at: str,
    base_url: str = JLC_OPENAPI_BASE,
    language: int | None = None,
) -> PreviewResult:
    """Fetch the parsing/preview result for an already-uploaded file key.

    This is a **separate** request from :func:`upload_gerber` -- never issued
    by it, and never inferred from an upload succeeding. The caller must pass a
    receipt whose file key is actually recorded as successful in the ledger for
    the same app identity and file hash; a receipt that does not match a
    persisted success fails closed without a request.

    On success the binding (attempt, file key, file hash, app identity) and the
    payload digest are appended to the ledger as their own record. Interpreting
    the payload is #5146's scope; nothing here derives a DFM verdict.
    """
    if not isinstance(requested_at, str) or not requested_at.strip():
        raise UploadGateError("Explicit request timestamp required")
    identity = _require_offline_transport(transport)
    if language is not None and (type(language) is not int or language < 0):
        raise UploadGateError("language must be a non-negative integer when supplied")
    if credentials.app_id != receipt.app_identity:
        raise UploadGateError("Receipt is bound to a different app identity")

    attempt = ledger.attempts().get(receipt.attempt_id)
    if (
        attempt is None
        or attempt.state != STATE_SUCCEEDED
        or attempt.file_key != receipt.file_key
        or attempt.intent["file"]["sha256"] != receipt.file_sha256
        or attempt.intent["app_identity"] != receipt.app_identity
    ):
        raise UploadGateError(
            "No persisted successful upload matches this receipt's file key binding"
        )

    # An attempt's endpoint binding is (file_sha256, app_identity, endpoint) --
    # endpoints are deliberately non-interchangeable (see upload_gerber). The
    # persisted intent's endpoint is `<upload base_url> + UPLOAD_GERBER_PATH`;
    # recover that base_url and require the caller's base_url to match it, so
    # a file key issued by a non-default host can't be silently previewed
    # against production (or vice versa).
    upload_endpoint = attempt.intent["endpoint"]
    if not upload_endpoint.endswith(UPLOAD_GERBER_PATH):
        raise UploadGateError("Persisted attempt endpoint has an unexpected shape")
    upload_base_url = upload_endpoint[: -len(UPLOAD_GERBER_PATH)]
    if base_url.rstrip("/") != upload_base_url:
        raise UploadGateError(
            "base_url does not match the host this file key was actually issued by"
        )

    body = {"key": receipt.file_key}
    if language is not None:
        body["language"] = language  # type: ignore[assignment]
    payload = _compact_json(body)
    headers, signature = _signed_headers(credentials, path=PREVIEW_AUDIT_PATH, body=payload)
    headers["Content-Type"] = "application/json"
    redactions = _redactions(credentials, headers["Authorization"], signature)
    endpoint = base_url.rstrip("/") + PREVIEW_AUDIT_PATH

    try:
        response = transport.post_json(endpoint, headers=headers, body=payload.encode("utf-8"))
    except TransportError as exc:
        reason = _whitelisted_reason(_safe_error_reason(exc.reason, redactions))
        raise UploadRejectedError(
            f"JLCPCB preview request failed: {reason}",
            classification="transport-error",
            reason=reason,
        ) from None

    envelope = _envelope(response)
    status = response.status_code
    ok = _successful_response(response, envelope)
    if not ok:
        classification, reason, code, trace_id = _business_failure(response, envelope, redactions)
        raise UploadRejectedError(
            f"JLCPCB preview failed [{classification}] (HTTP {status}, code={code},"
            f" {TRACE_HEADER}={trace_id}): {reason}",
            classification=classification,
            reason=reason,
            http_status=status,
            code=code,
            trace_id=trace_id,
        )

    assert envelope is not None
    _check_response_identity(
        envelope,
        app_identity=receipt.app_identity,
        file_md5=receipt.file_md5,
        file_sha256=receipt.file_sha256,
    )
    data = envelope.get("data")
    if data is None or (isinstance(data, str) and not data.strip()):
        raise UploadRejectedError(
            "JLCPCB preview response carried no result",
            classification="incomplete-response",
            reason="preview result is missing",
            http_status=status,
        )
    try:
        payload_bytes = _json(data)
    except (TypeError, ValueError) as exc:
        raise UploadRejectedError(
            "JLCPCB preview payload is not canonically serializable.",
            classification="incomplete-response",
            reason="preview payload is not serializable",
            http_status=status,
        ) from exc
    payload_sha256 = _digest(payload_bytes)
    evidence = EVIDENCE_LIVE if identity.live else EVIDENCE_MOCK

    ledger._append(
        {
            "schema_version": SCHEMA_VERSION,
            "kind": _PREVIEW_KIND,
            "attempt_id": receipt.attempt_id,
            "endpoint": endpoint,
            "app_identity": receipt.app_identity,
            "file": {"upload_name": receipt.upload_name, "sha256": receipt.file_sha256},
            "file_key": receipt.file_key,
            "payload_sha256": payload_sha256,
            "payload_size": len(payload_bytes),
            "requested_at": requested_at,
            "http_status": status,
            "trace_id": _trace_id(response),
            "evidence": evidence,
        }
    )
    return PreviewResult(
        attempt_id=receipt.attempt_id,
        endpoint=endpoint,
        app_identity=receipt.app_identity,
        file_sha256=receipt.file_sha256,
        file_key=receipt.file_key,
        payload_sha256=payload_sha256,
        payload_size=len(payload_bytes),
        data=data,
        evidence=evidence,
    )
