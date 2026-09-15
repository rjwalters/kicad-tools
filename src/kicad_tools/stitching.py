"""Transactional power stitching with native refill and physical acceptance.

The geometric stitcher proposes copper. This stage commits that proposal only
when the selected nets are physically complete, existing pad bonds survive,
and native DRC introduces no new findings. Failed candidates remain in the
evidence directory; the input and its authored constraints remain untouched.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from kicad_tools._shapely import has_shapely
from kicad_tools.core.atomic_write import atomic_write_text
from kicad_tools.core.kicad_lock import check_kicad_lock
from kicad_tools.validate.connectivity import ConnectivityValidator


class StitchRejected(RuntimeError):
    """A candidate failed physical acceptance; the input was not promoted."""


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot(path: Path) -> dict[str, Any]:
    if not has_shapely():
        raise StitchRejected("Physical stitching requires Shapely")
    validator = ConnectivityValidator(path)
    groups, bindings = validator.extract_pad_occurrences()
    counts = Counter(bindings.values())
    stable_ids: dict[str, str] = {}
    nets: dict[str, str] = {}
    item_nets: dict[str, str] = {}
    pad_geometry: dict[str, Any] = {}
    for fi, fp in enumerate(validator.pcb.footprints):
        if not fp.reference or fp.reference.startswith("#"):
            continue
        for pi, pad in enumerate(fp.pads):
            if pad.number in (None, ""):
                continue
            node = validator._pad_id(fi, pi)
            identity = ".".join(bindings[node])
            if counts[bindings[node]] > 1:
                if not pad.uuid:
                    raise StitchRejected("Duplicate-number pads need stable UUIDs")
                identity += "@" + pad.uuid
            if identity in nets:
                raise StitchRejected("Ambiguous physical pad identity")
            stable_ids[node] = identity
            nets[identity] = pad.net_name
            pad_geometry[identity] = [
                fp.position,
                fp.rotation % 360,
                pad.position,
                pad.rotation % 360,
                pad.size,
                sorted(pad.layers),
                pad.type,
                pad.shape,
                pad.drill,
            ]
            if pad.uuid:
                item_nets[pad.uuid] = pad.net_name
    if not nets:
        raise StitchRejected("Physical pad census is empty")
    fixed_copper = []
    for item in [*validator.pcb.segments, *validator.pcb.arcs]:
        fixed_copper.append(
            json.dumps(
                [
                    type(item).__name__,
                    item.start,
                    item.end,
                    getattr(item, "mid", None),
                    item.width,
                    item.layer,
                    item.net_name,
                ]
            )
        )
    for via in validator.pcb.vias:
        fixed_copper.append(
            json.dumps(
                [
                    "via",
                    via.position,
                    via.size,
                    via.drill,
                    sorted(via.layers),
                    via.via_type,
                    via.net_name,
                ]
            )
        )
    copper_items: list[Any] = [
        *validator.pcb.segments,
        *validator.pcb.arcs,
        *validator.pcb.vias,
        *validator.pcb.zones,
    ]
    for item in copper_items:
        if item.uuid:
            item_nets[item.uuid] = item.net_name
    components = sorted(sorted(stable_ids[node] for node in group) for group in groups)
    if {node for group in components for node in group} != set(nets):
        raise StitchRejected("Physical pad census is incomplete")
    return {
        "sha256": _sha(path),
        "components": components,
        "pad_nets": nets,
        "item_nets": item_nets,
        "pad_geometry": pad_geometry,
        "fixed_copper": sorted(fixed_copper),
    }


def _preserve_bonds(before: dict[str, Any], after: dict[str, Any]) -> None:
    if before["pad_nets"] != after["pad_nets"]:
        raise StitchRejected("Pad identities or authored nets changed")
    if any(after["item_nets"].get(uid) != net for uid, net in before["item_nets"].items()):
        raise StitchRejected("Existing copper identities or authored nets changed")
    components = {pad: index for index, group in enumerate(after["components"]) for pad in group}
    if any(len({components[pad] for pad in group}) != 1 for group in before["components"]):
        raise StitchRejected("Previously connected physical pads were disconnected")
    if before["pad_geometry"] != after["pad_geometry"]:
        raise StitchRejected("Authored physical pad geometry changed")
    if Counter(before["fixed_copper"]) - Counter(after["fixed_copper"]):
        raise StitchRejected("Existing routed copper was removed or changed")
    for group in after["components"]:
        names = {after["pad_nets"][pad] for pad in group} - {""}
        if len(names) > 1:
            raise StitchRejected(f"Different-net copper contact: {sorted(names)}")


def _require_complete(snapshot: dict[str, Any], targets: list[str]) -> None:
    components = {pad: index for index, group in enumerate(snapshot["components"]) for pad in group}
    for name in targets:
        pads = [pad for pad, net in snapshot["pad_nets"].items() if net == name]
        if not pads:
            raise StitchRejected(f"No physical pads on target net {name}")
        if len({components[pad] for pad in pads}) != 1:
            raise StitchRejected(f"Power net {name} remains physically disconnected")


def _native_refill(path: Path, cli: str, *, refill: bool = True) -> dict[str, Any]:
    report = path.parent / "native-drc.json"
    report.unlink(missing_ok=True)
    original_hash = _sha(path)
    command = [
        cli,
        "pcb",
        "drc",
        str(path),
        *(["--refill-zones", "--save-board"] if refill else []),
        "--format",
        "json",
        "-o",
        str(report),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=180)
    (path.parent / "native-drc.log").write_text(
        json.dumps(command) + "\n" + result.stdout + result.stderr
    )
    if result.returncode:
        raise StitchRejected(f"Native refill failed ({result.returncode}); see {path.parent}")
    if not refill and _sha(path) != original_hash:
        raise StitchRejected("Native saved-state check changed the input snapshot")
    data = json.loads(report.read_text())
    if not isinstance(data, dict) or not str(data.get("kicad_version", "")).startswith("10."):
        raise StitchRejected("A KiCad 10 native report is required")
    if any(not isinstance(data.get(key), list) for key in ("violations", "unconnected_items")):
        raise StitchRejected("Native report is missing required finding arrays")
    return data


def _finding_signatures(report: dict[str, Any]) -> Counter[str]:
    signatures: Counter[str] = Counter()
    for finding in [*report["violations"], *report["unconnected_items"]]:
        if finding.get("severity") not in {"error", "warning", "info"} or not finding.get("type"):
            raise StitchRejected("Malformed native finding")
        items = finding.get("items")
        if not isinstance(items, list) or any(not item.get("uuid") for item in items):
            raise StitchRejected("Native finding lacks physical item identities")
        signatures[
            json.dumps(
                [finding["type"], finding["severity"], sorted(item["uuid"] for item in items)]
            )
        ] += 1
    return signatures


def _native_target_opens(
    report: dict[str, Any], snapshot: dict[str, Any], targets: list[str]
) -> None:
    for finding in report["unconnected_items"]:
        items = finding.get("items")
        if not isinstance(items, list) or not items:
            raise StitchRejected("Native unconnected item has no physical identities")
        names = set()
        for item in items:
            name = snapshot["item_nets"].get(item.get("uuid"))
            if not name:
                raise StitchRejected("Cannot bind native open to an authored net")
            names.add(name)
        if names.intersection(targets):
            raise StitchRejected(f"Native refill leaves power connection open: {sorted(names)}")


def _context_files(source: Path) -> list[Path]:
    """Retain native project constraints and project-local footprint libraries.

    Native DRC checks footprint/library agreement as well as board rules.
    Keep project-relative library URIs resolvable without rewriting the table
    or project. Installed absolute libraries keep their original resolution.
    """
    from kicad_tools.sexp import parse_string

    files = {
        path
        for path in source.parent.iterdir()
        if path.is_file()
        and (
            (path.suffix == ".json" and path.name != "native-drc.json")
            or path == source.with_suffix(".kicad_pro")
            or path == source.with_suffix(".kicad_dru")
        )
    }
    project = source.with_suffix(".kicad_pro")
    variables = dict(os.environ)
    if project.is_file():
        variables.update(json.loads(project.read_text()).get("text_variables", {}))
    variables["KIPRJMOD"] = str(source.parent)
    table = source.parent / "fp-lib-table"
    if table.is_file():
        files.add(table)
        for library in parse_string(table.read_text()).find_children("lib"):
            uri = library.find("uri")
            if uri is None:
                raise StitchRejected("Project footprint library has no URI")
            raw = str(uri.get_string(0))
            expanded = re.sub(r"\$\{([^}]+)\}", lambda m: variables.get(m[1], m[0]), raw)
            resource = Path(expanded)
            if re.fullmatch(r"\$\{KICAD10_FOOTPRINT_DIR\}/[^$]+", expanded):
                continue  # Native KiCad resolves its own installed library root.
            if "${" in expanded:
                raise StitchRejected(f"Unresolved project footprint library: {raw}")
            if not resource.is_absolute():
                resource = source.parent / resource
            resource = resource.resolve()
            if resource.is_relative_to(source.parent):
                if not resource.is_dir() or resource.suffix != ".pretty":
                    raise StitchRejected(f"Unsupported project-local footprint library: {raw}")
                files.update(path for path in resource.rglob("*") if path.is_file())
            elif not Path(expanded).is_absolute() or "${KIPRJMOD}" in raw:
                # Moving ../ or variable-relative resources would change the
                # selected project's native context. Refuse instead of defaulting.
                raise StitchRejected(f"Cannot preserve relative footprint library: {raw}")
    return sorted(files)


@contextmanager
def _publisher_lock(source: Path):
    """Serialize cooperating stitch commits; never infer stale-lock ownership."""
    marker = source.with_name(f".{source.name}.stitch.lock")
    try:
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise StitchRejected(f"Stitch publisher lock exists: {marker}") from error
    state: dict[str, str] = {}
    try:
        os.close(fd)
        yield state
    finally:
        try:
            marker.unlink()
        except OSError as error:
            state["publisher_lock_cleanup_error"] = str(error)


def complete_power_connections(
    pcb_path: str | Path,
    net_names: list[str] | None = None,
    *,
    evidence_dir: str | Path | None = None,
    kicad_cli: str = "kicad-cli",
    via_size: float = 0.6,
    drill: float = 0.3,
    clearance: float = 0.2,
    trace_width: float = 0.2,
    avoid_pad_overlap: bool = True,
    strict_drc: bool = False,
) -> dict[str, Any]:
    """Stitch selected plane nets and commit only a physically accepted result.

    The original .kicad_pro/.kicad_dru and neighboring JSON constraints are
    copied into all evidence stages, including local footprint libraries. No constraints are generated or relaxed.
    Both before and after receive a real native refill. Existing signal opens
    may remain, but every selected power net must be complete and no existing
    connection or native finding may regress. Raises :class:`StitchRejected`
    on failure; evidence survives and this stage does not promote its candidate.
    Cooperating commits are serialized and late source/context edits are
    rechecked before rename; external editors do not participate in that lock.

    A native KiCad 10 CLI and Shapely are required. Use the selected process's
    project/rules beside the PCB, and run its complete manufacturing signoff
    after this stage; this function does not turn existing violations into a
    release approval.
    """
    from kicad_tools.cli.stitch_cmd import find_all_plane_nets, run_stitch
    from kicad_tools.sexp import parse_string

    source = Path(pcb_path).resolve()
    check_kicad_lock(source)
    if not source.is_file():
        raise StitchRejected(f"Missing PCB: {source}")
    original = source.read_bytes()
    targets = sorted(set(net_names or find_all_plane_nets(parse_string(original.decode("utf-8")))))
    if not targets:
        raise StitchRejected("No target power nets")
    evidence = (
        Path(evidence_dir).resolve()
        if evidence_dir
        else Path(tempfile.mkdtemp(prefix="kct-stitch-"))
    )
    if evidence_dir:
        evidence.mkdir(parents=True, exist_ok=False)
    record: dict[str, Any] = {
        "implementation_sha256": _sha(Path(__file__)),
        "native_cli": str(shutil.which(kicad_cli) or kicad_cli),
        "source": str(source),
        "evidence_dir": str(evidence),
        "inputs": {},
        "target_nets": targets,
        "status": "pending",
    }
    try:
        sidecars = _context_files(source)
        hashes = {str(path): _sha(path) for path in sidecars}
        hashes[str(source)] = hashlib.sha256(original).hexdigest()
        record["inputs"] = hashes
        stages = {}
        for label in ("saved", "before", "candidate"):
            folder = evidence / label
            folder.mkdir()
            board = folder / source.name
            board.write_bytes(original)
            for sidecar in sidecars:
                destination = folder / sidecar.relative_to(source.parent)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(sidecar, destination)
            stages[label] = board
        saved = _snapshot(stages["saved"])
        record["saved"] = saved
        if _sha(source) != hashes[str(source)]:
            raise StitchRejected("Source changed concurrently during input capture")
        saved_report = _native_refill(stages["saved"], kicad_cli, refill=False)
        record["saved"] = {**saved, "native": saved_report}
        before_report = _native_refill(stages["before"], kicad_cli)
        before = _snapshot(stages["before"])
        _preserve_bonds(saved, before)
        record["before"] = {**before, "native": before_report}
        # Propose against the actual refilled copper, never stale fill caches.
        shutil.copy2(stages["before"], stages["candidate"])
        try:
            _require_complete(before, targets)
            _native_target_opens(before_report, before, targets)
            vias_added = 0
        except StitchRejected:
            proposal = run_stitch(
                stages["candidate"],
                targets,
                via_size=via_size,
                drill=drill,
                clearance=clearance,
                trace_width=trace_width,
                avoid_pad_overlap=avoid_pad_overlap,
            )
            vias_added = len(proposal.vias_added)
            if is_dataclass(proposal):
                record["proposal_diagnostics"] = asdict(proposal)
        proposed = _snapshot(stages["candidate"])
        proposal_dir = evidence / "proposed"
        shutil.copytree(stages["candidate"].parent, proposal_dir)
        record["proposed"] = proposed
        _preserve_bonds(before, proposed)
        candidate_report = _native_refill(stages["candidate"], kicad_cli)
        candidate = _snapshot(stages["candidate"])
        record["candidate"] = {**candidate, "native": candidate_report}
        _preserve_bonds(before, candidate)
        _require_complete(candidate, targets)
        _native_target_opens(candidate_report, candidate, targets)
        if strict_drc and (candidate_report["violations"] or candidate_report["unconnected_items"]):
            raise StitchRejected("Strict native DRC still has findings")
        if _finding_signatures(candidate_report) - _finding_signatures(saved_report):
            raise StitchRejected(
                "Stitch/refill introduced native physical or manufacturing findings"
            )
        for stage in stages.values():
            for sidecar in sidecars:
                if _sha(stage.parent / sidecar.relative_to(source.parent)) != hashes[str(sidecar)]:
                    raise StitchRejected("Authored constraints changed during candidate validation")
        if any(_sha(Path(path)) != digest for path, digest in hashes.items()):
            raise StitchRejected("Source or authored constraints changed concurrently")
        check_kicad_lock(source)
        # Stage bytes under a unique name and serialize cooperating publishers.
        # Evidence persistence/fsync happens before the final source/context
        # comparison, leaving only the atomic rename after that comparison.
        with _publisher_lock(source) as lock_state:
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{source.name}.", suffix=".stitch-tmp", dir=source.parent
            )
            temporary: Path | None = Path(temporary_name)
            try:
                candidate_bytes = stages["candidate"].read_bytes()
                with os.fdopen(fd, "wb") as stream:
                    stream.write(candidate_bytes)
                    stream.flush()
                    os.fsync(stream.fileno())
                record.update(
                    status="accepted",
                    vias_added=vias_added,
                    output_sha256=_sha(stages["candidate"]),
                )
                atomic_write_text(
                    evidence / "evidence.json", json.dumps(record, indent=2, default=str) + "\n"
                )
                check_kicad_lock(source)
                if _context_files(source) != sidecars or any(
                    _sha(Path(path)) != digest for path, digest in hashes.items()
                ):
                    raise StitchRejected("Source or authored constraints changed concurrently")
                if candidate_bytes != original:
                    os.replace(temporary_name, source)
                    temporary = None
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        record.update(lock_state)
    except Exception as error:
        record.update(status="rejected", reason=str(error), output_sha256=_sha(source))
        try:
            atomic_write_text(
                evidence / "evidence.json", json.dumps(record, indent=2, default=str) + "\n"
            )
        except OSError as evidence_error:
            record["evidence_error"] = str(evidence_error)
        raise StitchRejected(f"{error}; evidence: {evidence}") from error

    # The PCB commit has succeeded. Do not turn a bookkeeping failure into a
    # false rejection: the pre-commit accepted record already identifies the
    # validated candidate and its exact output hash.
    record["status"] = "complete"
    try:
        atomic_write_text(
            evidence / "evidence.json", json.dumps(record, indent=2, default=str) + "\n"
        )
    except OSError as error:
        record["evidence_finalization_error"] = str(error)
    return record
