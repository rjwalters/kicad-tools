"""Tests for the operating-state component-stress analyzer (issue #5039).

Covers the softstart IRFB4110 reproduction case (D=+90V, S=-169.3V ->
VDS=259.3V against a 100V-rated part), the floating-driver-domain translation
invariance, the UNRESOLVED contract (undeclared state, unresolved pin role,
unbound terminal, missing/unparseable/uncited rating), pin-role cache
invalidation on a part swap, creepage-waiver independence, and the
`kct analyze component-stress` CLI (text + json, exit codes).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.analysis import (
    REQUIRED_COVERAGE_STATES,
    ComponentStressAnalyzer,
    ComponentStressResult,
    OperatingStateManifest,
    normalize_state_name,
)
from kicad_tools.analysis.component_stress import PinRoleCache, resolve_pin_roles
from kicad_tools.cli.analyze_cmd import main as analyze_main

FIXTURES = Path(__file__).parent / "fixtures" / "component_stress"
SCH = FIXTURES / "mosfets.kicad_sch"
STATES = FIXTURES / "states.yaml"
STATES_PARTIAL = FIXTURES / "states_partial.json"


def _rows(
    results: list[ComponentStressResult],
) -> dict[tuple[str, str, str], ComponentStressResult]:
    return {(r.reference, r.state, r.check): r for r in results}


def _analyze(manifest: OperatingStateManifest, **kwargs) -> list[ComponentStressResult]:
    return ComponentStressAnalyzer(manifest, **kwargs).analyze(SCH)


@pytest.fixture
def manifest() -> OperatingStateManifest:
    return OperatingStateManifest.load(STATES)


# ---------------------------------------------------------------------------
# Manifest loading / state-name normalization
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("startup", "startup"),
        ("Mains-Negative", "mains_negative"),
        ("mains negative", "mains_negative"),
        ("  LOSS OF DRIVE  ", "loss_of_drive"),
        ("loss-of-drive", "loss_of_drive"),
        ("", ""),
    ],
)
def test_normalize_state_name(raw, expected):
    assert normalize_state_name(raw) == expected


def test_manifest_loads_yaml_with_metadata(manifest):
    assert set(manifest.states) == set(REQUIRED_COVERAGE_STATES)
    neg = manifest.get("mains-negative")  # normalization is applied on lookup
    assert neg is not None
    assert neg.potentials["SRC_POS"] == pytest.approx(-169.3)
    assert neg.source is not None and "rev-B" in neg.source
    assert neg.assumptions is not None


def test_manifest_loads_json_shorthand():
    partial = OperatingStateManifest.load(STATES_PARTIAL)
    assert set(partial.states) == {"startup", "mains_negative"}
    assert partial.get("mains_negative").potential("SRC_POS") == pytest.approx(-169.3)
    # The required checklist is still the full seven states.
    assert partial.required_states == REQUIRED_COVERAGE_STATES
    assert partial.coverage_states()[: len(REQUIRED_COVERAGE_STATES)] == list(
        REQUIRED_COVERAGE_STATES
    )


def test_manifest_net_lookup_tolerates_leading_slash_and_case():
    m = OperatingStateManifest.from_dict({"states": {"startup": {"SRC_POS": -5}}})
    state = m.get("startup")
    assert state.potential("SRC_POS") == -5
    assert state.potential("/SRC_POS") == -5
    assert state.potential("src_pos") == -5
    assert state.potential("OTHER") is None


def test_manifest_rejects_malformed_documents(tmp_path):
    with pytest.raises(ValueError):
        OperatingStateManifest.from_dict(["not", "a", "mapping"])
    with pytest.raises(ValueError):
        OperatingStateManifest.from_dict({"no_states_key": 1})
    with pytest.raises(ValueError):
        OperatingStateManifest.from_dict({"states": {"startup": {"A": "not-a-voltage"}}})
    with pytest.raises(FileNotFoundError):
        OperatingStateManifest.load(tmp_path / "missing.yaml")


def test_manifest_required_states_override():
    m = OperatingStateManifest.from_dict(
        {"required_states": ["startup", "Mains-Negative"], "states": {"startup": {"A": 0}}}
    )
    assert m.required_states == ("startup", "mains_negative")
    assert m.coverage_states() == ["startup", "mains_negative"]


# ---------------------------------------------------------------------------
# The reproduction case: IRFB4110 across a negative mains half-cycle
# ---------------------------------------------------------------------------
def test_irfb4110_fails_on_negative_mains_half_cycle(manifest):
    row = _rows(_analyze(manifest))[("Q1A", "mains_negative", "vds")]

    # D = BANK_POS = +90V, S = SRC_POS = -169.3V -> VDS = 259.3V.
    assert row.high_net == "BANK_POS"
    assert row.low_net == "SRC_POS"
    assert row.high_potential_v == pytest.approx(90.0)
    assert row.low_potential_v == pytest.approx(-169.3)
    assert row.stress_v == pytest.approx(259.3)
    assert row.rated_v == pytest.approx(100.0)
    assert row.margin_v == pytest.approx(-159.3)
    assert row.status == "FAIL"
    assert row.mpn == "IRFB4110PBF"
    assert row.source is not None and "Infineon" in row.source
    assert row.assumptions  # every row carries its qualifying assumptions


def test_adequately_rated_part_passes_only_that_state(manifest):
    rows = _rows(_analyze(manifest))

    # Q2A (600V rated) sees the same 259.3V and PASSes -- for THIS state.
    q2_neg = rows[("Q2A", "mains_negative", "vds")]
    assert q2_neg.status == "PASS"
    assert q2_neg.stress_v == pytest.approx(259.3)
    assert q2_neg.margin_v == pytest.approx(600.0 - 259.3)

    # The PASS is per-state, not a blanket pass: every other declared state is
    # judged on its own declared potentials.
    per_state = {state: rows[("Q2A", state, "vds")].stress_v for state in REQUIRED_COVERAGE_STATES}
    assert per_state["startup"] == pytest.approx(0.0)
    assert per_state["precharge"] == pytest.approx(75.0)
    assert per_state["mains_positive"] == pytest.approx(-79.3)
    assert len({round(v, 3) for v in per_state.values()}) > 1


def test_one_row_per_component_state_and_terminal_pair(manifest):
    results = _analyze(manifest)
    mosfets = {r.reference for r in results}
    assert mosfets == {"Q1A", "Q2A", "Q3A", "Q4A"}
    # 4 MOSFETs x 7 required states x 2 terminal pairs (VDS, VGS).
    assert len(results) == 4 * len(REQUIRED_COVERAGE_STATES) * 2
    assert {r.check for r in results} == {"vds", "vgs"}
    # No candidate/state combination is ever silently dropped.
    for ref in mosfets:
        for state in REQUIRED_COVERAGE_STATES:
            for check in ("vds", "vgs"):
                assert (ref, state, check) in _rows(results)


def test_row_carries_full_provenance(manifest):
    row = _rows(_analyze(manifest))[("Q1A", "mains_negative", "vds")]
    payload = row.to_dict()
    json.dumps(payload)  # must be JSON-serializable
    for key in (
        "reference",
        "mpn",
        "state",
        "check",
        "status",
        "high_terminal",
        "low_terminal",
        "high_net",
        "low_net",
        "stress_v",
        "rated_v",
        "margin_v",
        "assumptions",
        "source",
    ):
        assert key in payload
    assert payload["status"] in ("PASS", "FAIL", "UNRESOLVED")


def test_vgs_uses_gate_minus_source(manifest):
    row = _rows(_analyze(manifest))[("Q1A", "support", "vgs")]
    # support: GATE_A=92, SRC_POS=80 -> VGS = 12V against a 20V rating.
    assert row.high_terminal == "G"
    assert row.low_terminal == "S"
    assert row.stress_v == pytest.approx(12.0)
    assert row.status == "PASS"


# ---------------------------------------------------------------------------
# Correlated-state modelling: translation invariance
# ---------------------------------------------------------------------------
def _translate(manifest: OperatingStateManifest, offset: float) -> OperatingStateManifest:
    """Shift every declared node potential by *offset* volts."""
    return OperatingStateManifest.from_dict(
        {
            "required_states": list(manifest.required_states),
            "states": {
                name: {net: volts + offset for net, volts in state.potentials.items()}
                for name, state in manifest.states.items()
            },
        }
    )


def test_translating_the_whole_domain_leaves_vds_vgs_unchanged(manifest):
    """Shifting D, S and G together (a floating driver domain's reference moving)
    must not change either differential -- they are computed inside one state."""
    base = _rows(_analyze(manifest))
    shifted = _rows(_analyze(_translate(manifest, +500.0)))

    assert base.keys() == shifted.keys()
    for key, row in base.items():
        other = shifted[key]
        assert other.status == row.status, key
        if row.stress_v is None:
            assert other.stress_v is None, key
        else:
            assert other.stress_v == pytest.approx(row.stress_v), key
            assert other.margin_v == pytest.approx(row.margin_v), key

    # Spot-check the headline row survived the translation with identical stress.
    assert shifted[("Q1A", "mains_negative", "vds")].stress_v == pytest.approx(259.3)
    assert shifted[("Q1A", "mains_negative", "vds")].status == "FAIL"
    # ...while the absolute node potentials really did move.
    assert shifted[("Q1A", "mains_negative", "vds")].low_potential_v == pytest.approx(330.7)


def test_negative_offset_translation_is_also_invariant(manifest):
    base = _rows(_analyze(manifest))[("Q1A", "mains_negative", "vgs")]
    moved = _rows(_analyze(_translate(manifest, -1000.0)))[("Q1A", "mains_negative", "vgs")]
    assert moved.stress_v == pytest.approx(base.stress_v)
    assert moved.status == base.status


# ---------------------------------------------------------------------------
# UNRESOLVED contract
# ---------------------------------------------------------------------------
def test_missing_coverage_state_is_unresolved_not_omitted():
    partial = OperatingStateManifest.load(STATES_PARTIAL)
    rows = _rows(_analyze(partial))
    for state in ("precharge", "mains_positive", "support", "trip", "loss_of_drive"):
        row = rows[("Q1A", state, "vds")]
        assert row.status == "UNRESOLVED"
        assert "not declared" in (row.reason or "")
        assert row.stress_v is None
    # Declared states are still judged normally.
    assert rows[("Q1A", "mains_negative", "vds")].status == "FAIL"
    assert rows[("Q1A", "startup", "vds")].status == "PASS"


def test_unresolved_pin_role_is_unresolved(manifest):
    rows = _rows(_analyze(manifest))
    row = rows[("Q3A", "mains_negative", "vds")]
    assert row.status == "UNRESOLVED"
    assert "pin role" in (row.reason or "")
    assert row.stress_v is None


def test_uncited_rating_is_unresolved_never_a_silent_pass(manifest):
    row = _rows(_analyze(manifest))[("Q4A", "startup", "vds")]
    assert row.status == "UNRESOLVED"
    assert "source-backed" in (row.reason or "")
    # The computed stress is still reported so a reviewer can act on it.
    assert row.stress_v == pytest.approx(0.0)
    assert row.rated_v == pytest.approx(500.0)


def test_allow_uncited_ratings_opt_in(manifest):
    rows = _rows(_analyze(manifest, require_rating_source=False))
    row = rows[("Q4A", "mains_negative", "vds")]
    # 500V rating vs 259.3V stress -> PASS once the citation requirement is relaxed.
    assert row.status == "PASS"
    assert any("no source citation" in a for a in row.assumptions)


def test_missing_rating_field_is_unresolved_never_a_default(tmp_path):
    """A MOSFET with no Vds_max field must never inherit a built-in default."""
    sch = tmp_path / "norating.kicad_sch"
    sch.write_text(SCH.read_text().replace('(property "Vds_max" "100V"', '(property "Vdsx" "100V"'))
    manifest = OperatingStateManifest.load(STATES)
    row = _rows(ComponentStressAnalyzer(manifest).analyze(sch))[("Q1A", "mains_negative", "vds")]
    assert row.status == "UNRESOLVED"
    assert "Vds_max" in (row.reason or "")
    assert row.rated_v is None
    # The differential was still computed; only the rating is missing.
    assert row.stress_v == pytest.approx(259.3)


def test_unparseable_rating_field_is_unresolved(tmp_path):
    sch = tmp_path / "badrating.kicad_sch"
    sch.write_text(
        SCH.read_text().replace('(property "Vds_max" "100V"', '(property "Vds_max" "junk"')
    )
    manifest = OperatingStateManifest.load(STATES)
    row = _rows(ComponentStressAnalyzer(manifest).analyze(sch))[("Q1A", "startup", "vds")]
    assert row.status == "UNRESOLVED"
    assert "unparseable" in (row.reason or "")


def test_missing_gate_potential_is_unresolved(manifest):
    """A state that declares D and S but not the gate net leaves VGS unresolved."""
    partial = OperatingStateManifest.from_dict(
        {
            "required_states": ["mains_negative"],
            "states": {"mains_negative": {"BANK_POS": 90, "SRC_POS": -169.3}},
        }
    )
    rows = _rows(_analyze(partial))
    assert rows[("Q1A", "mains_negative", "vds")].status == "FAIL"
    vgs = rows[("Q1A", "mains_negative", "vgs")]
    assert vgs.status == "UNRESOLVED"
    assert "GATE_A" in (vgs.reason or "")
    assert "no declared potential" in (vgs.reason or "")


def test_unbound_terminal_is_unresolved(manifest, tmp_path):
    """A terminal with no net binding resolves to UNRESOLVED, not a pass."""
    from kicad_tools.analysis.component_stress import ComponentStressAnalyzer as CSA

    analyzer = CSA(manifest)
    row = analyzer._evaluate(
        sym=_FakeSymbol(),
        ref="Q9",
        mpn="FAKE",
        rating_source="datasheet",
        roles=_FakeRoles(),
        pin_nets={"1": "GATE_A", "3": "SRC_POS"},  # pin 2 (drain) unbound
        state_name="mains_negative",
        state=manifest.get("mains_negative"),
        check="vds",
        high_role="D",
        low_role="S",
    )
    assert row.status == "UNRESOLVED"
    assert "not bound to a net" in (row.reason or "")


class _FakeRoles:
    def pin(self, role: str) -> str:
        return {"D": "2", "G": "1", "S": "3"}[role]

    origin = "test"


class _FakeSymbol:
    reference = "Q9"
    lib_id = "Device:Q_NMOS_GDS"
    footprint = ""
    properties: dict = {}

    def get_property(self, name: str):
        return None


# ---------------------------------------------------------------------------
# Independence from creepage / spacing waivers
# ---------------------------------------------------------------------------
def test_creepage_waiver_cannot_suppress_a_failure(manifest, tmp_path):
    """A footprint creepage waiver must not downgrade a device-stress FAIL.

    Q2A already carries a `Creepage_Waiver` field in the fixture; adding one to
    the failing Q1A too must leave its verdict untouched -- the two checks read
    disjoint inputs by construction.
    """
    baseline = _rows(_analyze(manifest))[("Q1A", "mains_negative", "vds")]
    assert baseline.status == "FAIL"

    waived = tmp_path / "waived.kicad_sch"
    waived.write_text(
        SCH.read_text().replace(
            '(property "Vds_max" "100V"',
            '(property "Creepage_Waiver" "waived per fab review"\n\t\t\t(at 104 78 0)\n\t\t)\n'
            '\t\t(property "Vds_max" "100V"',
            1,
        )
    )
    row = _rows(ComponentStressAnalyzer(manifest).analyze(waived))[("Q1A", "mains_negative", "vds")]
    assert row.status == "FAIL"
    assert row.stress_v == pytest.approx(baseline.stress_v)
    # The waived part (Q2A) is still judged on its own rating, not the waiver.
    assert _rows(_analyze(manifest))[("Q2A", "mains_negative", "vds")].rated_v == 600.0


# ---------------------------------------------------------------------------
# Pin-role resolution + cache invalidation
# ---------------------------------------------------------------------------
def _load_symbols():
    from kicad_tools.schema.schematic import Schematic

    sch = Schematic.load(SCH)
    return sch, {s.reference: s for s in sch.symbols if s.reference}


def test_pin_roles_from_library_pin_names():
    sch, symbols = _load_symbols()
    roles = resolve_pin_roles(symbols["Q1A"], sch.get_lib_symbol_resolved("Device:Q_NMOS_GDS"))
    assert roles is not None
    assert roles.roles == {"G": "1", "D": "2", "S": "3"}
    assert "pin names" in roles.origin


def test_pin_roles_unresolved_without_names_or_suffix():
    sch, symbols = _load_symbols()
    lib = sch.get_lib_symbol_resolved("Device:Q_NMOS_Generic")
    assert resolve_pin_roles(symbols["Q3A"], lib) is None


def test_pin_roles_from_lib_id_suffix_when_names_are_unhelpful():
    """A `_GDS` pin-order suffix resolves roles when pin names do not."""

    class _Sym:
        reference = "Q7"
        lib_id = "Device:Q_NMOS_GDS"
        footprint = ""
        properties: dict = {}

        def get_property(self, name):
            return None

    class _Pin:
        def __init__(self, number):
            self.number = number
            self.name = "~"

    class _Lib:
        pins = [_Pin("1"), _Pin("2"), _Pin("3")]

    roles = resolve_pin_roles(_Sym(), _Lib())
    assert roles is not None
    assert roles.roles == {"G": "1", "D": "2", "S": "3"}
    assert "suffix" in roles.origin


def test_pin_role_cache_hits_for_an_unchanged_part():
    sch, symbols = _load_symbols()
    lib = sch.get_lib_symbol_resolved("Device:Q_NMOS_GDS")
    cache = PinRoleCache()
    first = cache.resolve(symbols["Q1A"], lib)
    second = cache.resolve(symbols["Q1A"], lib)
    assert first is second
    assert cache.misses == 1
    assert cache.hits == 1
    assert cache.invalidations == 0


@pytest.mark.parametrize("changed_field", ["MPN", "Footprint"])
def test_pin_role_cache_invalidated_by_part_swap(changed_field):
    """Changing the MPN or footprint must drop any cached D/G/S mapping."""
    sch, symbols = _load_symbols()
    lib = sch.get_lib_symbol_resolved("Device:Q_NMOS_GDS")
    sym = symbols["Q1A"]

    cache = PinRoleCache()
    stale_key = cache.identity(sym)
    cache.resolve(sym, lib)
    assert cache.misses == 1
    assert stale_key in cache._entries

    # Swap the part: same reference designator, different physical device.
    sym.properties[changed_field].value = "SOMETHING-ELSE"

    cache.resolve(sym, lib)
    assert cache.invalidations == 1
    assert cache.misses == 2  # recomputed, not served from the stale entry
    assert cache.hits == 0
    assert stale_key not in cache._entries  # the stale mapping is gone


def test_explicit_pin_fields_win_over_library_names():
    class _Sym:
        reference = "Q8"
        lib_id = "Device:Q_NMOS_GDS"
        footprint = ""
        properties: dict = {}

        def get_property(self, name):
            return {"Pin_D": "5", "Pin_G": "4", "Pin_S": "6"}.get(name)

    class _Pin:
        def __init__(self, number, name):
            self.number = number
            self.name = name

    class _Lib:
        pins = [_Pin("1", "G"), _Pin("2", "D"), _Pin("3", "S")]

    roles = resolve_pin_roles(_Sym(), _Lib())
    assert roles is not None
    assert roles.roles == {"D": "5", "G": "4", "S": "6"}
    assert "explicit" in roles.origin


# ---------------------------------------------------------------------------
# Advisory robustness contract
# ---------------------------------------------------------------------------
def test_analyzer_never_raises_on_missing_file(manifest):
    assert ComponentStressAnalyzer(manifest).analyze(FIXTURES / "nope.kicad_sch") == []


def test_analyzer_never_raises_on_garbage(manifest, tmp_path):
    junk = tmp_path / "junk.kicad_sch"
    junk.write_text("this is not a schematic")
    assert ComponentStressAnalyzer(manifest).analyze(junk) == []


def test_analyzer_never_infers_states(manifest):
    """With an empty manifest every row is UNRESOLVED -- nothing is synthesized."""
    empty = OperatingStateManifest.from_dict({"states": {}})
    results = ComponentStressAnalyzer(empty).analyze(SCH)
    assert results
    assert {r.status for r in results} == {"UNRESOLVED"}
    assert all("not declared" in (r.reason or "") for r in results)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def test_cli_text_fail_exit_code(capsys):
    rc = analyze_main(["component-stress", str(SCH), "--states", str(STATES)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "Q1A" in out
    assert "FAIL" in out
    assert "unresolved=28" in out.replace("\n", "")


def test_cli_json_summary_and_census(capsys):
    rc = analyze_main(["component-stress", str(SCH), "--states", str(STATES), "--format", "json"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["fail"] == 1
    assert payload["summary"]["total"] == 56
    assert payload["parameters"]["missing_states"] == []
    assert sorted(payload["parameters"]["declared_states"]) == sorted(REQUIRED_COVERAGE_STATES)
    failing = [r for r in payload["census"] if r["status"] == "FAIL"]
    assert len(failing) == 1
    assert failing[0]["reference"] == "Q1A"
    assert failing[0]["state"] == "mains_negative"
    assert failing[0]["stress_v"] == pytest.approx(259.3)
    assert failing[0]["rated_v"] == 100.0


def test_cli_reports_coverage_gap(capsys):
    rc = analyze_main(
        ["component-stress", str(SCH), "--states", str(STATES_PARTIAL), "--format", "json"]
    )
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert set(payload["parameters"]["missing_states"]) == {
        "precharge",
        "mains_positive",
        "support",
        "trip",
        "loss_of_drive",
    }
    assert payload["summary"]["unresolved"] > 0


def test_cli_allow_unresolved_still_gates_on_fail(capsys):
    rc = analyze_main(["component-stress", str(SCH), "--states", str(STATES), "--allow-unresolved"])
    capsys.readouterr()
    assert rc == 1  # the Q1A FAIL still gates


def test_cli_unresolved_alone_gates_by_default(capsys, tmp_path):
    """A schematic with no FAIL but an unresolved row still exits non-zero."""
    states = tmp_path / "only_startup.json"
    states.write_text(
        json.dumps({"required_states": ["startup"], "states": {"startup": {"BANK_POS": 0}}})
    )
    rc = analyze_main(["component-stress", str(SCH), "--states", str(states), "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["fail"] == 0
    assert payload["summary"]["unresolved"] > 0
    assert rc == 1

    rc = analyze_main(
        [
            "component-stress",
            str(SCH),
            "--states",
            str(states),
            "--format",
            "json",
            "--allow-unresolved",
        ]
    )
    capsys.readouterr()
    assert rc == 0


def test_cli_missing_schematic(capsys):
    rc = analyze_main(
        ["component-stress", str(FIXTURES / "nope.kicad_sch"), "--states", str(STATES)]
    )
    assert rc == 1
    assert "File not found" in capsys.readouterr().err


def test_cli_bad_suffix(capsys, tmp_path):
    bad = tmp_path / "board.kicad_pcb"
    bad.write_text("")
    rc = analyze_main(["component-stress", str(bad), "--states", str(STATES)])
    assert rc == 1
    assert "kicad_sch" in capsys.readouterr().err


def test_cli_missing_manifest(capsys, tmp_path):
    rc = analyze_main(["component-stress", str(SCH), "--states", str(tmp_path / "none.yaml")])
    assert rc == 1
    assert "manifest not found" in capsys.readouterr().err


def test_cli_malformed_manifest(capsys, tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("states:\n  startup:\n    A: not-a-voltage\n")
    rc = analyze_main(["component-stress", str(SCH), "--states", str(bad)])
    assert rc == 1
    assert "invalid operating-state manifest" in capsys.readouterr().err


def test_cli_via_kct_dispatch(capsys):
    # Exercise the real parser -> commands.analyze dispatch path.
    from kicad_tools.cli.commands.analyze import run_analyze_command
    from kicad_tools.cli.parser import create_parser

    parser = create_parser()
    args = parser.parse_args(
        ["analyze", "component-stress", str(SCH), "--states", str(STATES), "--format", "json"]
    )
    rc = run_analyze_command(args)
    assert rc == 1
    assert '"mains_negative"' in capsys.readouterr().out
