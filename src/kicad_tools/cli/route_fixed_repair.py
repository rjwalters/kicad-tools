"""Publish automatic repairs only when placement-fixed copper survives."""

from __future__ import annotations

import shutil
import tempfile
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from kicad_tools.core.atomic_write import atomic_write_text
from kicad_tools.placement.routing import RoutingPlacementDisposition
from kicad_tools.router.optimizer.pcb import parse_net_names
from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp import parse_string


def _fixed_snapshot(path: Path, disposition: RoutingPlacementDisposition) -> tuple:
    text = path.read_text()
    document = parse_string(text)
    nets = parse_net_names(text)
    fixed_refs = disposition.invalid_references | frozenset(
        ref
        for ref, _pad, authored, effective in disposition.pad_net_identities
        if authored in disposition.preserve_copper_nets
        or effective in disposition.preserve_copper_nets
    )
    fixed: Counter[tuple[str | None, str]] = Counter()
    for node in document.children:
        protected = False
        if node.name in {"footprint", "module"}:
            for field in node.children:
                if field.name in {"property", "fp_text"} and len(field.children) >= 2:
                    if field.children[0].value in {"Reference", "reference"}:
                        protected = field.children[1].value in fixed_refs
                        break
        elif node.name in {"segment", "via", "arc", "zone"}:
            net = node.find("net")
            token = net.children[0].value if net is not None and net.children else 0
            name = nets.get(token, "") if isinstance(token, int) else str(token)
            protected = name in disposition.preserve_copper_nets
        if protected:
            fixed[(node.name, node.to_string(compact=True, preserve_source=False))] += 1
    pcb = PCB.load(path)
    identities = Counter(
        (f.reference, p.number, p.net_name) for f in pcb.footprints for p in f.pads
    )
    return fixed, identities, nets


def repair_fixed_copper(
    output: Path,
    disposition: RoutingPlacementDisposition,
    repair: Callable[[Path], int],
) -> tuple[int, str | None]:
    """Stage repair and reject changes to fixed objects or net membership.

    The canonical partial board remains untouched while the repair runs, even
    if the process is killed. Existing repair connectivity gates still apply.
    """
    try:
        original = output.read_bytes()
        baseline = _fixed_snapshot(output, disposition)
        with tempfile.TemporaryDirectory(prefix="kct-fixed-repair-") as directory:
            candidate = Path(directory) / output.name
            candidate.write_bytes(original)
            context = [
                output.with_suffix(suffix) for suffix in (".kicad_pro", ".kicad_dru", ".pro")
            ]
            context += [
                output.parent / name
                for name in (
                    "net_class_map.json",
                    "net_class_map.effective.json",
                    "fab_profile.json",
                    "current_paths.json",
                    "voltage_map.json",
                )
            ]
            for path in context:
                if path.is_file():
                    shutil.copy2(path, candidate.parent / path.name)
            result = repair(candidate)
            if _fixed_snapshot(candidate, disposition) != baseline:
                return 3, "repair changed placement-fixed copper or pad/net membership"
            # Do not overwrite an intervening user or tool edit.
            if output.read_bytes() != original:
                return 3, "output changed while the staged repair was running"
            if candidate.read_bytes() != original:
                atomic_write_text(output, candidate.read_text())
            return result, None
    except Exception as exc:
        return 3, f"staged repair failed: {exc}"
