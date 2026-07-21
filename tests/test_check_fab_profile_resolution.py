"""Tests for ``kct check`` fab-profile resolution + tier advisory (issue #3920).

A routed ``.kicad_pcb`` carries no embedded fab-tier hint, so bare ``kct check``
used to hard-default to the base ``jlcpcb`` tier and report a false ``FAILED``
on boards that route legal, tier-gated geometry (e.g. via-in-pad, legal at
``jlcpcb-tier1``).  These tests cover the two complementary layers that close
the gap:

* **Layer 1 (primary):** ``kct route`` writes a ``fab_profile.json`` sidecar;
  ``kct check`` auto-discovers it and resolves the effective ``--mfr`` with a
  documented precedence chain (explicit > sidecar > ``project.kct target_fab``
  > ``jlcpcb`` default).

* **Layer 2 (belt-and-suspenders):** for a standalone routed board with NO
  sidecar and NO ``project.kct``, a non-blocking stderr advisory names a
  permitting tier when via-in-pad findings appear at a base tier -- WITHOUT
  changing the verdict / exit code.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.cli.check_cmd import (
    _discover_fab_profile_sidecar,
    _maybe_emit_via_in_pad_tier_advisory,
    _profile_supports_via_in_pad,
    _resolve_effective_check_mfr,
    main,
)
from kicad_tools.cli.route_cmd import _write_fab_profile_sidecar
from kicad_tools.sync.discover import resolve_target_fab_for_pcb

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

# A synthetic routed board with a single via dead-centre inside an SMD pad on
# the same net -- the canonical via-in-pad geometry.  Illegal at ``jlcpcb``
# (via_in_pad_supported=False), legal at ``jlcpcb-tier1`` (=True).
_VIA_IN_PAD_PCB = """(kicad_pcb (version 20240108) (generator "test_fixture")
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "DATA")
  (footprint "Package:TEST" (layer "F.Cu")
    (at 100 100)
    (property "Reference" "U1" (at 0 -2 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000001"))
    (property "Value" "TEST" (at 0 2 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000002"))
    (pad "1" smd rect (at 0 0) (size 2 2) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "DATA") (uuid "00000000-0000-0000-0000-000000000003"))
  )
  (via (at 100 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "00000000-0000-0000-0000-000000000004"))
)
"""


def _write_pcb(directory: Path) -> Path:
    pcb = directory / "routed.kicad_pcb"
    pcb.write_text(_VIA_IN_PAD_PCB)
    return pcb


def _write_sidecar(directory: Path, mfr: str) -> Path:
    sidecar = directory / "fab_profile.json"
    sidecar.write_text(json.dumps({"mfr": mfr, "source": "kct route --manufacturer"}))
    return sidecar


def _write_project_kct(directory: Path, target_fab: str) -> Path:
    kct = directory / "project.kct"
    kct.write_text(
        'kct_version: "1.0"\n'
        "project:\n"
        "  name: T\n"
        "requirements:\n"
        "  manufacturing:\n"
        f"    target_fab: {target_fab}\n"
    )
    return kct


class _FakeViolation:
    """Minimal stand-in with the single attribute the advisory reads."""

    def __init__(self, rule_id: str) -> None:
        self.rule_id = rule_id


# ---------------------------------------------------------------------------
# Sidecar discovery
# ---------------------------------------------------------------------------


class TestDiscoverFabProfileSidecar:
    def test_finds_sidecar_next_to_pcb(self, tmp_path: Path) -> None:
        pcb = _write_pcb(tmp_path)
        sidecar = _write_sidecar(tmp_path, "jlcpcb-tier1")
        assert _discover_fab_profile_sidecar(pcb) == sidecar

    def test_finds_sidecar_in_output_subdir(self, tmp_path: Path) -> None:
        board = tmp_path / "board"
        output = board / "output"
        output.mkdir(parents=True)
        pcb = _write_pcb(board)
        sidecar = _write_sidecar(output, "jlcpcb-tier1")
        assert _discover_fab_profile_sidecar(pcb) == sidecar

    def test_finds_sidecar_as_sibling_in_output(self, tmp_path: Path) -> None:
        output = tmp_path / "output"
        output.mkdir()
        pcb = _write_pcb(output)
        # <pcb_dir>/fab_profile.json wins first, so remove that layout and use
        # the <pcb_dir>/../output/fab_profile.json candidate explicitly.
        sidecar = output / "fab_profile.json"
        sidecar.write_text(json.dumps({"mfr": "jlcpcb-tier1", "source": "x"}))
        assert _discover_fab_profile_sidecar(pcb) == sidecar

    def test_returns_none_when_absent(self, tmp_path: Path) -> None:
        pcb = _write_pcb(tmp_path)
        assert _discover_fab_profile_sidecar(pcb) is None


# ---------------------------------------------------------------------------
# project.kct target_fab discovery
# ---------------------------------------------------------------------------


class TestResolveTargetFabForPcb:
    def test_reads_target_fab_next_to_pcb(self, tmp_path: Path) -> None:
        pcb = _write_pcb(tmp_path)
        _write_project_kct(tmp_path, "jlcpcb-tier1")
        assert resolve_target_fab_for_pcb(pcb) == "jlcpcb-tier1"

    def test_reads_target_fab_one_dir_up(self, tmp_path: Path) -> None:
        board = tmp_path / "board"
        output = board / "output"
        output.mkdir(parents=True)
        pcb = _write_pcb(output)
        _write_project_kct(output, "jlcpcb-tier1")
        # project.kct is a sibling of the PCB here; also verify the parent
        # lookup by moving it up one level.
        (output / "project.kct").unlink()
        _write_project_kct(board, "jlcpcb-tier1")
        assert resolve_target_fab_for_pcb(pcb) == "jlcpcb-tier1"

    def test_returns_none_without_project_kct(self, tmp_path: Path) -> None:
        pcb = _write_pcb(tmp_path)
        assert resolve_target_fab_for_pcb(pcb) is None


# ---------------------------------------------------------------------------
# Precedence chain (the core deliverable)
# ---------------------------------------------------------------------------


class TestResolveEffectiveCheckMfr:
    def test_explicit_flag_wins_over_everything(self, tmp_path: Path) -> None:
        pcb = _write_pcb(tmp_path)
        _write_sidecar(tmp_path, "jlcpcb-tier1")
        _write_project_kct(tmp_path, "jlcpcb-tier1")
        mfr, messages = _resolve_effective_check_mfr("jlcpcb", pcb)
        assert mfr == "jlcpcb"
        # An explicit flag short-circuits before any discovery message.
        assert messages == []

    def test_sidecar_wins_over_project_kct_and_default(self, tmp_path: Path) -> None:
        pcb = _write_pcb(tmp_path)
        _write_sidecar(tmp_path, "jlcpcb-tier1")
        _write_project_kct(tmp_path, "jlcpcb")  # lower-precedence, different value
        mfr, messages = _resolve_effective_check_mfr(None, pcb)
        assert mfr == "jlcpcb-tier1"
        assert any("auto-loaded fab profile: jlcpcb-tier1" in m for m in messages)
        assert any("fab_profile.json" in m or "from" in m for m in messages)

    def test_project_kct_wins_over_default(self, tmp_path: Path) -> None:
        pcb = _write_pcb(tmp_path)
        _write_project_kct(tmp_path, "jlcpcb-tier1")
        mfr, messages = _resolve_effective_check_mfr(None, pcb)
        assert mfr == "jlcpcb-tier1"
        assert any("project.kct target_fab" in m for m in messages)

    def test_falls_back_to_default(self, tmp_path: Path) -> None:
        pcb = _write_pcb(tmp_path)
        mfr, messages = _resolve_effective_check_mfr(None, pcb)
        assert mfr == "jlcpcb"
        assert messages == []

    def test_full_precedence_ladder(self, tmp_path: Path) -> None:
        """explicit > sidecar > project.kct > default, asserted stepwise."""
        pcb = _write_pcb(tmp_path)

        # 4. default only
        assert _resolve_effective_check_mfr(None, pcb)[0] == "jlcpcb"

        # 3. project.kct beats default
        _write_project_kct(tmp_path, "jlcpcb-tier1")
        assert _resolve_effective_check_mfr(None, pcb)[0] == "jlcpcb-tier1"

        # 2. sidecar beats project.kct
        _write_sidecar(tmp_path, "pcbway")
        assert _resolve_effective_check_mfr(None, pcb)[0] == "pcbway"

        # 1. explicit beats sidecar
        assert _resolve_effective_check_mfr("jlcpcb", pcb)[0] == "jlcpcb"


# ---------------------------------------------------------------------------
# Graceful degradation of malformed / unknown sidecars
# ---------------------------------------------------------------------------


class TestSidecarGracefulDegradation:
    def test_malformed_json_warns_and_falls_back(self, tmp_path: Path) -> None:
        pcb = _write_pcb(tmp_path)
        (tmp_path / "fab_profile.json").write_text("{not valid json")
        mfr, messages = _resolve_effective_check_mfr(None, pcb)
        assert mfr == "jlcpcb"
        assert any("malformed fab-profile sidecar" in m for m in messages)

    def test_unknown_profile_id_warns_and_falls_back(self, tmp_path: Path) -> None:
        pcb = _write_pcb(tmp_path)
        _write_sidecar(tmp_path, "definitely-not-a-real-fab")
        mfr, messages = _resolve_effective_check_mfr(None, pcb)
        assert mfr == "jlcpcb"
        assert any("unknown profile" in m for m in messages)

    def test_missing_mfr_field_warns_and_falls_back(self, tmp_path: Path) -> None:
        pcb = _write_pcb(tmp_path)
        (tmp_path / "fab_profile.json").write_text(json.dumps({"source": "x"}))
        mfr, messages = _resolve_effective_check_mfr(None, pcb)
        assert mfr == "jlcpcb"
        assert any("no 'mfr' field" in m for m in messages)

    def test_unknown_target_fab_in_project_kct_warns_and_falls_back(self, tmp_path: Path) -> None:
        pcb = _write_pcb(tmp_path)
        _write_project_kct(tmp_path, "not-a-real-fab")
        mfr, messages = _resolve_effective_check_mfr(None, pcb)
        assert mfr == "jlcpcb"
        assert any("unknown profile" in m for m in messages)


# ---------------------------------------------------------------------------
# Route-side sidecar write + round-trip
# ---------------------------------------------------------------------------


class TestFabProfileSidecarRoundTrip:
    def test_route_writes_sidecar_check_reads_it(self, tmp_path: Path) -> None:
        pcb = _write_pcb(tmp_path)
        _write_fab_profile_sidecar(pcb, "jlcpcb-tier1", quiet=True)

        sidecar = tmp_path / "fab_profile.json"
        assert sidecar.is_file()
        payload = json.loads(sidecar.read_text())
        assert payload["mfr"] == "jlcpcb-tier1"
        assert payload["source"] == "kct route --manufacturer"

        mfr, _ = _resolve_effective_check_mfr(None, pcb)
        assert mfr == "jlcpcb-tier1"

    def test_empty_manufacturer_writes_nothing(self, tmp_path: Path) -> None:
        pcb = _write_pcb(tmp_path)
        _write_fab_profile_sidecar(pcb, "", quiet=True)
        assert not (tmp_path / "fab_profile.json").exists()


# ---------------------------------------------------------------------------
# Layer 2 advisory
# ---------------------------------------------------------------------------


class TestViaInPadTierAdvisory:
    def test_advisory_fires_at_base_tier_with_findings(self, capsys) -> None:
        _maybe_emit_via_in_pad_tier_advisory("jlcpcb", [_FakeViolation("via_in_pad")])
        err = capsys.readouterr().err
        assert "via_in_pad finding(s) at profile 'jlcpcb'" in err
        assert "jlcpcb-tier1" in err
        assert "--mfr" in err

    def test_no_advisory_when_active_profile_permits(self, capsys) -> None:
        _maybe_emit_via_in_pad_tier_advisory("jlcpcb-tier1", [_FakeViolation("via_in_pad")])
        assert capsys.readouterr().err == ""

    def test_no_advisory_without_via_in_pad_findings(self, capsys) -> None:
        _maybe_emit_via_in_pad_tier_advisory("jlcpcb", [_FakeViolation("clearance")])
        assert capsys.readouterr().err == ""

    def test_no_advisory_for_unknown_profile(self, capsys) -> None:
        _maybe_emit_via_in_pad_tier_advisory("not-a-real-fab", [_FakeViolation("via_in_pad")])
        assert capsys.readouterr().err == ""

    def test_profile_support_helper(self) -> None:
        assert _profile_supports_via_in_pad("jlcpcb") is False
        assert _profile_supports_via_in_pad("jlcpcb-tier1") is True
        assert _profile_supports_via_in_pad("nope") is False


# ---------------------------------------------------------------------------
# End-to-end CLI behaviour (the acceptance criteria)
# ---------------------------------------------------------------------------


class TestCheckCliEndToEnd:
    def test_bare_check_fails_at_base_tier_and_prints_advisory(
        self, tmp_path: Path, capsys
    ) -> None:
        """Bare check on a via-in-pad board: FAILED + Layer-2 advisory."""
        pcb = _write_pcb(tmp_path)
        rc = main([str(pcb), "--drc-only", "--only", "via_in_pad"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "via_in_pad finding(s) at profile 'jlcpcb'" in err
        assert "jlcpcb-tier1" in err

    def test_sidecar_makes_bare_check_pass(self, tmp_path: Path, capsys) -> None:
        """AC: with the sidecar present, bare check reports 0 blocking via_in_pad."""
        pcb = _write_pcb(tmp_path)
        _write_sidecar(tmp_path, "jlcpcb-tier1")
        rc = main([str(pcb), "--drc-only", "--only", "via_in_pad"])
        assert rc == 0
        err = capsys.readouterr().err
        assert "auto-loaded fab profile: jlcpcb-tier1" in err
        # No advisory when the resolved tier already permits via-in-pad.
        assert "via_in_pad finding(s)" not in err

    def test_project_kct_makes_bare_check_pass(self, tmp_path: Path, capsys) -> None:
        pcb = _write_pcb(tmp_path)
        _write_project_kct(tmp_path, "jlcpcb-tier1")
        rc = main([str(pcb), "--drc-only", "--only", "via_in_pad"])
        assert rc == 0
        err = capsys.readouterr().err
        assert "project.kct target_fab" in err

    def test_explicit_base_tier_overrides_sidecar(self, tmp_path: Path, capsys) -> None:
        """AC: explicit --mfr jlcpcb still surfaces the via_in_pad findings."""
        pcb = _write_pcb(tmp_path)
        _write_sidecar(tmp_path, "jlcpcb-tier1")
        rc = main([str(pcb), "--drc-only", "--only", "via_in_pad", "--mfr", "jlcpcb"])
        assert rc == 2
        err = capsys.readouterr().err
        # Explicit flag wins: no sidecar auto-load line.
        assert "auto-loaded fab profile" not in err

    def test_advisory_is_verdict_invariant(self, tmp_path: Path, capsys) -> None:
        """The Layer-2 advisory must NOT change the exit code.

        Compare the bare-check exit code (advisory fires) against an explicit
        ``--mfr jlcpcb`` run at the SAME base tier (advisory also fires): both
        must be exit 2, proving the hint is purely informational.
        """
        pcb = _write_pcb(tmp_path)
        rc_bare = main([str(pcb), "--drc-only", "--only", "via_in_pad"])
        capsys.readouterr()
        rc_explicit = main([str(pcb), "--drc-only", "--only", "via_in_pad", "--mfr", "jlcpcb"])
        assert rc_bare == rc_explicit == 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
