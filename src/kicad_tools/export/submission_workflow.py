"""Agent-prepared JLCPCB submission: preflight, stock gate, optional API leg, review page.

Thin orchestration over the existing, individually-tested pieces
(:mod:`.submission_plan`, :mod:`.submission_review`,
:mod:`kicad_tools.manufacturers.jlc_upload`). No state store is added: the
published plan, stock snapshot, review record, upload ledger and review page
are the records.

Tapeout manifest contract read by :func:`preflight` (``manifest.json``, written by :func:`sign_off_bundle` / ``kct tapeout-sign-off``)::

    {"files": {"<name>": {"sha256": ..., "size": ...}, ...},
     "tapeout": {"board_sha256": "<sha256 of the board file>",
                 "readiness_sha256": "<sha256 of the passing readiness.json receipt>",
                 "human_signoff": {"reviewer": "<name>", "signed_at": "<ISO 8601>",
                                   "gerber_sha256": "<sha256 of the gerber zip>"},
                 "fab_review_signoff": {"reviewer": "<name>", "signed_at": "<ISO 8601>",
                                        "gerber_sha256": "<sha256 of the gerber zip>"}}}

Two distinct attestations (issue #8818). ``human_signoff`` is the *release for
upload*: gates pass, these bytes may be sent to the fab for preview. It is
enough for every ``submit-jlcpcb`` mode up to the fab's gerber viewer / DFM
result but must not save a cart. ``fab_review_signoff`` is recorded afterwards
(``kct tapeout-sign-off --stage fab-review``), once a human has looked at the
fab's rendering of the uploaded bytes; only it lets the cart be saved. Both
bind the gerber zip sha256, and a change to the board or the gerbers
invalidates both.

Nothing here calls checkout, payment or parts purchase, and "ordered" is never
set by code: the states it reports stop at ``stock-checked`` / ``uploaded``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..parts.jlcpcb_api import JLCCredentials, JLCNoInventorySourceError
from .submission_plan import (
    FactorySettings,
    InventorySource,
    SourceEvidence,
    SubmissionError,
    SubmissionPlan,
    prepare_submission,
    refresh_inventory,
    verify_submission,
)
from .submission_review import (
    ReviewPage,
    render_review_page,
    submit_review,
)

#: Checklist the operator explicitly confirms to authorise the API upload leg.
UPLOAD_CHECKLIST = (
    "gerber-bytes-reviewed",
    "bom-cpl-reviewed",
    "stock-gate-reviewed",
)
POLICY_VERSION = "submit-jlcpcb-1"


class WorkflowError(SubmissionError):
    """A preflight, stock or upload gate refused; nothing further was attempted."""


@dataclass(frozen=True)
class Preflight:
    """Verified tapeout facts the plan inputs are derived from (never guessed)."""

    manifest: dict[str, Any]
    bundle_files: tuple[str, ...]
    artifacts: dict[str, str]
    board_layers: int
    assembly_sides: tuple[str, ...]
    board_sha256: str
    #: The valid fab-review attestation (issue #8818), or ``None`` (release-only).
    fab_review_signoff: dict[str, Any] | None = None


@dataclass(frozen=True)
class StockLine:
    catalog_id: str
    need: int
    stock: int | None
    status: str  # ok | short | unknown | covered
    reason: str = ""  # why a line is unknown, when the source says so

    def as_dict(self) -> dict[str, Any]:
        return {
            "catalog_id": self.catalog_id,
            "need": self.need,
            "stock": self.stock,
            "status": self.status,
            **({"reason": self.reason} if self.reason else {}),
        }


@dataclass(frozen=True)
class StockReport:
    lines: tuple[StockLine, ...]

    @property
    def blocked(self) -> bool:
        return any(line.status in ("short", "unknown") for line in self.lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "blocked" if self.blocked else "pass",
            "lines": [line.as_dict() for line in self.lines],
        }


@dataclass
class WorkflowResult:
    plan: SubmissionPlan
    stock: StockReport
    page: ReviewPage
    upload: dict[str, Any] | None = None
    states: dict[str, str] = field(default_factory=dict)
    fab_review_signoff: dict[str, Any] | None = None


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha(path: Path) -> str:
    return _sha(path.read_bytes())


NO_INVENTORY_MESSAGE = "no inventory source: JLC OpenAPI credentials not set"


class _NoInventory:
    """Stands in when no credentials were supplied: every code reads as unknown."""

    def get_component_inventory(self, codes: list[str]) -> Any:
        raise JLCNoInventorySourceError(NO_INVENTORY_MESSAGE)


def _count_copper_layers(board: Path) -> int:
    text = board.read_text(encoding="utf-8", errors="replace")
    block = re.search(r"\(layers\b(.*?)\n\s*\)", text, re.S)
    if block is None:
        raise WorkflowError(f"Cannot read layer table from {board.name}")
    count = len(re.findall(r'\(\d+\s+"[^"]*\.Cu"', block.group(1)))
    if count < 1:
        raise WorkflowError(f"{board.name} declares no copper layers")
    return count


def _pick(files: Collection[str], pattern: str, role: str) -> str:
    hits = sorted(n for n in files if re.search(pattern, Path(n).name, re.I))
    if len(hits) != 1:
        raise WorkflowError(
            f"Cannot identify the {role} artifact in the manifest (matches: {hits or 'none'})"
        )
    return hits[0]


def _verify_file_records(bundle_root: Path, files: dict[str, Any]) -> None:
    """Check every ``files`` record against disk, naming the file and the field at fault."""
    for name, record in sorted(files.items()):
        path = bundle_root / name
        if not isinstance(record, dict):
            raise WorkflowError(
                f"Manifest record for {name} is not an object with 'sha256' and 'size'"
            )
        for key in ("sha256", "size"):
            if key not in record:
                raise WorkflowError(
                    f"Manifest record for {name} is missing required key '{key}' "
                    "(expected files.<name>.{sha256, size})"
                )
        if not path.is_file():
            raise WorkflowError(f"Manifest lists {name} but it is not in the bundle")
        data = path.read_bytes()
        if _sha(data) != record["sha256"]:
            raise WorkflowError(f"Manifest sha256 mismatch for {name}")
        if len(data) != record["size"]:
            raise WorkflowError(
                f"Manifest size mismatch for {name} (manifest {record['size']}, disk {len(data)})"
            )


def _load_manifest(bundle_root: Path) -> dict[str, Any]:
    manifest_path = bundle_root / "manifest.json"
    if not manifest_path.is_file():
        raise WorkflowError("Tapeout manifest.json not found in bundle")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise WorkflowError("Tapeout manifest.json is not valid JSON") from exc
    if not isinstance(manifest, dict):
        raise WorkflowError("Tapeout manifest.json is not a JSON object")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise WorkflowError("Tapeout manifest lists no files")
    return manifest


#: Receipt checks that must be present and ``passed`` before a bundle can be signed.
REQUIRED_GATE_CHECKS = ("kct_check", "native_drc", "artifacts", "bom")


def _rehash_receipt_inputs(
    base: Path, hashes: Any, exempt: Path, kind: str, *, relative_to: str
) -> None:
    """Re-hash receipt ``hashes`` (name -> sha256) under ``base``; refuse on any change.

    Mirrors ``board_readiness.read_readiness``; only ``exempt`` (the bundle
    manifest sign-off itself rewrites) is skipped.
    """
    if not isinstance(hashes, dict):
        raise WorkflowError(f"Readiness receipt has an invalid '{kind}' hash map")
    root = base.resolve()
    for name, expected in hashes.items():
        rel = Path(str(name))
        if rel.is_absolute() or ".." in rel.parts or "\\" in str(name):
            raise WorkflowError(f"Readiness receipt {kind} path must be {relative_to}: {name}")
        target = (root / rel).resolve()
        if not target.is_relative_to(root):
            raise WorkflowError(f"Readiness receipt {kind} path leaves {relative_to} root: {name}")
        if target == exempt:
            continue
        try:
            actual = _sha(target.read_bytes())
        except OSError as exc:
            raise WorkflowError(
                f"Readiness receipt is stale: {name} cannot be read ({exc.__class__.__name__})"
            ) from exc
        if actual != expected:
            raise WorkflowError(
                f"Readiness receipt is stale: {name} changed after the receipt was written; "
                "re-run kct readiness --verify"
            )


_READINESS_LOCATION_MSG = (
    "Readiness receipt must be <board>/output/readiness.json or "
    "<board>/output/readiness-verification/readiness.json (where `kct readiness --verify` writes it)"
)


def _load_readiness(
    readiness: Path, board_sha: str, bundle_shas: dict[str, str], bundle_root: Path
) -> str:
    """Refuse unless a ``kct readiness`` receipt proves every gate passed for this board.

    Returns the receipt's sha256. The receipt must be ``<board>/output/readiness.json`` or
    ``<board>/output/readiness-verification/readiness.json`` (``--verify``'s location),
    ``schema_version`` 1, ``ready`` with no ``blockers``, every check ``passed``
    (including :data:`REQUIRED_GATE_CHECKS`). Every ``inputs`` / ``external_inputs``
    entry is re-hashed against disk (as ``read_readiness`` does), exempting only
    the bundle ``manifest.json`` that sign-off rewrites. Its ``inputs`` must hash
    a ``.kicad_pcb`` equal to ``--board``, and every bundle file (bar
    ``manifest.json``) must appear in ``inputs`` by sha256.
    """
    if not readiness.is_file():
        raise WorkflowError(f"Readiness receipt not found: {readiness}")
    raw = readiness.read_bytes()
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise WorkflowError("Readiness receipt is not valid JSON") from exc
    if not isinstance(data, dict):
        raise WorkflowError("Readiness receipt is not a JSON object")
    if data.get("schema_version") != 1:
        raise WorkflowError("Readiness receipt schema_version is not 1")
    if data.get("status") != "ready":
        raise WorkflowError(
            f"Readiness receipt status is {data.get('status')!r}, not 'ready'; "
            "run the tapeout gates (kct readiness --verify) until it is"
        )
    if data.get("blockers") != []:
        raise WorkflowError(f"Readiness receipt has blockers: {data.get('blockers')!r}")
    checks = data.get("checks")
    if not isinstance(checks, list) or any(not isinstance(c, dict) for c in checks):
        raise WorkflowError("Readiness receipt has no valid 'checks' list")
    status = {c.get("name"): c.get("status") for c in checks}
    missing = [n for n in REQUIRED_GATE_CHECKS if n not in status]
    failed = sorted(str(n) for n, st in status.items() if st != "passed")
    if missing or failed:
        raise WorkflowError(
            f"Readiness receipt gates not all passed (missing: {missing or 'none'}; "
            f"not passed: {failed or 'none'})"
        )
    inputs = data.get("inputs")
    if not isinstance(inputs, dict) or not inputs:
        raise WorkflowError("Readiness receipt has no 'inputs' hash map")
    readiness = readiness.resolve()
    if readiness.name != "readiness.json":
        raise WorkflowError(_READINESS_LOCATION_MSG)
    if readiness.parent.name == "output":
        board_dir = readiness.parent.parent
    elif (
        readiness.parent.name == "readiness-verification"
        and readiness.parent.parent.name == "output"
    ):
        board_dir = readiness.parent.parent.parent
    else:
        raise WorkflowError(_READINESS_LOCATION_MSG)
    exempt = (bundle_root / "manifest.json").resolve()
    _rehash_receipt_inputs(board_dir, inputs, exempt, "inputs", relative_to="board-relative")
    external = data.get("external_inputs")
    if external is not None:
        declared = data.get("project_root")
        if not isinstance(declared, str):
            raise WorkflowError("Readiness receipt has external_inputs but no project_root")
        project_root = (board_dir.resolve() / declared).resolve()
        _rehash_receipt_inputs(
            project_root, external, exempt, "external_inputs", relative_to="project-root-relative"
        )
    boards = {v for k, v in inputs.items() if str(k).endswith(".kicad_pcb")}
    if board_sha not in boards:
        raise WorkflowError("Readiness receipt was not produced for this --board (sha256 differs)")
    covered = set(inputs.values())
    uncovered = sorted(name for name, sha in bundle_shas.items() if sha not in covered)
    if uncovered:
        raise WorkflowError(f"Readiness receipt does not cover bundle files: {uncovered}")
    return _sha(raw)


SIGNOFF_STAGES = ("release", "fab-review")


def _gerber_sha(files: dict[str, Any]) -> str:
    return str(files[_pick(files, r"gerber.*\.zip$", "gerber")]["sha256"])


def sign_off_bundle(
    bundle_root: Path,
    board: Path,
    reviewer: str,
    *,
    readiness: Path | None = None,
    signed_at: str | None = None,
    stage: str = "release",
) -> dict[str, Any]:
    """Record an explicit human sign-off in ``manifest.json`` (the one producer of it).

    ``stage="release"`` (default) attests "gates pass; these bytes may be sent
    to the fab for preview". It refuses (manifest untouched) unless the bundle
    is intact and a passing ``kct readiness`` receipt (``readiness``, required
    at this stage) is bound to ``board`` and covers every bundle file; then
    writes ``tapeout.{board_sha256, readiness_sha256, human_signoff}`` and
    drops any earlier fab-review attestation.

    ``stage="fab-review"`` attests "I looked at the fab's rendering of the
    uploaded bytes". It needs a valid release sign-off for the same board and
    gerber zip, and writes ``tapeout.fab_review_signoff``.

    Both bind the gerber zip sha256. Re-running is idempotent apart from
    ``signed_at``. The reviewer is never invented: a blank name is refused.
    """
    bundle_root, board = Path(bundle_root), Path(board)
    if stage not in SIGNOFF_STAGES:
        raise WorkflowError(f"Unknown sign-off stage {stage!r} (expected one of {SIGNOFF_STAGES})")
    if not (reviewer or "").strip():
        raise WorkflowError("Sign-off needs a non-blank --reviewer (a human's name)")
    if not board.is_file():
        raise WorkflowError(f"Board file not found: {board}")
    manifest = _load_manifest(bundle_root)
    files = manifest["files"]
    on_disk = {
        p.relative_to(bundle_root).as_posix() for p in bundle_root.rglob("*") if p.is_file()
    } - {"manifest.json"}
    if on_disk != set(files):
        extra, missing = sorted(on_disk - set(files)), sorted(set(files) - on_disk)
        raise WorkflowError(
            f"Bundle files differ from manifest (not in manifest: {extra or 'none'}; "
            f"listed but absent: {missing or 'none'})"
        )
    _verify_file_records(bundle_root, files)
    board_sha = _file_sha(board)
    gerber_sha = _gerber_sha(files)
    record = {
        "reviewer": reviewer.strip(),
        "signed_at": signed_at or datetime.now(timezone.utc).isoformat(),
        "gerber_sha256": gerber_sha,
    }
    if stage == "fab-review":
        tapeout = manifest.get("tapeout")
        release = tapeout.get("human_signoff") if isinstance(tapeout, dict) else None
        if not isinstance(release, dict):
            raise WorkflowError(
                "No release sign-off to build on: run kct tapeout-sign-off (release stage) first"
            )
        assert isinstance(tapeout, dict)
        _check_signoff_binding(tapeout, release, board_sha, gerber_sha, "human_signoff")
        tapeout["fab_review_signoff"] = record
    else:
        if readiness is None:
            raise WorkflowError("The release sign-off needs --readiness <readiness.json>")
        receipt_sha = _load_readiness(
            Path(readiness),
            board_sha,
            {n: r["sha256"] for n, r in files.items() if n != "manifest.json"},
            bundle_root,
        )
        tapeout = {
            "board_sha256": board_sha,
            "readiness_sha256": receipt_sha,
            "human_signoff": record,
        }
        manifest["tapeout"] = tapeout
    (bundle_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    return tapeout


def _check_signoff_binding(
    tapeout: dict[str, Any], record: dict[str, Any], board_sha: str, gerber_sha: str, key: str
) -> None:
    """Refuse when a recorded attestation no longer matches the board or gerber zip."""
    if tapeout.get("board_sha256") != board_sha:
        raise WorkflowError("Board file sha256 differs from tapeout.board_sha256")
    bound = record.get("gerber_sha256")
    # Legacy release sign-offs predate the gerber binding; fab-review always has it.
    if (bound is None and key != "human_signoff") or (bound is not None and bound != gerber_sha):
        raise WorkflowError(
            f"Gerber zip sha256 differs from tapeout.{key}.gerber_sha256; "
            "the sign-off no longer covers these bytes, re-run kct tapeout-sign-off"
        )


def preflight(bundle_root: Path, board: Path) -> Preflight:
    """Refuse, offline, unless the tapeout bundle is intact, signed off and matches ``board``.

    Order is deliberate and everything here is local file I/O: a refusal can
    never have been preceded by a network call. It does not re-verify the
    readiness receipt; ``sign_off_bundle`` is the gate that does, and this only
    checks that a receipt hash was recorded.
    """
    bundle_root, board = Path(bundle_root), Path(board)
    manifest = _load_manifest(bundle_root)
    files = manifest["files"]
    _verify_file_records(bundle_root, files)

    tapeout = manifest.get("tapeout")
    signoff = tapeout.get("human_signoff") if isinstance(tapeout, dict) else None
    if (
        not isinstance(tapeout, dict)
        or not isinstance(signoff, dict)
        or not str(signoff.get("reviewer", "")).strip()
        or not str(signoff.get("signed_at", "")).strip()
    ):
        raise WorkflowError("No recorded human sign-off in the tapeout manifest")
    if not re.fullmatch(r"[0-9a-f]{64}", str(tapeout.get("readiness_sha256", ""))):
        raise WorkflowError(
            "Tapeout manifest records no readiness receipt (tapeout.readiness_sha256); "
            "re-run kct tapeout-sign-off --readiness <readiness.json>"
        )
    if not board.is_file():
        raise WorkflowError(f"Board file not found: {board}")
    board_sha = _file_sha(board)
    gerber = _pick(files, r"gerber.*\.zip$", "gerber")
    gerber_sha = str(files[gerber]["sha256"])
    _check_signoff_binding(tapeout, signoff, board_sha, gerber_sha, "human_signoff")
    fab_review = tapeout.get("fab_review_signoff")
    if fab_review is not None:
        # A stale or malformed fab-review record invalidates the bundle outright.
        if (
            not isinstance(fab_review, dict)
            or not str(fab_review.get("reviewer", "")).strip()
            or not str(fab_review.get("signed_at", "")).strip()
        ):
            raise WorkflowError("Malformed tapeout.fab_review_signoff in the manifest")
        _check_signoff_binding(tapeout, fab_review, board_sha, gerber_sha, "fab_review_signoff")
    bom = _pick(files, r"bom.*\.csv$", "BOM")
    cpl = _pick(files, r"(cpl|pos|placement).*\.csv$", "CPL")
    sides = _cpl_sides((bundle_root / cpl).read_bytes())
    return Preflight(
        manifest=manifest,
        bundle_files=tuple(sorted(files)),
        artifacts={"gerber": gerber, "bom": bom, "cpl": cpl},
        board_layers=_count_copper_layers(board),
        assembly_sides=sides,
        board_sha256=board_sha,
        fab_review_signoff=dict(fab_review) if fab_review is not None else None,
    )


def _cpl_sides(data: bytes) -> tuple[str, ...]:
    import csv
    import io

    rows = csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline=""))
    found = {{"Top": "top", "Bottom": "bottom"}.get(row.get("Layer", ""), "?") for row in rows}
    if not found or "?" in found:
        raise WorkflowError("CPL Layer column must be Top/Bottom to derive assembly sides")
    return tuple(sorted(found))


def stock_gate(
    plan: SubmissionPlan,
    *,
    source: InventorySource | None,
    snapshot_path: Path,
    observed_at: str,
    inventory_covered: Collection[str] = (),
    bundle_root: Path | None = None,
    source_root: Path | None = None,
) -> StockReport:
    """Observe stock at ``per_board x quantity``; short or unknown lines block.

    ``inventory_covered`` are catalog ids the operator explicitly says are in
    their own JLC parts inventory; they are shown as covered, never inferred.
    """
    snapshot = refresh_inventory(
        plan,
        source=source or _NoInventory(),
        destination=snapshot_path,
        observed_at=observed_at,
        bundle_root=bundle_root,
        source_root=source_root,
    )
    core = json.loads(snapshot.snapshot_bytes)
    observed = {o["catalog_id"]: o for o in core["observations"]}
    covered = {c.strip().upper() for c in inventory_covered}
    lines = []
    for item in core["demand"]:
        code, need = item["catalog_id"], item["required"]
        obs = observed.get(code, {})
        stock = obs.get("raw_stock") if obs.get("stock_verified") is True else None
        if code in covered:
            status = "covered"
        elif stock is None:
            status = "unknown"
        else:
            status = "ok" if stock >= need else "short"
        reason = ""
        if status == "unknown" and obs.get("status") == "no-inventory-source":
            reason = NO_INVENTORY_MESSAGE
        lines.append(StockLine(code, need, stock, status, reason))
    return StockReport(tuple(lines))


def format_stock_failures(report: StockReport) -> list[str]:
    return [
        f"{line.catalog_id}: need {line.need}, stock "
        f"{'unknown' if line.stock is None else line.stock} ({line.status})"
        + (f": {line.reason}" if line.reason else "")
        for line in report.lines
        if line.status in ("short", "unknown")
    ]


def run_submission(
    *,
    bundle_root: Path,
    board: Path,
    schematic: Path,
    quantity: int,
    output_dir: Path,
    observed_at: str | None = None,
    dry_run: bool = True,
    inventory_source: InventorySource | None = None,
    inventory_covered: Collection[str] = (),
    upload: bool = False,
    reviewer: str | None = None,
    credentials: JLCCredentials | None = None,
    transport: Any | None = None,
) -> WorkflowResult:
    """Preflight -> prepare/verify -> stock gate -> review page (-> optional API leg).

    ``dry_run`` (the default) performs no upload and builds no upload
    transport; the only network contact is the stock read through
    ``inventory_source``, and none at all when it is ``None``.
    """
    if upload and dry_run:
        raise WorkflowError("--dry-run and the upload leg are mutually exclusive")
    if upload and (credentials is None or not (reviewer or "").strip()):
        raise WorkflowError("Upload leg needs credentials and an explicit --reviewer")
    bundle_root, board, schematic = Path(bundle_root), Path(board), Path(schematic)
    output_dir = Path(output_dir)

    facts = preflight(bundle_root, board)  # offline; refuses before any network call
    source_root = board.parent
    try:
        schem_rel = schematic.resolve().relative_to(source_root.resolve()).as_posix()
    except ValueError as exc:
        raise WorkflowError("Schematic must live under the board's directory") from exc
    evidence = {
        "pcb": SourceEvidence(board.name, facts.board_sha256, board.stat().st_size),
        "schematic": SourceEvidence(schem_rel, _file_sha(schematic), schematic.stat().st_size),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    plan = prepare_submission(
        bundle_root=bundle_root,
        source_root=source_root,
        source_evidence=evidence,
        bundle_files=facts.bundle_files,
        artifacts=facts.artifacts,
        board_quantity=quantity,
        settings=FactorySettings("jlcpcb", facts.board_layers, facts.assembly_sides),
        destination=output_dir / "handoff",
    )
    plan = verify_submission(plan.directory, plan.sha256)
    plan = SubmissionPlan(plan.directory, plan.plan_bytes, bundle_root, source_root)

    now = observed_at or datetime.now(timezone.utc).isoformat()
    report = stock_gate(
        plan,
        source=inventory_source,
        snapshot_path=output_dir / "inventory-snapshot.json",
        observed_at=now,
        inventory_covered=inventory_covered,
    )

    upload_info: dict[str, Any] | None = None
    if upload:
        if report.blocked:
            raise WorkflowError(
                "Stock gate blocked; refusing upload: " + "; ".join(format_stock_failures(report))
            )
        assert credentials is not None and reviewer is not None
        upload_info = _upload_leg(
            plan, output_dir, reviewer, credentials, transport, now, source_root
        )

    page = render_review_page(
        plan,
        destination=output_dir / "review-page",
        stock=report.as_dict(),
        upload=upload_info,
    )
    states = {
        "prepared": "prepared",
        "stock_check": "blocked" if report.blocked else "stock-checked",
        "upload": (upload_info or {}).get("state", "not-performed"),
    }
    return WorkflowResult(plan, report, page, upload_info, states, facts.fab_review_signoff)


def _upload_leg(
    plan: SubmissionPlan,
    output_dir: Path,
    reviewer: str,
    credentials: JLCCredentials,
    transport: Any | None,
    now: str,
    source_root: Path,
) -> dict[str, Any]:
    from ..manufacturers.jlc_upload import (
        RequestsUploadTransport,
        UploadLedger,
        fetch_preview,
        upload_gerber,
    )

    record = submit_review(
        plan,
        reviewer=reviewer,
        reviewed_at=now,
        policy_version=POLICY_VERSION,
        checklist=dict.fromkeys(UPLOAD_CHECKLIST, True),
        required_items=UPLOAD_CHECKLIST,
        destination=output_dir / "review-record.json",
    )
    ledger = UploadLedger.open(output_dir / "upload-ledger.jsonl")
    transport = transport or RequestsUploadTransport()
    receipt = upload_gerber(
        plan,
        record,
        ledger=ledger,
        transport=transport,
        credentials=credentials,
        requested_at=now,
        source_root=source_root,
    )
    preview = fetch_preview(
        receipt, ledger=ledger, transport=transport, credentials=credentials, requested_at=now
    )
    return {
        "state": receipt.states["upload"],
        "file_key": receipt.file_key,
        "receipt_sha256": receipt.sha256,
        "preview_payload_sha256": preview.payload_sha256,
        "factory_receipt": receipt.is_factory_receipt,
    }
