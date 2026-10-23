"""``net-status --why`` agrees with the router on a pairwise decline (issue #7879).

The router searches under the HV pairwise (class-pair) clearance a
``--voltage-map`` derives; ``kct net-status --why`` classifies the SAVED board
with a geometry-only model that has no voltage map.  Before #7879 the router's
lattice decline never reached the ``.search_failures.json`` sidecar (and the
sidecar was written only from the post-route DRC step, so a ``--skip-drc`` run
left none), so ``--why`` called the declined HV net ``BUDGET_STARVED`` --
"re-route this net first or raise the per-net budget" -- on a net no budget
can route.

The board is fully synthetic, with made-up net names: a TO-220-style 3-pad
through-hole part ``Q1`` whose middle pin is the only pad on ``/HV_A`` (pins 1
and 3 carry no net, so Q1 is single-net and gets no #4506 attach zone); the
other ``/HV_A`` terminal ``J1`` sits 15 mm east.  Four SEPARATE single-pad
footprints on ``/LV_B`` ring ``Q1.2`` north/east/south/west -- far enough that
an escape stub can leave the pad, close enough that every way out of the ring
passes inside the HV-to-LV pairwise requirement.  Separate footprints, so no
attach zone waives the proximity.  The ring sits outside the classifier's 2 mm
congestion radius, so the neighbourhood is sparse and ``main`` reaches the
budget-starved branch deterministically.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from kicad_tools.cli.net_status_cmd import main as net_status_main
from kicad_tools.cli.route_cmd import main as route_main
from kicad_tools.router.stuck_classifier import DEFAULT_CONGESTION_RADIUS_MM

HV_VOLTS = 300.0
#: THT pad diameter used for every pad on the board.
PAD_D = 1.5
#: Q1 sits here (board-absolute mm); the outline spans 100..130 x 100..125.
QX, QY = 110.0, 110.0


def _derived_requirement() -> float:
    """HV-to-LV pairwise requirement the router derives for this voltage map."""
    from kicad_tools.placement.hv_domains import build_required_by_domain_pair

    required = build_required_by_domain_pair(
        {"HV_A": HV_VOLTS, "LV_B": 0.0},
        standard_id="iec60664",
        pollution_degree=2,
        material_group="IIIa",
        hv_threshold=30.0,
    )
    return max(required.values())


def _ring_radius() -> float:
    """Centre distance from Q1.2 to each ``/LV_B`` pad.

    The requirement plus one LV pad radius puts the LV pad EDGE just beyond
    the requirement from Q1.2's centre, plus a small margin so an escape stub
    can leave the pad; adjacent ring pads then sit far closer than twice the
    requirement, so no lane survives between them.
    """
    return round(_derived_requirement() + PAD_D / 2.0 + 0.55, 3)


def _tht(number: str, x: float, y: float, net: tuple[int, str] | None = None) -> str:
    n = f' (net {net[0]} "{net[1]}")' if net else ""
    return (
        f'(pad "{number}" thru_hole circle (at {x} {y}) (size {PAD_D} {PAD_D}) '
        f'(drill 0.8) (layers "*.Cu" "*.Mask"){n})'
    )


def _fp(ref: str, uid: int, x: float, y: float, pads: str) -> str:
    return f"""  (footprint "Synthetic:{ref}"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000{uid:02d}")
    (at {x} {y})
    (property "Reference" "{ref}" (at 0 -3 0) (layer "F.SilkS"))
    (property "Value" "X" (at 0 3 0) (layer "F.Fab"))
    {pads}
  )
"""


def _to220_ring_board(ring: float) -> str:
    hv = (1, "/HV_A")
    lv = (2, "/LV_B")
    q1 = " ".join([_tht("1", -2.54, 0), _tht("2", 0, 0, hv), _tht("3", 2.54, 0)])
    parts = [_fp("Q1", 10, QX, QY, q1), _fp("J1", 11, QX + 15.0, QY, _tht("1", 0, 0, hv))]
    for i, deg in enumerate((0, 90, 180, 270)):
        a = math.radians(deg)
        x = round(QX + ring * math.cos(a), 3)
        y = round(QY + ring * math.sin(a), 3)
        parts.append(_fp(f"R{i + 1}", 20 + i, x, y, _tht("1", 0, 0, lv)))
    return f"""(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup
    (pad_to_mask_clearance 0)
  )
  (net 0 "")
  (net 1 "/HV_A")
  (net 2 "/LV_B")
  (gr_rect (start 100 100) (end 130 125)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
{"".join(parts)})
"""


def _route_args(pcb: Path, out: Path, vmap: Path | None, engine: str = "lattice") -> list[str]:
    args = [str(pcb), "-o", str(out), "--route-engine", engine]
    if engine == "lattice":
        args += ["--strategy", "basic"]
    else:
        # The grid records its per-net search failure on the negotiated path
        # only; keep that path's budget small -- the C++ search drains its
        # open set at once, it is the Python fallback that would grind.
        args += ["--strategy", "negotiated", "--iterations", "1", "--per-net-timeout", "2"]
    args += [
        "--max-layers",
        "2",
        # No kicad-cli dependency; also the case that left no sidecar pre-#7879.
        "--skip-drc",
    ]
    if vmap is not None:
        args += [
            "--voltage-map",
            str(vmap),
            "--creepage-standard",
            "iec60664",
            "--pollution-degree",
            "2",
            "--material-group",
            "IIIa",
        ]
    return args


@pytest.fixture
def board(tmp_path: Path) -> Path:
    pcb = tmp_path / "to220.kicad_pcb"
    pcb.write_text(_to220_ring_board(_ring_radius()))
    return pcb


@pytest.fixture
def vmap(tmp_path: Path) -> Path:
    path = tmp_path / "vmap.json"
    path.write_text(json.dumps({"/HV_A": HV_VOLTS, "/LV_B": 0.0}))
    return path


def test_fixture_geometry_is_what_the_test_claims() -> None:
    """Pin the geometry assumptions to the derived requirement, not literals."""
    req = _derived_requirement()
    ring = _ring_radius()
    assert req > 1.0, "a 300 V HV-to-LV pair must need more than DRU spacing"
    # The LV pad edge clears the requirement from Q1.2's centre ...
    assert ring - PAD_D / 2.0 > req
    # ... yet adjacent ring pads leave no lane: their edge gap is under 2x req.
    assert ring * math.sqrt(2.0) - PAD_D < 2.0 * req
    # Sparse neighbourhood: the ring and Q1's own pins sit outside the
    # classifier's congestion radius, so the budget-starved branch is reachable.
    assert ring > DEFAULT_CONGESTION_RADIUS_MM
    assert DEFAULT_CONGESTION_RADIUS_MM < 2.54  # Q1 pins 1 and 3


def test_why_agrees_with_lattice_pairwise_decline(
    board: Path, vmap: Path, tmp_path: Path, capsys
) -> None:
    out = tmp_path / "to220_routed.kicad_pcb"
    route_main(_route_args(board, out, vmap))
    routed = capsys.readouterr().out
    # The router declines the HV net and names the pairwise clearance.
    assert "decline[no-path-pairwise-constrained]" in routed, routed
    assert "/HV_A" in routed

    # The decline reaches the sidecar ``net-status --why`` reads.
    sidecar = tmp_path / "to220_routed.search_failures.json"
    assert sidecar.is_file(), "lattice decline never reached the sidecar"
    failures = json.loads(sidecar.read_text())["search_failures"]
    assert "pairwise" in failures["/HV_A"]

    net_status_main([str(out), "--why", "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    hv = next(n for n in data["nets"] if n["net_name"] == "/HV_A")
    assert hv["classification"] != "budget_starved", hv
    assert hv["classification"] == "escape_blocked"
    assert hv["search_failure"] == failures["/HV_A"]
    assert "pairwise (HV) class-pair clearance stopped" in hv["evidence"]

    net_status_main([str(out), "--why"])
    text = capsys.readouterr().out
    assert "[ESCAPE_BLOCKED] /HV_A" in text
    assert "[BUDGET_STARVED] /HV_A" not in text
    assert "pairwise (HV) class-pair clearance" in text


def test_without_voltage_map_nothing_declines_and_no_sidecar(
    board: Path, tmp_path: Path, capsys
) -> None:
    """No map: the same board routes, no new reason appears, no sidecar."""
    out = tmp_path / "to220_novm.kicad_pcb"
    rc = route_main(_route_args(board, out, None))
    routed = capsys.readouterr().out
    assert rc == 0, routed
    assert "pairwise-constrained" not in routed
    assert not (tmp_path / "to220_novm.search_failures.json").exists()


@pytest.mark.slow
def test_why_agrees_with_grid_decline(board: Path, vmap: Path, tmp_path: Path, capsys) -> None:
    """Grid engine: the C++ search names the pairwise clearance, like the lattice.

    Before #7879 this failed too: the sidecar was written only by the
    post-route DRC step, so a ``--skip-drc`` run left none.  Before #7887 the
    C++ grid search drained its open set on this fixture and recorded the
    decline as ``no_path``: every escape of ``Q1.2`` was refused by the
    own-pad step gate (#6375), whose validator verdict widens each ``/LV_B``
    ring pad to the HV pairwise floor, and that gate named no blocker.  It now
    attributes a refusal the pairwise floor alone decided, so the sidecar says
    ``pairwise_blocked`` and ``--why`` quotes the HV clearance.
    """
    from kicad_tools.router.cpp_backend import is_cpp_available
    from kicad_tools.router.stuck_classifier import is_pairwise_search_failure

    if not is_cpp_available():
        pytest.skip("grid search-failure codes need the C++ backend (kct build-native)")
    out = tmp_path / "to220_grid.kicad_pcb"
    route_main(_route_args(board, out, vmap, engine="grid"))
    capsys.readouterr()

    sidecar = tmp_path / "to220_grid.search_failures.json"
    assert sidecar.is_file()
    reason = json.loads(sidecar.read_text())["search_failures"]["/HV_A"]
    assert reason == "pairwise_blocked", reason
    assert is_pairwise_search_failure(reason)

    net_status_main([str(out), "--why", "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    hv = next(n for n in data["nets"] if n["net_name"] == "/HV_A")
    assert hv["classification"] == "escape_blocked", hv
    assert hv["search_failure"] == reason
    assert "pairwise (HV) class-pair clearance stopped" in hv["evidence"]

    net_status_main([str(out), "--why"])
    text = capsys.readouterr().out
    assert "[ESCAPE_BLOCKED] /HV_A" in text
    assert "[BUDGET_STARVED] /HV_A" not in text
    assert "pairwise (HV) class-pair clearance" in text
