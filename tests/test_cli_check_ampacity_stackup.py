"""CLI tests for stackup-derived ampacity copper weight (Issue #4326).

The ampacity gate historically took its copper weight ONLY from ``--copper``
(default 1 oz) and ignored the board's declared ``(setup (stackup ...))``.
A board built at 2 oz outer but checked without ``--copper`` was silently
evaluated at 1 oz -- over-conservative at best, and (for the inverse case)
capable of masking a real thermal hazard.

This exercises all three tiers of the fix:

* **Tier 1** -- the declared stackup is the default source of truth: a 2 oz
  outer board checked WITHOUT ``--copper`` evaluates ampacity at 2 oz, and
  that flips a verdict vs the 1 oz default (guards against a silent no-op).
* **Tier 2** -- an EXPLICIT ``--copper`` that disagrees with the stackup
  wins but emits a loud WARNING; agreement is silent; ``--strict`` makes the
  disagreement fatal (exit 2).
* **Tier 3** -- ``--copper`` accepts a keyed ``outer=..,inner=..`` form
  alongside the scalar form, and rejects malformed input with exit 1.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# 2-layer board declaring an EXPLICIT 2 oz outer stackup (F.Cu / B.Cu at
# 0.070 mm = 2 oz).  A single F.Cu segment on net ``PWR`` is routed at a
# width supplied by the template.  The net-class-map sidecar assigns
# ``target_ampacity = 10 A`` to ``PWR``.
#
# IPC-2221 external required widths for 10 A (see the module docstring math):
#   1 oz -> 7.1941 mm      2 oz -> 3.5970 mm
_STACKUP_2OZ_BLOCK = """  (setup
    (stackup
      (layer "F.Cu" (type "copper") (thickness 0.070))
      (layer "dielectric 1" (type "core") (thickness 1.46) (material "FR4") (epsilon_r 4.5) (loss_tangent 0.02))
      (layer "B.Cu" (type "copper") (thickness 0.070))
      (copper_finish "HASL")
      (dielectric_constraints no)
    )
    (pad_to_mask_clearance 0)
  )
"""

_NO_STACKUP_BLOCK = "  (setup (pad_to_mask_clearance 0))\n"

REQ_1OZ_10A = 7.1941
REQ_2OZ_10A = 3.5970


def _board(segment_width_mm: float, *, stackup: bool) -> str:
    setup_block = _STACKUP_2OZ_BLOCK if stackup else _NO_STACKUP_BLOCK
    return f"""(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
{setup_block}  (net 0 "")
  (net 1 "PWR")
  (gr_rect (start 100 100) (end 200 200)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (segment (start 110 120) (end 170 120) (width {segment_width_mm}) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000001"))
)
"""


def _write_board(dir_path: Path, width_mm: float, *, stackup: bool) -> Path:
    pcb_file = dir_path / "board.kicad_pcb"
    pcb_file.write_text(_board(width_mm, stackup=stackup))
    return pcb_file


def _write_ncm(dir_path: Path, amps: float = 10.0) -> Path:
    ncm_file = dir_path / "ncm.json"
    ncm_file.write_text(json.dumps({"PWR": {"name": "PWR", "target_ampacity": amps}}))
    return ncm_file


def _ampacity_errors(report_path: Path) -> list[dict]:
    data = json.loads(report_path.read_text())
    return [
        v
        for v in data["violations"]
        if v.get("rule_id") == "ampacity" and v.get("severity") == "error"
    ]


def _run_check(pcb: Path, ncm: Path, report: Path, *, extra: list[str] | None = None) -> int:
    from kicad_tools.cli.check_cmd import main

    argv = [
        str(pcb),
        "--mfr",
        "jlcpcb",
        "--net-class-map",
        str(ncm),
        "--output",
        str(report),
        "--format",
        "json",
    ]
    if extra:
        argv.extend(extra)
    return main(argv)


class TestTier1StackupIsDefaultSourceOfTruth:
    """Tier 1: a declared 2 oz stackup drives ampacity without --copper."""

    def test_declared_2oz_evaluates_at_2oz(self, tmp_path: Path):
        """A grossly under-width net reports the 2 oz required width, not 1 oz.

        The 0.5 mm segment fails at any weight; the point is the *required*
        width printed is the 2 oz IPC-2221 floor (3.597 mm) derived from the
        declared stackup -- proving the stackup, not the 1 oz default, was
        the source of truth.
        """
        pcb = _write_board(tmp_path, 0.5, stackup=True)
        ncm = _write_ncm(tmp_path)
        report = tmp_path / "r.json"

        _run_check(pcb, ncm, report)  # no --copper

        errors = _ampacity_errors(report)
        assert len(errors) == 1
        assert errors[0]["required_value"] == pytest.approx(REQ_2OZ_10A, abs=0.01)
        # And decidedly NOT the 1 oz default value.
        assert errors[0]["required_value"] != pytest.approx(REQ_1OZ_10A, abs=0.01)

    def test_verdict_flip_vs_1oz_default(self, tmp_path: Path):
        """A width between the 2 oz and 1 oz floors flips the verdict.

        At 5.0 mm the net PASSES at 2 oz (>= 3.597) but FAILS at 1 oz
        (< 7.194).  Checked without --copper on the 2 oz board it must PASS;
        forcing --copper 1 must FAIL -- guarding against a silent no-op.
        """
        pcb = _write_board(tmp_path, 5.0, stackup=True)
        ncm = _write_ncm(tmp_path)

        report_stackup = tmp_path / "stackup.json"
        _run_check(pcb, ncm, report_stackup)  # 2 oz stackup, no --copper
        assert len(_ampacity_errors(report_stackup)) == 0

        report_1oz = tmp_path / "one.json"
        _run_check(pcb, ncm, report_1oz, extra=["--copper", "1"])  # force 1 oz
        errors_1oz = _ampacity_errors(report_1oz)
        assert len(errors_1oz) == 1
        assert errors_1oz[0]["required_value"] == pytest.approx(REQ_1OZ_10A, abs=0.01)

    def test_absent_stackup_falls_back_to_1oz(self, tmp_path: Path):
        """No declared stackup -> byte-identical 1 oz default behaviour.

        The same 5.0 mm net on a board WITHOUT a stackup block is evaluated
        at the 1 oz default and therefore FAILS -- confirming the fallback
        is unchanged when the stackup is absent.
        """
        pcb = _write_board(tmp_path, 5.0, stackup=False)
        ncm = _write_ncm(tmp_path)
        report = tmp_path / "r.json"

        _run_check(pcb, ncm, report)  # no --copper, no stackup

        errors = _ampacity_errors(report)
        assert len(errors) == 1
        assert errors[0]["required_value"] == pytest.approx(REQ_1OZ_10A, abs=0.01)


class TestTier2CrossCheckWarning:
    """Tier 2: explicit --copper wins but warns on stackup disagreement."""

    def test_warning_fires_on_disagreement(self, tmp_path: Path, capsys):
        """--copper 1 against a 2 oz stackup warns and evaluates at 1 oz."""
        pcb = _write_board(tmp_path, 0.5, stackup=True)
        ncm = _write_ncm(tmp_path)
        report = tmp_path / "r.json"

        _run_check(pcb, ncm, report, extra=["--copper", "1"])

        err = capsys.readouterr().err
        assert "stackup declares 2oz outer" in err
        assert "--copper" in err
        # Explicit --copper wins: evaluated at 1 oz.
        errors = _ampacity_errors(report)
        assert errors[0]["required_value"] == pytest.approx(REQ_1OZ_10A, abs=0.01)

    def test_silent_on_agreement(self, tmp_path: Path, capsys):
        """--copper 2 matching a 2 oz stackup emits no disagreement warning."""
        pcb = _write_board(tmp_path, 0.5, stackup=True)
        ncm = _write_ncm(tmp_path)
        report = tmp_path / "r.json"

        _run_check(pcb, ncm, report, extra=["--copper", "2"])

        err = capsys.readouterr().err
        assert "stackup declares" not in err
        errors = _ampacity_errors(report)
        assert errors[0]["required_value"] == pytest.approx(REQ_2OZ_10A, abs=0.01)

    def test_strict_makes_disagreement_fatal(self, tmp_path: Path):
        """Under --strict, a stackup-vs---copper disagreement forces exit 2.

        Uses ``--drc-only`` so the meta-check rollup (which would exit 2 on
        INCOMPLETE for a schematic-less board) cannot mask the result: the
        8.0 mm net clears the 1 oz floor (0 ampacity errors), so the ONLY
        thing that flips exit 0 -> 2 between the two runs is ``--strict``
        acting on the copper disagreement.
        """
        pcb = _write_board(tmp_path, 8.0, stackup=True)  # >= 7.194 (1 oz) -> passes
        ncm = _write_ncm(tmp_path)

        report_lax = tmp_path / "lax.json"
        rc_lax = _run_check(pcb, ncm, report_lax, extra=["--copper", "1", "--drc-only"])
        assert len(_ampacity_errors(report_lax)) == 0
        assert rc_lax == 0  # disagreement warns but is not fatal without --strict

        report_strict = tmp_path / "strict.json"
        rc_strict = _run_check(
            pcb, ncm, report_strict, extra=["--copper", "1", "--strict", "--drc-only"]
        )
        assert len(_ampacity_errors(report_strict)) == 0
        assert rc_strict == 2  # Tier 2 strict backstop

    def test_no_warning_without_explicit_copper(self, tmp_path: Path, capsys):
        """A 2 oz stackup with no --copper is the happy path (no warning)."""
        pcb = _write_board(tmp_path, 0.5, stackup=True)
        ncm = _write_ncm(tmp_path)
        report = tmp_path / "r.json"

        _run_check(pcb, ncm, report)

        assert "stackup declares" not in capsys.readouterr().err


class TestTier3KeyedCopper:
    """Tier 3: keyed --copper form + malformed-input rejection."""

    def test_parse_scalar(self):
        from kicad_tools.cli.check_cmd import _parse_copper_weight_arg

        assert _parse_copper_weight_arg("2") == (2.0, 2.0)
        assert _parse_copper_weight_arg("0.5") == (0.5, 0.5)

    def test_parse_keyed_both(self):
        from kicad_tools.cli.check_cmd import _parse_copper_weight_arg

        assert _parse_copper_weight_arg("outer=2,inner=0.5") == (2.0, 0.5)
        # Order-independent + whitespace tolerant.
        assert _parse_copper_weight_arg(" inner=0.5 , outer=2 ") == (2.0, 0.5)

    def test_parse_keyed_partial(self):
        from kicad_tools.cli.check_cmd import _parse_copper_weight_arg

        assert _parse_copper_weight_arg("outer=2") == (2.0, None)
        assert _parse_copper_weight_arg("inner=0.5") == (None, 0.5)

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "abc",
            "outer=2,bogus=3",
            "outer=abc",
            "outer=2,outer=3",
            "outer=-1",
            "outer=",
        ],
    )
    def test_parse_malformed_rejected(self, bad):
        from kicad_tools.cli.check_cmd import _parse_copper_weight_arg

        with pytest.raises(ValueError):
            _parse_copper_weight_arg(bad)

    def test_keyed_outer_drives_cli_verdict(self, tmp_path: Path):
        """``--copper outer=2`` evaluates the external net at 2 oz."""
        pcb = _write_board(tmp_path, 5.0, stackup=False)  # no stackup
        ncm = _write_ncm(tmp_path)
        report = tmp_path / "r.json"

        # Without a stackup and with keyed outer=2, the 5.0 mm net clears the
        # 2 oz floor (3.597) and PASSES -- whereas the 1 oz default would fail.
        _run_check(pcb, ncm, report, extra=["--copper", "outer=2"])
        assert len(_ampacity_errors(report)) == 0

    def test_malformed_copper_exits_1(self, tmp_path: Path, capsys):
        """A malformed --copper value is a hard Error with exit 1."""
        pcb = _write_board(tmp_path, 5.0, stackup=False)
        ncm = _write_ncm(tmp_path)
        report = tmp_path / "r.json"

        rc = _run_check(pcb, ncm, report, extra=["--copper", "outer=2,bogus=3"])
        assert rc == 1
        assert "Error:" in capsys.readouterr().err
