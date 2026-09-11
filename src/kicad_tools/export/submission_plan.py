"""Offline, exact-byte assembly handoffs. No supplier clients or transaction APIs.

See docs/guides/submission-preparation.md for the strict input dialect and
publication contract, milestone A. ``refresh_inventory`` is a narrow milestone
B increment: exact-ID stock observation through an explicit, caller-supplied
official adapter. It preserves unknown-vs-zero stock evidence, never infers
substitutes, and never mutates a published plan. Full milestone B (landed
#5033/#5034 provenance/freshness semantics) remains tracked by #5142.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Context, Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from ..parts.jlcpcb_api import (
    JLCAPIError,
    JLCAuthError,
    JLCIncompleteResponseError,
    JLCIPNotWhitelistedError,
    JLCPermissionError,
    JLCQuotaError,
)
from .bom_formats import JLCPCBBOMFormatter
from .pnp import JLCPCBPnPFormatter

_ROLES = {"gerber", "bom", "cpl"}
_RESERVED = {"plan.json", "plan.sha256", "expected-matches.json", "bundle-manifest.json"}
_DEFAULT_NAMES = {"gerber": "gerbers.zip", "bom": "bom.csv", "cpl": "cpl.csv"}
_REF = re.compile(r"[A-Za-z][A-Za-z0-9_.+-]*\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")


class SubmissionError(ValueError):
    """The supplied frozen inputs or handoff fail the local integrity contract."""


@dataclass(frozen=True)
class SourceEvidence:
    """Caller-supplied content binding for a path relative to source_root."""

    path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class FactorySettings:
    """Explicit planning intent, NOT a vendor order payload or vendor enum mapping."""

    factory: str
    board_layers: int
    assembly_sides: tuple[str, ...]


@dataclass(frozen=True)
class SubmissionPlan:
    """Immutable core bytes and external digest; directory is published read-only."""

    directory: Path
    plan_bytes: bytes

    @property
    def sha256(self) -> str:
        return _digest(self.plan_bytes)


@dataclass(frozen=True)
class _Snapshot:
    root: Path
    path: str
    data: bytes
    identity: tuple[int, ...]


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(data: Any) -> bytes:
    return (
        json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        + "\n"
    ).encode("utf-8")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SubmissionError("Duplicate JSON key")
        result[key] = value
    return result


def _load_json(data: bytes) -> Any:
    try:
        return json.loads(data, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError) as exc:
        raise SubmissionError("Invalid JSON manifest") from exc


def _path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise SubmissionError("Paths must be canonical relative POSIX paths")
    parts = value.split("/")
    if any(p in ("", ".", "..") or any(ord(c) < 32 or ord(c) == 127 for c in p) for p in parts):
        raise SubmissionError("Paths must be canonical relative POSIX paths")
    if unicodedata.normalize("NFC", value) != value or PurePosixPath(value).is_absolute():
        raise SubmissionError("Noncanonical path")
    return value


def _paths(values: Sequence[str]) -> list[str]:
    result = [_path(value) for value in values]
    if len({name.casefold() for name in result}) != len(result):
        raise SubmissionError("Duplicate canonical path or case alias")
    return result


def _root_fd(root: Path) -> int:
    """Open every absolute directory component without following symlinks."""
    fd = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in root.parts[1:]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except OSError as exc:
        os.close(fd)
        raise SubmissionError("Root must be an existing directory without symlinks") from exc


def _root(path: Path) -> Path:
    # abspath canonicalizes '.' and '..'; _root_fd rejects symlink ancestors.
    root = Path(os.path.abspath(path))
    fd = _root_fd(root)
    os.close(fd)
    return root


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _read(root: Path, name: str) -> _Snapshot:
    name = _path(name)
    fd = _root_fd(root)
    file_fd = None
    try:
        parts = name.split("/")
        for part in parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode):
            raise SubmissionError("Input must be a regular file")
        with os.fdopen(file_fd, "rb", closefd=False) as stream:
            data = stream.read()
        after = os.fstat(file_fd)
        if _identity(before) != _identity(after):
            raise SubmissionError("Input changed while reading")
        return _Snapshot(root, name, data, _identity(after))
    except OSError as exc:
        raise SubmissionError("Input missing, inaccessible, or symlinked") from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(fd)


def _inventory(root: Path, expected: set[str]) -> None:
    """Check exact files and implied directories; never traverse symlinks."""
    found: set[str] = set()
    directories = {
        str(parent)
        for name in expected
        for parent in PurePosixPath(name).parents
        if str(parent) != "."
    }

    def walk(fd: int, prefix: str) -> None:
        with os.scandir(fd) as entries:
            for entry in entries:
                name = _path(prefix + entry.name)
                if entry.is_symlink():
                    raise SubmissionError("Bundle contains a symlink")
                if entry.is_dir(follow_symlinks=False):
                    if name not in directories:
                        raise SubmissionError("Unexpected bundle directory")
                    child = os.open(
                        entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                    )
                    try:
                        walk(child, name + "/")
                    finally:
                        os.close(child)
                elif entry.is_file(follow_symlinks=False):
                    found.add(name)
                else:
                    raise SubmissionError("Bundle contains a nonregular entry")

    fd = _root_fd(root)
    try:
        walk(fd, "")
    except OSError as exc:
        raise SubmissionError("Bundle changed during inventory") from exc
    finally:
        os.close(fd)
    if found != expected:
        raise SubmissionError("Bundle file set differs from declared allowlist")


def _record(data: bytes) -> dict[str, Any]:
    return {"sha256": _digest(data), "size": len(data)}


def _check_record(data: bytes, record: Any) -> None:
    if (
        not isinstance(record, dict)
        or not isinstance(record.get("sha256"), str)
        or not _SHA.fullmatch(record["sha256"])
    ):
        raise SubmissionError("Complete SHA256/size evidence is required")
    size = record.get("size")
    if type(size) is not int or size < 0:
        raise SubmissionError("Complete SHA256/size evidence is required")
    if record["sha256"] != _digest(data) or size != len(data):
        raise SubmissionError("Hash or size mismatch")


def _csv(
    data: bytes, required: list[str], optional: set[str] | None = None
) -> list[dict[str, str]]:
    try:
        rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"), newline=""), strict=True))
    except (csv.Error, UnicodeError) as exc:
        raise SubmissionError("Invalid UTF-8 CSV") from exc
    if not rows:
        raise SubmissionError("Empty CSV")
    header = rows.pop(0)
    if (
        len(set(header)) != len(header)
        or not set(required) <= set(header)
        or set(header) - set(required) - (optional or set())
    ):
        raise SubmissionError("Unsupported or duplicate CSV columns")
    if not rows or any(len(row) != len(header) for row in rows):
        raise SubmissionError("Empty CSV population or inconsistent row length")
    return [dict(zip(header, row, strict=True)) for row in rows]


def _reference(value: str) -> str:
    if not isinstance(value, str) or not _REF.fullmatch(value):
        raise SubmissionError("Invalid or empty reference")
    return value


def _number(value: str, *, mm: bool = False) -> str:
    if mm and value.endswith("mm"):
        value = value[:-2]
    try:
        # String construction is exact; its explicit context contains any
        # conversion-error flags instead of mutating the caller context.
        number = Decimal(value, context=Context())
    except InvalidOperation as exc:
        raise SubmissionError("Invalid CPL numeric field") from exc
    if not number.is_finite() or number.copy_abs() > 1_000_000:
        raise SubmissionError("Nonfinite or out-of-range CPL numeric field")
    if not number:
        return "0"
    # normalize(), abs(), and Decimal.__str__ depend on ambient precision,
    # rounding or capitalization. Canonicalize the exact tuple using only
    # integer/string operations, without allocating huge exponent padding.
    sign, digits, exponent = number.as_tuple()
    assert isinstance(exponent, int)  # Nonfinite values were rejected above.
    coefficient = "".join(str(digit) for digit in digits)
    trimmed = coefficient.rstrip("0")
    exponent += len(coefficient) - len(trimmed)
    adjusted = exponent + len(trimmed) - 1
    if exponent <= 0 and adjusted >= -6:
        point = len(trimmed) + exponent
        if point <= 0:
            result = "0." + "0" * (-point) + trimmed
        elif point < len(trimmed):
            result = trimmed[:point] + "." + trimmed[point:]
        else:
            result = trimmed
    else:
        result = trimmed[0] + ("." + trimmed[1:] if len(trimmed) > 1 else "")
        result += "E" + ("+" if adjusted >= 0 else "") + str(adjusted)
    return ("-" if sign else "") + result


def _population(
    bom: bytes, cpl: bytes, sides: tuple[str, ...], exclusions: Mapping[str, str]
) -> list[dict[str, str]]:
    assignments: dict[str, dict[str, str]] = {}
    for row in _csv(bom, JLCPCBBOMFormatter().get_headers(), {"Quantity"}):
        refs = [_reference(ref.strip()) for ref in row["Designator"].split(",")]
        if not re.fullmatch(r"C[1-9][0-9]*", row["LCSC Part #"]):
            raise SubmissionError("Every BOM row needs an explicit exact catalog ID")
        if not row["Comment"].strip() or not row["Footprint"].strip():
            raise SubmissionError("BOM value and footprint must be explicit")
        if "Quantity" in row and (
            not re.fullmatch(r"[1-9][0-9]*", row["Quantity"]) or int(row["Quantity"]) != len(refs)
        ):
            raise SubmissionError("BOM row quantity disagrees with reference count")
        for ref in refs:
            if ref in assignments:
                raise SubmissionError("Duplicate BOM reference or conflicting ID")
            assignments[ref] = {
                "reference": ref,
                "catalog_id": row["LCSC Part #"],
                "value": row["Comment"],
                "footprint": row["Footprint"],
            }
    matches = []
    seen: set[str] = set()
    for row in _csv(cpl, JLCPCBPnPFormatter().get_headers()):
        ref = _reference(row["Designator"])
        if ref in seen or ref not in assignments:
            raise SubmissionError("Duplicate or extra CPL reference")
        seen.add(ref)
        assignment = assignments[ref]
        if row["Val"] != assignment["value"] or row["Package"] != assignment["footprint"]:
            raise SubmissionError("BOM/CPL value or footprint disagrees")
        side = {"Top": "top", "Bottom": "bottom"}.get(row["Layer"])
        if side not in sides:
            raise SubmissionError("CPL layer differs from explicit assembly sides")
        matches.append(
            assignment
            | {
                "x_mm": _number(row["Mid X"], mm=True),
                "y_mm": _number(row["Mid Y"], mm=True),
                "rotation_degrees": _number(row["Rotation"]),
                "side": str(side),
            }
        )
    if seen != assignments.keys():
        raise SubmissionError("Missing CPL references")
    for ref, reason in exclusions.items():
        _reference(ref)
        if reason not in ("dnp", "tht") or ref in seen:
            raise SubmissionError(
                "Explicit exclusion must already be absent from both frozen uploads"
            )
    return sorted(matches, key=lambda match: match["reference"])


def _settings(settings: FactorySettings) -> dict[str, Any]:
    if not isinstance(settings, FactorySettings) or settings.factory != "jlcpcb":
        raise SubmissionError("Only explicit jlcpcb planning intent is supported")
    if type(settings.board_layers) is not int or not 1 <= settings.board_layers <= 64:
        raise SubmissionError("Explicit positive board layer count required")
    sides = settings.assembly_sides
    if (
        not isinstance(sides, tuple)
        or not sides
        or len(set(sides)) != len(sides)
        or any(side not in ("top", "bottom") for side in sides)
    ):
        raise SubmissionError("Explicit unique top/bottom assembly sides required")
    return {
        "factory": settings.factory,
        "board_layers": settings.board_layers,
        "assembly_sides": sorted(sides),
    }


def _write_bytes(path: Path, data: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def prepare_submission(
    *,
    bundle_root: Path,
    source_root: Path,
    source_evidence: Mapping[str, SourceEvidence],
    bundle_files: Sequence[str],
    artifacts: Mapping[str, str],
    board_quantity: int,
    settings: FactorySettings,
    destination: Path,
    manifest_path: str = "manifest.json",
    output_names: Mapping[str, str] | None = None,
    exclusions: Mapping[str, str] | None = None,
) -> SubmissionPlan:
    """Validate frozen inputs and atomically publish a new read-only handoff.

    All input bytes are read and verified before staging. Revalidation after
    copying rejects mutation; existing destinations are never updated. No
    credentials, supplier clients, or network calls occur on any path.
    """
    if type(board_quantity) is not int or board_quantity <= 0:
        raise SubmissionError("Board quantity must be a positive integer")
    intent = _settings(settings)
    exclusions = dict(exclusions or {})
    bundle, source = _root(bundle_root), _root(source_root)
    manifest_path = _path(manifest_path)
    allowed = _paths(bundle_files)
    if not allowed or manifest_path.casefold() in {name.casefold() for name in allowed}:
        raise SubmissionError("Manifest cannot list itself; explicit file allowlist required")
    if (
        set(artifacts) != _ROLES
        or len(set(_paths(list(artifacts.values())))) != 3
        or not set(artifacts.values()) <= set(allowed)
    ):
        raise SubmissionError("Select exactly one distinct listed artifact per role")
    names = dict(_DEFAULT_NAMES if output_names is None else output_names)
    if set(names) != _ROLES:
        raise SubmissionError("Output names must specify all artifact roles")
    _paths(list(names.values()))
    if any("/" in name or name.casefold() in _RESERVED for name in names.values()):
        raise SubmissionError("Output names must be distinct nonreserved filenames")
    if set(source_evidence) != {"pcb", "schematic"} or any(
        not isinstance(e, SourceEvidence) for e in source_evidence.values()
    ):
        raise SubmissionError("Explicit PCB and schematic source evidence required")
    _paths([e.path for e in source_evidence.values()])
    destination = Path(os.path.abspath(destination))
    parent = _root(destination.parent)
    _path(destination.name)
    if destination.is_relative_to(bundle) or destination.is_relative_to(source):
        raise SubmissionError("Handoff must be outside frozen bundle and source roots")
    if os.path.lexists(destination):
        raise SubmissionError("Destination already exists")
    expected = set(allowed) | {manifest_path}
    _inventory(bundle, expected)
    manifest = _read(bundle, manifest_path)
    metadata = _load_json(manifest.data)
    if not isinstance(metadata, dict) or not isinstance(metadata.get("files"), dict):
        raise SubmissionError("Manifest requires complete file records")
    _paths(list(metadata["files"]))
    if set(metadata["files"]) != set(allowed):
        raise SubmissionError("Manifest and declared file allowlist differ")
    snapshots = {name: _read(bundle, name) for name in allowed}
    for name, snap in snapshots.items():
        _check_record(snap.data, metadata["files"][name])
    sources = {role: _read(source, evidence.path) for role, evidence in source_evidence.items()}
    for role, snap in sources.items():
        evidence = source_evidence[role]
        _check_record(snap.data, {"sha256": evidence.sha256, "size": evidence.size})
    matches = _population(
        snapshots[artifacts["bom"]].data,
        snapshots[artifacts["cpl"]].data,
        settings.assembly_sides,
        exclusions,
    )
    counts = Counter(match["catalog_id"] for match in matches)
    outputs = {names[role]: snapshots[path].data for role, path in artifacts.items()}
    outputs["expected-matches.json"] = _json(matches)
    outputs["bundle-manifest.json"] = manifest.data
    core = {
        "schema_version": 1,
        "board_quantity": board_quantity,
        "settings": intent,
        "population": "exact-frozen-bom-cpl-reference-set",
        "exclusions": exclusions,
        "bundle_manifest": {"path": manifest_path, **_record(manifest.data)},
        "bundle_files": {name: _record(snap.data) for name, snap in snapshots.items()},
        "sources": {
            role: {"path": snap.path, **_record(snap.data)} for role, snap in sources.items()
        },
        "artifacts": {
            role: {"input_path": path, "output_name": names[role]}
            for role, path in artifacts.items()
        },
        "outputs": {name: _record(data) for name, data in outputs.items()},
        "demand": [
            {
                "catalog_id": code,
                "per_board": counts[code],
                "required": counts[code] * board_quantity,
            }
            for code in sorted(counts)
        ],
        "states": {
            "preparation": "local-integrity-valid",
            "inventory": "unknown",
            "human_approval": "not-established",
            "factory_matching": "not-observed",
            "upload": "not-performed",
            "order": "not-performed",
        },
    }
    plan_bytes = _json(core)
    outputs["plan.json"] = plan_bytes
    outputs["plan.sha256"] = (_digest(plan_bytes) + "  plan.json\n").encode("ascii")
    # Exclusive cooperative lock protects two callers publishing the same path.
    lock = parent / ("." + destination.name + ".prepare-lock")
    try:
        lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise SubmissionError("Destination publication is already claimed") from exc
    stage: Path | None = None
    try:
        if os.path.lexists(destination):
            raise SubmissionError("Destination already exists")
        stage = Path(tempfile.mkdtemp(prefix="." + destination.name + ".stage-", dir=parent))
        for name, data in outputs.items():
            _write_bytes(stage / name, data)
        for name, data in outputs.items():
            if _read(stage, name).data != data:
                raise SubmissionError("Destination bytes differ from verified input")
        for snap in [manifest, *snapshots.values(), *sources.values()]:
            current = _read(snap.root, snap.path)
            if current.data != snap.data or current.identity != snap.identity:
                raise SubmissionError("Input changed between validation and copying")
        _inventory(bundle, expected)
        for name in outputs:
            (stage / name).chmod(0o444)
        stage.chmod(0o555)
        if os.path.lexists(destination):
            raise SubmissionError("Destination already exists")
        os.rename(stage, destination)
        stage = None
        return SubmissionPlan(destination, plan_bytes)
    finally:
        os.close(lock_fd)
        lock.unlink()
        if stage is not None:
            stage.chmod(0o700)
            shutil.rmtree(stage)


def verify_submission(directory: Path, expected_sha256: str) -> SubmissionPlan:
    """Recheck a published handoff against an externally retained plan digest."""
    directory = _root(directory)
    plan = _read(directory, "plan.json").data
    if _digest(plan) != expected_sha256:
        raise SubmissionError("Plan digest mismatch")
    core = _load_json(plan)
    if (
        not isinstance(core, dict)
        or core.get("schema_version") != 1
        or not isinstance(core.get("outputs"), dict)
    ):
        raise SubmissionError("Unsupported plan")
    names = _paths(list(core["outputs"]))
    if {"plan.json", "plan.sha256"} & set(names):
        raise SubmissionError("Plan cannot hash itself")
    _inventory(directory, set(names) | {"plan.json", "plan.sha256"})
    for name, evidence in core["outputs"].items():
        _check_record(_read(directory, name).data, evidence)
    if _read(directory, "plan.sha256").data != (expected_sha256 + "  plan.json\n").encode("ascii"):
        raise SubmissionError("External digest file mismatch")
    return SubmissionPlan(directory, plan)


_INVENTORY_STATUSES = frozenset(
    {
        "verified",
        "missing-field",
        "malformed",
        "not-returned",
        "forbidden",
        "quota-error",
        "transport-error",
        "dependency-error",
        "incomplete-response",
    }
)


class InventorySource(Protocol):
    """What ``refresh_inventory`` needs from an exact-ID official adapter.

    :class:`~kicad_tools.parts.jlcpcb_api.JLCOpenAPIClient` satisfies this
    structurally; a test double can too, without constructing credentials or
    a network session. This module never builds a client itself.
    """

    def get_component_detail_raw(self, codes: list[str]) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class StockObservation:
    """One approved-ID outcome. A verified numeric zero is not "unknown".

    ``status`` is one of :data:`_INVENTORY_STATUSES`. ``raw_stock`` is the
    exact non-negative int the adapter returned, present only when
    ``status == "verified"`` -- never a fabricated zero for any other status.
    """

    catalog_id: str
    status: str
    raw_stock: int | None


@dataclass(frozen=True)
class InventorySnapshot:
    """Immutable stock-observation record bound to plan/BOM/quantity/demand identity.

    Written outside the frozen fabrication bundle and outside the published
    handoff directory (which is read-only after ``prepare_submission``).
    """

    path: Path
    snapshot_bytes: bytes

    @property
    def sha256(self) -> str:
        return _digest(self.snapshot_bytes)


def _observe(source: InventorySource, codes: list[str]) -> list[StockObservation]:
    """Call the adapter once for every requested code and classify each outcome.

    A whole-batch adapter failure (auth/permission/IP/quota/transport/
    dependency/incomplete-response) is recorded for every requested code --
    never silently dropped and never coerced into a verified value. IDs the
    adapter returns that were not requested are read but discarded; they can
    never satisfy a different code's demand.
    """
    if not codes:
        return []
    try:
        raw = source.get_component_detail_raw(codes)
    except JLCIncompleteResponseError:
        return [StockObservation(code, "incomplete-response", None) for code in codes]
    except (JLCIPNotWhitelistedError, JLCPermissionError, JLCAuthError):
        return [StockObservation(code, "forbidden", None) for code in codes]
    except JLCQuotaError:
        return [StockObservation(code, "quota-error", None) for code in codes]
    except JLCAPIError:
        return [StockObservation(code, "transport-error", None) for code in codes]
    except ImportError:
        return [StockObservation(code, "dependency-error", None) for code in codes]

    by_code: dict[str, dict[str, Any]] = {}
    for component in raw:
        code = component.get("componentCode")
        if isinstance(code, str) and code.strip():
            # First occurrence wins; a duplicated/substituted code cannot
            # override an already-classified requested code.
            by_code.setdefault(code.strip().upper(), component)

    observations = []
    for code in codes:
        matched = by_code.get(code)
        if matched is None:
            observations.append(StockObservation(code, "not-returned", None))
            continue
        if "stockCount" not in matched:
            observations.append(StockObservation(code, "missing-field", None))
            continue
        value = matched["stockCount"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            observations.append(StockObservation(code, "malformed", None))
            continue
        observations.append(StockObservation(code, "verified", value))
    return observations


def refresh_inventory(
    plan: SubmissionPlan,
    *,
    source: InventorySource,
    destination: Path,
    observed_at: str,
) -> InventorySnapshot:
    """Observe exact-ID official stock for a prepared plan's approved demand.

    Milestone B, narrow increment: this reads ``plan.plan_bytes`` (never the
    published directory's files, which stay untouched) to recover the exact
    approved-ID demand map, calls ``source`` once with the deduplicated
    catalog IDs, and writes a new immutable snapshot file bound to the plan
    digest, the bound BOM output hash, board quantity, and the demand
    mapping. It never multiplies, substitutes, infers absence, or reports a
    reservation/feeder/attrition outcome.

    ``source`` must already be fully constructed by the caller (credentials,
    session, base URL): this function never builds one, never reads supplier
    environment variables, and never falls back to an anonymous or offline
    source. All local plan-shape validation happens before ``source`` is
    touched, so a malformed plan or destination never causes a network call.

    Full milestone B (landed #5033/#5034 provenance/freshness contracts, live
    smoke-tested signing) remains tracked by #5142; this increment targets
    the current, pre-merge ``jlcpcb_api`` adapter contract only.
    """
    core = _load_json(plan.plan_bytes)
    if (
        not isinstance(core, dict)
        or core.get("schema_version") != 1
        or not isinstance(core.get("demand"), list)
        or not isinstance(core.get("artifacts"), dict)
        or not isinstance(core.get("outputs"), dict)
        or type(core.get("board_quantity")) is not int
    ):
        raise SubmissionError("Unsupported or malformed plan")
    demand = core["demand"]
    approved: list[str] = []
    seen_ids: set[str] = set()
    for entry in demand:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("catalog_id"), str)
            or type(entry.get("per_board")) is not int
            or type(entry.get("required")) is not int
            or entry["per_board"] <= 0
            or entry["required"] != entry["per_board"] * core["board_quantity"]
        ):
            raise SubmissionError("Malformed plan demand entry")
        code = entry["catalog_id"]
        if code in seen_ids:
            raise SubmissionError("Duplicate plan demand catalog id")
        seen_ids.add(code)
        approved.append(code)
    bom_artifact = core["artifacts"].get("bom")
    if not isinstance(bom_artifact, dict) or not isinstance(bom_artifact.get("output_name"), str):
        raise SubmissionError("Plan is missing bound BOM artifact identity")
    bom_output = core["outputs"].get(bom_artifact["output_name"])
    if not isinstance(bom_output, dict) or not isinstance(bom_output.get("sha256"), str):
        raise SubmissionError("Plan is missing bound BOM output hash")
    if not isinstance(observed_at, str) or not observed_at.strip():
        raise SubmissionError("Explicit observation time required")

    destination = Path(os.path.abspath(destination))
    parent = _root(destination.parent)
    _path(destination.name)
    if destination.is_relative_to(plan.directory):
        raise SubmissionError("Inventory snapshot must be outside the published handoff")
    if os.path.lexists(destination):
        raise SubmissionError("Destination already exists")

    observations = _observe(source, sorted(approved))
    if {o.status for o in observations} - _INVENTORY_STATUSES:
        raise SubmissionError("Adapter returned an unrecognized observation status")

    snapshot_core = {
        "schema_version": 1,
        "plan_sha256": plan.sha256,
        "bom_sha256": bom_output["sha256"],
        "board_quantity": core["board_quantity"],
        "demand": demand,
        "observed_at": observed_at,
        "observations": [
            {"catalog_id": o.catalog_id, "status": o.status, "raw_stock": o.raw_stock}
            for o in observations
        ],
        "states": {
            "inventory": "observed",
            "human_approval": "not-established",
            "factory_matching": "not-observed",
            "reservation": "not-performed",
            "feeder_attrition": "not-established",
            "upload": "not-performed",
            "order": "not-performed",
        },
    }
    snapshot_bytes = _json(snapshot_core)

    lock = parent / ("." + destination.name + ".snapshot-lock")
    try:
        lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise SubmissionError("Destination publication is already claimed") from exc
    try:
        if os.path.lexists(destination):
            raise SubmissionError("Destination already exists")
        _write_bytes(destination, snapshot_bytes)
        if _read(parent, destination.name).data != snapshot_bytes:
            raise SubmissionError("Destination bytes differ from computed snapshot")
        destination.chmod(0o444)
        return InventorySnapshot(destination, snapshot_bytes)
    finally:
        os.close(lock_fd)
        lock.unlink()
