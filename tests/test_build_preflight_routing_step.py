"""Tests for the build pipeline PREFLIGHT_ROUTING step (issue #2831).

Before this fix, ``kct build`` would emit a full manufacturing package
(gerbers, BOM, CPL) even when the underlying PCB had unconnected pads.
The 2026-05-12 demo-board fleet survey found 4 boards with manufacturing
artefacts shipped despite incomplete routing.

These tests pin the new behaviour: ``BuildStep.PREFLIGHT_ROUTING`` runs
the in-process :class:`NetStatusAnalyzer` between STITCH and VERIFY, and
HALTS the build when any nets are incomplete or unrouted.  Two opt-outs
exist: ``--force`` (umbrella, mirrors SYNC) and ``--allow-incomplete``
(targeted, advertised in the FAIL message itself).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from rich.console import Console

from kicad_tools.analysis.net_status import NetStatus, NetStatusResult, PadInfo
from kicad_tools.cli.build_cmd import (
    BuildContext,
    BuildStep,
    _run_step_preflight_routing,
    main,
)

# Stub used to exercise the runner without constructing a valid KiCad
# file.  The NetStatusAnalyzer is monkey-patched in nearly every test,
# so the file contents do not need to parse.
STUB_PCB = "(kicad_pcb)\n"


@pytest.fixture
def pcb_file(tmp_path: Path) -> Path:
    p = tmp_path / "board.kicad_pcb"
    p.write_text(STUB_PCB)
    return p


def _make_ctx(
    pcb: Path | None,
    *,
    routed_pcb: Path | None = None,
    project_dir: Path | None = None,
    output_dir: Path | None = None,
    dry_run: bool = False,
    force: bool = False,
    allow_incomplete: bool = False,
    quiet: bool = True,
) -> BuildContext:
    return BuildContext(
        project_dir=project_dir or Path("/tmp"),
        spec_file=None,
        schematic_file=None,
        pcb_file=pcb,
        routed_pcb_file=routed_pcb,
        output_dir=output_dir,
        dry_run=dry_run,
        quiet=quiet,
        force=force,
        allow_incomplete=allow_incomplete,
    )


def _complete_net(net_number: int, name: str) -> NetStatus:
    """Build a NetStatus that classifies as ``complete``."""
    return NetStatus(
        net_number=net_number,
        net_name=name,
        total_pads=2,
        connected_pads=[
            PadInfo("U1", "1", (0.0, 0.0), True),
            PadInfo("U1", "2", (1.0, 0.0), True),
        ],
        unconnected_pads=[],
        has_routing=True,
    )


def _incomplete_net(net_number: int, name: str) -> NetStatus:
    """Build a NetStatus that classifies as ``incomplete``."""
    return NetStatus(
        net_number=net_number,
        net_name=name,
        total_pads=3,
        connected_pads=[
            PadInfo("U1", "1", (0.0, 0.0), True),
            PadInfo("U1", "2", (1.0, 0.0), True),
        ],
        unconnected_pads=[
            PadInfo("U2", "1", (2.0, 0.0), False),
        ],
        has_routing=True,
    )


def _unrouted_net(net_number: int, name: str) -> NetStatus:
    """Build a NetStatus that classifies as ``unrouted``."""
    return NetStatus(
        net_number=net_number,
        net_name=name,
        total_pads=2,
        connected_pads=[],
        unconnected_pads=[
            PadInfo("U3", "1", (3.0, 0.0), False),
            PadInfo("U3", "2", (4.0, 0.0), False),
        ],
        has_routing=False,
    )


def _build_result(
    *,
    complete: int = 0,
    incomplete: int = 0,
    unrouted: int = 0,
) -> NetStatusResult:
    """Build a NetStatusResult with the desired complete/incomplete/unrouted mix.

    ``complete_count`` etc. are derived properties — we populate
    ``result.nets`` and ``result.total_nets`` so the derived counts match.
    """
    nets: list[NetStatus] = []
    n = 1
    for i in range(complete):
        nets.append(_complete_net(n, f"NET{n}"))
        n += 1
    for i in range(incomplete):
        nets.append(_incomplete_net(n, f"NET{n}"))
        n += 1
    for i in range(unrouted):
        nets.append(_unrouted_net(n, f"NET{n}"))
        n += 1

    result = NetStatusResult(nets=nets, total_nets=len(nets))
    # Sanity-check the derived counts match what was requested.
    assert result.complete_count == complete
    assert result.incomplete_count == incomplete
    assert result.unrouted_count == unrouted
    return result


# ---------------------------------------------------------------------------
# Enum + pipeline wiring
# ---------------------------------------------------------------------------


class TestBuildStepEnum:
    """The PREFLIGHT_ROUTING enum entry and CLI choice are public surface."""

    def test_preflight_routing_is_in_buildstep_enum(self) -> None:
        """A new BuildStep.PREFLIGHT_ROUTING value exists and round-trips."""
        assert BuildStep.PREFLIGHT_ROUTING.value == "preflight-routing"
        assert BuildStep("preflight-routing") is BuildStep.PREFLIGHT_ROUTING

    def test_preflight_routing_falls_between_stitch_and_verify_in_enum(self) -> None:
        """PREFLIGHT_ROUTING sits between STITCH and VERIFY in the enum."""
        members = list(BuildStep.__members__.keys())
        assert members.index("STITCH") < members.index("PREFLIGHT_ROUTING")
        assert members.index("PREFLIGHT_ROUTING") < members.index("VERIFY")

    def test_preflight_routing_is_a_cli_step_choice(self) -> None:
        """``--step preflight-routing`` must be accepted by argparse.

        argparse rejects unknown choices with SystemExit(2); the rest of
        the build pipeline returns an int exit code instead.
        """
        # Valid step value must be accepted (argparse does not raise).
        # Failure later in the pipeline returns a non-zero int.
        rc = main(["--step", "preflight-routing", "/nonexistent-project-path-for-test"])
        assert isinstance(rc, int)
        # Bogus path -> error int, but not argparse rejection.
        assert rc != 2

    def test_default_all_chain_orders_preflight_between_stitch_and_verify(self) -> None:
        """Statically check the default chain ordering.

        The default chain is a literal list inside main(); just check
        the relative order of the substrings is correct.  This is a
        weak but extremely cheap invariant to enforce.
        """
        import inspect

        from kicad_tools.cli import build_cmd

        src = inspect.getsource(build_cmd.main)
        stitch_idx = src.index("BuildStep.STITCH")
        preflight_idx = src.index("BuildStep.PREFLIGHT_ROUTING")
        verify_idx = src.index("BuildStep.VERIFY")
        export_idx = src.index("BuildStep.EXPORT")

        assert stitch_idx < preflight_idx < verify_idx, (
            "PREFLIGHT_ROUTING must appear between STITCH and VERIFY in main()'s default chain"
        )
        assert preflight_idx < export_idx, (
            "PREFLIGHT_ROUTING must precede EXPORT in the default chain"
        )


# ---------------------------------------------------------------------------
# _run_step_preflight_routing behaviour
# ---------------------------------------------------------------------------


class TestPreflightRoutingSkipBehaviour:
    """Skip / dry-run behaviour of _run_step_preflight_routing."""

    def test_skipped_when_no_pcb_available(self) -> None:
        """No PCB and no routed_pcb -> skip with informative message."""
        ctx = _make_ctx(pcb=None, routed_pcb=None)

        with patch("kicad_tools.analysis.net_status.NetStatusAnalyzer") as mock_cls:
            result = _run_step_preflight_routing(ctx, Console(quiet=True))

        # Analyser must not be instantiated when no PCB is available.
        mock_cls.assert_not_called()
        assert result.success is True
        assert "no pcb" in result.message.lower()

    def test_skipped_when_pcb_path_missing(self, tmp_path: Path) -> None:
        """PCB path set but file does not exist -> skip."""
        missing = tmp_path / "missing.kicad_pcb"
        ctx = _make_ctx(pcb=missing)

        with patch("kicad_tools.analysis.net_status.NetStatusAnalyzer") as mock_cls:
            result = _run_step_preflight_routing(ctx, Console(quiet=True))

        mock_cls.assert_not_called()
        assert result.success is True
        assert "no pcb" in result.message.lower()

    def test_dry_run_skips_analyser(self, pcb_file: Path) -> None:
        """--dry-run never instantiates a NetStatusAnalyzer."""
        ctx = _make_ctx(pcb=pcb_file, dry_run=True)

        with patch("kicad_tools.analysis.net_status.NetStatusAnalyzer") as mock_cls:
            result = _run_step_preflight_routing(ctx, Console(quiet=True))

        mock_cls.assert_not_called()
        assert result.success is True
        assert result.message.startswith("[dry-run]")


class TestPreflightRoutingHappyPath:
    """All-nets-complete cases of _run_step_preflight_routing."""

    def test_all_nets_complete_passes(self, pcb_file: Path) -> None:
        """incomplete_count == 0 and unrouted_count == 0 -> success."""
        ctx = _make_ctx(pcb=pcb_file)

        result_obj = _build_result(complete=5)

        with patch("kicad_tools.analysis.net_status.NetStatusAnalyzer") as mock_cls:
            mock_cls.return_value.analyze.return_value = result_obj
            result = _run_step_preflight_routing(ctx, Console(quiet=True))

        assert result.success is True
        assert "5/5" in result.message
        assert "complete" in result.message.lower()


class TestPreflightRoutingIncompleteHalts:
    """Incomplete-routing cases halt the build by default."""

    def test_incomplete_blocks_without_overrides(self, pcb_file: Path) -> None:
        """Incomplete nets halt the build with an actionable message.

        This is the gate that prevents board 01/02/06/07's incomplete
        routing from shipping as a manufacturing package.
        """
        ctx = _make_ctx(pcb=pcb_file)

        result_obj = _build_result(complete=7, incomplete=2, unrouted=1)

        with patch("kicad_tools.analysis.net_status.NetStatusAnalyzer") as mock_cls:
            mock_cls.return_value.analyze.return_value = result_obj
            result = _run_step_preflight_routing(ctx, Console(quiet=True))

        assert result.success is False
        # FAIL message must name both escape hatches the issue specifies.
        assert "--allow-incomplete" in result.message
        assert "kct route" in result.message
        # 3 offending nets out of 10 total.
        assert "3/10" in result.message

    def test_allow_incomplete_overrides_failure(self, pcb_file: Path) -> None:
        """--allow-incomplete converts FAIL into a yellow warning."""
        ctx = _make_ctx(pcb=pcb_file, allow_incomplete=True)

        result_obj = _build_result(complete=7, incomplete=2, unrouted=1)

        with patch("kicad_tools.analysis.net_status.NetStatusAnalyzer") as mock_cls:
            mock_cls.return_value.analyze.return_value = result_obj
            result = _run_step_preflight_routing(ctx, Console(quiet=True))

        assert result.success is True
        assert "continuing" in result.message.lower()
        assert "--allow-incomplete" in result.message

    def test_force_overrides_failure(self, pcb_file: Path) -> None:
        """--force also bypasses (mirrors SYNC's umbrella escape hatch)."""
        ctx = _make_ctx(pcb=pcb_file, force=True)

        result_obj = _build_result(complete=7, incomplete=2, unrouted=1)

        with patch("kicad_tools.analysis.net_status.NetStatusAnalyzer") as mock_cls:
            mock_cls.return_value.analyze.return_value = result_obj
            result = _run_step_preflight_routing(ctx, Console(quiet=True))

        assert result.success is True
        assert "continuing" in result.message.lower()
        assert "--force" in result.message

    def test_analyser_failure_returns_failure(self, pcb_file: Path) -> None:
        """If NetStatusAnalyzer construction raises, step reports failure."""
        ctx = _make_ctx(pcb=pcb_file)

        with patch("kicad_tools.analysis.net_status.NetStatusAnalyzer") as mock_cls:
            mock_cls.side_effect = RuntimeError("cannot load")
            result = _run_step_preflight_routing(ctx, Console(quiet=True))

        assert result.success is False
        assert "failed to analyze" in result.message


class TestPreflightRoutingPrefersRoutedPcb:
    """The check should prefer the routed PCB when available."""

    def test_routed_pcb_takes_precedence(self, pcb_file: Path, tmp_path: Path) -> None:
        """When both pcb_file and routed_pcb_file exist, prefer routed."""
        routed = tmp_path / "board_routed.kicad_pcb"
        routed.write_text(STUB_PCB)
        ctx = _make_ctx(pcb=pcb_file, routed_pcb=routed)

        result_obj = _build_result(complete=3)

        with patch("kicad_tools.analysis.net_status.NetStatusAnalyzer") as mock_cls:
            mock_cls.return_value.analyze.return_value = result_obj
            _run_step_preflight_routing(ctx, Console(quiet=True))

        # The analyser was constructed against the routed PCB, not the
        # pre-route PCB.
        call_args = mock_cls.call_args
        assert call_args is not None
        passed_pcb = call_args[0][0]
        assert passed_pcb == routed


class TestPreflightRoutingNoSubprocess:
    """The preflight-routing step must use the in-process analyser."""

    def test_no_subprocess_in_runner_source(self) -> None:
        """_run_step_preflight_routing source does not shell out."""
        import inspect

        src = inspect.getsource(_run_step_preflight_routing)
        # The runner must not call subprocess.run() — issue #2831
        # explicitly requires in-process use of NetStatusAnalyzer.
        assert "subprocess.run" not in src
        assert "subprocess.Popen" not in src


# ---------------------------------------------------------------------------
# Integration: PREFLIGHT_ROUTING halts build before EXPORT
# ---------------------------------------------------------------------------


class TestPreflightRoutingBlocksManufacturing:
    """End-to-end-ish: incomplete routing prevents manufacturing writes."""

    def test_incomplete_routing_in_isolated_preflight_exits_nonzero(self, tmp_path: Path) -> None:
        """`kct build --step preflight-routing` exits non-zero on incomplete nets.

        Smallest end-to-end assertion: the dispatch ladder is wired and
        an incomplete-routing analysis propagates into a non-zero process
        exit -- which is what `kct build`'s caller (CI, kct pipeline)
        relies on to short-circuit before manufacturing artefacts land.
        """
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        (project_dir / "board.kicad_pcb").write_text(STUB_PCB)

        result_obj = _build_result(complete=7, incomplete=2, unrouted=1)

        with patch("kicad_tools.analysis.net_status.NetStatusAnalyzer") as mock_cls:
            mock_cls.return_value.analyze.return_value = result_obj
            rc = main(["--step", "preflight-routing", str(project_dir)])

        assert rc != 0, "PREFLIGHT_ROUTING step must exit non-zero on incomplete routing"
        # And critically: no manufacturing/ dir was created.
        assert not (project_dir / "manufacturing").exists()
        assert not (project_dir / "output" / "manufacturing").exists()

    def test_incomplete_routing_with_allow_incomplete_exits_zero(self, tmp_path: Path) -> None:
        """`kct build --step preflight-routing --allow-incomplete` exits zero."""
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        (project_dir / "board.kicad_pcb").write_text(STUB_PCB)

        result_obj = _build_result(complete=7, incomplete=2, unrouted=1)

        with patch("kicad_tools.analysis.net_status.NetStatusAnalyzer") as mock_cls:
            mock_cls.return_value.analyze.return_value = result_obj
            rc = main(
                [
                    "--step",
                    "preflight-routing",
                    "--allow-incomplete",
                    str(project_dir),
                ]
            )

        assert rc == 0, "--allow-incomplete must allow the step to succeed"

    def test_incomplete_routing_with_force_exits_zero(self, tmp_path: Path) -> None:
        """`kct build --step preflight-routing --force` exits zero."""
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        (project_dir / "board.kicad_pcb").write_text(STUB_PCB)

        result_obj = _build_result(complete=7, incomplete=2, unrouted=1)

        with patch("kicad_tools.analysis.net_status.NetStatusAnalyzer") as mock_cls:
            mock_cls.return_value.analyze.return_value = result_obj
            rc = main(["--step", "preflight-routing", "--force", str(project_dir)])

        assert rc == 0, "--force must allow the step to succeed"

    def test_real_fixture_voltage_divider_routed_pcb_is_rejected(self) -> None:
        """Real fixture: board 01-voltage-divider's routed PCB.

        Surveyed 2026-05-12: 2/3 nets routed.  This is the exact failure
        surface the issue's bug report names, so we use it as the
        incomplete-routing fixture rather than synthesising one.

        If the fixture board is later repaired (full routing), this
        test will fail loudly -- which is the correct signal that the
        bug-reproducer should be updated.
        """
        repo_root = Path(__file__).resolve().parent.parent
        fixture = (
            repo_root
            / "boards"
            / "01-voltage-divider"
            / "output"
            / "voltage_divider_routed.kicad_pcb"
        )
        if not fixture.exists():
            pytest.skip(f"fixture PCB not present at {fixture}")

        # Run the analyser in-process against the real fixture.  We do
        # NOT monkey-patch here; we want to verify the real net-status
        # analyser, the real dispatch path, and the real exit code all
        # agree that this board is not manufacturable as-is.
        ctx = _make_ctx(pcb=fixture)
        result = _run_step_preflight_routing(ctx, Console(quiet=True))

        assert result.success is False, (
            f"Real fixture {fixture} should be classified as incomplete; "
            f"if it has been repaired, update this test's expectations."
        )
        assert "--allow-incomplete" in result.message
        assert "kct route" in result.message
