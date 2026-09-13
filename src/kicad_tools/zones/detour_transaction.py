"""Stage complete local detours and publish only after native validation."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from kicad_tools.cli.runner import find_kicad_cli, run_fill_zones
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.connectivity import ConnectivityValidator

from .local_detour import LocalDetour, PairMatch


def _native_refill_report(board: Path, executable: Path, *, refill: bool = True) -> dict[str, Any]:
    report = board.with_suffix(".drc.json")
    completed = subprocess.run(
        [
            str(executable),
            "pcb",
            "drc",
            *(["--refill-zones", "--save-board"] if refill else []),
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
    if completed.returncode:
        raise RuntimeError(
            f"Native detour validation failed: {completed.stderr or completed.stdout}"
        )
    result: dict[str, Any] = json.loads(report.read_text())
    if not isinstance(result, dict):
        raise RuntimeError("Invalid native report object")
    for section in ("violations", "unconnected_items"):
        if not isinstance(result.get(section), list):
            raise RuntimeError(f"Native report is missing {section}")
        for entry in result[section]:
            if not isinstance(entry.get("type"), str) or not isinstance(entry.get("items"), list):
                raise RuntimeError("Invalid native finding")
            if section == "unconnected_items" and (
                not entry["items"] or any(not item.get("uuid") for item in entry["items"])
            ):
                raise RuntimeError("Native connectivity finding has no item identity")
    return result


def _recipe_refill_report(board: Path, executable: Path) -> dict[str, Any]:
    # The recipe settles native fills with foreign-pad clearance carving.
    # A subsequent raw refill reverses that carve, so inspect the settled
    # copper without refilling it again.
    result = run_fill_zones(board, kicad_cli=executable)
    if not result.success:
        raise RuntimeError(f"Recipe detour refill failed: {result.stderr}")
    return _native_refill_report(board, executable, refill=False)


def _finding_entries(report):
    # Preserve every field and multiplicity, including identityless native
    # warnings. No warning category is ignored or reduced to a total count.
    return Counter(json.dumps(entry, sort_keys=True) for entry in report["violations"])


def _unconnected_identities(report):
    return Counter(
        tuple(sorted(item["uuid"] for item in entry["items"]))
        for entry in report["unconnected_items"]
    )


def publish_local_detour(
    pcb_path: Path,
    plan: LocalDetour,
    pair_match: PairMatch,
    *,
    power_net: str,
    power_layer: str,
    source_sha256: str,
    validate_pair: Callable[[PCB], None],
    kicad_cli: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply a complete candidate without exposing partial or failed copper.

    Pair validation must raise on failure. Sidecars stay unchanged, and any
    intervening source edit invalidates publication. Native connectivity must
    improve without new item relationships, and no physical pad group may split.
    """
    pcb_path = Path(pcb_path).absolute()
    publish_path = pcb_path.resolve()
    paths = [pcb_path, pcb_path.with_suffix(".kicad_pro"), pcb_path.with_suffix(".kicad_dru")]
    snapshots = {path: path.read_bytes() if path.exists() else None for path in paths}
    original_bytes = snapshots[pcb_path]
    if original_bytes is None or hashlib.sha256(original_bytes).hexdigest() != source_sha256:
        raise RuntimeError("The detour plan's source board has changed")
    executable = kicad_cli or find_kicad_cli()
    if executable is None:
        raise RuntimeError("Native KiCad is required to validate a local detour")
    with tempfile.TemporaryDirectory(prefix="kct-local-detour-") as temporary:
        root = Path(temporary)
        staged = []
        for name in ("baseline", "candidate"):
            directory = root / name
            directory.mkdir()
            for path, data in snapshots.items():
                if data is not None:
                    (directory / path.name).write_bytes(data)
            staged.append(directory / pcb_path.name)
        baseline, candidate = staged
        board = PCB.load(candidate)
        removed = set(plan.removed_tracks)
        adjustment = pair_match.adjustment
        expected_nets = dict.fromkeys(removed, plan.signal_net)
        if adjustment is not None:
            removed.add(adjustment.removed_track)
            expected_nets[adjustment.removed_track] = adjustment.net
        actual = {
            t.uuid: board.nets[t.net_number].name for t in board.segments if t.uuid in removed
        }
        if actual != expected_nets:
            raise RuntimeError("The planned cut no longer matches the source copper")
        for node in list(board._sexp.children):
            if not node.is_atom and node.name == "segment":
                uid = node.find("uuid")
                if uid is not None and uid.get_string(0) in removed:
                    board._sexp.remove(node)
        board._segments = [track for track in board.segments if track.uuid not in removed]
        board._invalidate_dedup_keys()
        ox, oy = board.board_origin

        def emit(net, layer, escape):
            points = [(x - ox, y - oy) for x, y in escape.points]
            board.add_trace(
                points[0],
                points[-1],
                waypoints=points[1:-1],
                net=net,
                layer=layer,
                width=escape.rules.width,
            )
            if escape.via:
                board.add_via(
                    *points[-1], net=net, size=escape.rules.diameter, drill=escape.rules.drill
                )

        emit(power_net, power_layer, plan.power)
        for layer, escape in plan.signal_paths:
            emit(plan.signal_net, layer, escape)
        if adjustment is not None:
            emit(adjustment.net, adjustment.layer, adjustment.replacement)
        board.save(candidate)
        before = _native_refill_report(baseline, executable)
        after = _native_refill_report(candidate, executable)
        if _finding_entries(after) - _finding_entries(before):
            raise RuntimeError("Local detour introduced native DRC findings")
        if _unconnected_identities(after) - _unconnected_identities(before):
            raise RuntimeError("Local detour introduced a native connectivity relationship")
        before = _recipe_refill_report(baseline, executable)
        after = _recipe_refill_report(candidate, executable)
        if _finding_entries(after) - _finding_entries(before):
            raise RuntimeError("Local detour introduced settled DRC findings")
        if _unconnected_identities(after) - _unconnected_identities(before):
            raise RuntimeError("Local detour introduced a settled connectivity relationship")
        if len(after["unconnected_items"]) >= len(before["unconnected_items"]):
            raise RuntimeError("Local detour did not improve native connectivity")
        old_groups, old_bindings = ConnectivityValidator(baseline).extract_pad_occurrences()
        new_groups, new_bindings = ConnectivityValidator(candidate).extract_pad_occurrences()
        if old_bindings != new_bindings or any(
            not any(group <= new_group for new_group in new_groups) for group in old_groups
        ):
            raise RuntimeError("Local detour split an existing physical pad group")
        # Native sidecar rewrites are private to staging. The supplied rules
        # remain authoritative for the final pair-length measurement.
        for path, data in snapshots.items():
            if path != pcb_path and data is not None:
                (candidate.parent / path.name).write_bytes(data)
        validate_pair(PCB.load(candidate))
        published = candidate.read_bytes()
    for path, data in snapshots.items():
        if (path.read_bytes() if path.exists() else None) != data:
            raise RuntimeError("Source board or rules changed during detour validation")
    fd, name = tempfile.mkstemp(prefix=".detour-", suffix=".kicad_pcb", dir=publish_path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(published)
        os.chmod(name, publish_path.stat().st_mode)
        os.replace(name, publish_path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return before, after
