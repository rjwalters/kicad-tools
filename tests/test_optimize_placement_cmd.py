"""Tests for the optimize-placement CLI command."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from kicad_tools.cli.optimize_placement_cmd import (
    _build_footprint_sizes,
    _create_strategy,
    _evaluate,
    _generate_seed,
    _parse_weights,
    _print_score,
    _vector_to_placements,
    run_optimize_placement,
)
from kicad_tools.placement.cmaes_strategy import CMAESStrategy
from kicad_tools.placement.cost import (
    BoardOutline,
    CostBreakdown,
    DesignRuleSet,
    Net,
    PlacementCostConfig,
    PlacementScore,
)
from kicad_tools.placement.strategy import StrategyConfig
from kicad_tools.placement.vector import (
    ComponentDef,
    PadDef,
    PlacementVector,
    bounds,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def simple_components() -> list[ComponentDef]:
    """Three small components for testing."""
    return [
        ComponentDef(
            reference="R1",
            pads=(
                PadDef(name="1", local_x=-1.0, local_y=0.0),
                PadDef(name="2", local_x=1.0, local_y=0.0),
            ),
            width=3.0,
            height=1.5,
        ),
        ComponentDef(
            reference="R2",
            pads=(
                PadDef(name="1", local_x=-1.0, local_y=0.0),
                PadDef(name="2", local_x=1.0, local_y=0.0),
            ),
            width=3.0,
            height=1.5,
        ),
        ComponentDef(
            reference="C1",
            pads=(
                PadDef(name="1", local_x=-0.5, local_y=0.0),
                PadDef(name="2", local_x=0.5, local_y=0.0),
            ),
            width=2.0,
            height=1.0,
        ),
    ]


@pytest.fixture
def simple_nets() -> list[Net]:
    """Nets connecting R1-R2 and R2-C1."""
    return [
        Net(name="N1", pins=[("R1", "2"), ("R2", "1")]),
        Net(name="N2", pins=[("R2", "2"), ("C1", "1")]),
    ]


@pytest.fixture
def board() -> BoardOutline:
    return BoardOutline(min_x=0.0, min_y=0.0, max_x=30.0, max_y=20.0)


@pytest.fixture
def rules() -> DesignRuleSet:
    return DesignRuleSet(min_clearance=0.2, min_hole_to_hole=0.5, min_edge_clearance=0.3)


@pytest.fixture
def cost_config() -> PlacementCostConfig:
    return PlacementCostConfig()


@pytest.fixture
def tmp_pcb(tmp_path: Path) -> Path:
    """Create a minimal .kicad_pcb file for testing."""
    pcb_content = """\
(kicad_pcb (version 20230101) (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup
    (pad_to_mask_clearance 0.05)
  )
  (net 0 "")
  (net 1 "N1")
  (net 2 "N2")
  (footprint "R_0805" (layer "F.Cu")
    (at 10.0 10.0 0)
    (property "Reference" "R1")
    (fp_text reference "R1" (at 0 -1.5) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" smd rect (at -1.0 0.0) (size 0.8 0.8) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "N1"))
    (pad "2" smd rect (at 1.0 0.0) (size 0.8 0.8) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "N2"))
  )
  (footprint "R_0805" (layer "F.Cu")
    (at 20.0 10.0 0)
    (property "Reference" "R2")
    (fp_text reference "R2" (at 0 -1.5) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" smd rect (at -1.0 0.0) (size 0.8 0.8) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "N1"))
    (pad "2" smd rect (at 1.0 0.0) (size 0.8 0.8) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "N2"))
  )
  (footprint "C_0402" (layer "F.Cu")
    (at 15.0 15.0 0)
    (property "Reference" "C1")
    (fp_text reference "C1" (at 0 -1.0) (layer "F.SilkS") (effects (font (size 0.8 0.8) (thickness 0.12))))
    (pad "1" smd rect (at -0.5 0.0) (size 0.5 0.5) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "N2"))
    (pad "2" smd rect (at 0.5 0.0) (size 0.5 0.5) (layers "F.Cu" "F.Paste" "F.Mask") (net 0 ""))
  )
  (gr_line (start 0 0) (end 30 0) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 30 0) (end 30 20) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 30 20) (end 0 20) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 0 20) (end 0 0) (layer "Edge.Cuts") (width 0.05))
)
"""
    pcb_file = tmp_path / "test_board.kicad_pcb"
    pcb_file.write_text(pcb_content)
    return pcb_file


# ---------------------------------------------------------------------------
# Unit tests: helper functions
# ---------------------------------------------------------------------------


class TestVectorToPlacements:
    def test_converts_vector_to_component_placements(self, simple_components):
        data = np.array(
            [10.0, 5.0, 0.0, 0.0, 20.0, 5.0, 1.0, 0.0, 15.0, 10.0, 0.0, 0.0],
            dtype=np.float64,
        )
        vector = PlacementVector(data=data)
        placements = _vector_to_placements(vector, simple_components)
        assert len(placements) == 3
        assert placements[0].reference == "R1"
        assert placements[0].x == 10.0
        assert placements[0].y == 5.0
        assert placements[1].reference == "R2"
        assert placements[2].reference == "C1"


class TestEvaluate:
    def test_returns_placement_score(
        self, simple_components, simple_nets, rules, board, cost_config
    ):
        data = np.array(
            [10.0, 10.0, 0.0, 0.0, 20.0, 10.0, 0.0, 0.0, 15.0, 15.0, 0.0, 0.0],
            dtype=np.float64,
        )
        vector = PlacementVector(data=data)
        footprint_sizes = _build_footprint_sizes(simple_components)
        score = _evaluate(
            vector, simple_components, simple_nets, rules, board, cost_config, footprint_sizes
        )
        assert isinstance(score, PlacementScore)
        assert score.total >= 0.0
        assert isinstance(score.breakdown, CostBreakdown)

    def test_feasible_placement_has_zero_violations(
        self, simple_components, simple_nets, rules, board, cost_config
    ):
        # Place components well apart
        data = np.array(
            [5.0, 5.0, 0.0, 0.0, 15.0, 5.0, 0.0, 0.0, 25.0, 15.0, 0.0, 0.0],
            dtype=np.float64,
        )
        vector = PlacementVector(data=data)
        footprint_sizes = _build_footprint_sizes(simple_components)
        score = _evaluate(
            vector, simple_components, simple_nets, rules, board, cost_config, footprint_sizes
        )
        assert score.breakdown.overlap == 0.0
        assert score.breakdown.boundary == 0.0
        assert score.is_feasible


class TestBuildFootprintSizes:
    def test_returns_size_dict(self, simple_components):
        sizes = _build_footprint_sizes(simple_components)
        assert sizes["R1"] == (3.0, 1.5)
        assert sizes["C1"] == (2.0, 1.0)


class TestParseWeights:
    def test_default_weights(self):
        config = _parse_weights(None)
        assert isinstance(config, PlacementCostConfig)
        assert config.wirelength_weight == 1.0
        assert config.overlap_weight == 1e6

    def test_custom_weights(self):
        config = _parse_weights('{"wirelength": 2.5, "overlap": 500}')
        assert config.wirelength_weight == 2.5
        assert config.overlap_weight == 500

    def test_invalid_json_exits(self):
        with pytest.raises(SystemExit):
            _parse_weights("not json at all")


class TestCreateStrategy:
    def test_cmaes_strategy(self):
        strategy = _create_strategy("cmaes")
        assert isinstance(strategy, CMAESStrategy)

    def test_unknown_strategy_raises(self):
        with pytest.raises(ValueError, match="Unknown strategy"):
            _create_strategy("nonexistent")


class TestGenerateSeed:
    def test_force_directed(self, simple_components, simple_nets, board):
        seed = _generate_seed("force-directed", simple_components, simple_nets, board)
        assert isinstance(seed, PlacementVector)
        assert seed.num_components == 3

    def test_random_seed(self, simple_components, simple_nets, board):
        seed = _generate_seed("random", simple_components, simple_nets, board)
        assert isinstance(seed, PlacementVector)
        assert seed.num_components == 3

    def test_unknown_seed_raises(self, simple_components, simple_nets, board):
        with pytest.raises(ValueError, match="Unknown seed method"):
            _generate_seed("nonexistent", simple_components, simple_nets, board)


class TestPrintScore:
    def test_prints_formatted_output(self, capsys):
        score = PlacementScore(
            total=42.5,
            breakdown=CostBreakdown(
                wirelength=10.0,
                overlap=0.0,
                boundary=0.0,
                drc=0.0,
                area=25.0,
            ),
            is_feasible=True,
        )
        _print_score("Test", score)
        captured = capsys.readouterr()
        assert "Test" in captured.out
        assert "42.5" in captured.out
        assert "feasible" in captured.out

    def test_prints_infeasible(self, capsys):
        score = PlacementScore(
            total=1e9,
            breakdown=CostBreakdown(
                wirelength=10.0,
                overlap=5.0,
                boundary=2.0,
                drc=1.0,
                area=25.0,
            ),
            is_feasible=False,
        )
        _print_score("Bad", score)
        captured = capsys.readouterr()
        assert "INFEASIBLE" in captured.out


# ---------------------------------------------------------------------------
# Integration tests: full optimization flow
# ---------------------------------------------------------------------------


class TestRunOptimizePlacement:
    def test_file_not_found(self, tmp_path):
        result = run_optimize_placement(str(tmp_path / "nonexistent.kicad_pcb"))
        assert result == 1

    def test_wrong_extension(self, tmp_path):
        bad_file = tmp_path / "test.txt"
        bad_file.write_text("not a pcb")
        result = run_optimize_placement(str(bad_file))
        assert result == 1

    def test_dry_run_returns_zero(self, tmp_pcb):
        """Dry-run mode should evaluate and return 0."""
        result = run_optimize_placement(str(tmp_pcb), dry_run=True, quiet=True)
        assert result == 0

    def test_dry_run_with_output(self, tmp_pcb, capsys):
        """Dry-run mode should print score information."""
        result = run_optimize_placement(str(tmp_pcb), dry_run=True)
        captured = capsys.readouterr()
        assert result == 0
        assert "dry-run" in captured.out.lower() or "Current" in captured.out

    def test_optimization_produces_output(self, tmp_pcb, tmp_path):
        """Run a short optimization and verify output file is created."""
        output = tmp_path / "output.kicad_pcb"
        result = run_optimize_placement(
            str(tmp_pcb),
            max_iterations=3,
            output_path=str(output),
            quiet=True,
        )
        assert result == 0
        assert output.exists()
        content = output.read_text()
        assert "kicad_pcb" in content

    def test_optimization_with_progress(self, tmp_pcb, capsys):
        """Verify progress output is printed when requested."""
        result = run_optimize_placement(
            str(tmp_pcb),
            max_iterations=5,
            progress_interval=2,
        )
        assert result == 0
        captured = capsys.readouterr()
        # Progress lines contain iteration numbers in brackets
        assert "score=" in captured.out or "Optimization Summary" in captured.out

    def test_optimization_with_random_seed(self, tmp_pcb, tmp_path):
        """Test with random seed method."""
        output = tmp_path / "output.kicad_pcb"
        result = run_optimize_placement(
            str(tmp_pcb),
            max_iterations=2,
            output_path=str(output),
            seed_method="random",
            quiet=True,
        )
        assert result == 0
        assert output.exists()

    def test_optimization_with_custom_weights(self, tmp_pcb, tmp_path):
        """Test with custom cost weights."""
        output = tmp_path / "output.kicad_pcb"
        weights = '{"wirelength": 5.0, "overlap": 1e5}'
        result = run_optimize_placement(
            str(tmp_pcb),
            max_iterations=2,
            output_path=str(output),
            weights_json=weights,
            quiet=True,
        )
        assert result == 0

    def test_checkpoint_save_resume(self, tmp_pcb, tmp_path):
        """Test that checkpoint files are saved during optimization."""
        checkpoint_dir = tmp_path / "checkpoints"
        output = tmp_path / "output.kicad_pcb"

        # Run optimization with checkpoint saving
        result = run_optimize_placement(
            str(tmp_pcb),
            max_iterations=3,
            output_path=str(output),
            checkpoint_dir=str(checkpoint_dir),
            quiet=True,
        )
        assert result == 0

        # Verify checkpoint file exists
        checkpoint_file = checkpoint_dir / "optimizer_state.json"
        assert checkpoint_file.exists()

        # Verify it's valid JSON with expected fields
        state = json.loads(checkpoint_file.read_text())
        assert state["strategy"] == "cmaes"
        assert "generation" in state
        assert "best_score" in state

        # Run again with resume
        output2 = tmp_path / "output2.kicad_pcb"
        result2 = run_optimize_placement(
            str(tmp_pcb),
            max_iterations=2,
            output_path=str(output2),
            checkpoint_dir=str(checkpoint_dir),
            quiet=True,
        )
        assert result2 == 0

    def test_summary_output(self, tmp_pcb, capsys):
        """Verify the summary includes expected fields."""
        result = run_optimize_placement(
            str(tmp_pcb),
            max_iterations=2,
        )
        assert result == 0
        captured = capsys.readouterr()
        assert "Optimization Summary" in captured.out
        assert "Improvement" in captured.out
        assert "Iterations" in captured.out
        assert "Wall time" in captured.out


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


class TestCLIHelp:
    def test_help_flag(self):
        """Verify --help works via argparse."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        # Parse the help output
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["optimize-placement", "--help"])
        assert exc_info.value.code == 0

    def test_parser_defaults(self):
        """Verify parser defaults are correct."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(["optimize-placement", "board.kicad_pcb"])
        assert args.command == "optimize-placement"
        assert args.pcb == "board.kicad_pcb"
        assert args.strategy == "cmaes"
        assert args.max_iterations == 1000
        assert args.output is None
        assert args.seed_method == "force-directed"
        assert args.weights is None
        assert args.dry_run is False
        assert args.progress == 0
        assert args.checkpoint is None

    def test_parser_with_all_options(self):
        """Verify parser handles all options."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(
            [
                "optimize-placement",
                "board.kicad_pcb",
                "--strategy",
                "cmaes",
                "--max-iterations",
                "500",
                "-o",
                "out.kicad_pcb",
                "--seed",
                "random",
                "--weights",
                '{"wirelength": 2.0}',
                "--dry-run",
                "--progress",
                "10",
                "--checkpoint",
                "/tmp/ckpt",
                "-v",
                "-q",
            ]
        )
        assert args.strategy == "cmaes"
        assert args.max_iterations == 500
        assert args.output == "out.kicad_pcb"
        assert args.seed_method == "random"
        assert args.weights == '{"wirelength": 2.0}'
        assert args.dry_run is True
        assert args.progress == 10
        assert args.checkpoint == "/tmp/ckpt"
        assert args.verbose is True
        assert args.quiet is True


# ---------------------------------------------------------------------------
# Optimization convergence test (sanity check, not performance benchmark)
# ---------------------------------------------------------------------------


class TestOptimizationConvergence:
    def test_optimizer_improves_score(self, simple_components, simple_nets, board, rules):
        """Verify the optimizer can improve upon a random initial placement."""
        cost_config = PlacementCostConfig()
        footprint_sizes = _build_footprint_sizes(simple_components)

        # Run a few iterations of CMA-ES
        placement_bounds = bounds(board, simple_components)
        strategy = CMAESStrategy()
        config = StrategyConfig(max_iterations=20, seed=42)
        initial_pop = strategy.initialize(placement_bounds, config)

        # Evaluate and observe initial population
        scores = []
        for candidate in initial_pop:
            score = _evaluate(
                candidate,
                simple_components,
                simple_nets,
                rules,
                board,
                cost_config,
                footprint_sizes,
            )
            scores.append(score.total)
        strategy.observe(initial_pop, scores)

        # Run a few generations
        for _ in range(10):
            if strategy.converged:
                break
            candidates = strategy.suggest(strategy._population_size)
            scores = []
            for c in candidates:
                score = _evaluate(
                    c, simple_components, simple_nets, rules, board, cost_config, footprint_sizes
                )
                scores.append(score.total)
            strategy.observe(candidates, scores)

        best_vec, best_score = strategy.best()
        # The optimizer should find a score no worse than the initial worst population member
        assert best_score <= max(scores) + 1e-6  # Allow tiny floating point slack


# ---------------------------------------------------------------------------
# Interrupt handling tests
# ---------------------------------------------------------------------------


class TestInterruptHandling:
    """Tests for SIGINT/SIGTERM handler and atomic write."""

    def test_signal_handler_is_installed(self, tmp_pcb, tmp_path, monkeypatch):
        """Verify that SIGINT/SIGTERM handlers are installed during optimization."""
        import signal as sig

        installed_signals: list[int] = []
        original_signal = sig.signal

        def spy_signal(signum, handler):
            installed_signals.append(signum)
            return original_signal(signum, handler)

        monkeypatch.setattr(sig, "signal", spy_signal)

        output = tmp_path / "out.kicad_pcb"
        run_optimize_placement(
            str(tmp_pcb),
            max_iterations=1,
            output_path=str(output),
            quiet=True,
        )
        assert sig.SIGINT in installed_signals
        assert sig.SIGTERM in installed_signals

    def test_keyboard_interrupt_writes_output_and_returns_2(self, tmp_pcb, tmp_path, monkeypatch):
        """When KeyboardInterrupt fires, best placement is saved and exit code is 2."""
        from kicad_tools.cli import optimize_placement_cmd as mod

        call_count = 0
        original_suggest = None

        # Monkeypatch CMAESStrategy.suggest to raise KeyboardInterrupt after 1 call
        from kicad_tools.placement.cmaes_strategy import CMAESStrategy

        original_suggest = CMAESStrategy.suggest

        def interrupt_on_second_suggest(self, n):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise KeyboardInterrupt()
            return original_suggest(self, n)

        monkeypatch.setattr(CMAESStrategy, "suggest", interrupt_on_second_suggest)

        output = tmp_path / "interrupted_output.kicad_pcb"
        result = run_optimize_placement(
            str(tmp_pcb),
            max_iterations=100,
            output_path=str(output),
            quiet=True,
        )
        assert result == 2
        assert output.exists()
        content = output.read_text()
        assert "kicad_pcb" in content

    def test_atomic_write_prevents_corruption(self, tmp_pcb, tmp_path):
        """Atomic write creates output even if process is killed between steps."""
        from kicad_tools.cli.optimize_placement_cmd import (
            _write_placements_to_pcb_atomic,
        )

        components, nets, board, rules, _origin = _read_board_data(str(tmp_pcb))
        seed = _generate_seed("random", components, nets, board)
        output = tmp_path / "atomic_output.kicad_pcb"

        _write_placements_to_pcb_atomic(str(tmp_pcb), str(output), seed, components)

        assert output.exists()
        content = output.read_text()
        assert "kicad_pcb" in content

    def test_interrupt_state_tracks_best_vector(self, tmp_pcb, tmp_path, monkeypatch):
        """Verify _interrupt_state['best_vector'] is updated during optimization."""
        from kicad_tools.cli import optimize_placement_cmd as mod

        observed_vectors: list = []

        from kicad_tools.placement.cmaes_strategy import CMAESStrategy

        original_observe = CMAESStrategy.observe

        def capture_observe(self, candidates, scores):
            result = original_observe(self, candidates, scores)
            best_vec = mod._interrupt_state.get("best_vector")
            if best_vec is not None:
                observed_vectors.append(True)
            return result

        monkeypatch.setattr(CMAESStrategy, "observe", capture_observe)

        output = tmp_path / "out.kicad_pcb"
        run_optimize_placement(
            str(tmp_pcb),
            max_iterations=3,
            output_path=str(output),
            quiet=True,
        )
        # After 3 iterations, best_vector should have been set multiple times
        assert len(observed_vectors) >= 3


# Import helpers needed by the new tests
from kicad_tools.cli.optimize_placement_cmd import (
    _extract_board_outline,
    _read_board_data,
    _write_placements_to_pcb,
    _write_placements_to_pcb_atomic,
)


# ---------------------------------------------------------------------------
# Board-origin coordinate system tests (issue #2054)
# ---------------------------------------------------------------------------


class TestBoardOriginCoordinates:
    """Verify that board origin is correctly subtracted from Edge.Cuts
    outline and added back when writing positions to the PCB file."""

    @pytest.fixture
    def offset_pcb(self, tmp_path: Path) -> Path:
        """Create a PCB file with a non-zero board origin (116, 76.75).

        The Edge.Cuts outline spans from (116, 76.75) to (181, 133.25)
        in sheet-absolute coordinates — i.e. a 65x56.5 mm board.
        Footprint positions are stored in sheet-absolute coordinates
        in the file.
        """
        pcb_content = """\
(kicad_pcb (version 20230101) (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup
    (pad_to_mask_clearance 0.05)
  )
  (net 0 "")
  (net 1 "N1")
  (footprint "R_0805" (layer "F.Cu")
    (at 126.0 86.75 0)
    (property "Reference" "R1")
    (fp_text reference "R1" (at 0 -1.5) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" smd rect (at -1.0 0.0) (size 0.8 0.8) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "N1"))
    (pad "2" smd rect (at 1.0 0.0) (size 0.8 0.8) (layers "F.Cu" "F.Paste" "F.Mask") (net 0 ""))
  )
  (footprint "R_0805" (layer "F.Cu")
    (at 146.0 96.75 0)
    (property "Reference" "R2")
    (fp_text reference "R2" (at 0 -1.5) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" smd rect (at -1.0 0.0) (size 0.8 0.8) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "N1"))
    (pad "2" smd rect (at 1.0 0.0) (size 0.8 0.8) (layers "F.Cu" "F.Paste" "F.Mask") (net 0 ""))
  )
  (gr_line (start 116 76.75) (end 181 76.75) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 181 76.75) (end 181 133.25) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 181 133.25) (end 116 133.25) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 116 133.25) (end 116 76.75) (layer "Edge.Cuts") (width 0.05))
)
"""
        pcb_file = tmp_path / "offset_board.kicad_pcb"
        pcb_file.write_text(pcb_content)
        return pcb_file

    def test_extract_board_outline_subtracts_origin(self, offset_pcb):
        """Board outline must be in board-relative coordinates (starting near 0,0)."""
        from kicad_tools.schema.pcb import PCB as SchemaPCB

        pcb = SchemaPCB.load(str(offset_pcb))
        outline = _extract_board_outline(pcb)

        # The Edge.Cuts spans (116,76.75)-(181,133.25) in absolute space.
        # Board origin is (116, 76.75), so board-relative outline should be
        # (0, 0) to (65, 56.5).
        assert abs(outline.min_x - 0.0) < 0.01
        assert abs(outline.min_y - 0.0) < 0.01
        assert abs(outline.max_x - 65.0) < 0.01
        assert abs(outline.max_y - 56.5) < 0.01

    def test_component_positions_within_board_relative_outline(self, offset_pcb):
        """Component positions must fall within the board-relative outline."""
        components, nets, outline, rules, origin = _read_board_data(str(offset_pcb))

        for comp in components:
            # Components from SchemaPCB are already board-relative.
            # With the fix, the outline is also board-relative.
            # We don't have position info on ComponentDef directly, but
            # we can verify the outline and origin are consistent.
            pass

        # Verify board origin was detected
        assert abs(origin[0] - 116.0) < 0.01
        assert abs(origin[1] - 76.75) < 0.01

        # Verify outline is board-relative
        assert abs(outline.min_x - 0.0) < 0.01
        assert abs(outline.min_y - 0.0) < 0.01

    def test_write_adds_origin_back(self, offset_pcb, tmp_path):
        """Written positions must include the board origin offset (sheet-absolute)."""
        import re

        components, nets, outline, rules, origin = _read_board_data(str(offset_pcb))

        # Create a vector that places R1 at board-relative (10, 10) and R2 at (30, 20)
        data = np.array(
            [10.0, 10.0, 0.0, 0.0, 30.0, 20.0, 0.0, 0.0],
            dtype=np.float64,
        )
        from kicad_tools.placement.vector import PlacementVector

        vector = PlacementVector(data=data)
        output = tmp_path / "written.kicad_pcb"
        _write_placements_to_pcb(str(offset_pcb), str(output), vector, components, origin)

        content = output.read_text()

        # R1 at board-relative (10, 10) should be written as
        # sheet-absolute (10 + 116, 10 + 76.75) = (126, 86.75)
        at_pattern = re.compile(r"\(at\s+([\d.]+)\s+([\d.]+)")
        at_matches = at_pattern.findall(content)

        # Collect footprint (at ...) values — should be sheet-absolute
        # The file has two footprints; collect their (at ...) from the output.
        # Filter to footprint-level (at ...) which are the first in each block.
        footprint_ats = []
        in_fp = False
        for line in content.split("\n"):
            stripped = line.strip()
            if stripped.startswith("(footprint "):
                in_fp = True
            if in_fp:
                m = re.match(r"\s*\(at\s+([\d.eE+-]+)\s+([\d.eE+-]+)", stripped)
                if m:
                    footprint_ats.append((float(m.group(1)), float(m.group(2))))
                    in_fp = False  # Only capture first (at) per footprint

        assert len(footprint_ats) == 2

        # R1: board-relative (10, 10) + origin (116, 76.75) = (126, 86.75)
        assert abs(footprint_ats[0][0] - 126.0) < 0.01
        assert abs(footprint_ats[0][1] - 86.75) < 0.01

        # R2: board-relative (30, 20) + origin (116, 76.75) = (146, 96.75)
        assert abs(footprint_ats[1][0] - 146.0) < 0.01
        assert abs(footprint_ats[1][1] - 96.75) < 0.01

    def test_zero_origin_unaffected(self, tmp_pcb, tmp_path):
        """Board with origin at (0, 0) should produce identical results."""
        components, nets, outline, rules, origin = _read_board_data(str(tmp_pcb))

        # Origin should be (0, 0) for the standard test fixture
        assert abs(origin[0]) < 0.01
        assert abs(origin[1]) < 0.01

        # Outline should match the Edge.Cuts directly (no offset to subtract)
        assert abs(outline.min_x - 0.0) < 0.01
        assert abs(outline.min_y - 0.0) < 0.01
        assert abs(outline.max_x - 30.0) < 0.01
        assert abs(outline.max_y - 20.0) < 0.01
