"""Stage native fills around immutable placement-excluded copper."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from kicad_tools.core.atomic_write import atomic_write_text
from kicad_tools.router.optimizer.pcb import _extract_balanced_blocks, parse_net_names
from kicad_tools.sexp import parse_string


def find_kicad_python() -> Path | None:
    """Find an interpreter with the native APIs this selective fill needs."""
    candidates = [Path(sys.executable)]
    if sys.platform == "darwin":
        candidates.append(
            Path(
                "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3"
            )
        )
    elif sys.platform == "win32":
        import os

        for location in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)")):
            if location:
                candidates.extend(
                    sorted(Path(location).glob("KiCad/*/bin/python.exe"), reverse=True)
                )
    candidates.append(Path("/usr/bin/python3"))
    located = shutil.which("python3")
    if located:
        candidates.append(Path(located))
    seen = set()
    for candidate in candidates:
        if not candidate.is_file() or candidate.resolve() in seen:
            continue
        seen.add(candidate.resolve())
        try:
            result = subprocess.run(
                [
                    str(candidate),
                    "-c",
                    "import pcbnew, wx; assert hasattr(pcbnew.ZONE, 'SetLayerSetAndRemoveUnusedFills')",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            return candidate
    return None


def fill_around_fixed_copper(
    board: Path, protected_nets: frozenset[str], *, python: Path | None = None
) -> None:
    """Fill eligible zones, publishing only their polygons into original source bytes.

    Temporary higher-priority zones model the exact protected fill polygons.
    Keeping them as zones preserves zone-specific clearance rule matching. The
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
        parsed = parse_string(zone)
        identifier = parsed.find("uuid") or parsed.find("tstamp")
        authored = str(identifier.children[0].value) if identifier and identifier.children else None
        # Preserve native identities: group membership and custom rule context
        # may refer to them. Only legacy/missing identifiers need a staging tag.
        try:
            if authored and re.fullmatch(r"[0-9a-fA-F]{8}", authored):
                # KiCad's KIID expands legacy 32-bit timestamps this way.
                identity = str(uuid.UUID(int=int(authored, 16)))
            else:
                identity = str(uuid.UUID(authored)) if authored else str(uuid.uuid4())
        except ValueError:
            identity = str(uuid.uuid4())
        if identity in identities:
            raise ValueError("Cannot selectively fill zones with duplicate identities")
        identities[identity] = (start, end, zone)
        net = parsed.find("net")
        token = net.children[0].value if net is not None and net.children else 0
        name = net_names.get(token, "") if isinstance(token, int) else str(token)
        if name not in protected_nets and parsed.find("keepout") is None:
            expected.add(identity)
        # Normalize legacy timestamp syntax without changing modern identity.
        tagged = re.sub(r"\((?:uuid|tstamp)\s+[^)]+\)", "", zone)
        tagged = tagged.replace("(zone", f'(zone (uuid "{identity}")', 1)
        staged = staged[:start] + tagged + staged[end:]
    if not expected:
        return
    if python is None:
        python = find_kicad_python()
    if python is None:
        raise RuntimeError("KiCad Python with selective zone-fill support is unavailable")
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
