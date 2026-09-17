"""Serialise a :class:`~tests.conformance.generator.CopperCase` to KiCad files.

Writes a real ``.kicad_pcb`` plus the sibling ``.kicad_pro`` that
``kicad-cli pcb drc`` loads automatically.  The project file is not optional
decoration: **kicad-cli reads the applied clearance from the project's
``Default`` netclass, not from the board**, which is exactly the value the
#5398 route-clearance resolver never consults.  A conformance board without a
project file would measure KiCad's stock 0.20 mm default instead of the rules
under test.

Three details are load-bearing:

``center=False``
    ``PCB.create`` otherwise offsets the board origin to centre the outline on
    the drawing sheet, so board-relative input coordinates and the
    sheet-absolute coordinates kicad-cli reports back would differ by that
    offset.  With ``center=False`` the two frames coincide and a fixture can
    quote the coordinates from the issue it reproduces verbatim.

Deterministic UUIDs and a pinned date
    ``add_trace`` / ``add_via`` / ``add_footprint_from_file`` mint
    ``uuid.uuid4()`` per object and ``PCB.create`` stamps ``date.today()``.
    Left alone, regenerating a committed fixture produces a byte-different
    file every single time and "byte-identical across two runs" is unmeetable.
    :func:`seeded_uuids` patches ``uuid.uuid4`` with a stream drawn from the
    case's own seed for the duration of the write, and the board date comes
    from ``CopperCase.board_date``.

Pads come from committed ``.kicad_mod`` files
    ``PCB.add_footprint`` resolves ``Library:Footprint`` through
    ``detect_kicad_library_path()`` and raises when the stock KiCad footprint
    library is absent, which is the normal state of a developer laptop.
    ``add_footprint_from_file`` against ``tests/fixtures/conformance/
    Conformance.pretty/`` is portable.
"""

from __future__ import annotations

import hashlib
import random
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from kicad_tools.core.project_file import save_project
from kicad_tools.manufacturers.base import DesignRules
from kicad_tools.manufacturers.project_generator import build_project_data
from kicad_tools.pcb.editor import PCBEditor
from kicad_tools.schema.pcb import PCB
from tests.conformance.generator import CaseRules, CopperCase

__all__ = [
    "PRETTY_DIR",
    "WrittenBoard",
    "manufacturer_rules",
    "seeded_uuids",
    "write_case",
]

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "conformance"
PRETTY_DIR = FIXTURES_DIR / "Conformance.pretty"


@dataclass(frozen=True)
class WrittenBoard:
    """Where a case landed on disk."""

    case: CopperCase
    pcb_path: Path
    project_path: Path


@contextmanager
def seeded_uuids(seed: int) -> Iterator[None]:
    """Replace ``uuid.uuid4`` with a deterministic stream for this block.

    ``kicad_tools``' writers call ``uuid.uuid4()`` through the module (not a
    from-import), so patching the attribute on :mod:`uuid` reaches every
    writer -- the schema ``PCB`` and ``PCBEditor`` alike -- without any of them
    needing a hook of their own.

    The stream is drawn from a dedicated ``random.Random(seed)`` so it cannot
    perturb the case generator's own RNG consumption.
    """
    rng = random.Random(seed)

    def _fake_uuid4() -> uuid.UUID:
        return uuid.UUID(int=rng.getrandbits(128), version=4)

    with mock.patch.object(uuid, "uuid4", _fake_uuid4):
        yield


def manufacturer_rules(rules: CaseRules) -> DesignRules:
    """Project-side rules: what ``kicad-cli pcb drc`` will actually apply.

    ``build_project_data`` maps ``min_clearance_mm`` onto the ``Default``
    netclass ``clearance`` (the applied copper-to-copper requirement) and the
    hole/edge fields onto ``board.design_settings.rules``.
    """
    return DesignRules(
        min_trace_width_mm=rules.min_trace_width,
        min_clearance_mm=rules.project_clearance,
        min_via_drill_mm=rules.min_via_drill,
        min_via_diameter_mm=rules.min_via_diameter,
        min_annular_ring_mm=0.05,
        min_hole_diameter_mm=0.2,
        min_copper_to_edge_mm=rules.min_copper_to_edge,
        min_hole_to_hole_mm=rules.min_hole_to_hole,
    )


def write_case(case: CopperCase, directory: Path, stem: str | None = None) -> WrittenBoard:
    """Write ``case`` as ``<stem>.kicad_pcb`` + ``<stem>.kicad_pro``.

    Args:
        case: The copper configuration to serialise.
        directory: Destination directory (created if missing).
        stem: Filename stem; defaults to ``case.name``.

    Returns:
        A :class:`WrittenBoard` naming both files.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stem = stem or case.name
    pcb_path = directory / f"{stem}.kicad_pcb"
    project_path = directory / f"{stem}.kicad_pro"

    # The UUID seed is derived from the case rather than taken from
    # ``case.seed`` directly so that two cases with the same seed but a
    # different name (a named fixture vs. a corpus case) do not collide.
    # ``hash()`` is NOT usable here: CPython salts string hashing per process
    # (PYTHONHASHSEED), which is precisely the non-determinism this whole
    # module exists to remove.
    uuid_seed = int.from_bytes(
        hashlib.sha256(f"{case.seed}:{case.name}".encode()).digest()[:8], "big"
    )

    with seeded_uuids(uuid_seed):
        pcb = PCB.create(
            width=case.width,
            height=case.height,
            layers=case.layers,
            title=case.name,
            board_date=case.board_date,
            center=False,
        )

        # Declare every net up front and in a stable order, so net numbers do
        # not depend on which object happened to be written first.
        for net_name in case.nets:
            pcb.add_net(net_name)

        for pad in case.pads:
            pcb.add_footprint_from_file(
                kicad_mod_path=PRETTY_DIR / f"{pad.footprint}.kicad_mod",
                reference=pad.reference,
                x=pad.x,
                y=pad.y,
                rotation=pad.rotation,
                layer=pad.layer,
            )
            pcb.assign_net_to_footprint_pad(pad.reference, "1", pad.net)

        for seg in case.segments:
            pcb.add_trace(
                start=seg.start,
                end=seg.end,
                width=seg.width,
                layer=seg.layer,
                net=seg.net,
            )

        for via in case.vias:
            pcb.add_via(
                x=via.x,
                y=via.y,
                size=via.diameter,
                drill=via.drill,
                layers=via.layers,
                net=via.net,
            )

        pcb.save(pcb_path)

        # ``PCB`` has no zone writer; ``PCBEditor`` does.  Second pass over the
        # saved file (the editor requires the net to be declared already, which
        # the loop above guarantees).
        if case.zone is not None:
            editor = PCBEditor(str(pcb_path))
            editor.add_zone(
                net_name=case.zone.net,
                layer=case.zone.layer,
                boundary=list(case.zone.boundary),
                clearance=case.zone.clearance,
                min_thickness=case.zone.min_thickness,
            )
            editor.save()

    project_data = build_project_data(
        manufacturer_rules(case.rules),
        project_name=stem,
    )
    save_project(project_data, project_path)

    return WrittenBoard(case=case, pcb_path=pcb_path, project_path=project_path)
