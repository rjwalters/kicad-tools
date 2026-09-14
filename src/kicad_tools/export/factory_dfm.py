"""Bind raster factory DFM reports and their transcribed evidence to exact uploads.

This is the fourth and final slice of #5056's JLCPCB-handoff decomposition
(#5142 -> #5143 -> #5145 -> #5146): a clean-room Python implementation with no
copied SDK code and no runtime Java/Windows dependency. Like its three merged
siblings (:mod:`kicad_tools.export.submission_plan`,
:mod:`kicad_tools.export.submission_review`, and
:mod:`kicad_tools.manufacturers.jlc_upload`), this module is a pure,
dependency-injectable binding/comparison module: it makes no network call, and
it never renders, screenshots, OCRs, or otherwise generates the evidence it
binds. A human (or another tool) already produced the report bytes and, when
the report is raster/image-only, already produced a manual or OCR-assisted
transcription of its findings; this module only validates and binds that
already-produced evidence together with the exact upload identity it claims
to have checked.

What this module never does
----------------------------
* **No headless DFM verification claim.** This module does not run JLCPCB's
  own DFM checker and does not claim to. It parses/binds evidence a human (or
  an OCR pass) already produced from the factory's own report.
* **No report bytes are ever edited, re-encoded, or "cleaned up."** The only
  thing ever computed from ``report_bytes`` is its own SHA-256 digest and
  size, for later re-verification with :func:`verify_dfm_attachment`.
* **No implicit "selected PCB/SMT modules" from a component-selection CSV.**
  :mod:`kicad_tools.export.factory_selection` verifies which populated
  component references a factory's selection CSV says are selected -- it has
  no concept of which DFM analysis modules (PCB fabrication vs. SMT/assembly)
  a report actually covered, and this module never infers one from it. The
  analysis-module coverage bound here comes only from the DFM report's own
  transcribed evidence; a module the report never mentions stays explicitly
  unknown (``covered=None``), never defaulted to ``True``.
* **No implicit "pass."** Zero successfully transcribed finding rows, an
  explicit OCR failure, an incomplete per-category breakdown, or a
  cap/truncation flag that could be hiding additional findings all keep the
  computed :attr:`DFMAttachment.dfm_status` at ``"unresolved"`` -- never
  ``"pass"``. A clean bill of health is never the default outcome of missing
  or incomplete evidence.
* **No live upload, preview, or protocol bypass.** This module only *reads* an
  already-durable :class:`~kicad_tools.manufacturers.jlc_upload.UploadLedger`
  (optional) to look up an already-recorded
  :class:`~kicad_tools.manufacturers.jlc_upload.UploadReceipt` by its exact
  ``(file_sha256, app_identity, endpoint)`` binding -- the same "never
  selected by filename" invariant
  :func:`~kicad_tools.manufacturers.jlc_upload.find_receipt` already
  enforces. An attachment can be created fully offline, with no ledger at
  all; its :attr:`DFMAttachment.upload_binding` then stays explicitly
  ``"unknown"`` rather than silently ``"bound"``.
* **No Windows dependency.** Like its siblings, this module performs no
  filesystem I/O of its own (it is a pure comparison/binding module, matching
  :mod:`kicad_tools.export.factory_selection`'s style rather than the
  file-publishing style of ``submission_plan``/``submission_review``) and
  runs anywhere Python does.

Filename/revision alone is never sufficient identity
-----------------------------------------------------
A DFM attachment's binding is the *combination* of the exact Gerber bundle
hash (``gerber_sha256``), the exact application identity that would upload
it (``app_identity``) and endpoint, and the report's own declared revision
and checker/report timestamp -- never any one of those alone. An optional
``report_claimed_gerber_sha256`` lets a caller transcribe a hash the report
itself states it audited (many factory DFM/audit pages echo the file
identity they checked); when supplied and it disagrees with the current
``gerber_sha256``, the report is stale or was run against a different
Gerber bundle (for example, a v25 report reviewed against the current v27
Gerber) and :func:`attach_dfm_report` raises :class:`DFMError` rather than
silently binding a report to a bundle it never actually examined.

When an ``UploadLedger`` is supplied but its bound receipt's own
``plan_sha256``/``review_sha256`` no longer match a currently supplied
:class:`~kicad_tools.export.submission_plan.SubmissionPlan` /
:class:`~kicad_tools.export.submission_review.ReviewRecord`, the receipt is
stale relative to the current submission state and is likewise rejected --
mirroring :func:`~kicad_tools.manufacturers.jlc_upload.upload_gerber`'s own
re-verify-before-trust sequence and
:func:`~kicad_tools.manufacturers.jlc_upload._check_response_identity`'s
fail-closed identity pattern.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .submission_plan import SubmissionPlan, _digest, _json

if TYPE_CHECKING:
    from ..manufacturers.jlc_upload import UploadLedger, UploadReceipt
    from .submission_review import ReviewRecord

__all__ = [
    "CategoryFinding",
    "DFMAttachment",
    "DFMError",
    "ModuleCoverage",
    "TranscriptionEvidence",
    "attach_dfm_report",
    "verify_dfm_attachment",
]

SCHEMA_VERSION = 1

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PROVENANCE_KINDS = frozenset({"manual", "ocr", "unknown"})
_MODULES = frozenset({"pcb_fabrication", "smt_assembly"})


class DFMError(ValueError):
    """The supplied DFM report, transcription, or upload binding is invalid."""


@dataclass(frozen=True)
class CategoryFinding:
    """One DFM finding category's transcribed evidence.

    ``count`` and ``limit`` are ``None`` when the report doesn't state them
    (never defaulted to zero). ``capped`` is ``True`` when the report itself
    flags the category as truncated/capped (the true count may exceed
    ``count``), ``False`` when the report explicitly states the category was
    not truncated, and ``None`` when the report doesn't say either way.
    ``threshold`` is the raw transcribed threshold text (for example,
    ``"0.1 mm"``), or ``None`` when not stated.
    """

    category: str
    count: int | None
    limit: int | None
    capped: bool | None
    coordinates_available: bool
    threshold: str | None


@dataclass(frozen=True)
class ModuleCoverage:
    """Whether the DFM report's own evidence states coverage for one module.

    ``module`` is ``"pcb_fabrication"`` or ``"smt_assembly"``. ``covered`` is
    ``True``/``False`` only when the report itself explicitly states whether
    that analysis module ran; ``None`` when the report never says.
    """

    module: str
    covered: bool | None


@dataclass(frozen=True)
class TranscriptionEvidence:
    """A human or OCR-assisted transcription of a (possibly raster-only) report.

    ``provenance`` records who/what performed the transcription:
    ``"manual"``, ``"ocr"``, or ``"unknown"`` when even that isn't known.
    ``performed_by`` is an explicit, never-fabricated identity string (pass
    the literal ``"unknown"`` when it truly isn't known -- an empty string is
    rejected). ``confidence`` is a ``0.0``-``1.0`` float, or ``None`` when
    unknown. ``extracted_rows`` is the total count of finding rows the
    transcription actually produced (``0`` when nothing was successfully
    transcribed). ``ocr_failed`` is an explicit boolean flag for an OCR pass
    that failed outright. ``image_only_source`` records whether the source
    report had no embedded text layer (raster-only / scanned), for context;
    it does not by itself change :func:`attach_dfm_report`'s computed status
    -- a raster-only report with a genuinely successful transcription is not
    penalized for its source format.
    """

    provenance: str
    performed_by: str
    confidence: float | None
    extracted_rows: int
    ocr_failed: bool
    image_only_source: bool
    categories: tuple[CategoryFinding, ...]
    modules: tuple[ModuleCoverage, ...]


@dataclass(frozen=True)
class DFMAttachment:
    """An immutable, hash-bound DFM report attachment.

    ``dfm_status`` is one of ``"pass"``, ``"fail"``, or ``"unresolved"`` --
    computed by :func:`attach_dfm_report` from the transcription evidence,
    never accepted as a caller-supplied claim. ``upload_binding`` is
    ``"bound"`` only when a real, currently valid
    :class:`~kicad_tools.manufacturers.jlc_upload.UploadReceipt` was found
    for the exact ``(gerber_sha256, app_identity, endpoint)`` binding;
    otherwise it is explicitly ``"unknown"``.
    """

    report_sha256: str
    report_size: int
    report_revision: str
    checker_time: str
    report_label: str | None
    gerber_sha256: str
    app_identity: str
    endpoint: str
    upload_binding: str
    receipt_sha256: str | None
    dfm_status: str
    transcription: TranscriptionEvidence
    modules: tuple[ModuleCoverage, ...]

    @property
    def states(self) -> dict[str, str]:
        """Stage vocabulary compatible with the plan/review/upload records."""
        return {"dfm": self.dfm_status, "upload_binding": self.upload_binding}

    @property
    def readiness_eligible(self) -> bool:
        """``True`` only once the DFM evidence passed AND the upload is bound.

        Readiness/Ready-badge integration must consult this rather than
        ``dfm_status`` alone: a "pass" transcription bound to an
        ``upload_binding`` of ``"unknown"`` (no verified #5145 receipt yet)
        must never promote a board.
        """
        return self.dfm_status == "pass" and self.upload_binding == "bound"

    @property
    def attachment_bytes(self) -> bytes:
        """Canonical JSON of the bound attachment core (stable, sorted, UTF-8)."""
        transcription = self.transcription
        return _json(
            {
                "schema_version": SCHEMA_VERSION,
                "report": {
                    "sha256": self.report_sha256,
                    "size": self.report_size,
                    "revision": self.report_revision,
                    "checker_time": self.checker_time,
                    "label": self.report_label,
                },
                "gerber_sha256": self.gerber_sha256,
                "app_identity": self.app_identity,
                "endpoint": self.endpoint,
                "upload_binding": self.upload_binding,
                "receipt_sha256": self.receipt_sha256,
                "dfm_status": self.dfm_status,
                "modules": [
                    {"module": module.module, "covered": module.covered} for module in self.modules
                ],
                "transcription": {
                    "provenance": transcription.provenance,
                    "performed_by": transcription.performed_by,
                    "confidence": transcription.confidence,
                    "extracted_rows": transcription.extracted_rows,
                    "ocr_failed": transcription.ocr_failed,
                    "image_only_source": transcription.image_only_source,
                    "categories": [
                        {
                            "category": finding.category,
                            "count": finding.count,
                            "limit": finding.limit,
                            "capped": finding.capped,
                            "coordinates_available": finding.coordinates_available,
                            "threshold": finding.threshold,
                        }
                        for finding in transcription.categories
                    ],
                },
                "states": self.states,
            }
        )

    @property
    def sha256(self) -> str:
        return _digest(self.attachment_bytes)


def _check_category(finding: CategoryFinding) -> None:
    if not isinstance(finding, CategoryFinding) or not finding.category.strip():
        raise DFMError("Every category finding needs an explicit nonblank category name")
    if finding.count is not None and (type(finding.count) is not int or finding.count < 0):
        raise DFMError(f"{finding.category}: count must be a non-negative integer or unknown")
    if finding.limit is not None and (type(finding.limit) is not int or finding.limit < 0):
        raise DFMError(f"{finding.category}: limit must be a non-negative integer or unknown")
    if finding.capped is not None and type(finding.capped) is not bool:
        raise DFMError(f"{finding.category}: capped must be an explicit boolean or unknown")
    if finding.capped is True and finding.count is None:
        raise DFMError(f"{finding.category}: a capped/truncated category needs its observed count")
    if type(finding.coordinates_available) is not bool:
        raise DFMError(f"{finding.category}: coordinates_available must be an explicit boolean")
    if finding.threshold is not None and (
        not isinstance(finding.threshold, str) or not finding.threshold.strip()
    ):
        raise DFMError(f"{finding.category}: threshold must be a nonblank string or unknown")


def _check_module(module: ModuleCoverage) -> None:
    if not isinstance(module, ModuleCoverage) or module.module not in _MODULES:
        raise DFMError("Every module coverage entry needs a recognized module name")
    if module.covered is not None and type(module.covered) is not bool:
        raise DFMError(f"{module.module}: covered must be an explicit boolean or unknown")


def _check_transcription(transcription: TranscriptionEvidence) -> None:
    if not isinstance(transcription, TranscriptionEvidence):
        raise DFMError("Explicit TranscriptionEvidence required")
    if transcription.provenance not in _PROVENANCE_KINDS:
        raise DFMError("Transcription provenance must be 'manual', 'ocr', or 'unknown'")
    if not isinstance(transcription.performed_by, str) or not transcription.performed_by.strip():
        raise DFMError(
            "Explicit transcription performer identity required (use 'unknown' if truly unknown)"
        )
    if transcription.confidence is not None and (
        type(transcription.confidence) not in (int, float)
        or not 0.0 <= float(transcription.confidence) <= 1.0
    ):
        raise DFMError("Transcription confidence must be a 0..1 value or unknown")
    if type(transcription.extracted_rows) is not int or transcription.extracted_rows < 0:
        raise DFMError("extracted_rows must be a non-negative integer")
    if type(transcription.ocr_failed) is not bool:
        raise DFMError("ocr_failed must be an explicit boolean")
    if type(transcription.image_only_source) is not bool:
        raise DFMError("image_only_source must be an explicit boolean")

    categories = transcription.categories
    if not isinstance(categories, tuple) or any(
        not isinstance(finding, CategoryFinding) for finding in categories
    ):
        raise DFMError("categories must be an explicit tuple of CategoryFinding")
    for finding in categories:
        _check_category(finding)
    names = [finding.category for finding in categories]
    if len(set(names)) != len(names):
        raise DFMError("Duplicate DFM category name")

    modules = transcription.modules
    if not isinstance(modules, tuple) or any(
        not isinstance(module, ModuleCoverage) for module in modules
    ):
        raise DFMError("modules must be an explicit tuple of ModuleCoverage")
    for module in modules:
        _check_module(module)
    module_names = [module.module for module in modules]
    if len(set(module_names)) != len(module_names):
        raise DFMError("Duplicate DFM module coverage entry")


def _normalized_modules(modules: tuple[ModuleCoverage, ...]) -> tuple[ModuleCoverage, ...]:
    """Every recognized module, filling anything the report never stated as unknown."""
    by_name = {module.module: module for module in modules}
    return tuple(by_name.get(name, ModuleCoverage(name, None)) for name in sorted(_MODULES))


def _category_status(finding: CategoryFinding) -> str:
    if finding.count is None or finding.limit is None or finding.capped is None:
        return "unknown"
    if finding.capped:
        # A truncated/capped category's true count may exceed what was
        # recorded; it can never be reported as within-limits from a count
        # that was itself cut off.
        return "unknown"
    if finding.count > finding.limit:
        return "exceeded"
    return "ok"


def _dfm_status(transcription: TranscriptionEvidence) -> str:
    """Compute the DFM verdict; never "pass" from absent or failed evidence."""
    if transcription.extracted_rows == 0 or transcription.ocr_failed:
        return "unresolved"
    if not transcription.categories:
        return "unresolved"
    statuses = {_category_status(finding) for finding in transcription.categories}
    if "unknown" in statuses:
        return "unresolved"
    if "exceeded" in statuses:
        return "fail"
    return "pass"


def attach_dfm_report(
    *,
    report_bytes: bytes,
    report_revision: str,
    checker_time: str,
    gerber_sha256: str,
    app_identity: str,
    endpoint: str,
    transcription: TranscriptionEvidence,
    report_claimed_gerber_sha256: str | None = None,
    report_label: str | None = None,
    ledger: UploadLedger | None = None,
    plan: SubmissionPlan | None = None,
    review: ReviewRecord | None = None,
) -> DFMAttachment:
    """Bind an original DFM report and its transcription to an exact upload.

    ``report_bytes`` is never edited, re-encoded, or otherwise transformed --
    only its own SHA-256/size are recorded, for later re-verification with
    :func:`verify_dfm_attachment`. ``gerber_sha256``, ``app_identity`` and
    ``endpoint`` are the exact Gerber bundle hash and application identity
    this report was run against; a bare filename or revision string alone is
    never accepted as identity (there is no such parameter here).

    If ``report_claimed_gerber_sha256`` is supplied (transcribed from a
    report that itself echoes the identity of the file it audited) and
    disagrees with ``gerber_sha256``, the report is stale or bound to a
    different bundle and :class:`DFMError` is raised.

    If ``ledger`` is supplied, this looks up a receipt for the exact
    ``(gerber_sha256, app_identity, endpoint)`` binding via
    :func:`~kicad_tools.manufacturers.jlc_upload.find_receipt` (never by
    filename or upload order). When found, ``plan``/``review`` -- if
    supplied -- must still match the receipt's own bound
    ``plan_sha256``/``review_sha256``; a stale receipt raises
    :class:`DFMError` rather than being silently bound. When no ledger is
    supplied, or no matching receipt exists yet, the resulting
    :attr:`DFMAttachment.upload_binding` is explicitly ``"unknown"`` -- this
    attachment can be created entirely offline.

    :attr:`DFMAttachment.dfm_status` is computed here from ``transcription``,
    never accepted as a caller claim: zero extracted rows, an explicit OCR
    failure, an entirely absent per-category breakdown, or any category left
    ``"unknown"`` (missing count/limit, or flagged capped/truncated) keeps
    the status at ``"unresolved"`` rather than an implicit ``"pass"``.
    """
    if not isinstance(report_bytes, (bytes, bytearray)) or not report_bytes:
        raise DFMError("Explicit nonempty original DFM report bytes required")
    if not isinstance(report_revision, str) or not report_revision.strip():
        raise DFMError("Explicit report revision required (use 'unknown' if not stated)")
    if not isinstance(checker_time, str) or not checker_time.strip():
        raise DFMError("Explicit checker/report timestamp required (use 'unknown' if not stated)")
    if not isinstance(gerber_sha256, str) or not _SHA256.fullmatch(gerber_sha256):
        raise DFMError("Explicit lowercase hex SHA-256 Gerber bundle hash required")
    if not isinstance(app_identity, str) or not app_identity.strip():
        raise DFMError("Explicit app identity required")
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise DFMError("Explicit upload endpoint required")
    if report_label is not None and (not isinstance(report_label, str) or not report_label.strip()):
        raise DFMError("report_label must be a nonblank string when supplied")
    if report_claimed_gerber_sha256 is not None:
        if not isinstance(report_claimed_gerber_sha256, str) or not _SHA256.fullmatch(
            report_claimed_gerber_sha256
        ):
            raise DFMError(
                "report_claimed_gerber_sha256 must be a lowercase hex SHA-256 or omitted"
            )
        if report_claimed_gerber_sha256 != gerber_sha256:
            raise DFMError(
                "DFM report is stale or bound to a different Gerber bundle than the one supplied"
            )
    _check_transcription(transcription)

    receipt: UploadReceipt | None = None
    if ledger is not None:
        from ..manufacturers.jlc_upload import find_receipt

        receipt = find_receipt(
            ledger, file_sha256=gerber_sha256, app_identity=app_identity, endpoint=endpoint
        )
    if receipt is not None:
        if plan is not None and receipt.plan_sha256 != plan.sha256:
            raise DFMError("Bound upload receipt is stale relative to the current submission plan")
        if review is not None and receipt.review_sha256 != review.sha256:
            raise DFMError("Bound upload receipt is stale relative to the current review record")

    upload_binding = "bound" if receipt is not None else "unknown"
    receipt_sha256 = receipt.sha256 if receipt is not None else None

    return DFMAttachment(
        report_sha256=_digest(bytes(report_bytes)),
        report_size=len(report_bytes),
        report_revision=report_revision,
        checker_time=checker_time,
        report_label=report_label,
        gerber_sha256=gerber_sha256,
        app_identity=app_identity,
        endpoint=endpoint,
        upload_binding=upload_binding,
        receipt_sha256=receipt_sha256,
        dfm_status=_dfm_status(transcription),
        transcription=transcription,
        modules=_normalized_modules(transcription.modules),
    )


def verify_dfm_attachment(attachment: DFMAttachment, report_bytes: bytes) -> None:
    """Recheck ``attachment`` against the exact report bytes it claims to bind.

    Raises :class:`DFMError` if the original report bytes no longer match the
    attachment's bound hash/size -- the report itself is never edited,
    re-encoded, or "cleaned up" by this module, so any drift here means the
    caller is holding the wrong file, not that the file changed legitimately.
    """
    if not isinstance(report_bytes, (bytes, bytearray)):
        raise DFMError("Explicit report bytes required")
    if (
        _digest(bytes(report_bytes)) != attachment.report_sha256
        or len(report_bytes) != attachment.report_size
    ):
        raise DFMError("DFM report bytes no longer match the bound attachment")
