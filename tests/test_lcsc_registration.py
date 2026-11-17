"""Auto-registration of EasyEDA bodies to KiCad footprints (issue #7686).

**Fixture provenance (read before trusting these tests).**  No agent contacts
EasyEDA (operator ruling Q44), so the "EasyEDA" payloads below are
*hand-synthesized* in the layout ``parse_easyeda_pads`` reads: they are built
from the real ``PCIE_Mini_Edge`` pad geometry in
``boards/06-diffpair-test/generate_pcb.py`` rotated into the frame the part
vendor's body is documented to use (``lcsc_model_transforms`` C444929
provenance).  They prove the registration *math* and the sign/frame
conventions reproduce the hand-calibrated transform, and exercise the parser
against the layout we assume.  They do NOT prove that the live EasyEDA API
returns that layout: live verification is tracked in the PR for #7686.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from unittest import mock

import pytest

from kicad_tools.footprints.library_path import LibraryPaths
from kicad_tools.pcb import lcsc_models
from kicad_tools.pcb.lcsc_model_transforms import LCSC_MODEL_TRANSFORMS
from kicad_tools.pcb.lcsc_registration import (
    EASYEDA_UNIT_MM,
    EasyEdaPad,
    parse_easyeda_pads,
    register_pads,
)
from kicad_tools.pcb.models3d import add_model_refs

FAKE_STEP = b"ISO-10303-21;\r\nHEADER;\r\nEND-ISO-10303-21;\r\n"
UUID = "0123456789abcdef0123456789abcdef"

# Real J3 pad geometry (boards/06-diffpair-test/generate_pcb.py): 12 pads, 0.8 mm
# pitch, one column along footprint Y, centred on the footprint origin.
PCIE_TARGET = [(str(i + 1), (0.0, (i - 5.5) * 0.8)) for i in range(12)]
PCIE_FOOTPRINT = (
    '(kicad_pcb\n  (footprint "Connector_PCIE:PCIE_Mini_Edge" (layer "F.Cu")\n'
    '    (at 100 100)\n    (property "Reference" "J3")\n'
    + "".join(
        f'    (pad "{n}" smd rect (at {x} {y}) (size 0.5 0.6) (layers "F.Cu"))\n'
        for n, (x, y) in PCIE_TARGET
    )
    + "  ))\n"
)


def _rot(x: float, y: float, deg: float) -> tuple[float, float]:
    r = math.radians(deg)
    return x * math.cos(r) - y * math.sin(r), x * math.sin(r) + y * math.cos(r)


def _easyeda_payload(
    pads: list[tuple[str, float, float]], head: tuple[float, float] = (4000.0, 3000.0)
) -> bytes:
    """Component-info JSON in the assumed layout: PAD~ strings in 10-mil units."""
    shapes = [
        f"PAD~RECT~{head[0] + x / EASYEDA_UNIT_MM:.2f}~{head[1] + y / EASYEDA_UNIT_MM:.2f}"
        f"~2~2.4~1~~{n}~0~~0~gge{i}~0~~Y~0~0~0.2~{x:.2f},{y:.2f}"
        for i, (n, x, y) in enumerate(pads)
    ]
    shapes.append("SVGNODE~" + json.dumps({"attrs": {"uuid": UUID}}))
    return json.dumps(
        {
            "success": True,
            "result": {
                "packageDetail": {
                    "dataStr": {"head": {"x": head[0], "y": head[1]}, "shape": shapes}
                }
            },
        }
    ).encode()


def _vendor_pads(deg: float) -> list[tuple[str, float, float]]:
    """EasyEDA-side pads = target pads rotated by *deg* (the body's authoring frame)."""
    return [(n, *_rot(x, y, deg)) for n, (x, y) in PCIE_TARGET]


class TestParser:
    def test_parses_pads_relative_to_head_in_mm(self):
        pads = parse_easyeda_pads(_easyeda_payload([("1", 1.0, -2.0), ("2", 0.0, 0.0)]))
        assert [p.number for p in pads] == ["1", "2"]
        assert pads[0].x == pytest.approx(1.0, abs=0.003)
        assert pads[0].y == pytest.approx(-2.0, abs=0.003)

    @pytest.mark.parametrize("bad", [b"", b"not json", b"{}", b'{"result": 3}'])
    def test_unrecognised_layout_yields_no_pads(self, bad):
        assert parse_easyeda_pads(bad) == []

    def test_skips_malformed_pad_strings(self):
        doc = {"result": {"packageDetail": {"dataStr": {"shape": ["PAD~RECT~x~y", "PAD~R~1~2"]}}}}
        assert parse_easyeda_pads(doc) == []


class TestRegisterPads:
    def test_c444929_reproduces_hand_calibrated_transform(self):
        """Within 0.05 mm and 1 degree of the curated table entry, residual reported."""
        pads = [EasyEdaPad(n, x, y) for n, x, y in _vendor_pads(90.0)]
        result = register_pads(pads, PCIE_TARGET)
        assert result is not None and result.accepted
        curated = LCSC_MODEL_TRANSFORMS["C444929"]
        assert curated.rotate is not None
        assert abs(result.rotate[2] - curated.rotate[2]) <= 1.0
        assert result.rotate[0] == 0.0 and result.rotate[1] == 0.0
        expected_offset = curated.offset or (0.0, 0.0, 0.0)
        for got, want in zip(result.offset, expected_offset, strict=True):
            assert abs(got - want) <= 0.05
        assert result.rms_mm < 0.01 and result.max_mm < 0.01
        assert "rms" in result.describe() and "derived" in result.describe()

    def test_survives_easyeda_quantisation_via_parser(self):
        parsed = parse_easyeda_pads(_easyeda_payload(_vendor_pads(90.0)))
        result = register_pads(parsed, PCIE_TARGET)
        assert result is not None and result.accepted
        assert result.rotate[2] == pytest.approx(-90.0, abs=1.0)
        assert result.rms_mm < 0.01

    @pytest.mark.parametrize("deg", [0.0, 90.0, 180.0, -90.0, 37.0])
    def test_recovers_any_authoring_rotation(self, deg):
        pads = [EasyEdaPad(n, x, y) for n, x, y in _vendor_pads(deg)]
        result = register_pads(pads, PCIE_TARGET)
        assert result is not None and result.accepted
        want = -deg if deg != 180.0 else 180.0
        diff = (result.rotate[2] - want + 180.0) % 360.0 - 180.0
        assert abs(diff) <= 0.1

    def test_translation_becomes_model_frame_offset_with_y_negated(self):
        # Source pads sit 1.5 mm right of / 2.0 mm below the EasyEDA origin.
        pads = [EasyEdaPad(n, x - 1.5, y - 2.0) for n, x, y in _vendor_pads(0.0)]
        result = register_pads(pads, PCIE_TARGET)
        assert result is not None and result.accepted
        # t = c_t - R c_s = (1.5, 2.0) footprint-2D; model offset = (1.5, -2.0).
        assert result.offset == (1.5, -2.0, 0.0)

    def test_high_residual_is_not_accepted(self):
        scrambled = [
            EasyEdaPad(n, x + (0.4 if i % 2 else -0.4), y)
            for i, (n, x, y) in enumerate(_vendor_pads(0.0))
        ]
        result = register_pads(scrambled, PCIE_TARGET)
        assert result is not None and not result.accepted
        assert "needs human calibration" in result.reason

    def test_mirrored_footprint_is_not_accepted(self):
        """A rigid fit cannot undo a mirror: an asymmetric pad set must be rejected."""
        target = [("1", (0.0, 0.0)), ("2", (2.0, 0.0)), ("3", (2.0, 1.0)), ("4", (0.0, 3.0))]
        mirrored = [EasyEdaPad(n, -x, y) for n, (x, y) in target]
        result = register_pads(mirrored, target)
        assert result is not None and not result.accepted

    def test_unmatched_pad_numbers_give_no_fit(self):
        pads = [EasyEdaPad(f"X{i}", 0.0, float(i)) for i in range(5)]
        assert register_pads(pads, PCIE_TARGET) is None

    def test_too_few_matches_is_not_accepted(self):
        pads = [EasyEdaPad("1", 0.0, 0.0), EasyEdaPad("2", 0.0, 0.8)]
        result = register_pads(pads, PCIE_TARGET)
        assert result is not None and not result.accepted
        assert "matched by number" in result.reason

    def test_coincident_pads_cannot_fix_a_rotation(self):
        pads = [EasyEdaPad("1", 1.0, 1.0), EasyEdaPad("2", 1.0, 1.0)]
        assert register_pads(pads, [("1", (0.0, 0.0)), ("2", (0.0, 0.8))]) is None


@pytest.fixture
def lcsc_env(tmp_path, monkeypatch):
    monkeypatch.setenv(lcsc_models.FETCH_INTERVAL_ENV_VAR, "0")
    lcsc_models.reset_fetch_budget()
    yield tmp_path
    lcsc_models.reset_fetch_budget()


# A part with no entry in the packaged table, so derivation is what decides.
C_NUMBER = "C999001"


def _fetch_into_cache(cache: Path, payload: bytes, c_number: str = C_NUMBER) -> None:
    def fake_get(url):
        return payload if "components" in url else FAKE_STEP

    with mock.patch.object(lcsc_models, "_http_get", fake_get):
        assert lcsc_models.resolve_lcsc_step(c_number, cache_dir=cache, fetch=True)


def _patch(tmp_path: Path, cache: Path, mapping=None, c_number: str = C_NUMBER):
    pcb = tmp_path / "j3.kicad_pcb"
    pcb.write_text(PCIE_FOOTPRINT)
    empty = tmp_path / "fp"
    empty.mkdir(exist_ok=True)
    report = add_model_refs(
        pcb,
        library_paths=LibraryPaths(footprints_path=empty, source="config"),
        lcsc_mapping=mapping or {"Connector_PCIE:PCIE_Mini_Edge": c_number},
        lcsc_cache_dir=cache,
    )
    return report, pcb.read_text()


class TestEndToEnd:
    def test_fetch_stores_pads_and_registration_writes_the_transform(self, lcsc_env):
        cache = lcsc_env / "cache"
        _fetch_into_cache(cache, _easyeda_payload(_vendor_pads(90.0)))
        meta = lcsc_models.read_cache_meta(C_NUMBER, cache)
        assert meta is not None and len(meta.pads) == 12
        report, text = _patch(lcsc_env, cache)
        assert "Connector_PCIE:PCIE_Mini_Edge" in report.lcsc_registration
        assert report.lcsc_needs_calibration == {}
        assert "(rotate\n" in text and "(xyz 0 0 -90)" in text

    def test_high_residual_keeps_identity_and_flags_calibration(self, lcsc_env):
        cache = lcsc_env / "cache"
        bad = [
            (n, x + (0.5 if i % 2 else -0.5), y) for i, (n, x, y) in enumerate(_vendor_pads(0.0))
        ]
        _fetch_into_cache(cache, _easyeda_payload(bad))
        report, text = _patch(lcsc_env, cache)
        assert "Connector_PCIE:PCIE_Mini_Edge" in report.lcsc_needs_calibration
        assert report.lcsc_registration == {}
        assert "(xyz 0 0 0)" in text  # identity rotate kept

    def test_authored_transform_beats_derivation(self, lcsc_env):
        from kicad_tools.pcb.lcsc_models import LcscModelEntry

        cache = lcsc_env / "cache"
        _fetch_into_cache(cache, _easyeda_payload(_vendor_pads(90.0)))
        mapping = {
            "Connector_PCIE:PCIE_Mini_Edge": LcscModelEntry(lcsc=C_NUMBER, rotate=(0.0, 0.0, 90.0))
        }
        report, text = _patch(lcsc_env, cache, mapping)
        assert report.lcsc_registration == {}
        assert "(xyz 0 0 90)" in text

    def test_cache_without_pads_leaves_identity(self, lcsc_env):
        cache = lcsc_env / "cache"
        _fetch_into_cache(cache, _easyeda_payload([]))
        report, text = _patch(lcsc_env, cache)
        assert report.lcsc_registration == {} and report.lcsc_needs_calibration == {}
        assert "(xyz 0 0 0)" in text

    def test_packaged_table_beats_derivation(self, lcsc_env):
        """C444929 has a curated entry; it wins even though the pads would derive."""
        cache = lcsc_env / "cache"
        _fetch_into_cache(cache, _easyeda_payload(_vendor_pads(90.0)), "C444929")
        report, text = _patch(lcsc_env, cache, c_number="C444929")
        assert report.lcsc_registration == {}
        assert "(xyz 0 0 -90)" in text
