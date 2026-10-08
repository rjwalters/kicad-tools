"""End-to-end: a netclass stricter than the fab floor routes DRC-clean (#6243).

A tiny two-layer board split by a wall of through-hole pads whose shortest
``HV`` crossing is a 1.0 mm pad-edge corridor -- 0.4 mm each side, legal at the
0.15 mm ``Default`` clearance and illegal under the project's 0.5 mm ``HV``
netclass -- while a longer corridor is legal for ``HV``.
``kct route`` must honour the stricter class on BOTH backends, and the native
referee (``kicad-cli pcb drc``, which loads the routed project and its
netclasses) must report zero clearance errors and nothing unconnected.

The routing assertions run everywhere; the ``kicad-cli`` referee is skipped
when the binary is unavailable.  Before #6243 the router ignored every
netclass but ``Default`` and ran ``HV`` straight through the short corridor,
which ``kicad-cli`` flags as clearance violations against the wall pads.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from kicad_tools.router.authored_clearance import authored_violations
from kicad_tools.router.cpp_backend import is_cpp_available

NETS = {1: "HV", 2: "SIG"}
HV_CLEARANCE = 0.5
DEFAULT_CLEARANCE = 0.15

#: A vertical wall of unconnected through-hole pads (0.7 mm on 0.3 mm drills,
#: 0.9 mm pitch, so 0.2 mm pad gaps: legal for the pads themselves, impassable
#: for any trace)
#: splits the board.  Three corridors cross it, top to bottom, each
#: ``(pad_edge_gap, pads_before)``: ``C`` carries SIG; ``A`` is HV's straight
#: line, 0.45 mm each side of a 0.2 mm trace -- legal at the 0.15 mm base and
#: ILLEGAL under HV's 0.5 mm class; ``B`` is the longer detour that is legal
#: for HV (0.7 mm each side).
WALL_X, WALL_TOP, WALL_PITCH, WALL_PAD = 115.0, 100.85, 0.9, 0.7
CORRIDOR_SPECS = ((1.6, 1), (1.1, 4), (1.6, 2))
TAIL_PADS = 2


def wall_layout() -> tuple[list[float], list[float]]:
    """``(pad centre ys, corridor centre ys)`` for the wall."""
    ys: list[float] = []
    centres: list[float] = []
    y = WALL_TOP
    for gap, run in CORRIDOR_SPECS:
        ys += [round(y + WALL_PITCH * i, 4) for i in range(run + 1)]
        y += WALL_PITCH * run
        centres.append(round(y + WALL_PAD / 2 + gap / 2, 4))
        y += WALL_PAD + gap
    ys += [round(y + WALL_PITCH * i, 4) for i in range(TAIL_PADS + 1)]
    return ys, centres


SIG_Y, HV_Y, DETOUR_Y = wall_layout()[1]
#: 0.5 mm below the last wall pad's copper (the fab's pad-to-edge minimum),
#: still too tight for a trace plus its edge and pad clearances to pass.
BOARD_BOTTOM = round(wall_layout()[0][-1] + WALL_PAD / 2 + 0.5, 4)


def _footprint(ref: str, x: float, y: float, pads: list[tuple[str, float, float, int]]) -> str:
    body = "\n".join(
        f'    (pad "{num}" smd rect (at {px} {py}) (size 0.6 0.6) '
        f'(layers "F.Cu" "F.Paste" "F.Mask") (net {net} "{NETS[net]}"))'
        for num, px, py, net in pads
    )
    return _footprint_block(ref, x, y, body)


def _footprint_block(ref: str, x: float, y: float, body: str) -> str:
    return f"""  (footprint "test:{ref}" (layer "F.Cu") (at {x} {y})
    (property "Reference" "{ref}" (at 0 -1.5 0) (layer "F.SilkS")
      (effects (font (size 0.8 0.8) (thickness 0.12))))
    (property "Value" "{ref}" (at 0 1.5 0) (layer "F.Fab")
      (effects (font (size 0.8 0.8) (thickness 0.12))))
{body}
  )"""


def _wall() -> str:
    body = "\n".join(
        f'    (pad "{i + 1}" thru_hole circle (at 0 {round(y - 109.0, 4)}) '
        f'(size {WALL_PAD} {WALL_PAD}) (drill 0.3) (layers "*.Cu" "*.Mask"))'
        for i, y in enumerate(wall_layout()[0])
    )
    return _footprint_block("W1", WALL_X, 109.0, body)


def board_text() -> str:
    """The corridor board (see :data:`CORRIDOR_SPECS`)."""
    parts = [
        _footprint(
            "J1",
            103,
            105.0,
            [("1", 0, round(HV_Y - 105, 4), 1), ("2", 0, round(SIG_Y - 105, 4), 2)],
        ),
        _footprint(
            "J2",
            127,
            105.0,
            [("1", 0, round(HV_Y - 105, 4), 1), ("2", 0, round(SIG_Y - 105, 4), 2)],
        ),
        _wall(),
    ]
    nets = "\n".join(f'  (net {i} "{n}")' for i, n in NETS.items())
    return f"""(kicad_pcb (version 20240108) (generator "pcbnew") (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal) (31 "B.Cu" signal)
    (34 "B.Paste" user) (35 "F.Paste" user)
    (36 "B.SilkS" user "B.Silkscreen") (37 "F.SilkS" user "F.Silkscreen")
    (38 "B.Mask" user) (39 "F.Mask" user)
    (44 "Edge.Cuts" user) (46 "B.CrtYd" user "B.Courtyard") (47 "F.CrtYd" user "F.Courtyard")
    (48 "B.Fab" user) (49 "F.Fab" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
{nets}
  (gr_rect (start 100 100) (end 130 {BOARD_BOTTOM}) (stroke (width 0.1) (type default)) (fill none)
    (layer "Edge.Cuts"))
{chr(10).join(parts)}
)
"""


def _netclass(name: str, clearance: float, priority: int) -> dict:
    return {
        "name": name,
        "clearance": clearance,
        "track_width": 0.2,
        "via_diameter": 0.6,
        "via_drill": 0.3,
        "microvia_diameter": 0.3,
        "microvia_drill": 0.1,
        "diff_pair_width": 0.2,
        "diff_pair_gap": 0.25,
        "diff_pair_via_gap": 0.25,
        "wire_width": 6,
        "bus_width": 12,
        "line_style": 0,
        "schematic_color": "rgba(0, 0, 0, 0.000)",
        "pcb_color": "rgba(0, 0, 0, 0.000)",
        "priority": priority,
    }


def project(name: str) -> dict:
    """KiCad 10-loadable project: ``Default`` 0.15 mm, ``HV`` 0.5 mm by pattern."""
    return {
        "meta": {"filename": f"{name}.kicad_pro", "version": 3},
        "board": {
            "design_settings": {
                "rules": {
                    "min_clearance": 0.1,
                    "min_track_width": 0.1,
                    "min_copper_edge_clearance": 0.2,
                }
            }
        },
        "net_settings": {
            "meta": {"version": 4},
            "classes": [
                _netclass("Default", DEFAULT_CLEARANCE, 2147483647),
                _netclass("HV", HV_CLEARANCE, 0),
            ],
            "netclass_assignments": None,
            "netclass_patterns": [{"netclass": "HV", "pattern": "HV"}],
        },
    }


def write_board(directory: Path) -> Path:
    """Write ``hv.kicad_pcb`` + ``hv.kicad_pro`` into ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    pcb = directory / "hv.kicad_pcb"
    pcb.write_text(board_text())
    pcb.with_suffix(".kicad_pro").write_text(json.dumps(project("hv"), indent=2))
    return pcb


def _kicad_cli() -> str | None:
    from kicad_tools.cli.runner import find_kicad_cli

    found = find_kicad_cli()
    return str(found) if found else shutil.which("kicad-cli")


def _route(pcb: Path, backend: str) -> Path:
    out = pcb.with_name("hv_routed.kicad_pcb")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "route",
            str(pcb),
            "-o",
            str(out),
            "--backend",
            backend,
            "--seed",
            "42",
            "--force",
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-4000:]
    return out


def _routed_census(out: Path) -> list:
    """Re-load the routed board and run the authored census on its copper."""
    from kicad_tools.router.io import load_pcb_for_routing

    router, _nets = load_pcb_for_routing(
        str(out), load_existing_routes=True, force_python=True, validate_drc=False
    )
    hv = _nets["HV"]
    assert router.rules.net_clearance_floors == {hv: HV_CLEARANCE}
    return authored_violations(
        router.rules.net_clearance_floors, router.grid.pads, router.existing_routes
    )


BACKENDS = [
    # The pure-Python search needs ~60 s for the detour, over the bulk job's
    # per-test budget; it runs in the slow-tests workflow instead.
    pytest.param("python", marks=pytest.mark.slow),
    pytest.param(
        "cpp", marks=pytest.mark.skipif(not is_cpp_available(), reason="native build required")
    ),
]


@pytest.mark.parametrize("backend", BACKENDS)
def test_stricter_netclass_routes_drc_clean(tmp_path: Path, backend: str) -> None:
    out = _route(write_board(tmp_path), backend)
    assert _routed_census(out) == []
    routed_project = json.loads(out.with_suffix(".kicad_pro").read_text())
    classes = {c["name"]: c["clearance"] for c in routed_project["net_settings"]["classes"]}
    assert classes["HV"] == HV_CLEARANCE  # the referee sees the stricter class

    cli = _kicad_cli()
    if cli is None:
        pytest.skip("kicad-cli not available for the native DRC referee")
    # Referee twice: with the routed board's own sidecars (the restated
    # netclass rule in the generated .kicad_dru), and with ONLY the source
    # project -- KiCad's own netclass resolution, no kct-generated rule.
    source_rules = tmp_path / "source_rules"
    source_rules.mkdir()
    shutil.copyfile(out, source_rules / out.name)
    shutil.copyfile(tmp_path / "hv.kicad_pro", (source_rules / out.name).with_suffix(".kicad_pro"))
    for board in (out, source_rules / out.name):
        report = board.with_name("drc.json")
        subprocess.run(
            [cli, "pcb", "drc", "--format", "json", "--output", str(report), str(board)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        data = json.loads(report.read_text())
        errors = [v for v in data["violations"] if v.get("severity") == "error"]
        assert errors == [], (board, errors)
        assert data.get("unconnected_items", []) == []
