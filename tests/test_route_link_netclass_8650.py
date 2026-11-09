"""``kct route-link`` honours ``.kicad_pro`` netclass clearances (Issue #8650)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("shapely")

from kicad_tools.cli.route_link_cmd import (  # noqa: E402
    LinkSpecError,
    _rules_for_link,
    _rules_from_args,
    _stage_project_files,
)
from kicad_tools.schema.pcb import PCB  # noqa: E402
from tests.test_route_link_hv_8546 import _pad, _parse, _seg, _write  # noqa: E402

BODY = _pad(1, 105, 110, 1, "SIG") + _pad(2, 125, 110, 1, "SIG") + _seg(1, 108, 111, 122, 111, 2)


def _project(path: Path, clearance: float = 0.4) -> None:
    path.with_suffix(".kicad_pro").write_text(
        json.dumps(
            {
                "meta": {"version": 3},
                "net_settings": {
                    "meta": {"version": 3},
                    "classes": [
                        {"name": "Default", "clearance": 0.2},
                        {"name": "HVC", "clearance": clearance},
                    ],
                    "netclass_assignments": {"HV": "HVC"},
                },
            }
        )
    )


def _floor(tmp_path, *extra, clearance=0.4):
    p = _write(tmp_path, BODY)
    _project(p, clearance)
    pcb = PCB.load(str(p))
    a = _parse(str(p), "--link", "SIG R1.1 R2.1", *extra)
    return _rules_for_link(a, pcb, _rules_from_args(a, pcb), a.links[0])


def test_authored_neighbour_floor_applied_by_default(tmp_path):
    assert _floor(tmp_path).clearance_for(2) == pytest.approx(0.4)


def test_explicit_net_clearance_cannot_lower_authored(tmp_path):
    assert _floor(tmp_path, "--net-clearance", "HV=0.25").clearance_for(2) == pytest.approx(0.4)
    assert _floor(tmp_path, "--net-clearance", "HV=1").clearance_for(2) == pytest.approx(1.0)


def test_missing_project_adds_no_floor(tmp_path):
    p = _write(tmp_path, BODY)
    pcb = PCB.load(str(p))
    a = _parse(str(p), "--link", "SIG R1.1 R2.1")
    base = _rules_from_args(a, pcb)
    assert _rules_for_link(a, pcb, base, a.links[0]) is base


def test_malformed_project_is_a_refusal(tmp_path):
    p = _write(tmp_path, BODY)
    p.with_suffix(".kicad_pro").write_text("{not json")
    pcb = PCB.load(str(p))
    a = _parse(str(p), "--link", "SIG R1.1 R2.1")
    with pytest.raises(LinkSpecError):
        _rules_for_link(a, pcb, _rules_from_args(a, pcb), a.links[0])


def _probe_len(tmp_path, capsys, clearance):
    from kicad_tools.cli import main

    p = _write(tmp_path, BODY)
    if clearance:
        _project(p, clearance)
    main(["route-link", "--format", "json", "--probe", str(p), "--link", "SIG R1.1 R2.1",
          "--link-layers", "F.Cu"])  # fmt: skip
    link = json.loads(capsys.readouterr().out)["links"][0]
    return link["length_mm"] if link["routable"] else float("inf")


def test_probe_detours_for_authored_floor(tmp_path, capsys):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    free = _probe_len(tmp_path / "a", capsys, 0)
    bent = _probe_len(tmp_path / "b", capsys, 2.0)
    assert bent > free


def test_malformed_project_exits_nonzero(tmp_path, capsys):
    from kicad_tools.cli import main

    p = _write(tmp_path, BODY)
    p.with_suffix(".kicad_pro").write_text("{not json")
    rc = main(["route-link", "--format", "json", "--probe", str(p), "--link", "SIG R1.1 R2.1"])
    assert rc == 1


def test_gate_stages_project_files_next_to_output(tmp_path):
    src = tmp_path / "in" / "b.kicad_pcb"
    out = tmp_path / "out" / "o.kicad_pcb"
    src.parent.mkdir()
    out.parent.mkdir()
    src.write_text("x")
    src.with_suffix(".kicad_pro").write_text("{}")
    src.with_suffix(".kicad_dru").write_text("(version 1)")
    staged = _stage_project_files(src, out)
    assert {f.suffix for f in staged} == {".kicad_pro", ".kicad_dru"}
    assert all(f.stem == "o" and f.is_file() for f in staged)
    assert _stage_project_files(src, src) == []
