"""Agent-prepared JLCPCB submission: preflight, stock gate, optional API leg, review page.

Thin orchestration over the existing, individually-tested pieces
(:mod:`.submission_plan`, :mod:`.submission_review`,
:mod:`kicad_tools.manufacturers.jlc_upload`). No state store is added: the
published plan, stock snapshot, review record, upload ledger and review page
are the records.

Tapeout manifest contract read by :func:`preflight` (``manifest.json``, written by :func:`sign_off_bundle` / ``kct tapeout-sign-off``)::

    {"files": {"<name>": {"sha256": ..., "size": ...}, ...},
     "tapeout": {"board_sha256": "<sha256 of the board file>",
                 "human_signoff": {"reviewer": "<name>", "signed_at": "<ISO 8601>"}}}

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

from ..parts.jlcpcb_api import JLCAPIError, JLCCredentials
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


@dataclass(frozen=True)
class StockLine:
    catalog_id: str
    need: int
    stock: int | None
    status: str  # ok | short | unknown | covered

    def as_dict(self) -> dict[str, Any]:
        return {
            "catalog_id": self.catalog_id,
            "need": self.need,
            "stock": self.stock,
            "status": self.status,
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


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha(path: Path) -> str:
    return _sha(path.read_bytes())


class _NoInventory:
    """Stands in when no credentials were supplied: every code reads as unknown."""

    def get_component_inventory(self, codes: list[str]) -> Any:
        raise JLCAPIError("no inventory source supplied")


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
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, dict) or not files:
        raise WorkflowError("Tapeout manifest lists no files")
    return manifest


def sign_off_bundle(
    bundle_root: Path, board: Path, reviewer: str, *, signed_at: str | None = None
) -> dict[str, Any]:
    """Record an explicit human sign-off in ``manifest.json`` (the one producer of it).

    Verifies the manifest's file set equals the bundle directory and every
    recorded sha256/size, then writes ``tapeout.{board_sha256, human_signoff}``.
    Re-running is idempotent apart from ``signed_at``. The reviewer is never
    invented: a blank name is refused.
    """
    bundle_root, board = Path(bundle_root), Path(board)
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
    manifest["tapeout"] = {
        "board_sha256": _file_sha(board),
        "human_signoff": {
            "reviewer": reviewer.strip(),
            "signed_at": signed_at or datetime.now(timezone.utc).isoformat(),
        },
    }
    (bundle_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    return manifest["tapeout"]


def preflight(bundle_root: Path, board: Path) -> Preflight:
    """Refuse, offline, unless the tapeout bundle is intact, signed off and matches ``board``.

    Order is deliberate and everything here is local file I/O: a refusal can
    never have been preceded by a network call.
    """
    bundle_root, board = Path(bundle_root), Path(board)
    manifest = _load_manifest(bundle_root)
    files = manifest["files"]
    _verify_file_records(bundle_root, files)

    tapeout = manifest.get("tapeout")
    signoff = tapeout.get("human_signoff") if isinstance(tapeout, dict) else None
    if (
        not isinstance(signoff, dict)
        or not str(signoff.get("reviewer", "")).strip()
        or not str(signoff.get("signed_at", "")).strip()
    ):
        raise WorkflowError("No recorded human sign-off in the tapeout manifest")
    if not board.is_file():
        raise WorkflowError(f"Board file not found: {board}")
    board_sha = _file_sha(board)
    if tapeout.get("board_sha256") != board_sha:
        raise WorkflowError("Board file sha256 differs from tapeout.board_sha256")

    gerber = _pick(files, r"gerber.*\.zip$", "gerber")
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
        lines.append(StockLine(code, need, stock, status))
    return StockReport(tuple(lines))


def format_stock_failures(report: StockReport) -> list[str]:
    return [
        f"{line.catalog_id}: need {line.need}, stock "
        f"{'unknown' if line.stock is None else line.stock} ({line.status})"
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
    return WorkflowResult(plan, report, page, upload_info, states)


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
