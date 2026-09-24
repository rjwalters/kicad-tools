"""Measured object/layer controls for the complete generated factory rules."""

import functools
import json
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from kicad_tools.cli.runner import find_kicad_cli
from kicad_tools.export.gerber import get_kicad_cli_version
from kicad_tools.manufacturers import get_profile, write_drc_constraints
from kicad_tools.manufacturers.dru_generator import SMD_PAD_CLEARANCE_MIN_KICAD_VERSION
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.rules.clearance import ClearanceRule
from kicad_tools.validate.rules.factory_clearance import check_silk_pad_clearance
from tests._native_drc_findings import findings_for_rules


def board_fixture(path, kind, gap, *, layer="F.Cu", same_net=False, pad_type="smd", masked=True):
    net = 1 if same_net else 2
    if kind in ("pth", "via"):
        pad = (
            '(pad "1" thru_hole circle (at 0 0) (size .7 .7) (drill .4) '
            '(layers "*.Cu" "*.Mask") (net 1 "A"))'
        )
        first = f'(footprint "T" (layer "F.Cu") (at 10 10) {_REF_TEXT[0]} {pad})'
        if kind == "via":
            first = '(via (at 10 10) (size .7) (drill .4) (layers "F.Cu" "B.Cu") (net 1))'
        x = 10 + 0.2 + gap + 0.06
        other = f'(segment (start {x} 9) (end {x} 11) (width .12) (layer "{layer}") (net {net}))'
    else:
        mask = ' "F.Mask"' if masked else ""
        drill = "(drill .4)" if pad_type == "thru_hole" else ""
        first = f'(footprint "T" (layer "F.Cu") (at 10 10) {_REF_TEXT[0]} (pad "1" {pad_type} rect (at 0 0) (size 1 1) {drill} (layers "F.Cu"{mask}) (net 1 "A")))'
        if kind == "silk":
            x = 10.5 + gap + 0.075
            silk_layer = "F.SilkS" if layer == "F.Cu" else "B.SilkS"
            other = f'(gr_line (start {x} 9) (end {x} 11) (stroke (width .15) (type default)) (layer "{silk_layer}"))'
        else:
            other = f'(footprint "T" (layer "{layer}") (at {11 + gap} 10) {_REF_TEXT[1]} (pad "2" smd rect (at 0 0) (size 1 1) (layers "{layer}") (net {net} "{"A" if same_net else "B"}")))'
    path.write_text(f"""(kicad_pcb (version 20240108) (generator pcbnew)
      (general (thickness 1.6)) (paper "A4")
      (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal)
       (31 "B.Cu" signal) (36 "B.SilkS" user) (37 "F.SilkS" user)
       (38 "B.Mask" user) (39 "F.Mask" user) (44 "Edge.Cuts" user))
      (setup (pad_to_mask_clearance 0)) (net 0 "") (net 1 "A") (net 2 "B")
      (gr_rect (start 0 0) (end 20 20) (stroke (width .1) (type default))
       (fill none) (layer "Edge.Cuts")) {first} {other})""")
    return path


#: Reference designators for the fixture's two independently placed
#: footprints.  KiCad 10 documents that footprint children (pads) carry
#: their parent footprint's ``Reference`` (pcbnew manual, custom design
#: rules -> footprint properties), which is what the native
#: different-footprint scope keys on -- so the fixture models a realistic
#: placed board, where every footprint has a reference.  The texts sit on
#: opposite sides of their footprints so they never overlap each other or
#: the probe gap and become findings of their own.
_REF_TEXT = (
    '(fp_text reference "U1" (at 0 -2.5) (layer "F.SilkS")'
    " (effects (font (size 1 1) (thickness 0.15))))",
    '(fp_text reference "U2" (at 0 2.5) (layer "F.SilkS")'
    " (effects (font (size 1 1) (thickness 0.15))))",
)


CASES = [
    ("silk", 0.085, {}, True),
    ("silk", 0.16, {}, False),
    ("silk", 0.085, {"layer": "B.Cu"}, False),
    ("silk", 0.085, {"masked": False}, True),
    ("smd", 0.12, {}, True),
    ("smd", 0.16, {}, False),
    ("smd", 0.12, {"same_net": True}, False),
    ("smd", 0.12, {"layer": "B.Cu"}, False),
    ("smd", 0.12, {"pad_type": "thru_hole"}, False),
    ("pth", 0.27, {}, True),
    ("pth", 0.29, {}, False),
    ("pth", 0.29, {"layer": "In1.Cu"}, True),
    ("pth", 0.31, {"layer": "In1.Cu"}, False),
    ("pth", 0.27, {"same_net": True}, False),
    ("via", 0.27, {}, False),
    ("via", 0.29, {"layer": "In1.Cu"}, False),
]


@pytest.mark.parametrize("kind,gap,options,expected", CASES)
def test_python_object_specific_clearance(tmp_path, kind, gap, options, expected):
    path = board_fixture(tmp_path / "probe.kicad_pcb", kind, gap, **options)
    pcb = PCB.load(path)
    rules = get_profile("jlcpcb").get_design_rules(layers=4, copper_oz=1)
    results = (
        check_silk_pad_clearance(pcb, rules)
        if kind == "silk"
        else ClearanceRule().check(pcb, rules)
    )
    relevant = (
        [v for v in results.violations if v.rule_id == "pth_hole_clearance"]
        if kind in ("pth", "via")
        else results.violations
    )
    assert bool(relevant) == expected, relevant
    if expected:
        assert relevant[0].actual_value == pytest.approx(gap, abs=0.0001)
        assert relevant[0].severity == "error"


@pytest.mark.parametrize("kind,gap,options,expected", CASES)
def test_native_object_specific_clearance(tmp_path, kind, gap, options, expected):
    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("Native KiCad CLI is not installed")
    path = board_fixture(tmp_path / "probe.kicad_pcb", kind, gap, **options)
    rules = get_profile("jlcpcb").get_design_rules(layers=4, copper_oz=1)
    write_drc_constraints(path, rules, manufacturer_id="jlcpcb", layers=4)
    report = tmp_path / "native.json"
    # ``--severity-all`` is REQUIRED here: the ``Silk to Pad`` rule no longer
    # forces ``(severity error)``, so its violations now carry KiCad's default
    # ``silk_over_copper`` severity (warning) and would be filtered out of the
    # report by kicad-cli's default error-only severity mask.  Asking for all
    # severities keeps this test's verdict independent of that default and lets
    # it assert the per-rule severity explicitly below.
    proc = subprocess.run(
        [
            str(cli),
            "pcb",
            "drc",
            "--severity-all",
            "--format",
            "json",
            "-o",
            str(report),
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    violations = json.loads(report.read_text())["violations"]
    assert not [v for v in violations if v["type"] == "drc_rule_error"], violations
    if kind == "smd":
        # Issue #5713: below KiCad 10.0.2 the rule is emitted but never
        # evaluated, which turns ``expected=True`` into a bare
        # ``AssertionError: []`` and ``expected=False`` into a pass for the
        # wrong reason.  Gate on a measured positive control, not on luck.
        _require_native_smd_rule()
        # The different-net SMD pad floor is emitted natively, scoped by the
        # two predicates the deferral in #5705 thought impossible:
        # ``A.Pad_Type == 'SMD'`` on both sides and
        # ``A.Reference != B.Reference`` -- footprint children carry their
        # parent's Reference (KiCad 10 custom-rules docs, footprint
        # properties), so package-internal pairs compare equal and stay
        # exempt.  ``expected`` describes the ``kct check`` verdict; the
        # native rule agrees on every case in this matrix because the
        # fixture gives its two footprints distinct references.
        relevant = findings_for_rules(violations, "SMD Pad Clearance")
        assert bool(relevant) == expected, violations
        if expected:
            assert relevant[0]["severity"] == "error"
        return
    names = {
        "silk": ("Silk to Pad",),
        "pth": ("PTH Hole to Track", "Inner PTH Hole to Copper"),
        "via": ("PTH Hole to Track", "Inner PTH Hole to Copper"),
    }[kind]
    relevant = findings_for_rules(violations, *names)
    assert bool(relevant) == expected, violations
    if expected:
        # ``Silk to Pad`` deliberately carries NO ``(severity error)``
        # override: KiCad classifies ``silk_over_copper`` as a warning by
        # default, and the factory limit is a legibility/DFM limit (JLCPCB
        # clips encroaching silkscreen) rather than a fabrication stop.  The
        # PTH hole rules ARE hard errors.
        expected_severity = "warning" if kind == "silk" else "error"
        assert relevant[0]["severity"] == expected_severity


def test_optional_constraints_preserve_other_profiles_and_stricter_general_clearance(tmp_path):
    from kicad_tools.manufacturers.dru_generator import generate_dru

    rules = get_profile("jlcpcb").get_design_rules(layers=4, copper_oz=1)
    emitted = generate_dru(rules)
    assert "Silk to Pad" in emitted
    assert "PTH Hole to Track" in emitted
    assert "Inner PTH Hole to Copper" in emitted
    # The different-net SMD pad floor IS emitted natively, scoped to pads of
    # different footprints via ``A.Reference != B.Reference``: KiCad 10
    # documents that footprint children (pads) carry their parent's
    # Reference, so package-internal pairs compare equal and stay exempt --
    # the QFP/QFN wall that kept this rule Python-only before.  The Python
    # floor remains authoritative where the string key cannot discriminate
    # (blank or duplicated reference designators); see
    # ``test_native_smd_floor_omits_pairs_sharing_a_reference_designator``.
    assert "SMD Pad Clearance" in emitted
    assert "A.Reference != B.Reference" in emitted
    assert "A.Pad_Type == 'SMD'" in emitted
    legacy = replace(
        rules,
        min_silk_to_pad_clearance_mm=None,
        min_smd_pad_clearance_mm=None,
        min_pth_hole_to_track_mm=None,
        min_inner_pth_hole_to_copper_mm=None,
    )
    assert "Silk to Pad" not in generate_dru(legacy)
    assert "SMD Pad Clearance" not in generate_dru(legacy)
    strict = replace(rules, min_clearance_mm=0.2)
    path = board_fixture(tmp_path / "probe.kicad_pcb", "smd", 0.18)
    violations = ClearanceRule().check(PCB.load(path), strict).violations
    assert violations and violations[0].required_value == 0.2
    assert "(constraint clearance (min 0.2mm))" in generate_dru(strict)


def _two_pad_board(path, gap, *, same_footprint, references=("U1", "U2")):
    """Two different-net SMD pads ``gap`` apart, in one or two footprints.

    ``references`` names the two footprints' reference designators.  The
    default ``("U1", "U2")`` models a realistic placed board and is what
    the native ``A.Reference`` scope keys on; passing blank or identical
    strings exercises the documented boundary where the native rule
    cannot discriminate and the Python identity-scoped floor stands alone.
    """
    pad_a = '(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "A"))'
    pad_b = f'(pad "2" smd rect (at {1 + gap} 0) (size 1 1) (layers "F.Cu") (net 2 "B"))'
    ref_a = f'(fp_text reference "{references[0]}" (at 0 -2.5) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))'
    if same_footprint:
        # One placed footprint owning both pads -- package-internal geometry.
        # Both pads necessarily share the parent's single reference.
        bodies = f'(footprint "T" (layer "F.Cu") (at 10 10) {ref_a} {pad_a} {pad_b})'
    else:
        # Two independently placed footprints.  With blank references this
        # pins the Python scope to footprint IDENTITY (which is neither
        # unique nor guaranteed present as a string); with distinct
        # references it is also the native rule's case.
        ref_b = f'(fp_text reference "{references[1]}" (at 0 -2.5) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))'
        pad_b_local = '(pad "2" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 2 "B"))'
        bodies = (
            f'(footprint "T" (layer "F.Cu") (at 10 10) {ref_a} {pad_a}) '
            f'(footprint "T" (layer "F.Cu") (at {11 + gap} 10) {ref_b} {pad_b_local})'
        )
    path.write_text(f"""(kicad_pcb (version 20240108) (generator pcbnew)
      (general (thickness 1.6)) (paper "A4")
      (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
      (setup (pad_to_mask_clearance 0)) (net 0 "") (net 1 "A") (net 2 "B")
      (gr_rect (start 0 0) (end 20 20) (stroke (width .1) (type default))
       (fill none) (layer "Edge.Cuts")) {bodies})""")
    return path


@pytest.mark.parametrize("same_footprint,expected", [(False, True), (True, False)])
def test_python_smd_floor_is_scoped_to_different_footprints(tmp_path, same_footprint, expected):
    """The 0.15 mm SMD floor is a PLACEMENT limit, not a package-geometry one.

    A gap of 0.12 mm clears the general copper floor (0.1016 mm) but is under
    the different-net SMD pad floor (0.15 mm).  Between two independently
    placed footprints that is a real, fixable violation.  Between two pads of
    the SAME footprint it is vendor-fixed package geometry no placement or
    routing change can alter -- and stock fine-pitch packages routinely sit
    below the floor (``Package_QFP:LQFP-48_7x7mm_P0.5mm`` has a 0.1414 mm
    diagonal gap between adjacent pad rows, board 06's U3 pads 24/36 and
    25/37), so flagging it would declare every such package unmanufacturable.

    The two-footprint board deliberately gives both footprints a BLANK
    reference, pinning the Python scope to footprint IDENTITY rather than to
    the reference string (which is neither unique nor guaranteed present).
    """
    path = _two_pad_board(
        tmp_path / "probe.kicad_pcb", 0.12, same_footprint=same_footprint, references=("", "")
    )
    rules = get_profile("jlcpcb").get_design_rules(layers=4, copper_oz=1)
    assert rules.min_smd_pad_clearance_mm == 0.15
    violations = ClearanceRule().check(PCB.load(path), rules).violations
    assert bool(violations) == expected, violations
    if expected:
        assert violations[0].required_value == pytest.approx(0.15)


def _run_native_drc(path: Path) -> list[dict]:
    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("Native KiCad CLI is not installed")
    rules = get_profile("jlcpcb").get_design_rules(layers=4, copper_oz=1)
    write_drc_constraints(path, rules, manufacturer_id="jlcpcb", layers=4)
    report = path.parent / "native.json"
    proc = subprocess.run(
        [
            str(cli),
            "pcb",
            "drc",
            "--severity-all",
            "--format",
            "json",
            "-o",
            str(report),
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    violations = json.loads(report.read_text())["violations"]
    assert not [v for v in violations if v["type"] == "drc_rule_error"], violations
    return violations


# ---------------------------------------------------------------------------
# ``SMD Pad Clearance`` availability gate (Issue #5713)
# ---------------------------------------------------------------------------
#
# The emitted ``SMD Pad Clearance`` rule is SILENTLY INERT on KiCad 10.0.0 and
# 10.0.1: its ``A.Reference != B.Reference`` scope needs pads to inherit their
# parent footprint's reference designator, which those builds do not do.  The
# engine raises no ``drc_rule_error`` -- it just reports a clean board.  Every
# assertion below that says "this shape produces no SMD finding" would
# therefore pass for the wrong reason, and every assertion that says "this
# shape produces one" would fail as a bare ``AssertionError: []`` naming
# nothing.  Both are replaced by an explicit, measured capability probe.

#: Sub-floor gap (mm) for the SMD positive control: clears the general copper
#: floor (0.1016 mm) but is under the 0.15 mm different-net SMD pad floor, so
#: a working ``SMD Pad Clearance`` rule MUST report it.
_SMD_POSITIVE_CONTROL_GAP_MM = 0.12

_MIN_KICAD_VERSION_STR = ".".join(str(part) for part in SMD_PAD_CLEARANCE_MIN_KICAD_VERSION)


def _kicad_cli_version_tuple(cli: Path) -> tuple[int, ...] | None:
    """Parse ``kicad-cli version`` into a comparable tuple, or ``None``."""
    raw = get_kicad_cli_version(cli)
    if not raw:
        return None
    head = raw.split()[0].split("-")[0].split("~")[0]
    parts: list[int] = []
    for chunk in head.split("."):
        if not chunk.isdigit():
            break
        parts.append(int(chunk))
    return tuple(parts) or None


def _native_smd_rule_fires(cli: Path, workdir: Path) -> bool:
    """Run the SMD positive control and report whether the rule fired.

    The control board is the exact shape
    ``test_native_smd_floor_is_scoped_to_different_footprints[False-True]``
    asserts on: two independently placed footprints with DISTINCT reference
    designators, at a sub-floor gap.  If ``SMD Pad Clearance`` is evaluated at
    all, it fires here.
    """
    path = _two_pad_board(
        workdir / "smd-positive-control.kicad_pcb",
        _SMD_POSITIVE_CONTROL_GAP_MM,
        same_footprint=False,
    )
    rules = get_profile("jlcpcb").get_design_rules(layers=4, copper_oz=1)
    write_drc_constraints(path, rules, manufacturer_id="jlcpcb", layers=4)
    report = workdir / "smd-positive-control.json"
    proc = subprocess.run(
        [
            str(cli),
            "pcb",
            "drc",
            "--severity-all",
            "--format",
            "json",
            "-o",
            str(report),
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    violations = json.loads(report.read_text())["violations"]
    # A rejected sidecar would make the probe report "unavailable" for a
    # completely different reason; surface it instead of masking it.
    assert not [v for v in violations if v["type"] == "drc_rule_error"], violations
    return bool(findings_for_rules(violations, "SMD Pad Clearance"))


@functools.lru_cache(maxsize=1)
def _native_smd_rule_status() -> tuple[bool, str]:
    """``(fires, description)`` for the installed kicad-cli, probed once."""
    cli = find_kicad_cli()
    if cli is None:
        return False, "Native KiCad CLI is not installed"
    version = get_kicad_cli_version(cli) or "an unknown version"
    with tempfile.TemporaryDirectory(prefix="kct-smd-probe-") as raw:
        fires = _native_smd_rule_fires(cli, Path(raw))
    return fires, f"kicad-cli {version}"


def _require_native_smd_rule() -> None:
    """Skip -- naming the rule -- when the native engine will not evaluate it.

    Never let a caller fall through to an assertion that an absent rule would
    satisfy vacuously.  The reason string names the rule, the probe that was
    run, and the measured version floor, so a skipped run is diagnosable from
    the pytest report alone rather than surfacing as ``AssertionError: []``.
    """
    fires, description = _native_smd_rule_status()
    if fires:
        return
    if find_kicad_cli() is None:
        pytest.skip("Native KiCad CLI is not installed")
    pytest.skip(
        f"The native 'SMD Pad Clearance' rule is not evaluated by {description}: a "
        f"positive control (two distinct-reference footprints at "
        f"{_SMD_POSITIVE_CONTROL_GAP_MM} mm, under the 0.15 mm floor) produced no "
        f"finding. KiCad >= {_MIN_KICAD_VERSION_STR} is required -- 10.0.0/10.0.1 do "
        "not give a pad its parent footprint's Reference, so the rule's "
        "'A.Reference != B.Reference' scope is permanently false."
    )


def test_native_smd_pad_clearance_rule_is_available():
    """The ``SMD Pad Clearance`` minimum-KiCad-version dependency, asserted.

    Every other native SMD assertion in this module is gated on this same
    probe and SKIPS below the floor, so without this test an old KiCad would
    make the whole SMD surface quietly disappear from the report.  Here the
    dependency is explicit: at or above the recorded floor the rule MUST fire,
    and a regression fails by name rather than as an empty list.
    """
    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("Native KiCad CLI is not installed")
    version = _kicad_cli_version_tuple(cli)
    fires, description = _native_smd_rule_status()
    if version is not None and version < SMD_PAD_CLEARANCE_MIN_KICAD_VERSION:
        assert not fires, (
            f"{description} evaluates 'SMD Pad Clearance' below the recorded floor of "
            f"{_MIN_KICAD_VERSION_STR}; lower SMD_PAD_CLEARANCE_MIN_KICAD_VERSION."
        )
        pytest.skip(
            f"{description} is below the measured {_MIN_KICAD_VERSION_STR} floor for the "
            "native 'SMD Pad Clearance' rule (pads gained their parent footprint's "
            "Reference in 10.0.2); the rule is emitted but inert here."
        )
    assert fires, (
        f"{description} is at or above the recorded {_MIN_KICAD_VERSION_STR} floor but the "
        "emitted 'SMD Pad Clearance' rule did not fire on a 0.12 mm sub-floor positive "
        "control. Either the rule is no longer emitted, or KiCad regressed the "
        "'A.Reference' property that scopes it."
    )


@pytest.mark.parametrize("same_footprint,expected", [(False, True), (True, False)])
def test_native_smd_floor_is_scoped_to_different_footprints(tmp_path, same_footprint, expected):
    """The emitted ``SMD Pad Clearance`` rule exempts package-internal pairs.

    Native twin of ``test_python_smd_floor_is_scoped_to_different_footprints``
    on a referenced board (every footprint carries a distinct reference
    designator, as any realistically placed board does).  The QFP/QFN wall
    that kept this floor Python-only (#5705's deferral) is closed by
    ``A.Reference != B.Reference``: KiCad 10 documents that footprint
    children -- pads -- carry their parent footprint's Reference, so pads of
    one footprint compare equal and the rule never fires package-internally,
    while two independently placed footprints at the same 0.12 mm
    sub-floor gap do fire -- measured, not assumed, against
    ``kicad-cli pcb drc``.

    Gated on the ``SMD Pad Clearance`` availability probe (Issue #5713): the
    ``same_footprint=True`` half asserts an ABSENCE, which a KiCad that never
    evaluates the rule would satisfy without evaluating anything.
    """
    _require_native_smd_rule()
    path = _two_pad_board(tmp_path / "probe.kicad_pcb", 0.12, same_footprint=same_footprint)
    violations = _run_native_drc(path)
    relevant = findings_for_rules(violations, "SMD Pad Clearance")
    assert bool(relevant) == expected, violations
    if expected:
        assert relevant[0]["severity"] == "error"
        assert "actual 0.1200 mm" in relevant[0]["description"]


def test_native_smd_floor_omits_pairs_sharing_a_reference_designator(tmp_path):
    """Documented divergence: the native scope is the reference STRING.

    Two different footprints that share one reference designator (a
    duplicate-reference anomaly) sit below the floor with distinct
    footprint identities, so the Python checker -- scoped by identity --
    reports the pair, while the native ``A.Reference != B.Reference``
    condition cannot tell them apart and stays silent.  This is the
    measured boundary of the native rule, not a bug in either engine:
    duplicate references are themselves a board defect a full DRC run
    flags separately, and the ``kct check`` floor remains authoritative
    for exactly this shape.  Both engines must agree once the references
    are distinct.

    The native half is a NEGATIVE assertion keyed on the rule name, so it is
    paired with an in-test positive control on the identical board shape with
    DISTINCT references (Issue #5713).  Without it the assertion passes on any
    KiCad that omits the rule entirely -- which is exactly what 10.0.0/10.0.1
    do, and was confirmed empirically: this test passed on 10.0.1 while its
    own siblings failed with ``AssertionError: []``.  Now the absence is only
    meaningful because the presence was measured first, in the same run.
    """
    _require_native_smd_rule()
    rules = get_profile("jlcpcb").get_design_rules(layers=4, copper_oz=1)

    # Positive control FIRST: same geometry, distinct references.  If this
    # does not fire, the rule is not being evaluated and the negative below
    # would be vacuous -- fail here, naming the rule, rather than there.
    control = _two_pad_board(
        tmp_path / "control.kicad_pcb", 0.12, same_footprint=False, references=("U1", "U2")
    )
    control_findings = findings_for_rules(_run_native_drc(control), "SMD Pad Clearance")
    assert control_findings, (
        "Positive control produced no 'SMD Pad Clearance' finding: the native rule is not "
        "being evaluated on this KiCad, so the shared-reference negative below would pass "
        "vacuously. KiCad >= "
        f"{_MIN_KICAD_VERSION_STR} is required."
    )

    path = _two_pad_board(
        tmp_path / "probe.kicad_pcb", 0.12, same_footprint=False, references=("U1", "U1")
    )
    python_violations = ClearanceRule().check(PCB.load(path), rules).violations
    assert python_violations, "Python identity-scoped floor must still catch the pair"
    native_violations = _run_native_drc(path)
    assert not findings_for_rules(native_violations, "SMD Pad Clearance"), native_violations


@pytest.mark.parametrize("kind,gap", [("silk", 0.085), ("smd", 0.12), ("pth", 0.27)])
def test_native_old_profile_misses_new_constraint(tmp_path, kind, gap):
    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("Native KiCad CLI is not installed")
    path = board_fixture(tmp_path / "probe.kicad_pcb", kind, gap)
    rules = replace(
        get_profile("jlcpcb").get_design_rules(layers=4),
        min_silk_to_pad_clearance_mm=None,
        min_smd_pad_clearance_mm=None,
        min_pth_hole_to_track_mm=None,
        min_inner_pth_hole_to_copper_mm=None,
    )
    write_drc_constraints(path, rules, manufacturer_id="jlcpcb", layers=4)
    report = tmp_path / "native.json"
    subprocess.run(
        [str(cli), "pcb", "drc", "--format", "json", "-o", str(report), str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    violations = json.loads(report.read_text())["violations"]
    assert not [
        v
        for v in violations
        if v["type"] in ("silk_over_copper", "silk_overlap", "clearance", "hole_clearance")
    ], violations


def test_pth_slot_rotation_offset_and_zone_fill(tmp_path):
    from kicad_tools.validate.rules.factory_clearance import _hole_geometry

    path = board_fixture(tmp_path / "probe.kicad_pcb", "pth", 0.27, layer="In1.Cu")
    path.write_text(
        path.read_text().replace(
            "(at 0 0) (size .7 .7) (drill .4)",
            "(at 0 0 90) (size 1 1) (drill oval .4 .8 (offset .1 0))",
        )
    )
    pcb = PCB.load(path)
    fp = pcb.footprints[0]
    geom = _hole_geometry(fp.pads[0], fp)
    assert geom.bounds == pytest.approx((9.6, 9.7, 10.4, 10.1), abs=0.0001)
    # A slot's long dimension reaches the nearby inner track: drill=0 in
    # the legacy schema must not cause this hole to disappear from checking.
    rules = get_profile("jlcpcb").get_design_rules(layers=4)
    assert [
        v for v in ClearanceRule().check(pcb, rules).violations if v.rule_id == "pth_hole_clearance"
    ]
    text = board_fixture(path, "pth", 0.4, layer="In1.Cu").read_text()
    zone = """(zone (net 2) (net_name "B") (layer "In1.Cu") (hatch edge .5)
      (connect_pads (clearance .1)) (min_thickness .1) (fill yes)
      (polygon (pts (xy 10.49 9) (xy 11 9) (xy 11 11) (xy 10.49 11)))
      (filled_polygon (layer "In1.Cu") (pts (xy 10.49 9) (xy 11 9) (xy 11 11) (xy 10.49 11))))"""
    path.write_text(text[:-1] + zone + ")")
    violations = [
        v
        for v in ClearanceRule().check(PCB.load(path), rules).violations
        if v.rule_id == "pth_hole_clearance"
    ]
    assert len(violations) == 1
    assert violations[0].actual_value == pytest.approx(0.29, abs=0.0001)
    assert "Zone" in violations[0].items[1]


def test_custom_rule_survives_repeated_emission(tmp_path):
    path = board_fixture(tmp_path / "probe.kicad_pcb", "smd", 0.16)
    dru = path.with_suffix(".kicad_dru")
    custom = (
        '(version 1)\n# Reviewed creepage\n(rule "Reviewed HV" (constraint clearance (min 2mm)))\n'
    )
    dru.write_text(custom)
    rules = get_profile("jlcpcb").get_design_rules(layers=4)
    write_drc_constraints(path, rules, manufacturer_id="jlcpcb", layers=4)
    first = dru.read_bytes()
    write_drc_constraints(path, rules, manufacturer_id="jlcpcb", layers=4)
    assert dru.read_bytes() == first
    assert custom.strip() in first.decode()
    assert first.count(b'(rule "Silk to Pad') == 1


@pytest.mark.parametrize("layer", ["F.Cu", "In1.Cu"])
@pytest.mark.parametrize(
    "hole_net,track_net,gap,expected",
    [
        (0, 2, 0.27, True),
        (1, 0, 0.27, True),
        (0, 0, 0.27, True),
        (1, 1, 0.27, False),
        (0, 2, 0.31, False),
    ],
)
def test_unassigned_pth_native_python_parity(tmp_path, layer, hole_net, track_net, gap, expected):
    from kicad_tools.validate.rules.factory_clearance import check_pth_hole_clearance

    path = board_fixture(tmp_path / "probe.kicad_pcb", "pth", gap, layer=layer)
    text = path.read_text().replace(
        '(net 1 "A")))', f'(net {hole_net} "{"A" if hole_net else ""}")))'
    )
    text = text.replace("(net 2))", f"(net {track_net}))")
    path.write_text(text)
    rules = get_profile("jlcpcb").get_design_rules(layers=4)
    violations = check_pth_hole_clearance(PCB.load(path), rules).violations
    assert bool(violations) == expected, violations
    assert all(v.actual_value == pytest.approx(gap, abs=0.0001) for v in violations)
    # Native hole_clearance skips equal net codes, including two net-0
    # objects. Python deliberately does not infer a connection from that.
    _assert_native_pth_scope(path, rules, expected and (hole_net != 0 or track_net != 0))


def _assert_native_pth_scope(path, rules, expected):
    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("Native KiCad CLI is not installed")
    write_drc_constraints(path, rules, manufacturer_id="jlcpcb", layers=4)
    report = path.with_suffix(".json")
    subprocess.run(
        [str(cli), "pcb", "drc", "--format", "json", "-o", str(report), str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    violations = json.loads(report.read_text())["violations"]
    relevant = findings_for_rules(violations, "PTH Hole to Track", "Inner PTH Hole to Copper")
    assert bool(relevant) == expected, violations


@pytest.mark.parametrize("layer,expected", [("F.Cu", False), ("In1.Cu", True)])
@pytest.mark.parametrize("zone_net", [0, 2])
def test_pth_zone_layer_native_python_parity(tmp_path, layer, expected, zone_net):
    from kicad_tools.validate.rules.factory_clearance import check_pth_hole_clearance

    path = board_fixture(tmp_path / "probe.kicad_pcb", "pth", 0.4, layer=layer)
    zone = f'''(zone (net {zone_net}) (net_name "{"B" if zone_net else ""}") (layer "{layer}") (hatch edge .5)
      (connect_pads (clearance .1)) (min_thickness .1) (fill yes)
      (polygon (pts (xy 10.47 9) (xy 11 9) (xy 11 11) (xy 10.47 11)))
      (filled_polygon (layer "{layer}") (pts (xy 10.47 9) (xy 11 9) (xy 11 11) (xy 10.47 11))))'''
    path.write_text(path.read_text()[:-1] + zone + ")")
    rules = get_profile("jlcpcb").get_design_rules(layers=4)
    violations = check_pth_hole_clearance(PCB.load(path), rules).violations
    assert bool(violations) == expected, violations
    _assert_native_pth_scope(path, rules, expected)
