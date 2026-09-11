"""Independent native-export comparisons and fail-closed Gerber controls."""

import uuid
from pathlib import Path

import pytest
from _mask_gerbonara_oracle import read_native_gerber
from shapely.geometry import Point

from kicad_tools.validate.gerber_geometry import GerberGeometryError, parse_gerber_geometry
from kicad_tools.validate.mask_export_geometry import (
    MaskExportOptions,
    inspect_exported_mask_geometry,
)

HEADER = "%FSLAX46Y46*%\n%MOMM*%\n%LPD*%\n"


@pytest.mark.parametrize(
    "command",
    ["%AMBad*1,1,1,0,0*%", "%SRX2Y2I1J1*%", "%LMX*%", "%LR30*%", "G74*", "G99*", "%MIA1B0*%"],
)
def test_unknown_gerber_rejects_whole_layer(command):
    with pytest.raises(GerberGeometryError):
        parse_gerber_geometry(HEADER + "%ADD10C,1*%\nD10*X0Y0D03*\n" + command + "\nM02*")


@pytest.mark.parametrize("suffix", ["", "G36*X0Y0D02*", "M02*D10*"])
def test_incomplete_or_trailing_gerber_is_rejected(suffix):
    with pytest.raises(GerberGeometryError):
        parse_gerber_geometry(HEADER + suffix)


def test_polarity_and_aperture_hole_have_distinct_meanings():
    source = HEADER + "%ADD10C,1*%%ADD11C,3X2*%D10*X0Y0D03*D11*X0Y0D03*M02*"
    image = parse_gerber_geometry(source).geometry
    assert image.contains(Point(0, 0))  # Transparent hole cannot erase prior copper.
    cleared = parse_gerber_geometry(source.replace("M02*", "%LPC*%D10*X0Y0D03*M02*")).geometry
    assert not cleared.intersects(Point(0, 0))


def _board(path: Path, body: str, *, setup="(pad_to_mask_clearance 0)"):
    # Every plotted object has an independent source identity. Footprint and
    # primitive nesting is native source syntax, not resolver-authored expected geometry.
    import re

    index = 0

    def identify(match):
        nonlocal index
        index += 1
        return match[0] + f' (uuid "{uuid.UUID(int=index)}")'

    body = re.sub(
        r'\(fp_text (?:user|value|reference) "[^"]+"|\(pad "[^"]+" \w+ \w+|\(footprint "[^"]+"|\(gr_(?:line|rect|circle|text) "[^"]+"|\(gr_(?:line|rect|circle)|\(fp_(?:line|rect|circle)|\(zone',
        identify,
        body,
    )
    path.write_text(f"""(kicad_pcb (version 20241229) (generator pcbnew)
      (general (thickness 1.6)) (paper "A4")
      (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (38 "B.Mask" user) (39 "F.Mask" user))
      (setup {setup}) {body})""")
    from kicad_tools.sexp import parse_string

    tree = parse_string(path.read_text())

    def reorder(node):
        if node.name.startswith(("gr_", "fp_")):
            identity = node.find_child("uuid")
            if identity:
                node.children.remove(identity)
                node.children.append(identity)
        for child in node.children:
            if not child.is_atom:
                reorder(child)

    reorder(tree)
    path.write_text(tree.to_string())
    return path


NATIVE_CASES = {
    "custom_rotated": """(footprint "Custom" (layer "F.Cu") (at 10 10 37)
      (pad "1" smd custom (at 2 0 37) (size .5 .5) (layers "F.Cu" "F.Mask")
      (options (clearance outline) (anchor rect))
      (primitives (gr_poly (pts (xy -.5 -.5) (xy 1.5 -.5) (xy .5 1)) (width 0) (fill yes)))))""",
    "chamfer": """(footprint "Chamfer" (layer "F.Cu") (at 10 10 29)
      (pad "1" smd roundrect (at 0 0 29) (size 2 3) (layers "F.Cu" "F.Mask")
      (roundrect_rratio .1) (chamfer_ratio .3) (chamfer top_left bottom_right)))""",
    "graphics_text": """(gr_text "MASK" (at 10 10 33) (layer "F.Mask")
        (effects (font (size 1 1) (thickness .15))))
      (gr_circle (center 15 15) (end 16 15) (stroke (width .2) (type solid)) (fill none) (layer "B.Mask"))
      (footprint "Graphic" (layer "B.Cu") (at 20 20 47)
        (fp_text user "BACK" (at 4 0 17) (layer "B.Mask") (effects (font (size 1 1) (thickness .15))))
        (fp_rect (start -1 -2) (end 1 2) (stroke (width .2) (type solid)) (fill solid) (layer "B.Mask")))""",
    "zone": """(zone (net 0) (net_name "") (layer "F.Mask") (hatch edge .5)
      (connect_pads (clearance .2)) (min_thickness .1) (filled_areas_thickness no) (fill yes)
      (polygon (pts (xy 10 10) (xy 14 10) (xy 14 13) (xy 10 13)))
      (filled_polygon (layer "F.Mask") (pts (xy 10 10) (xy 14 10) (xy 14 13) (xy 10 13))))""",
    "merged": """(footprint "Merged" (layer "F.Cu") (at 10 10)
      (pad "1" smd rect (at -.6 0) (size 1 2) (layers "F.Cu" "F.Mask"))
      (pad "2" smd rect (at .6 0) (size 1 2) (layers "F.Cu" "F.Mask"))
      (pad "3" smd rect (at 0 0) (size .1 1) (layers "F.Cu")))""",
}

NATIVE_CASES["padstack"] = """(footprint "Stack" (layer "F.Cu") (at 10 10)
 (pad "1" thru_hole circle (at 0 0) (size 1 1) (drill .4) (layers "*.Cu" "*.Mask")
 (padstack (mode custom) (layer "F.Cu" (shape circle) (size 1 1))
 (layer "B.Cu" (shape rect) (size 2 3)))))"""


@pytest.mark.parametrize("case", NATIVE_CASES)
def test_native_advanced_layer_matches_independent_gerber(tmp_path, case):
    from kicad_tools.cli.runner import find_kicad_cli

    if find_kicad_cli() is None:
        pytest.skip("native KiCad required for export-equivalence controls")
    pytest.importorskip("gerbonara", minversion="1.6.3")
    setup = (
        "(pad_to_mask_clearance 0) (solder_mask_min_width .3)"
        if case == "merged"
        else "(pad_to_mask_clearance .05)"
    )
    path = _board(tmp_path / f"{case}.kicad_pcb", NATIVE_CASES[case], setup=setup)
    before = path.read_bytes()
    artifact = tmp_path / "export"
    actual = inspect_exported_mask_geometry(path, artifact_dir=artifact)
    assert actual.complete, actual.unsupported
    assert path.read_bytes() == before
    assert (artifact / "source" / path.name).read_bytes() == before
    for layer, geometry in actual.layers.items():
        reference = read_native_gerber(artifact / f"{case}-{layer.replace('.', '_')}.gbr")
        if reference.is_empty:
            assert geometry.is_empty
        else:
            assert geometry.difference(reference.buffer(0.000005)).is_empty
            assert reference.difference(geometry.buffer(0.000005)).is_empty
            assert geometry.symmetric_difference(reference).area <= 0.000005 * reference.length
    assert actual.features
    assert all(f["source_uuid"] for f in actual.features)
    if case == "custom_rotated":
        pad = next(f for f in actual.features if f["kind"] == "pad:custom")
        assert pad["authored_at"] == [2, 0, 37]
        assert pad["position_frame"] == "footprint-local"
        assert pad["angle_frame"] == "board"
        assert pad["parent_placement"]["at"] == [10, 10, 37]
    if case in {"custom_rotated", "chamfer", "padstack"}:
        assert len(actual.source_geometries) == 1
        identity, standalone = next(iter(actual.source_geometries.items()))
        assert identity in {f["source_uuid"] for f in actual.features}
        assert len(standalone["derivative_sha256"]) == 64
        assert standalone["layers"]["F.Mask"].area > 0
    if case == "padstack":
        assert actual.layers["F.Mask"].area == pytest.approx(3.141592653589793 / 4, abs=0.000005)
        assert actual.layers["B.Mask"].area == pytest.approx(6, abs=0.000005)
    if case == "merged":
        # The native web-merger opens mask over unrelated intervening copper.
        # Neither same-net nor negative-margin waivers exist in this geometry layer.
        center = Point(10, 10)
        assert actual.layers["F.Mask"].contains(center)
        assert actual.layers["F.Cu"].contains(center)


@pytest.mark.parametrize("command", ["%TD*ZZignored*%", "%ADD10C,1X-1*%", "%TDunknown*%"])
def test_invalid_extended_fields_do_not_disappear(command):
    with pytest.raises(GerberGeometryError):
        parse_gerber_geometry(HEADER + command + "M02*")


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), 0, -1])
def test_timeout_must_be_finite_positive(tmp_path, timeout):
    with pytest.raises(ValueError, match="positive"):
        inspect_exported_mask_geometry(
            tmp_path / "absent.kicad_pcb", options=MaskExportOptions(timeout_seconds=timeout)
        )


@pytest.mark.parametrize(
    "board_margin,fp_margin,pad_margin,expected",
    [
        (None, None, None, 0),
        (0.1, None, None, 0.1),
        (0.1, 0.2, None, 0.2),
        (0.1, 0.2, 0, 0),
        (0.1, None, -0.1, -0.1),
    ],
)
def test_native_project_and_local_margin_precedence(
    tmp_path, board_margin, fp_margin, pad_margin, expected
):
    import json

    from kicad_tools.cli.runner import find_kicad_cli

    if find_kicad_cli() is None:
        pytest.skip("native KiCad required")
    source = f'(footprint "Precedence" (layer "F.Cu") (at 10 10) {f"(solder_mask_margin {fp_margin})" if fp_margin is not None else ""} (pad "1" smd rect (at 0 0) (size 1 2) (layers "F.Cu" "F.Mask") {f"(solder_mask_margin {pad_margin})" if pad_margin is not None else ""}))'
    path = _board(
        tmp_path / "precedence.kicad_pcb",
        source,
        setup=f"(pad_to_mask_clearance {board_margin})" if board_margin is not None else "",
    )
    project = path.with_suffix(".kicad_pro")
    project.write_text(
        json.dumps({"board": {"design_settings": {"rules": {"solder_mask_clearance": 0.3}}}})
    )
    before = project.read_bytes()
    result = inspect_exported_mask_geometry(path)
    assert result.complete, result.unsupported
    assert result.layers["F.Mask"].bounds == pytest.approx(
        (9.5 - expected, 9 - expected, 10.5 + expected, 11 + expected), abs=0.000005
    )
    assert project.read_bytes() == before
    assert result.project_sha256 is not None


def test_unknown_padstack_field_stays_visible(tmp_path):
    from kicad_tools.cli.runner import find_kicad_cli

    if find_kicad_cli() is None:
        pytest.skip("native KiCad required")
    body = NATIVE_CASES["padstack"].replace(
        "(shape circle)", "(shape circle) (solder_mask_margin .5)"
    )
    path = _board(tmp_path / "unknown.kicad_pcb", body)
    result = inspect_exported_mask_geometry(path)
    assert not result.complete
    assert any(d["source_uuid"] and d["feature"] == "padstack" for d in result.unsupported)


def test_unsupported_native_stream_invalidates_complete_layer(tmp_path, monkeypatch):
    import subprocess

    import kicad_tools.validate.mask_export_geometry as module

    path = _board(tmp_path / "input.kicad_pcb", NATIVE_CASES["merged"])

    def run(command, **kwargs):
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, "10.0.5\n", "")
        destination = Path(command[command.index("-o") + 1])
        destination.mkdir()
        for layer in module.LAYERS:
            data = HEADER + "%ADD10C,1*%D10*X0Y0D03*"
            if layer == "F.Mask":
                data += "%TD*UNSUPPORTED*%"
            (destination / f"input-{layer.replace('.', '_')}.gbr").write_text(data + "M02*")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(module.subprocess, "run", run)
    result = module.inspect_exported_mask_geometry(path, native_command=["fake-kicad-cli"])
    assert not result.complete
    assert "F.Mask" not in result.layers
    assert any(d["feature"] == "F.Mask" for d in result.unsupported)
    assert "B.Mask" in result.layers  # Partial evidence is explicit; never called complete.


def test_stored_plot_options_preserved_with_hashed_macro_free_representation(tmp_path):
    import subprocess

    from kicad_tools.cli.runner import find_kicad_cli

    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("native KiCad required")
    pytest.importorskip("gerbonara", minversion="1.6.3")
    path = _board(
        tmp_path / "plot.kicad_pcb",
        """(footprint "Rounded" (layer "F.Cu") (at 10 10 37)
      (pad "1" smd roundrect (at 0 0 37) (size 1 2) (roundrect_rratio .25) (layers "F.Cu" "F.Mask")))""",
        setup="(pad_to_mask_clearance .1) (pcbplotparams (disableapertmacros false) (usegerberextensions true))",
    )
    before = path.read_bytes()
    # Independent native baseline retains original macros and Protel extensions.
    baseline = tmp_path / "original"
    subprocess.run(
        [
            str(cli),
            "pcb",
            "export",
            "gerbers",
            "--layers",
            "F.Cu,B.Cu,F.Mask,B.Mask",
            "--board-plot-params",
            "-o",
            str(baseline),
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    actual = inspect_exported_mask_geometry(path, options=MaskExportOptions(board_plot_params=True))
    assert actual.complete, actual.unsupported
    assert path.read_bytes() == before
    assert actual.export_identity["plot_profile_source_sha256"] != actual.source_sha256
    for layer, extension in [
        ("F.Mask", "gts"),
        ("B.Mask", "gbs"),
        ("F.Cu", "gtl"),
        ("B.Cu", "gbl"),
    ]:
        reference = read_native_gerber(next(baseline.glob(f"*.{extension}")))
        geometry = actual.layers[layer]
        assert geometry.difference(reference.buffer(0.000005)).is_empty
        assert reference.difference(geometry.buffer(0.000005)).is_empty
        assert geometry.symmetric_difference(reference).area <= 0.000005 * reference.length


def test_native_padstack_mask_participation_is_distinct_per_side(tmp_path):
    from kicad_tools.cli.runner import find_kicad_cli

    if find_kicad_cli() is None:
        pytest.skip("native KiCad required")
    body = NATIVE_CASES["padstack"].replace('"*.Cu" "*.Mask"', '"*.Cu" "F.Mask"')
    path = _board(tmp_path / "front.kicad_pcb", body)
    actual = inspect_exported_mask_geometry(path)
    assert actual.complete, actual.unsupported
    assert actual.layers["F.Mask"].area > 0.7
    assert actual.layers["B.Mask"].is_empty
    assert actual.layers["B.Cu"].area == pytest.approx(6)


@pytest.mark.parametrize("option", ["(anchor banana)", "(new_option yes)"])
def test_unknown_nested_padstack_option_stays_visible(tmp_path, option):
    from kicad_tools.cli.runner import find_kicad_cli

    if find_kicad_cli() is None:
        pytest.skip("native KiCad required")
    body = NATIVE_CASES["padstack"].replace("(shape circle)", f"(shape circle) (options {option})")
    path = _board(tmp_path / "unknown-option.kicad_pcb", body)
    result = inspect_exported_mask_geometry(path)
    assert not result.complete
    assert any(d["source_uuid"] and d["feature"] == "padstack" for d in result.unsupported)
