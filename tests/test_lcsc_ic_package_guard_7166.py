"""LCSC assignment verification must not call an unchecked part "verified".

Issue #7166 found three gaps that let a wrong part ship as ``READY``:

1. ``kct readiness`` ignored the bundle's ``bom_lcsc_values`` FAIL (covered in
   ``tests/test_kct_readiness_cmd.py``; the end-to-end mutation cases below
   feed a real preflight result into that gate too).
2. A part in neither the parts cache nor the offline catalog counted as
   "checked" -- now ``unverified`` -> WARN that names it.
3. Package validation stopped at chip passives -- a SOT-25-5 regulator on a
   SOT-23-6 footprint passed.  ``find_ic_package_mismatch`` now compares IC
   package family and pin count (preflight only; enrichment is unchanged).

Everything here is offline: the fleet's part records come from the committed
``tests/fixtures/lcsc_fleet_packages_7166.json`` and the jlcparts catalog is a
tiny fixture SQLite.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

from kicad_tools.cli import readiness_cmd
from kicad_tools.export.lcsc_value_check import (
    check_lcsc_against_cache,
    find_ic_package_mismatch,
    find_package_mismatch,
    parse_ic_package,
    verify_lcsc_assignment,
)
from kicad_tools.export.preflight import PreflightChecker, PreflightConfig
from kicad_tools.parts.cache import PartsCache
from kicad_tools.parts.jlcparts_catalog import JlcpartsCatalog
from kicad_tools.parts.models import Part
from kicad_tools.schema.bom import BOM, BOMItem
from kicad_tools.schema.pcb import PCB

REPO = Path(__file__).resolve().parents[1]
BOARDS = REPO / "boards"
FLEET_FIXTURE = Path(__file__).parent / "fixtures" / "lcsc_fleet_packages_7166.json"
FLEET_BOARDS = sorted(p.name for p in BOARDS.glob("0[0-7]-*") if p.is_dir())

# The real on-machine cache record for C25744 (UNI-ROYAL 10k 0402): sparse,
# the MPN is the only package evidence.
C25744_SPARSE = Part(
    lcsc_part="C25744",
    mfr_part="0402WGF1002TCE",
    description="0402WGF1002TCE",
)

_spec = importlib.util.spec_from_file_location(
    "build_jlcparts_fixture_7166",
    Path(__file__).parent / "parts" / "fixtures" / "build_jlcparts_fixture.py",
)
assert _spec is not None and _spec.loader is not None
_builder = importlib.util.module_from_spec(_spec)
sys.modules["build_jlcparts_fixture_7166"] = _builder
_spec.loader.exec_module(_builder)


def _fleet_parts() -> dict[str, dict[str, str]]:
    return json.loads(FLEET_FIXTURE.read_text())["parts"]


def _seeded_cache(tmp_path: Path, *extra: Part) -> PartsCache:
    """A parts cache in tmp seeded from the committed fleet fixture.

    No ``jlcparts.sqlite3`` sits beside it, so the preflight's default
    offline catalog is absent -- the check stays hermetic.
    """
    (tmp_path / "cache").mkdir(exist_ok=True)
    cache = PartsCache(db_path=tmp_path / "cache" / "parts.db")
    for lcsc, record in _fleet_parts().items():
        cache.put(Part(lcsc_part=lcsc, **record))
    for part in extra:
        cache.put(part)
    return cache


def _numbers(n: int) -> list[str]:
    return [str(i) for i in range(1, n + 1)]


# ---------------------------------------------------------------------------
# Fleet regression table (curator plan, measured 2026-10-18)
# ---------------------------------------------------------------------------

FLEET_CASES = [
    # (case id, footprint, pad numbers, part package)
    ("03-U2", "Package_TO_SOT_SMD:SOT-23-6", _numbers(6), "SOT-23-6L"),
    ("07-U3", "Package_TO_SOT_SMD:SOT-23-5", _numbers(5), "SOT-25-5"),
    ("03-U1", "Package_QFP:TQFP-44_10x10mm_P0.8mm", _numbers(44), "QFP-44(10x10)"),
    ("05-U1", "Package_QFP:TQFP-32_7x7mm_P0.8mm", _numbers(32), "TQFP-32(7x7)"),
    ("04-U2", "Package_QFP:LQFP-48_7x7mm_P0.5mm", _numbers(48), "LQFP-48(7x7)"),
    ("07-U1", "Package_QFP:LQFP-144_20x20mm_P0.5mm", _numbers(144), "LQFP-144(20x20)"),
    (
        # 59 pads: 28 signal + EP numbered 29 split into many copies + 5 unnumbered.
        "05-U2",
        "Package_SO:TI_PWP0028C_EP3.4x9.7_P0.65mm_ThermalVias",
        _numbers(28) + ["29"] * 26 + [""] * 5,
        "HTSSOP-28",
    ),
    (
        "05-U3",
        "Package_SO:HVSSOP-8-1EP_3x3mm_P0.65mm_EP1.57x1.89mm_ThermalVias",
        _numbers(8) + ["9"] * 6 + [""] * 4,
        "MSOP-8-EP",
    ),
    ("04-U1", "Package_TO_SOT_SMD:SOT-223-3_TabPin2", ["1", "2", "3", "2"], "SOT-223-3"),
    ("07-U2", "Package_SO:TSOP-II-54_22.2x10.16mm_P0.8mm", _numbers(54), "TSOP-54-10.2mm"),
    ("02-U1", "Package_DIP:DIP-8_W7.62mm", _numbers(8), "PDIP-8"),
    ("06-U1", "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", _numbers(8), "SOIC-8"),
    (
        "03-J2",
        "Connector_JST:JST_PH_S5B-PH-SM4-TB_1x05-1MP_P2.00mm_Horizontal",
        _numbers(5) + ["MP", "MP"],
        "SMD,P=2mm,Surface Mount,Right Angle",
    ),
    ("03-SW1", "Button_Switch_SMD:SW_SPST_TL3342", ["1", "1", "2", "2"], "SMD-4P,5.2x5.2mm"),
    ("03-Y1", "Crystal:Crystal_SMD_3225-4Pin_3.2x2.5mm", _numbers(4), "SMD3225-4P"),
    ("05-C1", "Capacitor_SMD:CP_Elec_8x10.5", _numbers(2), "SMD"),
    ("05-D1", "Diode_SMD:D_SMA", _numbers(2), "SMA(DO-214AC)"),
    ("05-D3", "Diode_SMD:D_SMB", _numbers(2), "DO-214AA"),
    ("05-RV1", "Potentiometer_THT:Potentiometer_Bourns_3296W_Vertical", _numbers(3), "SIP-3P"),
    ("03-J3", "Connector_PinHeader_2.54mm:PinHeader_2x03", _numbers(6), "SMD,P=2.54mm"),
    ("hdr", "Connector_PinHeader_2.54mm:PinHeader_1x06", _numbers(6), "Through Hole,P=2.54mm"),
]


@pytest.mark.parametrize(
    ("footprint", "pads", "package"),
    [case[1:] for case in FLEET_CASES],
    ids=[case[0] for case in FLEET_CASES],
)
def test_fleet_ic_packages_pass_or_skip(footprint, pads, package):
    assert find_ic_package_mismatch(footprint, pads, package) is None
    # Without pads (no PCB loaded) the footprint-name comparison must agree too.
    assert find_ic_package_mismatch(footprint, None, package) is None


@pytest.mark.parametrize(
    ("package", "family", "pins"),
    [
        ("SOT-23-6L", "SOT23", 6),
        ("SOT-25-5", "SOT23", 5),
        ("SOT-26", "SOT23", 6),
        ("SOT-23", "SOT23", 3),
        ("SOT-23-3(TO-236)", "SOT23", 3),
        ("SOT-223-3", "SOT223", 3),
        ("QFP-44(10x10)", "QFP", 44),
        ("HTSSOP-28", "SO", 28),
        ("MSOP-8-EP", "SO", 8),
        ("TSOP-54-10.2mm", "SO", 54),
        ("PDIP-8", "DIP", 8),
        ("QFN-32(5x5)", "QFN", 32),
    ],
)
def test_parse_ic_package(package, family, pins):
    parsed = parse_ic_package(package)
    assert parsed is not None
    assert (parsed.family, parsed.pins) == (family, pins)


@pytest.mark.parametrize(
    "package", ["SMD-4P,5.2x5.2mm", "SMD3225-4P", "SMD", "0603", "SMA(DO-214AC)", "SIP-3P", ""]
)
def test_non_ic_packages_are_not_parsed(package):
    assert parse_ic_package(package) is None


# ---------------------------------------------------------------------------
# AC5: the IC check catches real wrong parts
# ---------------------------------------------------------------------------


def test_sot25_regulator_on_sot23_6_footprint_fails():
    """Hermit mutation 3: U2 SOT-23-6 footprint <- C51118 (SOT-25-5)."""
    mismatch = find_ic_package_mismatch("Package_TO_SOT_SMD:SOT-23-6", _numbers(6), "SOT-25-5")
    assert mismatch is not None
    assert "pin count 5 vs 6 footprint pads" in mismatch.describe()


def test_qfn_part_on_soic_footprint_fails_cross_group():
    mismatch = find_ic_package_mismatch(
        "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", _numbers(8), "QFN-32(5x5)"
    )
    assert mismatch is not None
    assert "family QFN vs footprint family SO" in mismatch.describe()


def test_wrong_pin_count_same_family_fails():
    assert (
        find_ic_package_mismatch("Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", _numbers(8), "SOIC-14")
        is not None
    )


def test_qfp_body_disagreement_fails():
    mismatch = find_ic_package_mismatch(
        "Package_QFP:LQFP-48_7x7mm_P0.5mm", _numbers(48), "LQFP-48(10x10)"
    )
    assert mismatch is not None
    assert "body" in mismatch.describe()


def test_extra_pad_without_exposed_pad_marker_fails():
    # 9 numeric pads on a plain SOIC-8 footprint is not an EP allowance.
    assert find_ic_package_mismatch("SOIC-8_3.9x4.9mm", _numbers(9), "SOIC-8") is not None


@pytest.mark.parametrize(
    ("footprint", "package"),
    [
        ("Package_SO:TSSOP-8_4.4x3mm_P0.65mm", "SOIC-8"),
        ("Package_SO:MSOP-8_3x3mm_P0.65mm", "SOIC-8"),
        ("Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", "TSSOP-8"),
        ("Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", "MSOP-8"),
        ("SOIC-8", "VSSOP-8"),
    ],
)
def test_so_pitch_mismatch_fails_issue_7180(footprint, package):
    mismatch = find_ic_package_mismatch(footprint, _numbers(8), package)
    assert mismatch is not None
    assert "pitch" in mismatch.describe()


@pytest.mark.parametrize(
    ("footprint", "package"),
    [
        ("Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", "SO-8"),
        ("Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", "SOP-8"),
        ("Package_SO:TSSOP-8_4.4x3mm_P0.65mm", "MSOP-8"),
        ("Package_SO:TSSOP-8_4.4x3mm_P0.65mm", "TSSOP-8"),
        ("Package_TO_SOT_SMD:SOT-23-5", "SOT-25-5"),
        ("Package_TO_SOT_SMD:SOT-23-5", "SOT-23-5"),
        # SSOP pitch is unknown without an explicit token: never compared.
        ("Package_SO:SSOP-8_P0.65mm", "SSOP-8"),
        ("Package_SO:SSOP-8", "SOIC-8"),
    ],
)
def test_so_aliases_and_unknown_pitch_still_match_issue_7180(footprint, package):
    pads = _numbers(5 if "SOT" in footprint else 8)
    assert find_ic_package_mismatch(footprint, pads, package) is None


def test_parse_ic_package_pitch_issue_7180():
    assert parse_ic_package("Package_SO:SOIC-8_3.9x4.9mm_P1.27mm").pitch == 1.27
    assert parse_ic_package("MSOP-8-EP").pitch == 0.65
    assert parse_ic_package("SSOP-20").pitch is None
    assert parse_ic_package("SOT-23-5").pitch is None


# ---------------------------------------------------------------------------
# AC6: enrichment-facing helpers keep their behaviour
# ---------------------------------------------------------------------------


def test_find_package_mismatch_stays_chip_only():
    assert (
        find_package_mismatch("Package_TO_SOT_SMD:SOT-23-6", "U2", part_package="SOT-25-5") is None
    )


def test_check_lcsc_against_cache_unchanged_for_ic_and_unknown(tmp_path):
    cache = _seeded_cache(tmp_path)
    # Enrichment's wrapper still does not run the IC check ...
    assert (
        check_lcsc_against_cache(
            cache, "C51118", "USBLC6", "U2", footprint="Package_TO_SOT_SMD:SOT-23-6"
        )
        is None
    )
    # ... and still returns None (not a verdict) for unknown parts.
    assert check_lcsc_against_cache(cache, "C424242", "10k", "R1") is None


# ---------------------------------------------------------------------------
# Tri-state verdict (AC3/AC4)
# ---------------------------------------------------------------------------


def _catalog(tmp_path: Path, rows: list[dict]) -> JlcpartsCatalog:
    return JlcpartsCatalog(_builder.build_fixture(tmp_path / "catalog.sqlite3", rows=rows))


def test_verdict_cache_hit_match(tmp_path):
    cache = _seeded_cache(tmp_path)
    verdict = verify_lcsc_assignment(
        cache, None, "C98220", "10k", "R5", footprint="Resistor_SMD:R_0603_1608Metric"
    )
    assert (verdict.status, verdict.source, verdict.mismatch) == ("match", "parts cache", None)


def test_verdict_cache_hit_mismatch(tmp_path):
    cache = _seeded_cache(tmp_path, C25744_SPARSE)
    verdict = verify_lcsc_assignment(
        cache, None, "C25744", "10k", "R5", footprint="Resistor_SMD:R_0603_1608Metric"
    )
    assert verdict.status == "mismatch"
    assert verdict.mismatch is not None
    assert "0402" in verdict.mismatch.describe()


def test_verdict_both_miss_is_unverified(tmp_path):
    cache = PartsCache(db_path=tmp_path / "parts.db")
    verdict = verify_lcsc_assignment(
        cache, _catalog(tmp_path, []), "C25905", "10k", "R5", footprint="R_0603_1608Metric"
    )
    assert (verdict.status, verdict.source) == ("unverified", None)


def test_verdict_catalog_hit_validates_and_writes_nothing(tmp_path):
    """AC4: a cache miss falls back to the catalog ``package`` -- read-only."""
    cache = PartsCache(db_path=tmp_path / "parts.db")
    row = dict(
        _builder.SAMPLE_ROWS[0],
        lcsc=25744,
        mfr="0402WGF1002TCE",
        package="0402",
        description="10kΩ ±1% 62.5mW 0402 Thick Film Resistor",
    )
    catalog = _catalog(tmp_path, [row])

    bad = verify_lcsc_assignment(
        cache, catalog, "C25744", "10k", "R5", footprint="Resistor_SMD:R_0603_1608Metric"
    )
    good = verify_lcsc_assignment(
        cache, catalog, "C25744", "10k", "R5", footprint="Resistor_SMD:R_0402_1005Metric"
    )

    assert (bad.status, bad.source) == ("mismatch", "offline catalog")
    assert (good.status, good.source) == ("match", "offline catalog")
    assert cache.get("C25744", ignore_expiry=True) is None  # nothing written


def _checker(tmp_path, items, cache, catalog=None) -> PreflightChecker:
    checker = PreflightChecker(
        pcb_path=tmp_path / "board.kicad_pcb",
        config=PreflightConfig(skip_drc=True, skip_erc=True),
        parts_cache=cache,
        parts_catalog=catalog,
    )
    checker._bom = BOM(items=items)
    return checker


def _item(ref, value, footprint, lcsc) -> BOMItem:
    return BOMItem(reference=ref, value=value, footprint=footprint, lib_id="Device:R", lcsc=lcsc)


def test_empty_cache_and_no_catalog_never_reports_ok(tmp_path):
    cache = PartsCache(db_path=tmp_path / "parts.db")
    items = [
        _item("R1", "10k", "Resistor_SMD:R_0603_1608Metric", "C98220"),
        _item("C1", "100nF", "Capacitor_SMD:C_0603_1608Metric", "C14663"),
    ]

    result = _checker(tmp_path, items, cache)._check_bom_lcsc_values()

    assert result.status == "WARN"
    assert result.message.startswith("0 of 2 assigned item(s) verified")
    assert "R1 -> C98220" in result.details and "C1 -> C14663" in result.details
    # --strict-preflight promotes only FAIL: an unverifiable part never blocks.
    assert not PreflightChecker.has_failures([result])


def test_catalog_fallback_reported_as_source(tmp_path):
    cache = PartsCache(db_path=tmp_path / "parts.db")
    row = dict(_builder.SAMPLE_ROWS[0], lcsc=98220, mfr="RC0603FR-0710KL", package="0603")
    items = [_item("R1", "10k", "Resistor_SMD:R_0603_1608Metric", "C98220")]

    result = _checker(tmp_path, items, cache, _catalog(tmp_path, [row]))._check_bom_lcsc_values()

    assert result.status == "OK"
    assert "1 of 1 assigned item(s) verified against offline catalog" in result.message


def test_catalog_only_chip_mismatch_fails(tmp_path):
    cache = PartsCache(db_path=tmp_path / "parts.db")
    row = dict(_builder.SAMPLE_ROWS[0], lcsc=25744, mfr="0402WGF1002TCE", package="0402")
    items = [_item("R5", "10k", "Resistor_SMD:R_0603_1608Metric", "C25744")]

    result = _checker(tmp_path, items, cache, _catalog(tmp_path, [row]))._check_bom_lcsc_values()

    assert result.status == "FAIL"
    assert "[offline catalog]" in result.details


# ---------------------------------------------------------------------------
# The Hermit's three mutations of board 03 (scratch copy; committed board untouched)
# ---------------------------------------------------------------------------

BOARD03 = BOARDS / "03-usb-joystick" / "output"


def _mutated_board03(tmp_path: Path, swaps: dict[str, tuple[str, str]]) -> PreflightChecker:
    """Copy board 03's schematic + PCB and re-point LCSC on the given refs."""
    work = tmp_path / "board03"
    work.mkdir()
    pcb = shutil.copy(BOARD03 / "usb_joystick_routed.kicad_pcb", work)
    sch_text = (BOARD03 / "usb_joystick.kicad_sch").read_text()
    for ref, (old, new) in swaps.items():
        anchor = sch_text.index(f'(property "Reference" "{ref}"')
        at = sch_text.index('(property "LCSC" "', anchor)
        assert sch_text.index("\n\t(symbol", anchor) > at, f"LCSC for {ref} not in its block"
        old_prop, new_prop = f'(property "LCSC" "{old}"', f'(property "LCSC" "{new}"'
        assert sch_text.startswith(old_prop, at), sch_text[at : at + 40]
        sch_text = sch_text[:at] + new_prop + sch_text[at + len(old_prop) :]
    sch = work / "usb_joystick.kicad_sch"
    sch.write_text(sch_text)
    return PreflightChecker(
        pcb_path=pcb,
        schematic_path=sch,
        config=PreflightConfig(skip_drc=True, skip_erc=True),
        parts_cache=_seeded_cache(tmp_path, C25744_SPARSE),
    )


def _run_lcsc_check(checker: PreflightChecker):
    assert checker._check_pcb_parseable().status == "OK"
    return checker._check_bom_lcsc_values()


def test_unmutated_board03_is_ok(tmp_path):
    result = _run_lcsc_check(_mutated_board03(tmp_path, {}))
    assert result.status == "OK", result.details
    assert result.message.startswith("38 of 38 assigned item(s) verified")


def test_mutation1_r5_0402_part_on_0603_fails(tmp_path):
    result = _run_lcsc_check(_mutated_board03(tmp_path, {"R5": ("C98220", "C25744")}))
    assert result.status == "FAIL"
    assert "R5 (10k) -> C25744" in result.details
    assert "'0402'" in result.details and "'0603'" in result.details


def test_mutation2_r5_unknown_part_warns_and_names_it(tmp_path):
    result = _run_lcsc_check(_mutated_board03(tmp_path, {"R5": ("C98220", "C25905")}))
    assert result.status == "WARN"
    assert "37 of 38 assigned item(s) verified" in result.message
    assert "R5 -> C25905" in result.details


def test_mutation3_u2_sot25_regulator_on_sot23_6_fails(tmp_path):
    result = _run_lcsc_check(_mutated_board03(tmp_path, {"U2": ("C7519", "C51118")}))
    assert result.status == "FAIL"
    assert "U2" in result.details and "C51118" in result.details
    assert "pin count 5 vs 6 footprint pads" in result.details


@pytest.mark.parametrize(
    ("swaps", "needle"),
    [
        ({"R5": ("C98220", "C25744")}, "C25744"),
        ({"U2": ("C7519", "C51118")}, "C51118"),
    ],
    ids=["mutation1", "mutation3"],
)
def test_mutations_block_the_readiness_bom_gate(tmp_path, swaps, needle):
    """End to end: the real preflight result, recorded in a bundle manifest,
    must turn the readiness ``bom`` gate FAILED (it passed before #7166)."""
    from tests.test_kct_readiness_cmd import FakeEngines, check_status, make_board, run

    result = _run_lcsc_check(_mutated_board03(tmp_path, swaps))
    board = make_board(tmp_path / "readiness")
    fake = FakeEngines(board, lcsc_check=result.to_dict())

    code, report = run(board, fake)

    assert code != 0
    assert check_status(report, "bom") == readiness_cmd.FAILED
    assert any(needle in blocker for blocker in report["blockers"])


# ---------------------------------------------------------------------------
# AC7: no false positives across the fleet
# ---------------------------------------------------------------------------


def _board_files(board: str) -> tuple[Path, Path]:
    output = BOARDS / board / "output"
    pcb = next(output.glob("*_routed.kicad_pcb"))
    sch = next(p for p in output.glob("*.kicad_sch"))
    return pcb, sch


@pytest.mark.parametrize("board", FLEET_BOARDS)
def test_fleet_preflight_lcsc_check_is_ok(tmp_path, board):
    pcb, sch = _board_files(board)
    checker = PreflightChecker(
        pcb_path=pcb,
        schematic_path=sch,
        config=PreflightConfig(skip_drc=True, skip_erc=True),
        parts_cache=_seeded_cache(tmp_path),
    )
    result = _run_lcsc_check(checker)
    assert result.status == "OK", f"{result.message}: {result.details}"


@pytest.mark.parametrize("board", FLEET_BOARDS)
def test_fleet_committed_bom_rows_verify(tmp_path, board):
    """Every row of the committed ``bom_jlcpcb.csv`` -- including LCSC numbers
    only the export-time spec overlay assigns -- verifies with no mismatch."""
    pcb_path, _ = _board_files(board)
    bom_csv = BOARDS / board / "output" / "manufacturing" / "bom_jlcpcb.csv"
    pads: dict[str, list[str]] = {}
    for fp in PCB.load(str(pcb_path)).footprints:
        pads.setdefault(fp.reference, []).extend(str(p.number) for p in fp.pads)
    cache = _seeded_cache(tmp_path)

    checked = 0
    problems = []
    with bom_csv.open(newline="") as handle:
        for row in csv.DictReader(handle):
            for ref in [r.strip() for r in row["Designator"].split(",") if r.strip()]:
                verdict = verify_lcsc_assignment(
                    cache,
                    None,
                    row["LCSC Part #"].strip(),
                    row["Comment"],
                    ref,
                    footprint=row["Footprint"],
                    pad_numbers=pads.get(ref),
                )
                checked += 1
                if verdict.status != "match":
                    detail = verdict.mismatch.describe() if verdict.mismatch else ""
                    problems.append(f"{ref} {row['LCSC Part #']}: {verdict.status} {detail}")
    assert checked > 0
    assert problems == []


# ---------------------------------------------------------------------------
# Issue #7180: parts assigned only in the .kct spec are package-checked
# ---------------------------------------------------------------------------

BOARD00 = BOARDS / "00-simple-led" / "output"


def _board00_checker(tmp_path: Path, lcsc: str | None) -> PreflightChecker:
    """Scratch copy of board 00 whose .kct assigns ``lcsc`` to R1 (0805 part)."""
    work = tmp_path / "board00"
    work.mkdir()
    pcb = shutil.copy(BOARD00 / "simple_led.kicad_pcb", work)
    sch = shutil.copy(BOARD00 / "simple_led.kicad_sch", work)
    kct = (BOARDS / "00-simple-led" / "project.kct").read_text()
    if lcsc is not None:
        kct = kct.replace("lcsc: C17630", f"lcsc: {lcsc}", 1)
    (work / "project.kct").write_text(kct)
    return PreflightChecker(
        pcb_path=pcb,
        schematic_path=sch,
        config=PreflightConfig(skip_drc=True, skip_erc=True),
        parts_cache=_seeded_cache(tmp_path, C25744_SPARSE),
    )


def test_board00_kct_assigned_parts_are_checked_issue_7180(tmp_path):
    result = _run_lcsc_check(_board00_checker(tmp_path, None))
    assert result.message != "No assigned LCSC parts to validate"
    assert result.status in ("OK", "WARN"), result.details


def test_board00_kct_wrong_package_fails_and_blocks_readiness_issue_7180(tmp_path):
    from tests.test_kct_readiness_cmd import FakeEngines, check_status, make_board, run

    result = _run_lcsc_check(_board00_checker(tmp_path, "C25744"))
    assert result.status == "FAIL"
    assert "R1" in result.details and "C25744" in result.details

    board = make_board(tmp_path / "readiness")
    code, report = run(board, FakeEngines(board, lcsc_check=result.to_dict()))
    assert code != 0
    assert check_status(report, "bom") == readiness_cmd.FAILED


def test_malformed_kct_does_not_crash_preflight_issue_7180(tmp_path):
    checker = _board00_checker(tmp_path, None)
    (checker.pcb_path.parent / "project.kct").write_text("::: not [valid yaml")
    result = _run_lcsc_check(checker)
    assert result.status in ("OK", "WARN")
