"""End-to-end starvation verification for issue #5590 (board 03).

Board 03 ships a hand-authored ``VCC`` pour on ``In2.Cu`` (full-board
boundary, priority 0) and ``kct route``'s auto-pour adds a ``VBUS`` pour
on the same layer.  Before #5590 the new zone tied the incumbent's
priority and kept the full-board outline, so KiCad's fill resolver
tie-broke on the zone UUID and starved one of the two pours (measured
2026-09-19, KiCad 10.0.5: one branch ``VCC`` 0.289 mm² / ``VBUS``
4624 mm², the other ``VCC`` 4627 mm² / ``VBUS`` **0** mm²).

The fix (``kicad_tools.zones.generator``) surfaces pre-existing
same-layer zones to both allocators, so the auto-poured zone lands with

* a priority strictly above the incumbent's maximum, and
* a carved per-net pad-bbox outline instead of the full board.

This module verifies that allocation where it matters -- on the real
board, through the real fill pipeline:

* :func:`test_board03_auto_pour_allocates_around_incumbent` (fast) --
  the emitted ``VBUS`` zone outranks ``VCC``, claims only its pad bbox,
  leaves every hand-authored zone untouched, and emits no
  "zero copper" warning.
* :func:`test_board03_auto_pour_allocation_is_reproducible` (fast) --
  repeated auto-pours produce byte-identical zone blocks.
* :func:`test_board03_both_pours_fill_regardless_of_uuid_order`
  (``@pytest.mark.slow``) -- after a real ``run_fill_zones`` pass, BOTH
  ``In2.Cu`` pours own non-trivial copper, and the outcome is identical
  when the ``VBUS`` zone's UUID is forced to sort either side of the
  ``VCC`` zone's.  That is the acceptance criterion the issue words as
  "regardless of which zone's UUID sorts first".

The fill instruments (``_fill_union_signature`` and the area tolerance)
are reused from ``tests/test_pour_fill_determinism_5578.py`` so the two
modules cannot disagree about what "same copper" means.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

BOARD03 = REPO_ROOT / "boards/03-usb-joystick/output/usb_joystick.kicad_pcb"

#: The hand-authored VCC zone's UUID on board 03 (first block, fixed).
_VCC_ZONE_UUID8 = "99b8f0a0"

#: UUIDs that sort strictly after / before the VCC zone's UUID.  Only the
#: ordering relative to ``_VCC_ZONE_UUID8`` matters -- these reproduce the
#: two branches measured in the #5578/#5590 root-cause analysis.
_UUID_SORTS_AFTER_VCC = "e4c41fb8-1111-4111-8111-111111111111"
_UUID_SORTS_BEFORE_VCC = "18329af8-1111-4111-8111-111111111111"

#: Both pours must own at least this much In2.Cu copper (mm^2).  The
#: fixed allocation measures ~4180 mm^2 (VCC) and ~420 mm^2 (VBUS); the
#: starved branches measured 0.289 mm^2 and exactly 0 -- so 100 mm^2
#: separates the outcomes by more than an order of magnitude in both
#: directions.
_MIN_FILL_AREA_MM2 = 100.0


def _zone_blocks_normalized(path: Path) -> list[str]:
    """Whitespace-normalized ``(zone ...)`` blocks in file order."""
    blocks: list[str] = []
    text = path.read_text()
    idx = 0
    while True:
        start = text.find("(zone", idx)
        if start < 0:
            return blocks
        depth = 0
        pos = start
        while pos < len(text):
            if text[pos] == "(":
                depth += 1
            elif text[pos] == ")":
                depth -= 1
                if depth == 0:
                    pos += 1
                    break
            pos += 1
        blocks.append(re.sub(r"\s+", " ", text[start:pos]))
        idx = pos


def _override_vbus_zone_uuid(path: Path, uuid: str) -> None:
    """Force the auto-poured VBUS zone's UUID to *uuid* (in place).

    The content-addressed derivation (#5578) already fixes this UUID, but
    the point of this override is to prove the allocation no longer
    *depends* on it: both sort orders relative to the hand-authored VCC
    zone's UUID must produce the same fill.
    """
    text = path.read_text()
    idx = 0
    while True:
        start = text.find("(zone", idx)
        if start < 0:
            break
        depth = 0
        pos = start
        while pos < len(text):
            if text[pos] == "(":
                depth += 1
            elif text[pos] == ")":
                depth -= 1
                if depth == 0:
                    pos += 1
                    break
            pos += 1
        block = text[start:pos]
        if '"VBUS"' in block and '"In2.Cu"' in block:
            new_block, n = re.subn(
                r'\(uuid\s+"[0-9a-f-]+"\)',
                f'(uuid "{uuid}")',
                block,
                count=1,
            )
            assert n == 1, "VBUS zone block carries no uuid to override"
            path.write_text(text[:start] + new_block + text[pos:])
            return
        idx = pos
    raise AssertionError("no VBUS/In2.Cu zone found to override")


def _zones_by_net(path: Path) -> dict:
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load(str(path))
    return {z.net_name: z for z in pcb.zones}


class TestBoard03Allocation:
    """Fast checks on the real board-03 fixture (no kicad-cli needed)."""

    def test_board03_auto_pour_allocates_around_incumbent(self, tmp_path, capsys):
        """VBUS outranks VCC on In2.Cu and claims only its pad bbox."""
        from kicad_tools.router.auto_pour import auto_pour_if_missing

        target = tmp_path / "board03.kicad_pcb"
        shutil.copy(BOARD03, target)
        before = {
            net: (z.layer, z.priority, z.uuid, tuple(z.polygon))
            for net, z in _zones_by_net(target).items()
        }

        created, names = auto_pour_if_missing(target, quiet=True)

        assert created == 1
        assert names == ["VBUS"]

        zones = _zones_by_net(target)
        vbus, vcc = zones["VBUS"], zones["VCC"]
        assert vbus.layer == "In2.Cu" == vcc.layer
        # Priority stagger: strictly above the incumbent's maximum.
        assert vbus.priority > vcc.priority
        # Carved outline: the VBUS pad bbox (pads span roughly
        # (35.9, 4.5)-(54.2, 22.3) in board-relative coordinates) plus the
        # 1.5 mm default margin -- far smaller than the 79 x 59 mm board.
        # (Zone.polygon is board-relative after PCB.load; board 03's
        # origin is (108.5, 57.5).)
        bx = [p[0] for p in vbus.polygon]
        by = [p[1] for p in vbus.polygon]
        assert (max(bx) - min(bx)) * (max(by) - min(by)) < 700.0  # mm^2
        assert 30.0 < min(bx) and max(bx) < 58.0
        assert 1.0 < min(by) and max(by) < 25.0

        # Every hand-authored zone is untouched (net, layer, priority,
        # uuid, polygon); only the VBUS entry is new.
        after = {
            net: (z.layer, z.priority, z.uuid, tuple(z.polygon))
            for net, z in zones.items()
        }
        assert {k: v for k, v in after.items() if k != "VBUS"} == before

        # The starvation warning is gone.
        captured = capsys.readouterr()
        assert "zero copper" not in captured.err

    def test_board03_auto_pour_allocation_is_reproducible(self, tmp_path):
        """Repeated auto-pours emit byte-identical zone blocks (#5578 holds)."""
        from kicad_tools.router.auto_pour import auto_pour_if_missing

        runs: list[list[str]] = []
        for run in range(2):
            target = tmp_path / f"run{run}.kicad_pcb"
            shutil.copy(BOARD03, target)
            created, _ = auto_pour_if_missing(target, quiet=True)
            assert created == 1
            runs.append(_zone_blocks_normalized(target))

        assert runs[0] == runs[1], (
            "auto_pour_if_missing produced different zone blocks across "
            "runs over the same board-03 input (regression of #5578's "
            "determinism under the #5590 allocation)."
        )


@pytest.mark.slow
@pytest.mark.timeout(1800)
def test_board03_both_pours_fill_regardless_of_uuid_order(tmp_path):
    """Live fill: both In2.Cu pours own real copper, UUID-order invariant.

    For each UUID sort order relative to the hand-authored VCC zone, the
    board is auto-poured, the VBUS zone's UUID is forced to that order,
    and the real fill pipeline (``run_fill_zones`` -- the same entry
    ``kct route`` uses) fills the board.  Both pours must end with
    non-trivial copper and the two orders must agree.
    """
    pytest.importorskip("shapely")
    from kicad_tools.cli.runner import find_kicad_cli, run_fill_zones
    from kicad_tools.router.auto_pour import auto_pour_if_missing
    from tests.test_pour_fill_determinism_5578 import (
        _UNION_AREA_TOLERANCE_MM2,
        _fill_union_signature,
    )

    if find_kicad_cli() is None:
        pytest.skip("kicad-cli not installed -- zone fill is a no-op, nothing to compare")

    areas: dict[str, dict[tuple[str, str], float]] = {}
    for tag, uuid in (
        ("after", _UUID_SORTS_AFTER_VCC),
        ("before", _UUID_SORTS_BEFORE_VCC),
    ):
        target = tmp_path / f"uuid_{tag}.kicad_pcb"
        shutil.copy(BOARD03, target)
        created, names = auto_pour_if_missing(target, quiet=True)
        assert created == 1 and names == ["VBUS"]
        _override_vbus_zone_uuid(target, uuid)

        result = run_fill_zones(target)
        assert result.success, f"fill failed for uuid_{tag}: {result.stderr}"

        sig = _fill_union_signature(target)
        areas[tag] = {key: sig[key][0] for key in (("VCC", "In2.Cu"), ("VBUS", "In2.Cu"))}

    for tag in ("after", "before"):
        for key, area in areas[tag].items():
            assert area > _MIN_FILL_AREA_MM2, (
                f"uuid sorts {tag} VCC: {key} pour owns only {area} mm^2 on "
                "In2.Cu (starved).  The staggered-priority + carved-outline "
                "allocation must leave both pours real copper regardless of "
                "the zone-UUID tie-break (issue #5590)."
            )

    for key in (("VCC", "In2.Cu"), ("VBUS", "In2.Cu")):
        assert abs(areas["after"][key] - areas["before"][key]) <= _UNION_AREA_TOLERANCE_MM2, (
            f"{key} pour copper depends on which zone UUID sorts first "
            f"({areas['after'][key]} vs {areas['before'][key]} mm^2) -- the "
            "allocation is still tie-break dependent (issue #5590)."
        )
