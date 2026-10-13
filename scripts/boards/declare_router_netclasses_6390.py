#!/usr/bin/env python3
"""Declare the router's net classes in the committed routed sidecars (Issue #6390).

``kct route`` now writes the router's stricter net classes (``Power``,
``Clock``, ...) into the routed ``.kicad_pro`` / ``.kicad_dru``.  The committed
demo boards were routed before that, so their sidecars declare ``Default``
alone.  This script brings them to what a route writes today *without
re-routing*: copper is untouched and only the sidecars change.  ``--refill``
also recomputes the saved zone fills under the new rules (zones filled
against the old fab-floor-only rules sit too close to classed copper), but
that invalidates each board's hash-bound manufacturing bundle, so it belongs
with regenerating that evidence (#6404).

The router's effective map is rebuilt the way the router builds it
(``DEFAULT_NET_CLASS_MAP`` plus the name classifier over the board's nets,
``router/io.py``), overlaid with the board's committed ``net_class_map.json``
when present -- either the sidecar a route wrote or the ``--net-class-map``
input the recipe passes, the same overlay ``kct route`` applies.

Usage::

    uv run python scripts/boards/declare_router_netclasses_6390.py [--refill] [BOARD ...]

``BOARD`` is a directory name under ``boards/`` (default: boards 00-07).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

#: ``board dir -> (routed PCB, manufacturer, copper layers)``.  Manufacturer
#: and layers are the ones the committed sidecars were written for (their
#: ``meta`` and fab-floor rule labels).
BOARDS: dict[str, tuple[str, str, int]] = {
    "00-simple-led": ("simple_led_routed.kicad_pcb", "jlcpcb", 2),
    "01-voltage-divider": ("voltage_divider_routed.kicad_pcb", "jlcpcb", 2),
    "02-charlieplex-led": ("charlieplex_3x3_routed.kicad_pcb", "jlcpcb", 2),
    "03-usb-joystick": ("usb_joystick_routed.kicad_pcb", "jlcpcb-tier1", 4),
    "04-stm32-devboard": ("stm32_devboard_routed.kicad_pcb", "jlcpcb-tier1", 2),
    "05-bldc-motor-controller": ("bldc_controller_routed.kicad_pcb", "jlcpcb", 4),
    "06-diffpair-test": ("diffpair_test_routed.kicad_pcb", "jlcpcb", 4),
    "07-matchgroup-test": ("matchgroup_test_routed.kicad_pcb", "jlcpcb", 6),
}


def router_net_class_map(pcb: Path) -> dict:
    """The router's effective ``{net: NetClassRouting}`` map for ``pcb``."""
    from kicad_tools.manufacturers.project_generator import board_net_names
    from kicad_tools.router.net_class import classify_and_apply_rules
    from kicad_tools.router.rules import DEFAULT_NET_CLASS_MAP, net_class_map_from_dict

    nets = sorted(n for n in (board_net_names(pcb) or ()) if n)
    effective = dict(DEFAULT_NET_CLASS_MAP)
    for name, routing in classify_and_apply_rules(dict(enumerate(nets, 1))).items():
        effective.setdefault(name, routing)
    committed = pcb.parent / "net_class_map.json"
    if committed.is_file():
        effective.update(net_class_map_from_dict(json.loads(committed.read_text())))
    return effective


def update_board(board: str, *, refill: bool) -> None:
    from kicad_tools.cli.route_cmd import _write_drc_constraint_sidecars

    name, manufacturer, layers = BOARDS[board]
    pcb = REPO / "boards" / board / "output" / name
    _write_drc_constraint_sidecars(
        pcb, manufacturer, layers, net_class_map=router_net_class_map(pcb)
    )
    if board == "04-stm32-devboard":
        # Board 04's reviewed paid-drill floors (KCT_PRESERVE_BOARD_RULES=0
        # plus the 0.075 mm annular ring), exactly as its recipe re-applies
        # them after routing.
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "board04_process", REPO / "boards" / board / "manufacturing_process.py"
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        module.apply_native_floors(pcb)
    if refill:
        refill_zones_in_place(pcb)


def _top_level_zones(text: str) -> list[tuple[int, int]]:
    """``(start, end)`` spans of the board's top-level ``(zone ...)`` blocks."""
    spans: list[tuple[int, int]] = []
    depth = 0
    in_string = False
    start = -1
    i = 0
    while i < len(text):
        char = text[i]
        if in_string:
            if char == "\\":
                i += 2
                continue
            if char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "(":
            depth += 1
            if depth == 2 and text.startswith("(zone", i) and text[i + 5] in " \n\t":
                start = i
        elif char == ")":
            if depth == 2 and start >= 0:
                spans.append((start, i + 1))
                start = -1
            depth -= 1
        i += 1
    return spans


_UUID_RE = re.compile(r'\((?:uuid|tstamp)\s+"?([0-9a-fA-F-]+)"?\)')


def refill_zones_in_place(pcb: Path) -> None:
    """Refill ``pcb``'s zones under its sidecars, changing only the zone blocks.

    Every committed board's manufacturing bundle hash-binds the exact routed
    PCB bytes (``kicad_project.zip`` + manifest, Gerbers plotted from the
    saved fills), and ``kct check``'s ``manifest`` meta-check fails once they
    differ.  So refill only together with regenerating that evidence
    (#6404); #6390 itself ships the sidecars and leaves the fills alone.

    ``kicad-cli`` re-saves the whole board in its own formatting; transplanting
    just the refilled ``(zone ...)`` blocks (matched by UUID) back into the
    committed text keeps the diff to the fills.
    """
    from kicad_tools.cli.runner import _run_fill_zones_via_drc, find_kicad_cli

    original = pcb.read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="kct-6390-fill-") as tmp:
        staged = Path(tmp) / pcb.name
        for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
            source = pcb.with_suffix(suffix)
            if source.exists():
                shutil.copyfile(source, staged.with_suffix(suffix))
        # A plain native refill (``pcb drc --refill-zones --save-board``),
        # deliberately without ``run_fill_zones``' starved-thermal
        # remediation: that also edits footprint pads, which the zone-block
        # transplant below would drop.  The saved fill is then exactly what
        # every ``--refill-zones`` gate computes.
        result = _run_fill_zones_via_drc(staged, None, find_kicad_cli())
        if not result.success:
            raise RuntimeError(f"zone refill failed for {pcb}: {result.stderr}")
        refilled = staged.read_text(encoding="utf-8")

    def by_uuid(text: str) -> dict[str, tuple[int, int]]:
        found: dict[str, tuple[int, int]] = {}
        for start, end in _top_level_zones(text):
            match = _UUID_RE.search(text, start, end)
            if match is None or match.group(1) in found:
                raise RuntimeError(f"zone without a unique uuid in {pcb}")
            found[match.group(1)] = (start, end)
        return found

    old_zones, new_zones = by_uuid(original), by_uuid(refilled)
    if set(old_zones) != set(new_zones):
        raise RuntimeError(f"refill changed the zone set of {pcb}")
    pieces: list[str] = []
    cursor = 0
    for uuid, (start, end) in sorted(old_zones.items(), key=lambda item: item[1]):
        new_start, new_end = new_zones[uuid]
        pieces += [original[cursor:start], refilled[new_start:new_end]]
        cursor = end
    pieces.append(original[cursor:])
    pcb.write_text("".join(pieces), encoding="utf-8")
    print(f"  zone refill {pcb.name}: {len(old_zones)} zone(s) refilled")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("boards", nargs="*", default=list(BOARDS))
    parser.add_argument("--refill", action="store_true", help="refill saved zones afterwards")
    args = parser.parse_args(argv)
    for board in args.boards:
        if board not in BOARDS:
            print(f"unknown board {board!r}", file=sys.stderr)
            return 2
        print(f"== {board}")
        update_board(board, refill=args.refill)
    return 0


if __name__ == "__main__":
    sys.exit(main())
