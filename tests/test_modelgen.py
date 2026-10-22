"""Parametric 3D body generation: ``kct models generate`` (issue #7687).

Most tests need no CAD kernel: family geometry, placeholder STEP output, the
missing-extra path, provenance and the external bridge are pure Python.  The
CadQuery tests at the bottom skip cleanly when the ``[models]`` extra is not
installed (it is not, in CI).
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

import pytest

from kicad_tools.modelgen import families as fam_mod
from kicad_tools.modelgen import generate as gen_mod
from kicad_tools.modelgen.bridge import BRIDGE_LICENSE, BridgeError, run_external_generator
from kicad_tools.modelgen.families import (
    FAMILIES,
    Box,
    ParamError,
    Prism,
    build_body,
    build_placeholder,
    family_for_lib_id,
    parse_params,
)
from kicad_tools.modelgen.footprint_geom import BBox, load_footprint_from_pcb
from kicad_tools.modelgen.generate import ExtraMissingError, generate_model
from kicad_tools.modelgen.step_writer import StepBox, write_boxes_step

REPO = Path(__file__).resolve().parents[1]
BOARD03 = REPO / "boards/03-usb-joystick/output/usb_joystick_routed.kicad_pcb"
BOARD09 = REPO / "boards/09-usbc-pd-power/output/usbc_pd_power.kicad_pcb"

#: The issue's three acceptance targets: (pcb, ref, family override, params).
TARGETS = [
    (BOARD03, "J2", None, {}),
    (BOARD09, "RSH1", None, {"H": 0.9}),
    (BOARD09, "L1", "box", {"H": 5.0}),
]


def _geom(pcb: Path, ref: str):
    return load_footprint_from_pcb(pcb, reference=ref)


def _no_cadquery(monkeypatch: pytest.MonkeyPatch) -> None:
    from kicad_tools.modelgen import cadquery_backend

    monkeypatch.setattr(cadquery_backend, "available", lambda: False)


# ------------------------------------------------------------ footprint geometry


def test_fab_core_recovers_jst_housing_without_ears():
    geom = _geom(BOARD03, "J2")
    assert geom.lib_id == "Connector_JST:JST_PH_S5B-PH-SM4-TB_1x05-1MP_P2.00mm_Horizontal"
    # the fab outline has hold-down ears down to y=-3.2; the housing edge is y=-1.6
    assert geom.fab == BBox(-6.95, -3.2, 6.95, 4.4)
    assert geom.fab_core() == BBox(-6.95, -1.6, 6.95, 4.4)
    assert [p.number for p in geom.signal_pads] == ["1", "2", "3", "4", "5"]
    assert len(geom.mounting_pads) == 2
    assert geom.pad_axis() == "x"


def test_custom_footprint_read_from_board():
    geom = _geom(BOARD09, "L1")
    assert geom.lib_id == "board09:SRP7050TA"
    assert geom.courtyard is not None and geom.fab is not None
    assert geom.sha256 and len(geom.sha256) == 64


def test_project_fp_lib_table_lookup():
    from kicad_tools.modelgen.footprint_geom import find_footprint_file, load_footprint_file

    path = find_footprint_file("board09:SRP7050TA", project_dir=BOARD09.parent)
    assert path is not None and path.name == "SRP7050TA.kicad_mod"
    geom = load_footprint_file(path, lib_id="board09:SRP7050TA")
    placed = _geom(BOARD09, "L1")
    assert geom.courtyard == placed.courtyard
    assert geom.source == "board09.pretty/SRP7050TA.kicad_mod"


# --------------------------------------------------------------------- families


@pytest.mark.parametrize(
    ("lib_id", "family"),
    [
        ("Resistor_SMD:R_0603_1608Metric", "chip"),
        ("Capacitor_SMD:C_0402_1005Metric", "chip"),
        ("Inductor_SMD:L_0805_2012Metric", "chip"),
        ("Resistor_SMD:R_Shunt_Vishay_WSK2512_6332Metric_T1.19mm", "box"),
        ("Inductor_SMD:L_Bourns_SRP7028A_7.3x6.6mm", "box"),
        ("Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", "gullwing"),
        ("Package_SO:TSSOP-20_4.4x6.5mm_P0.65mm", "gullwing"),
        ("Package_SO:MSOP-10_3x3mm_P0.5mm", "gullwing"),
        ("Package_TO_SOT_SMD:SOT-23-6", "gullwing"),
        ("Package_DFN_QFN:QFN-16-1EP_3x3mm_P0.5mm_EP1.7x1.7mm", "nolead"),
        ("Connector_JST:JST_PH_S5B-PH-SM4-TB_1x05-1MP_P2.00mm_Horizontal", "header"),
        ("Connector_JST:JST_XH_B4B-XH-A_1x04_P2.50mm_Vertical", "header"),
        ("Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical", "header"),
        ("Connector_PinHeader_2.00mm:PinHeader_2x05_P2.00mm_Vertical", "header"),
        ("board09:SRP7050TA", None),
        ("Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12", None),
    ],
)
def test_family_inference(lib_id, family):
    match = family_for_lib_id(lib_id)
    assert (match.name if match else None) == family


def test_parse_params():
    assert parse_params(["H=1.2", "terminals=ends,shrouded=false", "pins=8"]) == {
        "H": 1.2,
        "terminals": "ends",
        "shrouded": False,
        "pins": 8,
    }
    with pytest.raises(ParamError):
        parse_params(["H"])


def _solid_extent(spec) -> BBox:
    ext = spec.extent()
    assert ext is not None
    return ext


@pytest.mark.parametrize(("pcb", "ref", "family", "params"), TARGETS)
def test_target_bodies_sit_inside_courtyard_along_pad_field(pcb, ref, family, params):
    geom = _geom(pcb, ref)
    fam = FAMILIES[family] if family else family_for_lib_id(geom.lib_id)
    assert fam is not None
    spec = build_body(fam, geom, params)
    ext = _solid_extent(spec)
    assert geom.courtyard.contains(ext, tol=1e-6), (ext, geom.courtyard)
    assert spec.params["_inside_courtyard"] is True
    # long axis of the body is parallel to the pad field
    assert geom.pad_axis() == "x"
    assert ext.width > ext.height
    assert spec.height() == pytest.approx(params.get("H", spec.params.get("H")))


def test_box_family_requires_height_when_not_in_the_name():
    geom = _geom(BOARD09, "L1")
    with pytest.raises(ParamError, match="H="):
        build_body(FAMILIES["box"], geom, {})


def test_shunt_terminal_length_comes_from_the_footprint_name():
    spec = build_body(FAMILIES["box"], _geom(BOARD09, "RSH1"), {"H": 0.9})
    assert spec.params["T"] == pytest.approx(1.19)
    assert spec.params["terminals"] == "ends"
    tins = [p for p in spec.parts if isinstance(p, Box) and p.material == "tin"]
    assert len(tins) == 2


def test_gullwing_leads_follow_the_pads():
    geom = _geom(BOARD03, "U2")  # SOT-23-6
    spec = build_body(FAMILIES["gullwing"], geom, {})
    leads = [p for p in spec.parts if isinstance(p, Prism)]
    assert len(leads) == 6
    for lead, pad in zip(leads, geom.signal_pads, strict=True):
        assert lead.axis == "x"
        assert lead.across == pytest.approx(pad.y)
        tip = max(abs(u) for u, _ in lead.points)
        assert tip <= abs(pad.x) + pad.w / 2
    assert geom.courtyard.contains(_solid_extent(spec))


@pytest.mark.parametrize(
    ("family", "params"),
    [
        ("chip", {"L": 3.2, "W": 1.6, "H": 0.55}),
        ("box", {"L": 7.0, "W": 7.0, "H": 4.5}),
        ("gullwing", {"pins": 8, "pitch": 1.27, "L": 4.9, "W": 3.9, "E": 6.0}),
        ("nolead", {"pins": 8, "pitch": 0.5, "L": 2.0, "W": 2.0}),
        ("header", {"positions": 4, "pitch": 2.54}),
        ("header", {"positions": 4, "pitch": 2.5, "shrouded": True}),
    ],
)
def test_families_synthesize_without_a_footprint(family, params):
    spec = build_body(FAMILIES[family], None, params)
    assert spec.parts
    assert spec.height() > 0


def test_synthesized_family_reports_missing_parameters():
    with pytest.raises(ParamError, match="pins"):
        build_body(FAMILIES["gullwing"], None, {"pitch": 1.27})


# ------------------------------------------------------------------ placeholder


def test_placeholder_is_courtyard_box():
    geom = _geom(BOARD09, "L1")
    spec = build_placeholder(geom, {})
    assert spec.placeholder
    (box,) = spec.parts
    assert geom.courtyard.contains(BBox(box.x0, box.y0, box.x1, box.y1))
    assert "placeholder" in spec.name


def test_step_writer_output_is_iso_10303_21(tmp_path):
    path = tmp_path / "b.step"
    text = write_boxes_step(
        str(path), [StepBox(-1, -0.5, 1, 0.5, 0, 0.6, (0.5, 0.5, 0.5))], product="b"
    )
    assert text.startswith("ISO-10303-21;\nHEADER;")
    assert text.rstrip().endswith("END-ISO-10303-21;")
    assert text.count("ADVANCED_FACE(") == 6
    assert text.count("EDGE_CURVE(") == 12
    assert text.count("VERTEX_POINT(") == 8
    assert "MANIFOLD_SOLID_BREP(" in text and "COLOUR_RGB(" in text
    # every entity reference points at a defined entity
    defined = {int(n) for n in re.findall(r"^#(\d+)=", text, re.MULTILINE)}
    used = {int(n) for n in re.findall(r"#(\d+)", text)}
    assert used <= defined
    # y is flipped into the model frame
    assert "(1.,-0.5,0.)" in text or "(-1.,-0.5,0.)" in text


def test_placeholder_mode_needs_no_extra_and_is_audited_as_placeholder(tmp_path, monkeypatch):
    _no_cadquery(monkeypatch)
    pcb = tmp_path / BOARD09.name
    shutil.copy(BOARD09, pcb)
    result = generate_model(
        "board09:SRP7050TA", pcb=pcb, reference="L1", placeholder=True, attach=True
    )
    assert result.path == tmp_path / "3dshapes" / "SRP7050TA_placeholder.step"
    assert result.path.read_text().startswith("ISO-10303-21;")
    assert result.attached == ["board09:SRP7050TA"]
    manifest = json.loads((tmp_path / "3dshapes" / "local_models.json").read_text())
    assert manifest == {"board09:SRP7050TA": "SRP7050TA_placeholder.step"}

    from kicad_tools.pcb.model_audit import audit_pcb_models

    report = audit_pcb_models(pcb, env="host")
    entry = next(e for e in report.entries if e.reference == "L1")
    assert entry.klass == "placeholder"
    assert not entry.covered


def test_placeholder_output_is_reproducible(tmp_path, monkeypatch):
    _no_cadquery(monkeypatch)
    a = generate_model(
        "Resistor_SMD:R_0603_1608Metric",
        pcb=BOARD03,
        reference="R1",
        placeholder=True,
        out_dir=tmp_path / "a",
    )
    b = generate_model(
        "Resistor_SMD:R_0603_1608Metric",
        pcb=BOARD03,
        reference="R1",
        placeholder=True,
        out_dir=tmp_path / "b",
    )
    assert a.path.read_bytes() == b.path.read_bytes()


def test_provenance_records_generator_version_and_params(tmp_path, monkeypatch):
    _no_cadquery(monkeypatch)
    result = generate_model(
        "board09:SRP7050TA",
        pcb=BOARD09,
        reference="L1",
        placeholder=True,
        params={"H": 4.0},
        out_dir=tmp_path,
    )
    prov = json.loads(result.provenance_path.read_text())
    assert prov["generator"] == "kicad-tools modelgen"
    assert prov["generator_version"]
    assert prov["family"] == "placeholder" and prov["placeholder"] is True
    assert prov["params"]["H"] == 4.0
    assert prov["footprint"]["lib_id"] == "board09:SRP7050TA"
    assert len(prov["footprint"]["sha256"]) == 64
    assert prov["license"] == gen_mod.LICENSE_OURS
    assert prov["step"]["file"] == result.path.name
    header = result.path.read_text().split("DATA;", 1)[0]
    assert "kicad-tools modelgen" in header and '"H":4.0' in header


# ------------------------------------------------------------ missing extra / CLI


def test_family_generation_without_extra_raises_install_hint(tmp_path, monkeypatch):
    _no_cadquery(monkeypatch)
    with pytest.raises(ExtraMissingError, match=r"kicad-tools\[models\]"):
        generate_model("chip", params={"L": 1.6, "W": 0.8}, out_dir=tmp_path)
    assert not any(tmp_path.iterdir())


def test_cli_without_extra_prints_hint_and_exits_nonzero(tmp_path, monkeypatch, capsys):
    _no_cadquery(monkeypatch)
    from kicad_tools.cli import main

    rc = main(
        [
            "models",
            "generate",
            "Connector_JST:JST_PH_S5B-PH-SM4-TB_1x05-1MP_P2.00mm_Horizontal",
            "--pcb",
            str(BOARD03),
            "--ref",
            "J2",
            "-o",
            str(tmp_path),
        ]
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert "pip install 'kicad-tools[models]'" in err
    assert "--placeholder" in err
    assert not any(tmp_path.iterdir())


def test_cli_placeholder_and_families(tmp_path, monkeypatch, capsys):
    _no_cadquery(monkeypatch)
    from kicad_tools.cli import main

    rc = main(
        [
            "models",
            "generate",
            "Resistor_SMD:R_Shunt_Vishay_WSK2512_6332Metric_T1.19mm",
            "--pcb",
            str(BOARD09),
            "--placeholder",
            "--param",
            "H=0.9",
            "-o",
            str(tmp_path),
            "--format",
            "json",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["placeholder"] is True and out["params"]["H"] == 0.9
    assert (
        Path(out["step_path"]).name == "R_Shunt_Vishay_WSK2512_6332Metric_T1.19mm_placeholder.step"
    )

    assert main(["models", "families", "--format", "json"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["cadquery"] is False
    assert {f["family"] for f in listing["families"]} == set(FAMILIES)


def test_unknown_custom_footprint_needs_a_family(tmp_path, monkeypatch):
    from kicad_tools.modelgen import cadquery_backend

    monkeypatch.setattr(cadquery_backend, "available", lambda: True)
    with pytest.raises(ParamError, match="--family"):
        generate_model("board09:SRP7050TA", pcb=BOARD09, reference="L1", out_dir=tmp_path)


def test_models_extra_is_optional_and_not_in_all():
    tomllib = pytest.importorskip("tomllib")  # stdlib on 3.11+
    data = tomllib.loads((REPO / "pyproject.toml").read_text())
    project = data["project"]
    assert not any("cadquery" in d.lower() for d in project["dependencies"])
    extras = project["optional-dependencies"]
    assert any(d.startswith("cadquery") for d in extras["models"])
    assert not any("cadquery" in d for d in extras["all"])


# ------------------------------------------------------------------------ bridge


def test_bridge_unconfigured(monkeypatch, tmp_path):
    monkeypatch.delenv("KCT_KICAD_LIBRARY_TOOLS_CMD", raising=False)
    with pytest.raises(BridgeError, match="not configured"):
        run_external_generator("chip", {}, tmp_path)


def test_bridge_runs_external_tool_and_records_provenance(tmp_path, monkeypatch):
    tool = tmp_path / "fake_generator.py"
    tool.write_text(
        "import json, pathlib, sys\n"
        "out = pathlib.Path(sys.argv[2]); params = json.loads(pathlib.Path(sys.argv[3]).read_text())\n"
        "(out / f\"{sys.argv[1]}.step\").write_text('ISO-10303-21;\\nEND-ISO-10303-21;\\n')\n"
    )
    monkeypatch.setenv("KCT_KICAD_LIBRARY_TOOLS_VERSION", "9.9.9")
    command = f"{sys.executable} {tool} {{target}} {{out_dir}} {{params_json}}"
    result = run_external_generator("C_0805", {"H": 1.0}, tmp_path / "out", command=command)
    assert [p.name for p in result.files] == ["C_0805.step"]
    prov = json.loads(result.provenance_paths[0].read_text())
    assert prov["generator_version"] == "9.9.9"
    assert prov["license"] == BRIDGE_LICENSE
    assert "GPL-3.0-or-later" in prov["license"] and "attribution" in prov["license"]
    assert "<params_json>" in prov["command"] and "<out_dir>" in prov["command"]


def test_bridge_failure_is_reported(tmp_path):
    command = f"{sys.executable} -c 'import sys; sys.exit(3)'"
    with pytest.raises(BridgeError, match="exited 3"):
        run_external_generator("x", {}, tmp_path, command=command)


# --------------------------------------------------------------- CadQuery backend


def _step_bbox(path: Path):
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.STEPControl import STEPControl_Reader

    reader = STEPControl_Reader()
    assert reader.ReadFile(str(path)) == IFSelect_RetDone
    reader.TransferRoots()
    shape = reader.OneShape()
    assert BRepCheck_Analyzer(shape).IsValid()
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    return box.Get()


@pytest.mark.parametrize(("pcb", "ref", "family", "params"), TARGETS)
def test_cadquery_generates_valid_step_inside_courtyard(tmp_path, pcb, ref, family, params):
    pytest.importorskip("cadquery")
    geom = _geom(pcb, ref)
    result = generate_model(
        geom.lib_id, pcb=pcb, reference=ref, family=family, params=params, out_dir=tmp_path
    )
    assert result.path.read_text().startswith("ISO-10303-21;")
    assert result.provenance["backend"].startswith("cadquery")
    assert result.provenance["inside_courtyard"] is True
    x0, y0, z0, x1, y1, z1 = _step_bbox(result.path)
    crt = geom.courtyard
    # model frame is +y up: footprint y range maps to (-y1, -y0)
    assert x0 >= crt.x0 - 0.01 and x1 <= crt.x1 + 0.01
    assert -y1 >= crt.y0 - 0.01 and -y0 <= crt.y1 + 0.01
    assert z0 >= -0.01 and z1 > 0
    assert (x1 - x0) > (y1 - y0)  # long axis along the pad field (x)


@pytest.mark.parametrize(
    "lib_ref",
    [(BOARD03, "U2"), (BOARD03, "R1"), (BOARD03, "C1")],
)
def test_cadquery_other_families(tmp_path, lib_ref):
    pytest.importorskip("cadquery")
    pcb, ref = lib_ref
    geom = _geom(pcb, ref)
    result = generate_model(geom.lib_id, pcb=pcb, reference=ref, out_dir=tmp_path)
    _step_bbox(result.path)


def test_cadquery_output_is_stable(tmp_path):
    """Same inputs -> same header, geometry and colours.

    OCCT orders the colour ``STYLED_ITEM`` entities by in-memory handles, so
    the bytes can differ between exports in one process; the content cannot.
    """
    pytest.importorskip("cadquery")
    runs = [
        generate_model("chip", params={"L": 1.6, "W": 0.8, "H": 0.45}, out_dir=tmp_path / d)
        for d in ("a", "b")
    ]
    texts = [r.path.read_text() for r in runs]
    assert texts[0].split("DATA;")[0] == texts[1].split("DATA;")[0]
    colours = [sorted(re.findall(r"COLOUR_RGB\('',([^;]*)\)", t)) for t in texts]
    assert colours[0] == colours[1] and len(colours[0]) == 2
    assert _step_bbox(runs[0].path) == pytest.approx(_step_bbox(runs[1].path))
    assert "NEXT_ASSEMBLY_USAGE_OCCURRENCE('1'" in texts[1]


def test_family_module_has_no_cad_import():
    src = Path(fam_mod.__file__).read_text()
    assert "import cadquery" not in src
