"""Stage native fills around immutable placement-excluded copper."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from kicad_tools.core.atomic_write import atomic_write_text
from kicad_tools.router.optimizer.pcb import _extract_balanced_blocks, parse_net_names
from kicad_tools.sexp import parse_string


def fill_around_fixed_copper(board: Path, protected_nets: frozenset[str], *, python: Path) -> None:
    """Fill eligible zones, publishing only their polygons into original source bytes.

    Native copper shapes model protected filled zones during the fill. The
    temporary board and proxies are discarded; source pad settings, tracks,
    arcs, vias and protected zone definitions are never replaced by native output.
    """
    source = board.read_text()
    zones = _extract_balanced_blocks(source, "zone")
    if not zones:
        return
    staged = source
    identities = {}
    expected = set()
    net_names = parse_net_names(source)
    for start, end, zone in reversed(zones):
        identity = str(uuid.uuid4())
        identities[identity] = (start, end, zone)
        parsed = parse_string(zone)
        net = parsed.find("net")
        token = net.children[0].value if net is not None and net.children else 0
        name = net_names.get(token, "") if isinstance(token, int) else str(token)
        if name not in protected_nets and parsed.find("keepout") is None:
            expected.add(identity)
        # Tag every temporary zone independently of legacy timestamp syntax.
        tagged = re.sub(r"\((?:uuid|tstamp)\s+[^)]+\)", "", zone)
        tagged = tagged.replace("(zone", f'(zone (uuid "{identity}")', 1)
        staged = staged[:start] + tagged + staged[end:]
    with tempfile.TemporaryDirectory(prefix="kct-fixed-fill-") as directory:
        stage = Path(directory) / board.name
        output = Path(directory) / "filled.kicad_pcb"
        stage.write_text(staged)
        for suffix in (".kicad_pro", ".kicad_dru", ".pro"):
            settings = board.with_suffix(suffix)
            if settings.exists():
                shutil.copy2(settings, stage.with_suffix(suffix))
        worker = Path(__file__).with_name("_fixed_fill_worker.py")
        subprocess.run(
            [str(python), str(worker), str(stage), str(output), json.dumps(sorted(protected_nets))],
            check=True,
            capture_output=True,
            text=True,
        )
        filled = output.read_text()
    replacements = []
    observed = set()
    for _, _, zone in _extract_balanced_blocks(filled, "zone"):
        match = re.search(r'\(uuid\s+"([^"\s]+)"\)', zone)
        if match is None or match[1] not in identities:
            raise ValueError("Native fill returned an unidentified zone")
        if match[1] not in expected:
            continue
        if match[1] in observed:
            raise ValueError("Native fill duplicated a zone")
        observed.add(match[1])
        start, end, original = identities[match[1]]
        # Native modern polygons are solid areas, even when the original board
        # version defaults to legacy stroked fills. Mark that explicitly.
        for token in ("filled_polygon", "fill_segments", "filled_areas_thickness"):
            for a, b, _ in reversed(_extract_balanced_blocks(original, token)):
                original = original[:a] + original[b:]
        polygons = [block for _, _, block in _extract_balanced_blocks(zone, "filled_polygon")]
        replacement = original.rstrip()[:-1] + "\n(filled_areas_thickness no)\n"
        replacement += "\n".join(polygons) + ")"
        replacements.append((start, end, replacement))
    if observed != expected:
        raise ValueError("Native fill omitted eligible zones")
    result = source
    for start, end, replacement in sorted(replacements, reverse=True):
        result = result[:start] + replacement + result[end:]
    atomic_write_text(board, result)
