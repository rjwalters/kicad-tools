"""Hash-bound human review record and portable local review page.

See docs/guides/submission-preparation.md for milestone A
(:mod:`kicad_tools.export.submission_plan`), the immutable, read-only
handoff this module binds against. This is the second slice of #5056
(#5142 -> #5143 -> #5145 -> #5146): a clean-room Python implementation with
no copied SDK code, no runtime Java/Windows dependency, and no
order/purchase operation of any kind.

A review *record* is a small immutable JSON file binding a human-authored
checklist, a reviewer identity, a review timestamp and a review-policy
version to the exact SHA-256 hashes of the Gerber bundle, BOM, CPL, the
overall published output set, the declared source evidence, and the plan
document itself (:attr:`~kicad_tools.export.submission_plan.SubmissionPlan.
sha256`). :func:`submit_review` is the *only* function in this module that
writes one, and it only runs once every explicitly required checklist item
is present and exactly ``True`` -- a missing item, an extra/unknown item, or
an item set to ``False`` all leave the checklist incomplete, and nothing is
written. No automated check in this module (or anywhere in this repository)
can synthesize a review record on a human's behalf.

:func:`verify_review` recomputes every one of those hashes from the current
plan (and, optionally, the current source bytes on disk) and raises
:class:`ReviewError` on the first mismatch. It is fail-closed: a
re-prepared bundle, a changed source file, or a hand-edited record all
invalidate the review; nothing here ever reports a stale record as
partially valid.

:func:`render_review_page` publishes a portable, self-contained local
review page: byte-identical copies of the plan's own declared upload/
download file set, a human-readable inventory (assembly demand) report,
any caller-supplied exported layer views, and -- kept in an unambiguous,
separately labeled section -- any caller-supplied synthetic/headless-
browser test-capture evidence, which never counts as proof that a factory
received or accepted anything. The exported directory contains no ambient
absolute machine paths, no secrets, and no files beyond the reviewed
bundle's own hashed inputs/outputs plus the caller's explicitly hash-bound
assets.

States use the same terse vocabulary as ``submission_plan.py``'s own
``states`` block: ``preparation``, ``inventory``, ``human_approval``,
``factory_matching``, ``upload`` and ``order``. This module only ever
establishes ``human_approval`` (to ``"reviewed"``, once and only once an
explicit checklist submission validates); ``upload``, ``factory_matching``
and ``order`` remain untouched placeholders here -- no stage is ever
inferred from another stage's success, and this slice performs no upload,
factory-matching, or order/purchase operation of any kind.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .submission_plan import (
    SubmissionPlan,
    _digest,
    _inventory,
    _json,
    _load_json,
    _path,
    _paths,
    _read,
    _record,
    _root,
    _write_bytes,
)

_ASSET_KINDS = frozenset({"manufacturing", "synthetic-test-evidence"})
_ROLES = ("gerber", "bom", "cpl")


class ReviewError(ValueError):
    """The supplied review inputs, bound plan, or export fail the review contract."""


@dataclass(frozen=True)
class ReviewAsset:
    """A caller-supplied file bound into the review page by exact hash.

    ``kind`` is either ``"manufacturing"`` (an already-exported layer view
    or photo documenting the actual reviewed bundle) or
    ``"synthetic-test-evidence"`` (for example, a headless-browser
    screenshot of this very page, kept only for test traceability). This
    module never renders, screenshots, or otherwise generates either kind
    itself -- it only verifies and copies bytes the caller already produced
    -- so it never regenerates fabrication bytes and never launches a
    browser.
    """

    path: str
    sha256: str
    size: int
    kind: str


@dataclass(frozen=True)
class ReviewRecord:
    """Immutable, hash-bound human review record bytes and their digest."""

    path: Path
    record_bytes: bytes

    @property
    def sha256(self) -> str:
        return _digest(self.record_bytes)


@dataclass(frozen=True)
class ReviewPage:
    """A published, portable local review-page directory and its manifest."""

    directory: Path
    manifest_bytes: bytes

    @property
    def sha256(self) -> str:
        return _digest(self.manifest_bytes)


def _plan_core(plan: SubmissionPlan) -> dict[str, Any]:
    core = _load_json(plan.plan_bytes)
    if (
        not isinstance(core, dict)
        or core.get("schema_version") != 1
        or not isinstance(core.get("outputs"), dict)
        or not isinstance(core.get("bundle_files"), dict)
        or not isinstance(core.get("sources"), dict)
        or not isinstance(core.get("artifacts"), dict)
        or not isinstance(core.get("demand"), list)
        or not isinstance(core.get("settings"), dict)
        or not isinstance(core.get("states"), dict)
        or type(core.get("board_quantity")) is not int
    ):
        raise ReviewError("Unsupported or malformed plan")
    return core


def _bundle_digest(core: Mapping[str, Any]) -> str:
    return _digest(_json(core["outputs"]))


def _artifact_outputs(core: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for role in _ROLES:
        artifact = core["artifacts"].get(role)
        if not isinstance(artifact, dict) or not isinstance(artifact.get("output_name"), str):
            raise ReviewError(f"Plan is missing bound {role} artifact identity")
        output = core["outputs"].get(artifact["output_name"])
        if not isinstance(output, dict) or not isinstance(output.get("sha256"), str):
            raise ReviewError(f"Plan is missing bound {role} output hash")
        result[role] = {"output_name": artifact["output_name"], **output}
    return result


def submit_review(
    plan: SubmissionPlan,
    *,
    reviewer: str,
    reviewed_at: str,
    policy_version: str,
    checklist: Mapping[str, bool],
    required_items: Sequence[str],
    destination: Path,
) -> ReviewRecord:
    """Write a new, hash-bound human review record for ``plan``.

    This is the only function that writes a review record, and it requires
    an explicit human checklist submission: every item in ``required_items``
    (the review-policy's own required checklist, supplied by the caller --
    never hardcoded or defaulted here) must appear in ``checklist`` mapped
    to exactly ``True``. A missing item, an unrecognized extra item, or a
    ``False``/falsy item all raise :class:`ReviewError` and publish nothing.
    """
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ReviewError("Explicit reviewer identity required")
    if not isinstance(reviewed_at, str) or not reviewed_at.strip():
        raise ReviewError("Explicit review timestamp required")
    if not isinstance(policy_version, str) or not policy_version.strip():
        raise ReviewError("Explicit review-policy version required")
    required = tuple(dict.fromkeys(required_items))
    if not required or any(not isinstance(item, str) or not item.strip() for item in required):
        raise ReviewError("Explicit nonempty required checklist items required")
    if (
        not isinstance(checklist, Mapping)
        or set(checklist) != set(required)
        or any(checklist[item] is not True for item in required)
    ):
        raise ReviewError("Incomplete checklist: every required item must be explicitly checked")

    core = _plan_core(plan)
    artifact_outputs = _artifact_outputs(core)

    record_core = {
        "schema_version": 1,
        "policy_version": policy_version,
        "reviewer": reviewer,
        "reviewed_at": reviewed_at,
        "checklist": dict.fromkeys(sorted(required), True),
        "plan_sha256": plan.sha256,
        "bundle_sha256": _bundle_digest(core),
        "artifact_outputs": artifact_outputs,
        "bundle_files": core["bundle_files"],
        "sources": core["sources"],
        "board_quantity": core["board_quantity"],
        "settings": core["settings"],
        "states": dict(core["states"]) | {"human_approval": "reviewed"},
    }
    record_bytes = _json(record_core)

    destination = Path(os.path.abspath(destination))
    parent = _root(destination.parent)
    _path(destination.name)
    if destination.is_relative_to(plan.directory):
        raise ReviewError("Review record must be written outside the published handoff")
    if os.path.lexists(destination):
        raise ReviewError("Destination already exists")

    lock = parent / ("." + destination.name + ".review-lock")
    try:
        lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise ReviewError("Destination publication is already claimed") from exc
    try:
        if os.path.lexists(destination):
            raise ReviewError("Destination already exists")
        _write_bytes(destination, record_bytes)
        if _read(parent, destination.name).data != record_bytes:
            raise ReviewError("Destination bytes differ from computed review record")
        destination.chmod(0o444)
        return ReviewRecord(destination, record_bytes)
    finally:
        os.close(lock_fd)
        lock.unlink()


def verify_review(
    record: ReviewRecord,
    plan: SubmissionPlan,
    *,
    source_root: Path | None = None,
) -> dict[str, Any]:
    """Recheck ``record`` against ``plan``'s current hashes; return its core on success.

    Raises :class:`ReviewError` on any mismatch: a bundle re-prepared into a
    different plan (different :attr:`SubmissionPlan.sha256`, bundle output
    set, or per-role Gerber/BOM/CPL hash), a hand-edited or incomplete
    record, or -- when ``source_root`` is supplied -- a declared source file
    that changed on disk *after* the review was written, even though the
    plan and bundle bytes were never regenerated. This function is
    fail-closed: the first mismatch anywhere in the bound chain invalidates
    the review; nothing here treats a stale record as partially valid.
    """
    record_core = _load_json(record.record_bytes)
    if (
        not isinstance(record_core, dict)
        or record_core.get("schema_version") != 1
        or not isinstance(record_core.get("checklist"), dict)
        or not record_core["checklist"]
        or any(value is not True for value in record_core["checklist"].values())
        or not isinstance(record_core.get("plan_sha256"), str)
        or not isinstance(record_core.get("reviewer"), str)
        or not record_core["reviewer"].strip()
        or not isinstance(record_core.get("reviewed_at"), str)
        or not record_core["reviewed_at"].strip()
        or not isinstance(record_core.get("policy_version"), str)
        or not record_core["policy_version"].strip()
    ):
        raise ReviewError("Unsupported, incomplete, or malformed review record")

    core = _plan_core(plan)
    if record_core["plan_sha256"] != plan.sha256:
        raise ReviewError("Review record is bound to a different plan")
    if record_core.get("bundle_sha256") != _bundle_digest(core):
        raise ReviewError("Review record is bound to a different bundle output set")
    if record_core.get("artifact_outputs") != _artifact_outputs(core):
        raise ReviewError("Review record is bound to different Gerber/BOM/CPL outputs")
    if record_core.get("bundle_files") != core["bundle_files"]:
        raise ReviewError("Review record is bound to different bundle files")
    if record_core.get("sources") != core["sources"]:
        raise ReviewError("Review record is bound to different declared source hashes")

    if source_root is not None:
        root = _root(source_root)
        for evidence in core["sources"].values():
            current = _read(root, evidence["path"])
            if _digest(current.data) != evidence["sha256"] or len(current.data) != evidence["size"]:
                raise ReviewError("Bound source file has changed since the review was recorded")

    return record_core


def render_review_page(
    plan: SubmissionPlan,
    *,
    destination: Path,
    record: ReviewRecord | None = None,
    assets: Mapping[str, ReviewAsset] | None = None,
    assets_root: Path | None = None,
) -> ReviewPage:
    """Publish a portable local review page for ``plan``.

    If ``record`` is supplied it is independently reverified against
    ``plan`` (see :func:`verify_review`) before anything is written; a stale
    or malformed record raises :class:`ReviewError` rather than silently
    rendering an "approved" page. If ``record`` is ``None`` the page is
    rendered with an explicit, unambiguous "not reviewed" state -- human
    approval is never inferred from a prepared plan alone.

    ``assets`` (each a :class:`ReviewAsset`, resolved relative to
    ``assets_root``) are reread and reverified against their declared hash
    before being copied. Assets of ``kind="manufacturing"`` are published
    under ``layer-views/``; assets of ``kind="synthetic-test-evidence"`` are
    published under ``test-evidence/`` and are always rendered in a
    separately labeled, explicitly non-manufacturing section of the page.

    The published directory contains exactly: the page itself
    (``index.md``), a human-readable ``inventory-report.txt``, byte-exact
    copies of the plan's own declared upload/download file set under
    ``uploads/``, the caller's hash-verified assets, and a self-describing
    ``review-page-manifest.json``. No ambient absolute machine path or
    secret is embedded in any of it, and nothing beyond those exact,
    already-hashed inputs is ever written.
    """
    core = _plan_core(plan)
    record_core: dict[str, Any] | None = None
    if record is not None:
        record_core = verify_review(record, plan)

    assets = dict(assets or {})
    if assets and assets_root is None:
        raise ReviewError("assets_root is required when review assets are supplied")
    asset_root = _root(assets_root) if assets_root is not None else None
    _paths(list(assets))
    checked_assets: dict[str, tuple[ReviewAsset, bytes]] = {}
    for name, asset in assets.items():
        if not isinstance(asset, ReviewAsset) or asset.kind not in _ASSET_KINDS:
            raise ReviewError("Every review asset needs an explicit recognized kind")
        if asset.path != name:
            raise ReviewError("Asset mapping key must equal its own declared path")
        assert asset_root is not None
        snapshot = _read(asset_root, asset.path)
        if _digest(snapshot.data) != asset.sha256 or len(snapshot.data) != asset.size:
            raise ReviewError("Review asset bytes differ from its declared hash/size")
        checked_assets[name] = (asset, snapshot.data)

    destination = Path(os.path.abspath(destination))
    parent = _root(destination.parent)
    _path(destination.name)
    if destination.is_relative_to(plan.directory):
        raise ReviewError("Review page must be published outside the handoff directory")
    if asset_root is not None and destination.is_relative_to(asset_root):
        raise ReviewError("Review page must be published outside the assets root")
    if os.path.lexists(destination):
        raise ReviewError("Destination already exists")

    # Exact upload/download file set: byte-identical copies of the plan's
    # own declared outputs, reread from the published handoff and
    # reverified against the plan's own per-file hash records.
    output_names = _paths(list(core["outputs"]))
    uploads: dict[str, bytes] = {}
    for name in output_names:
        snapshot = _read(plan.directory, name)
        evidence = core["outputs"][name]
        if _digest(snapshot.data) != evidence["sha256"] or len(snapshot.data) != evidence["size"]:
            raise ReviewError("Published handoff output no longer matches its bound hash")
        uploads["uploads/" + name] = snapshot.data

    inventory_lines = [
        "Assembly inventory (planning demand only; not a live stock observation).",
        f"Board quantity: {core['board_quantity']}",
        "Factory: {factory}  Layers: {layers}  Sides: {sides}".format(
            factory=core["settings"]["factory"],
            layers=core["settings"]["board_layers"],
            sides=",".join(core["settings"]["assembly_sides"]),
        ),
        "",
        "catalog_id\tper_board\trequired",
    ]
    for item in core["demand"]:
        inventory_lines.append(f"{item['catalog_id']}\t{item['per_board']}\t{item['required']}")
    inventory_report = ("\n".join(inventory_lines) + "\n").encode("utf-8")

    manufacturing_assets = {
        "layer-views/" + name: data
        for name, (asset, data) in checked_assets.items()
        if asset.kind == "manufacturing"
    }
    test_assets = {
        "test-evidence/" + name: data
        for name, (asset, data) in checked_assets.items()
        if asset.kind == "synthetic-test-evidence"
    }

    states = dict(record_core["states"]) if record_core is not None else dict(core["states"])

    index_lines = [
        "# Local submission review page",
        "",
        "Static, portable, hash-bound snapshot. No network calls, uploads, or",
        "purchase/order operations of any kind occur while rendering this page.",
        "",
        f"Plan sha256: {plan.sha256}",
        f"Bundle sha256: {_bundle_digest(core)}",
        "",
        "## States",
        *(f"- {key}: {value}" for key, value in sorted(states.items())),
        "",
        "## Human review",
    ]
    if record_core is None:
        index_lines.append(
            "NOT REVIEWED. No valid human review record is bound to this plan; this"
            " page must never be treated as an approval."
        )
    else:
        index_lines += [
            f"Reviewer: {record_core['reviewer']}",
            f"Reviewed at: {record_core['reviewed_at']}",
            f"Policy version: {record_core['policy_version']}",
            "Checklist (all items explicitly confirmed):",
            *(f"- [x] {item}" for item in sorted(record_core["checklist"])),
        ]
    index_lines += [
        "",
        "## Upload/download file set",
        *(f"- uploads/{name}" for name in output_names),
        "",
        "## Layer views (manufacturing evidence)",
    ]
    index_lines += (
        [f"- {path}" for path in sorted(manufacturing_assets)]
        if manufacturing_assets
        else ["(none supplied)"]
    )
    index_lines += [
        "",
        "## Synthetic/headless test evidence -- NOT MANUFACTURING EVIDENCE",
        "Everything below is automated test-capture output only. It is never proof",
        "that any factory portal received, matched, or accepted an upload.",
    ]
    index_lines += (
        [f"- {path}" for path in sorted(test_assets)] if test_assets else ["(none supplied)"]
    )
    index_lines += ["", "## Inventory report", "See inventory-report.txt", ""]
    index_bytes = "\n".join(index_lines).encode("utf-8")

    files: dict[str, bytes] = {
        "index.md": index_bytes,
        "inventory-report.txt": inventory_report,
        **uploads,
        **manufacturing_assets,
        **test_assets,
    }
    for name in files:
        _path(name)
    if len({name.casefold() for name in files}) != len(files):
        raise ReviewError("Duplicate review-page file name")

    manifest_core = {
        "schema_version": 1,
        "plan_sha256": plan.sha256,
        "review_sha256": record.sha256 if record is not None else None,
        "files": {name: _record(data) for name, data in files.items()},
    }
    manifest_bytes = _json(manifest_core)
    files["review-page-manifest.json"] = manifest_bytes

    lock = parent / ("." + destination.name + ".page-lock")
    try:
        lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise ReviewError("Destination publication is already claimed") from exc
    stage: Path | None = None
    try:
        if os.path.lexists(destination):
            raise ReviewError("Destination already exists")
        stage = Path(tempfile.mkdtemp(prefix="." + destination.name + ".stage-", dir=parent))
        for name, data in files.items():
            path = stage / name
            path.parent.mkdir(parents=True, exist_ok=True)
            _write_bytes(path, data)
        for name, data in files.items():
            if _read(stage, name).data != data:
                raise ReviewError("Destination bytes differ from computed review page content")
        _inventory(stage, set(files))
        for name in files:
            (stage / name).chmod(0o444)
        for directory in sorted(
            {str(Path(name).parent) for name in files if Path(name).parent != Path(".")},
            key=len,
            reverse=True,
        ):
            (stage / directory).chmod(0o555)
        stage.chmod(0o555)
        if os.path.lexists(destination):
            raise ReviewError("Destination already exists")
        os.rename(stage, destination)
        stage = None
        return ReviewPage(destination, manifest_bytes)
    finally:
        os.close(lock_fd)
        lock.unlink()
        if stage is not None:
            stage.chmod(0o700)
            for root, dirs, _files in os.walk(stage):
                for d in dirs:
                    os.chmod(os.path.join(root, d), 0o700)
            shutil.rmtree(stage)
