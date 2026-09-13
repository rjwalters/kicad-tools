"""Publish via relocation only after native refill and physical cross-checks.

Planning may disregard stale *foreign* fills, but that candidate never escapes
this transaction. Original same-net fills remain the attachment evidence. Both
an untouched baseline and the complete candidate are refilled by the same native
engine: comparing stale source fills with current fills would attribute changes
from refill itself to the relocation.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kicad_tools.cli.relocate_in_pad_vias import (
    RelocationResult,
    _alternative_board_region,
    _alternative_contained,
    _check_clearance,
    _check_stub_clearance,
    _collect_smd_pads_by_net,
    _collect_tht_pads,
    relocate_in_pad_vias,
)
from kicad_tools.cli.runner import find_kicad_cli
from kicad_tools.manufacturers.base import DesignRules
from kicad_tools.schema.pcb import PCB, Zone
from kicad_tools.validate.connectivity import ConnectivityValidator
from kicad_tools.validate.rules.via_in_pad import ViaInPadRule


@dataclass
class NativeRelocationResult:
    """Relocations and the raw native evidence used to authorize publication."""

    relocation: RelocationResult
    baseline_report: dict[str, Any]
    candidate_report: dict[str, Any]


def _native_refill(board: Path, executable: Path) -> dict[str, Any]:
    report = board.with_suffix(".drc.json")
    completed = subprocess.run(
        [
            str(executable),
            "pcb",
            "drc",
            "--refill-zones",
            "--save-board",
            "--format",
            "json",
            "--output",
            str(report),
            str(board),
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    # Without --exit-code-violations, a nonzero exit is a command failure.
    # Require explicit refill/save support; an older CLI must fail closed.
    if completed.returncode != 0:
        raise RuntimeError(f"Native zone refill failed: {completed.stderr or completed.stdout}")
    try:
        result: dict[str, Any] = json.loads(report.read_text())
        _violation_identities(result)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("Native refill produced no valid identity-bearing DRC report") from exc
    return result


def _violation_identities(report: dict[str, Any]) -> Counter[tuple[str, tuple[str, ...]]]:
    """Keep multiplicity and source UUIDs, never just aggregate error counts."""
    identities: Counter[tuple[str, tuple[str, ...]]] = Counter()
    for section in ("violations", "unconnected_items"):
        entries = report[section]
        if not isinstance(entries, list):
            raise ValueError(f"Invalid native {section}")
        for entry in entries:
            kind = entry["type"]
            items = entry["items"]
            if not isinstance(kind, str) or not isinstance(items, list) or not items:
                raise ValueError("Native violation has no source identity")
            uuids = [item["uuid"] for item in items]
            if any(not isinstance(uid, str) or not uid for uid in uuids):
                raise ValueError("Native violation has no source UUID")
            identities[(kind, tuple(sorted(uuids)))] += 1
    return identities


def _plan(source: PCB, rules: DesignRules) -> tuple[PCB, RelocationResult]:
    pcb = copy.deepcopy(source)
    result = RelocationResult()
    for net in sorted({via.net_name for via in pcb.vias if via.net_name}):
        zones = [copy.deepcopy(node) for node in pcb._sexp.children if node.name == "zone"]
        tree = copy.deepcopy(pcb._sexp)
        for zone in tree.find_all("zone"):
            parsed = Zone.from_sexp(zone)
            declared = pcb.nets.get(parsed.net_number)
            zone_name = parsed.net_name or (declared.name if declared else "")
            if zone_name != net:
                zone.children[:] = [
                    child
                    for child in zone.children
                    if child.name not in ("filled_polygon", "filled_segments")
                ]
        candidate = PCB(tree, path=pcb.path)
        planned = relocate_in_pad_vias(candidate, rules, nets={net}, search_alternatives=True)
        result.moved.extend(planned.moved)
        result.skipped.extend(planned.skipped)
        result.unresolvable.extend(planned.unresolvable)
        # Restore the original attachment evidence for every subsequent net.
        candidate._sexp.children[:] = [
            node for node in candidate._sexp.children if node.name != "zone"
        ] + zones
        pcb = PCB(candidate._sexp, path=pcb.path)
    if result.skipped or result.unresolvable or ViaInPadRule().check(pcb, rules).violations:
        raise RuntimeError(f"Unresolved pad/drill overlaps in staged candidate: {result}")
    return pcb, result


def _validate_geometry(
    source: PCB, candidate: PCB, result: RelocationResult, rules: DesignRules
) -> None:
    pads = _collect_smd_pads_by_net(candidate)
    holes = _collect_tht_pads(candidate)
    region = _alternative_board_region(candidate)
    if region is None or region.is_empty:
        raise RuntimeError("Native relocation requires a closed, supported board outline")
    old_vias = {via.uuid: via for via in source.vias}
    new_vias = {via.uuid: via for via in candidate.vias}
    old_segments = {segment.uuid for segment in source.segments}
    stubs = [segment for segment in candidate.segments if segment.uuid not in old_segments]
    checked_stubs: set[str] = set()
    for move in result.moved:
        via = new_vias[move.uuid]
        original = old_vias[move.uuid]
        if not _near(via.position, (move.new_x, move.new_y)):
            raise RuntimeError(f"Native refill changed planned via position: {move.uuid}")
        if (via.net_name, via.size, via.drill, via.layers) != (
            original.net_name,
            original.size,
            original.drill,
            original.layers,
        ):
            raise RuntimeError(f"Native refill changed relocated via identity: {move.uuid}")
        reason = _check_clearance(
            candidate,
            via,
            *via.position,
            pads,
            holes,
            rules.min_clearance_mm,
            rules.min_hole_to_hole_mm,
        )
        if reason:
            raise RuntimeError(f"Refilled via {move.uuid}: {reason}")
        old = copy.copy(original)
        # KiCad may renumber the native net table on save. Geometry gates must
        # use this reloaded table, while source identity remains UUID + name.
        old.net_number = via.net_number
        if not _alternative_contained(region, old, via.position, via.size):
            raise RuntimeError(f"Refilled via/stub outside board: {move.uuid}")
        for stub in stubs:
            if stub.net_number != via.net_number:
                continue
            ends = (stub.start, stub.end)
            if not any(_near(end, old.position) for end in ends):
                continue
            if not any(_near(end, via.position) for end in ends):
                continue
            reason = _check_stub_clearance(
                candidate,
                old,
                via.position,
                [stub.layer],
                stub.width,
                rules.min_clearance_mm,
            )
            if reason or not _alternative_contained(region, old, via.position, stub.width):
                raise RuntimeError(f"Refilled stub {stub.uuid}: {reason or 'outside board'}")
            checked_stubs.add(stub.uuid)
    if checked_stubs != {stub.uuid for stub in stubs}:
        raise RuntimeError("Native candidate contains an unvalidated added segment")
    if ViaInPadRule().check(candidate, rules).violations:
        raise RuntimeError("Refilled candidate retains pad/drill overlaps")


def _near(left: tuple[float, float], right: tuple[float, float]) -> bool:
    # Native decimal serialization may round coordinates by < 0.001 mm.
    return abs(left[0] - right[0]) < 0.001 and abs(left[1] - right[1]) < 0.001


def relocate_in_pad_vias_with_refill(
    pcb_path: Path,
    rules: DesignRules,
    *,
    kicad_cli: Path | None = None,
) -> NativeRelocationResult:
    """Stage, plan, refill, validate, then atomically replace only the PCB.

    Adjacent project/rule bytes are staged for both native runs and never
    published. Every pre-publication failure leaves the source files untouched.
    Physical pad partitions are compared between independently refilled baseline
    and candidate, without consulting net labels. Native report identities and
    their multiplicities must not regress, including unconnected items.
    """
    # Project context belongs to the supplied path, including a symlink's
    # adjacent sidecars. Publish through the link without replacing the link.
    pcb_path = Path(pcb_path).absolute()
    publish_path = pcb_path.resolve()
    paths = [pcb_path, pcb_path.with_suffix(".kicad_pro"), pcb_path.with_suffix(".kicad_dru")]
    snapshots = {path: path.read_bytes() if path.exists() else None for path in paths}
    if snapshots[pcb_path] is None:
        raise FileNotFoundError(pcb_path)
    source = PCB.load(pcb_path)
    if rules.via_in_pad_supported or not ViaInPadRule().check(source, rules).violations:
        return NativeRelocationResult(
            RelocationResult(supported_noop=rules.via_in_pad_supported),
            {},
            {},
        )
    executable = kicad_cli or find_kicad_cli()
    if executable is None:
        raise RuntimeError("Native KiCad zone refill is required for staged via relocation")
    with tempfile.TemporaryDirectory(prefix="kct-relocate-refill-") as temporary:
        root = Path(temporary)
        staged = []
        for name in ("baseline", "candidate"):
            directory = root / name
            directory.mkdir()
            for path, data in snapshots.items():
                if data is not None:
                    (directory / path.name).write_bytes(data)
            staged.append(directory / pcb_path.name)
        baseline_path, candidate_path = staged
        original = PCB.load(candidate_path)
        candidate, result = _plan(original, rules)
        candidate.save(candidate_path)
        baseline_report = _native_refill(baseline_path, executable)
        candidate_report = _native_refill(candidate_path, executable)
        new = _violation_identities(candidate_report) - _violation_identities(baseline_report)
        if new:
            raise RuntimeError(f"Relocation introduced native violation identities: {dict(new)}")
        # Project floors come from the supplied sidecars, even if native tools
        # happen to rewrite their staged copies while loading the project.
        for path, data in snapshots.items():
            if path != pcb_path and data is not None:
                (candidate_path.parent / path.name).write_bytes(data)
        reloaded = PCB.load(candidate_path)
        _validate_geometry(original, reloaded, result, rules)
        baseline_groups, baseline_bindings = ConnectivityValidator(
            baseline_path
        ).extract_pad_occurrences()
        candidate_groups, candidate_bindings = ConnectivityValidator(
            reloaded
        ).extract_pad_occurrences()
        if baseline_bindings != candidate_bindings or Counter(baseline_groups) != Counter(
            candidate_groups
        ):
            raise RuntimeError(
                "Relocation changed native-refilled physical connectivity partitions"
            )
        published_bytes = candidate_path.read_bytes()
        evidence = NativeRelocationResult(result, baseline_report, candidate_report)
    # Dispose of native staging before publication: even a cleanup failure must
    # leave the original board untouched.
    # Prepare publication before the final source check. The replacement is
    # in the same directory/filesystem and preserves the source file mode.
    fd, pending_name = tempfile.mkstemp(prefix=f".{pcb_path.name}.", dir=publish_path.parent)
    pending = Path(pending_name)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(published_bytes)
            output.flush()
            os.fsync(output.fileno())
        shutil.copymode(pcb_path, pending)
        for path, data in snapshots.items():
            current = path.read_bytes() if path.exists() else None
            if current != data:
                raise RuntimeError(f"Source changed during relocation: {path.name}")
        if pcb_path.resolve() != publish_path:
            raise RuntimeError("Source link changed during relocation")
        os.replace(pending, publish_path)
    finally:
        pending.unlink(missing_ok=True)
    return evidence
