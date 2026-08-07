"""CLI-level tests for keepout rule-area honoring on the lattice engine (#4605).

The softstart-shaped acceptance fixture: a two-domain board whose HV banks are
segregated by COMPLEMENTARY track-keepout rule areas (corridors expressed as
keepouts -- the ``mark_region_bound`` complement construction), filtered per
net class through the ``spatial_keepouts`` block of the ``--net-class-map``
sidecar.  Assertions:

1. the board routes 100% on ``--route-engine lattice``, every HV segment stays
   inside its own declared region, and the #4588 post-route pairwise gate
   passes (exit 0) -- spatial segregation by construction;
2. a ``copperpour not_allowed``-only rule area (the ``kct zones hv-keepout``
   output) leaves the committed copper byte-identical to the same board
   without it -- pour voids never constrain routing;
3. a ``spatial_keepouts`` entry naming an unknown net class fails loud
   (exit 1) before any routing work;
4. a structurally malformed ``spatial_keepouts`` block fails at preload;
5. a non-lattice engine warns (once, stderr) when the board declares
   track/via-blocking rule areas it will not honor.

Boards are fully synthetic S-expression strings (the
``test_route_pairwise_gate_4588.py`` approach) and deliberately NOT at sheet
origin, so the board-relative -> sheet-absolute polygon shift (#4603 lesson)
is exercised, not just the happy path.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from kicad_tools.cli.route_cmd import main as route_main

HV_VOLTS = 300.0

# Corridor bands (sheet coordinates).  Board rect: (100, 100) .. (130, 116).
# Bank A routes in y < 105.8, bank B in y > 110.2 -- a 4.4 mm declared gap,
# beyond the 3.2 mm requirement 300 V derives under IEC 60664-1 / PD2 / IIIa.
A_LIMIT = 105.8
B_LIMIT = 110.2


def _pad(number: str, x: float, y: float, net: int, net_name: str) -> str:
    return (
        f'(pad "{number}" smd rect (at {x} {y}) (size 0.4 0.4) '
        f'(layers "F.Cu" "F.Paste" "F.Mask") (net {net} "{net_name}"))'
    )


def _fp(ref: str, uid: int, x: float, y: float, pads: str) -> str:
    return f"""  (footprint "Resistor_SMD:R_0603_1608Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000{uid:02d}")
    (at {x} {y})
    (property "Reference" "{ref}" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    {pads}
  )
"""


def _keepout_zone(
    name: str,
    poly: list[tuple[float, float]],
    *,
    tracks: str = "not_allowed",
    vias: str = "not_allowed",
    copperpour: str = "allowed",
    uid: str = "cccccccc-0000-0000-0000-000000000001",
) -> str:
    pts = " ".join(f"(xy {x} {y})" for x, y in poly)
    return f"""  (zone
    (net 0)
    (net_name "")
    (name "{name}")
    (layers "F.Cu" "B.Cu")
    (uuid "{uid}")
    (hatch edge 0.5)
    (keepout (tracks {tracks}) (vias {vias}) (pads allowed) (copperpour {copperpour}))
    (polygon (pts {pts}))
  )
"""


def _board(extra: str = "") -> str:
    parts = [
        _fp("R1", 10, 103.0, 103.0, _pad("1", 0, 0, 1, "/HV_A")),
        _fp("R2", 11, 127.0, 103.0, _pad("1", 0, 0, 1, "/HV_A")),
        _fp("R3", 12, 103.0, 113.0, _pad("1", 0, 0, 2, "/HV_B")),
        _fp("R4", 13, 127.0, 113.0, _pad("1", 0, 0, 2, "/HV_B")),
    ]
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
  (net 2 "/HV_B")
  (gr_rect (start 100 100) (end 130 116)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
{"".join(parts)}{extra})
"""


def _two_domain_guards() -> str:
    """Complementary corridor guards: bank A excluded from everything north of
    ``A_LIMIT``, bank B from everything south of ``B_LIMIT``."""
    guard_a = _keepout_zone(
        "hv-a-corridor-guard",
        [(100, A_LIMIT), (130, A_LIMIT), (130, 116), (100, 116)],
        uid="cccccccc-0000-0000-0000-000000000001",
    )
    guard_b = _keepout_zone(
        "hv-b-corridor-guard",
        [(100, 100), (130, 100), (130, B_LIMIT), (100, B_LIMIT)],
        uid="cccccccc-0000-0000-0000-000000000002",
    )
    return guard_a + guard_b


def _write_sidecar(tmp_path: Path, spatial: dict | None) -> Path:
    payload: dict = {
        "/HV_A": {"name": "HV_A"},
        "/HV_B": {"name": "HV_B"},
    }
    if spatial is not None:
        payload["spatial_keepouts"] = spatial
    path = tmp_path / "net_class_map.json"
    path.write_text(json.dumps(payload))
    return path


def _write_voltage_map(tmp_path: Path) -> Path:
    vmap = tmp_path / "vmap.json"
    vmap.write_text(json.dumps({"/HV_A": HV_VOLTS, "/HV_B": 0.0}))
    return vmap


def _route_args(
    pcb: Path,
    out: Path,
    *,
    engine: str = "lattice",
    sidecar: Path | None = None,
    vmap: Path | None = None,
) -> list[str]:
    args = [
        str(pcb),
        "-o",
        str(out),
        "--route-engine",
        engine,
        "--strategy",
        "basic",
        "--skip-drc",
    ]
    if sidecar is not None:
        args += ["--net-class-map", str(sidecar)]
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


def _segments_by_net(text: str) -> dict[int, list[tuple[float, float, float, float]]]:
    out: dict[int, list[tuple[float, float, float, float]]] = {}
    pattern = re.compile(
        r"\(segment\s*\(start ([-\d.]+) ([-\d.]+)\)\s*\(end ([-\d.]+) ([-\d.]+)\)"
        r".*?\(net (\d+)",
        re.DOTALL,
    )
    for m in pattern.finditer(text):
        x1, y1, x2, y2 = (float(m.group(i)) for i in range(1, 5))
        out.setdefault(int(m.group(5)), []).append((x1, y1, x2, y2))
    return out


def _copper_signature(text: str) -> list[str]:
    """Routed copper (segments + vias) with volatile uuids stripped."""
    body = re.sub(r'\(uuid "[^"]*"\)', "(uuid)", text)
    items = re.findall(r"\(segment .*?\)\n", body, re.DOTALL)
    items += re.findall(r"\(via .*?\)\n", body, re.DOTALL)
    return sorted("".join(i.split()) for i in items)


def test_two_domain_keepout_segregation_routes_100pct_and_passes_gate(
    tmp_path: Path, capsys
) -> None:
    pcb = tmp_path / "two_domain.kicad_pcb"
    pcb.write_text(_board(_two_domain_guards()))
    out = tmp_path / "routed.kicad_pcb"
    sidecar = _write_sidecar(
        tmp_path,
        {
            "hv-a-corridor-guard": {"only_classes": ["HV_A"]},
            "hv-b-corridor-guard": {"only_classes": ["HV_B"]},
        },
    )
    vmap = _write_voltage_map(tmp_path)

    rc = route_main(_route_args(pcb, out, sidecar=sidecar, vmap=vmap))
    stdout = capsys.readouterr().out
    assert rc == 0, f"expected clean run, got rc={rc}\n{stdout}"
    assert "SUCCESS" in stdout

    by_net = _segments_by_net(out.read_text())
    assert by_net.get(1), "bank A did not route"
    assert by_net.get(2), "bank B did not route"
    # Every HV segment stays inside its own region: copper CENTRELINE strictly
    # south (A) / north (B) of the declared corridor limit, with the 0.1 mm
    # half-width margin.
    for x1, y1, x2, y2 in by_net[1]:
        assert max(y1, y2) <= A_LIMIT - 0.05, (
            f"bank A segment crosses its guard: {(x1, y1, x2, y2)}"
        )
    for x1, y1, x2, y2 in by_net[2]:
        assert min(y1, y2) >= B_LIMIT + 0.05, (
            f"bank B segment crosses its guard: {(x1, y1, x2, y2)}"
        )


def test_pour_void_only_rule_area_leaves_copper_byte_identical(tmp_path: Path, capsys) -> None:
    plain = tmp_path / "plain.kicad_pcb"
    plain.write_text(_board())
    voided = tmp_path / "voided.kicad_pcb"
    voided.write_text(
        _board(
            _keepout_zone(
                "hv-pour-void",
                [(110, 106), (120, 106), (120, 110), (110, 110)],
                tracks="allowed",
                vias="allowed",
                copperpour="not_allowed",
            )
        )
    )

    out_plain = tmp_path / "plain_routed.kicad_pcb"
    out_voided = tmp_path / "voided_routed.kicad_pcb"
    assert route_main(_route_args(plain, out_plain)) == 0
    assert route_main(_route_args(voided, out_voided)) == 0
    capsys.readouterr()

    assert _copper_signature(out_plain.read_text()) == _copper_signature(out_voided.read_text())


def test_unknown_class_in_spatial_keepouts_fails_loud(tmp_path: Path, capsys) -> None:
    pcb = tmp_path / "two_domain.kicad_pcb"
    pcb.write_text(_board(_two_domain_guards()))
    out = tmp_path / "routed.kicad_pcb"
    sidecar = _write_sidecar(tmp_path, {"hv-a-corridor-guard": {"only_classes": ["NOPE"]}})

    with pytest.raises(SystemExit) as excinfo:
        route_main(_route_args(pcb, out, sidecar=sidecar))
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "unknown net class 'NOPE'" in err
    assert "HV_A" in err  # the known-classes hint


def test_unknown_zone_name_in_spatial_keepouts_warns_but_routes(tmp_path: Path, capsys) -> None:
    """#4672: a ``spatial_keepouts`` key matching no rule-area zone name is a
    likely typo -- warn on stderr (naming the entry and the known rule areas)
    but keep the conservative exit-0 semantics."""
    pcb = tmp_path / "two_domain.kicad_pcb"
    pcb.write_text(_board(_two_domain_guards()))
    out = tmp_path / "routed.kicad_pcb"
    sidecar = _write_sidecar(
        tmp_path,
        {
            "hv-a-corridor-guard": {"only_classes": ["HV_A"]},
            "hv-b-corridor-guard": {"only_classes": ["HV_B"]},
            "hv-a-coridor-gaurd": {"only_classes": ["HV_A"]},  # typo'd zone name
        },
    )

    rc = route_main(_route_args(pcb, out, sidecar=sidecar))
    captured = capsys.readouterr()
    assert rc == 0, captured.out
    assert "spatial_keepouts entry 'hv-a-coridor-gaurd'" in captured.err
    assert "matches no rule-area zone name" in captured.err
    # The known-zone-names hint.
    assert "hv-a-corridor-guard" in captured.err
    assert "hv-b-corridor-guard" in captured.err
    # The correctly spelled entry does NOT warn.
    assert "entry 'hv-a-corridor-guard'" not in captured.err


def test_spatial_keepouts_key_matching_pour_void_only_area_warns(tmp_path: Path, capsys) -> None:
    """#4672: a key matching a real zone that was filtered out of the routing
    mask (a ``copperpour not_allowed``-only area, the ``kct zones hv-keepout``
    output) gets a distinct message -- the zone exists but never constrains
    routing, the likely confusion source."""
    pcb = tmp_path / "voided.kicad_pcb"
    pcb.write_text(
        _board(
            _keepout_zone(
                "hv-pour-void",
                [(110, 106), (120, 106), (120, 110), (110, 110)],
                tracks="allowed",
                vias="allowed",
                copperpour="not_allowed",
            )
        )
    )
    out = tmp_path / "routed.kicad_pcb"
    sidecar = _write_sidecar(tmp_path, {"hv-pour-void": {"only_classes": ["HV_A"]}})

    rc = route_main(_route_args(pcb, out, sidecar=sidecar))
    captured = capsys.readouterr()
    assert rc == 0, captured.out
    assert "spatial_keepouts entry 'hv-pour-void'" in captured.err
    assert "does not block tracks or vias" in captured.err


def test_well_formed_spatial_keepouts_do_not_warn(tmp_path: Path, capsys) -> None:
    pcb = tmp_path / "two_domain.kicad_pcb"
    pcb.write_text(_board(_two_domain_guards()))
    out = tmp_path / "routed.kicad_pcb"
    sidecar = _write_sidecar(
        tmp_path,
        {
            "hv-a-corridor-guard": {"only_classes": ["HV_A"]},
            "hv-b-corridor-guard": {"only_classes": ["HV_B"]},
        },
    )

    rc = route_main(_route_args(pcb, out, sidecar=sidecar))
    captured = capsys.readouterr()
    assert rc == 0, captured.out
    assert "spatial_keepouts entry" not in captured.err


def test_malformed_spatial_keepouts_block_fails_at_preload(tmp_path: Path, capsys) -> None:
    pcb = tmp_path / "two_domain.kicad_pcb"
    pcb.write_text(_board())
    out = tmp_path / "routed.kicad_pcb"
    sidecar = _write_sidecar(
        tmp_path,
        {"guard": {"only_classes": ["HV_A"], "except_classes": ["HV_B"]}},
    )

    rc = route_main(_route_args(pcb, out, sidecar=sidecar))
    err = capsys.readouterr().err
    assert rc == 1
    assert "spatial_keepouts" in err
    assert "both except_classes and only_classes" in err


def test_non_lattice_engine_warns_about_unhonored_rule_areas(tmp_path: Path, capsys) -> None:
    pcb = tmp_path / "two_domain.kicad_pcb"
    pcb.write_text(_board(_two_domain_guards()))
    out = tmp_path / "routed.kicad_pcb"

    rc = route_main(_route_args(pcb, out, engine="grid"))
    captured = capsys.readouterr()
    assert rc == 0, captured.out
    assert "does not honor" in captured.err
    assert "lattice" in captured.err


def test_lattice_engine_does_not_warn(tmp_path: Path, capsys) -> None:
    pcb = tmp_path / "plain.kicad_pcb"
    pcb.write_text(_board())
    out = tmp_path / "routed.kicad_pcb"

    rc = route_main(_route_args(pcb, out))
    captured = capsys.readouterr()
    assert rc == 0
    assert "does not honor" not in captured.err
