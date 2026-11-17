"""Standard mask snapshots must not misrepresent unsupported geometry as complete."""

import hashlib
import json
import re
import subprocess
import uuid

import pytest
from shapely.geometry import Point

from kicad_tools.validate.mask_geometry import inspect_mask_geometry


@pytest.fixture
def board(tmp_path):
    def create(pads=None, setup="(pad_to_mask_clearance 0.1)", footprint="", extra="", rotation=0):
        if pads is None:
            pads = '(pad "1" smd rect (at 0 0) (size 1 2) (layers "F.Cu" "F.Mask"))'
        # Stable valid IDs for every pad/via. Native fixture output is reproducible.
        counter = 0

        def identify(match):
            nonlocal counter
            counter += 1
            return match[0] + f' (uuid "{uuid.UUID(int=counter)}")'

        pads = re.sub(r'\(pad "[^"]+" \w+ \w+', identify, pads)
        extra = re.sub(r"\(via", identify, extra)
        path = tmp_path / "board.kicad_pcb"
        path.write_text(f"""(kicad_pcb (version 20241229) (generator pcbnew)
(general (thickness 1.6)) (paper "A4")
(layers (0 "F.Cu" signal) (31 "B.Cu" signal) (38 "B.Mask" user) (39 "F.Mask" user))
(setup {setup})
(footprint "Fixture" (layer "F.Cu") (at 10 10 {rotation}) {footprint} {pads}) {extra})""")
        return path

    return create


def test_precedence_explicit_zero_negative_and_hashes(board):
    pads = "".join(
        f'(pad "{n}" smd rect (at {n * 3} 0) (size 1 2) (layers "F.Cu" "F.Mask") {margin})'
        for n, margin in enumerate(("", "(solder_mask_margin 0)", "(solder_mask_margin -0.05)"))
    )
    path = board(pads, footprint="(solder_mask_margin 0.2)")
    before = path.read_bytes()
    result = inspect_mask_geometry(path)
    assert result.complete
    assert [(p.margin_mm, p.margin_source) for p in result.openings] == [
        (0.2, "footprint"),
        (0, "pad"),
        (-0.05, "pad"),
    ]
    assert result.openings[2].mask_defined
    assert result.openings[2].geometry.area < result.openings[2].copper.area
    assert path.read_bytes() == before
    assert result.source_sha256 == hashlib.sha256(before).hexdigest()
    assert result.to_dict() == inspect_mask_geometry(path).to_dict()


@pytest.mark.parametrize("shape", ["circle", "oval", "rect", "roundrect"])
def test_standard_shapes_rotations_and_sides(board, shape):
    size = "1 1" if shape == "circle" else "1 2"
    path = board(
        f'(pad "1" smd {shape} (at 0 0 37) (size {size}) (layers "*.Cu" "*.Mask") (roundrect_rratio 0.25))'
    )
    result = inspect_mask_geometry(path)
    assert result.complete
    assert [p.layer for p in result.openings] == ["B.Mask", "F.Mask"]
    assert result.openings[0].geometry.equals(result.openings[1].geometry)
    assert result.openings[0].geometry.contains(result.openings[0].copper)


@pytest.mark.parametrize(
    "feature",
    [
        '(gr_rect (start 0 0) (end 1 1) (layer "F.Mask") (stroke (width 0)) (fill solid))',
        '(zone (net 0) (net_name "") (layer "B.Mask") (hatch edge 0.5))',
    ],
)
def test_mask_features_mark_incomplete(board, feature):
    result = inspect_mask_geometry(board(extra=feature))
    assert not result.complete
    assert result.unsupported
    assert len(result.openings) == 1


def test_custom_and_chamfer_are_never_rectangles(board):
    path = board('(pad "1" smd custom (at 0 0) (size 1 2) (layers "F.Cu" "F.Mask"))')
    result = inspect_mask_geometry(path)
    assert not result.complete and not result.openings
    path = board(
        '(pad "1" smd roundrect (at 0 0) (size 1 2) (layers "F.Cu" "F.Mask") (chamfer top_left))'
    )
    result = inspect_mask_geometry(path)
    assert not result.complete and not result.openings


def test_project_and_merging_gaps_remain_visible(board):
    path = board(setup="(pad_to_mask_clearance 0.1) (solder_mask_min_width 0.2)")
    project = path.with_suffix(".kicad_pro")
    project.write_text(
        json.dumps({"board": {"design_settings": {"rules": {"solder_mask_clearance": 0.3}}}})
    )
    result = inspect_mask_geometry(path)
    assert not result.complete
    assert {p["feature"] for p in result.unsupported} == {
        "project-mask-settings",
        "mask-web-merging",
    }
    assert result.project_sha256 == hashlib.sha256(project.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "settings, expected",
    [
        ("", []),
        ("(tenting (front no) (back yes))", ["F.Mask"]),
        ("(tenting (front no) (back no))", ["B.Mask", "F.Mask"]),
    ],
)
def test_via_tenting(board, settings, expected):
    path = board(
        pads="", extra=f'(via (at 10 10) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") {settings})'
    )
    result = inspect_mask_geometry(path)
    assert result.complete
    assert [p.layer for p in result.openings] == expected


def test_native_mask_dimensions_and_zero_override(board, tmp_path):
    from kicad_tools.cli.runner import find_kicad_cli

    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("native KiCad unavailable")
    pads = "".join(
        f'(pad "{n}" smd rect (at {n * 3} 0) (size 1 2) (layers "F.Cu" "F.Mask") {margin})'
        for n, margin in enumerate(("", "(solder_mask_margin 0)", "(solder_mask_margin -0.05)"))
    )
    path = board(pads, footprint="(solder_mask_margin 0.2)")
    dest = tmp_path / "gerber"
    subprocess.run(
        [
            str(cli),
            "pcb",
            "export",
            "gerbers",
            "--layers",
            "F.Cu,F.Mask",
            "-o",
            str(dest),
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    mask = next(p for p in dest.iterdir() if "F_Mask" in p.name).read_text()
    copper = next(p for p in dest.iterdir() if "F_Cu" in p.name).read_text()
    # Native export independently establishes 0.2 rounded expansion, explicit
    # zero and negative dimensions; copper remains the unchanged 1 x 2 rectangle.
    assert re.search(r"%ADD\d+RoundRect,0\.200000X-0\.500000X-1\.000000", mask)
    assert re.search(r"%ADD\d+R,1\.000000X2\.000000\*%", mask)
    assert re.search(r"%ADD\d+R,0\.900000X1\.900000\*%", mask)
    assert re.search(r"%ADD\d+R,1\.000000X2\.000000\*%", copper)
    snapshot = inspect_mask_geometry(path)
    first, zero, negative = snapshot.openings
    assert first.geometry.bounds == pytest.approx((9.3, 8.8, 10.7, 11.2), abs=0.000002)
    assert not first.geometry.covers(Point(9.3, 8.8))  # expanded corner is rounded
    assert zero.geometry.area == pytest.approx(2)
    assert negative.geometry.area == pytest.approx(0.9 * 1.9)


def test_native_via_tenting_and_board_margin(board, tmp_path):
    from kicad_tools.cli.runner import find_kicad_cli

    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("native KiCad unavailable")
    path = board(
        pads="",
        extra='(via (at 10 10) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (tenting (front no) (back yes)))',
    )
    dest = tmp_path / "gerbers"
    subprocess.run(
        [
            str(cli),
            "pcb",
            "export",
            "gerbers",
            "--layers",
            "F.Cu,F.Mask,B.Mask",
            "-o",
            str(dest),
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    files = {p.stem: p.read_text() for p in dest.iterdir()}
    assert re.search(r"%ADD\d+C,0\.800000\*%", files["board-F_Mask"])
    assert re.search(r"%ADD\d+C,0\.600000\*%", files["board-F_Cu"])
    assert "D03*" not in files["board-B_Mask"]
    snapshot = inspect_mask_geometry(path)
    assert snapshot.complete
    assert len(snapshot.openings) == 1
    assert snapshot.openings[0].geometry.bounds == pytest.approx(
        (9.6, 9.6, 10.4, 10.4), abs=0.000003
    )


def test_native_rotated_roundrect(board, tmp_path):
    from shapely.geometry import MultiPoint

    from kicad_tools.cli.runner import find_kicad_cli

    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("native KiCad unavailable")
    path = board(
        '(pad "1" smd roundrect (at 0 0 37) (size 1 2) (layers "F.Cu" "F.Mask") (roundrect_rratio 0.25))'
    )
    dest = tmp_path / "roundrect"
    subprocess.run(
        [
            str(cli),
            "pcb",
            "export",
            "gerbers",
            "--layers",
            "F.Cu,F.Mask",
            "-o",
            str(dest),
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    snapshot = inspect_mask_geometry(path)
    assert snapshot.complete
    for suffix, geometry in (
        ("F_Mask", snapshot.openings[0].geometry),
        ("F_Cu", snapshot.openings[0].copper),
    ):
        gerber = next(p for p in dest.iterdir() if suffix in p.name).read_text()
        macro = re.search(r"%ADD\d+RoundRect,([^*]+)\*%", gerber)
        assert macro, gerber
        values = [float(v) for v in macro[1].split("X")]
        radius, *coords = values
        # Gerber macro exports rotated corner centers in y-up millimetres;
        # convert to the board's y-down coordinates, then use its native radius.
        points = [(10 + coords[i], 10 - coords[i + 1]) for i in range(0, 8, 2)]
        native = MultiPoint(points).convex_hull.buffer(radius, quad_segs=512)
        assert geometry.hausdorff_distance(native) < 0.000005


def test_custom_rules_hash_prevents_claiming_complete(board):
    path = board()
    rules = path.with_suffix(".kicad_dru")
    rules.write_text('(version 1)\n(rule "mask" (constraint solder_mask_expansion (min 0.4)))')
    first = inspect_mask_geometry(path)
    assert not first.complete
    assert first.rules_sha256 == hashlib.sha256(rules.read_bytes()).hexdigest()
    rules.write_text(rules.read_text().replace("0.4", "0.5"))
    assert inspect_mask_geometry(path).rules_sha256 != first.rules_sha256


def test_missing_identity_and_nonfinite_margin(board):
    path = board()
    path.write_text(re.sub(r'\(uuid "[^"]+"\)', "", path.read_text()))
    assert not inspect_mask_geometry(path).complete
    path = board(setup="(pad_to_mask_clearance nan)")
    result = inspect_mask_geometry(path)
    assert not result.complete and not result.openings


def test_board_tenting_inherited_and_overridden(board):
    path = board(
        pads="",
        setup="(pad_to_mask_clearance 0.1) (tenting (front no) (back no))",
        extra='(via (at 10 10) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (tenting (front yes) (back none)))',
    )
    result = inspect_mask_geometry(path)
    assert result.complete
    assert [opening.layer for opening in result.openings] == ["B.Mask"]


def test_incomplete_source_cannot_be_complete(board):
    path = board()
    path.write_text(path.read_text()[:-1])
    with pytest.raises(ValueError, match="Incomplete"):
        inspect_mask_geometry(path)


def test_native_footprint_local_translation(board, tmp_path):
    from kicad_tools.cli.runner import find_kicad_cli

    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("native KiCad unavailable")
    path = board('(pad "1" smd circle (at 2 1) (size 1 1) (layers "F.Cu" "F.Mask"))', rotation=90)
    dest = tmp_path / "rotated-footprint"
    subprocess.run(
        [str(cli), "pcb", "export", "gerbers", "--layers", "F.Mask", "-o", str(dest), str(path)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    gerber = next(p for p in dest.iterdir() if "F_Mask" in p.name).read_text()
    flash = re.search(r"X(-?\d+)Y(-?\d+)D03\*", gerber)
    assert flash
    center = (int(flash[1]) / 1e6, -int(flash[2]) / 1e6)
    geometry = inspect_mask_geometry(path).openings[0].geometry
    assert (geometry.centroid.x, geometry.centroid.y) == pytest.approx(center, abs=0.000001)


@pytest.mark.parametrize(
    "field",
    [
        "(solder_mask_margin banana)",
        "(roundrect_rratio banana)",
        "(roundrect_rratio 0.8)",
        "(at banana 0)",
        "(at 0 0 banana)",
        "(at 0 0 inf)",
        "(size 1 banana)",
        "(solder_mask_margin 0 0)",
    ],
)
def test_malformed_present_pad_fields_never_become_native_defaults(board, field):
    at = "" if field.startswith("(at ") else "(at 0 0)"
    size = "" if field.startswith("(size ") else "(size 1 2)"
    path = board(f'(pad "1" smd roundrect {at} {size} (layers "F.Cu" "F.Mask") {field})')
    result = inspect_mask_geometry(path)
    assert not result.complete
    assert not result.openings
    assert result.unsupported[0]["feature"] == "source-geometry"


@pytest.mark.parametrize(
    "change",
    [
        {"setup": "(tenting (front banana) (back yes))"},
        {"setup": "(tenting (front yes) (front no))"},
        {"setup": "(pad_to_mask_clearance banana)"},
        {"footprint": "(solder_mask_margin banana)"},
        {"rotation": "banana"},
        {"extra": '(via (at banana 0) (size 1) (layers "F.Cu" "B.Cu"))'},
        {"extra": '(via (at 0 0) (size 1) (layers "F.Cu" "B.Cu") (tenting (front banana)))'},
    ],
)
def test_malformed_inherited_and_via_fields_are_incomplete(board, change):
    result = inspect_mask_geometry(board(**change))
    assert not result.complete
    assert not result.openings


def test_valid_leading_dot_and_zero_source_fields_keep_native_semantics(board):
    path = board(
        '(pad "1" smd roundrect (at .0 0 0) (size .5 1) (layers "F.Cu" "F.Mask") (roundrect_rratio .25) (solder_mask_margin 0))'
    )
    result = inspect_mask_geometry(path)
    assert result.complete
    assert result.openings[0].margin_mm == 0
    assert result.openings[0].margin_source == "pad"
