"""Tests for ``OptimizationWorkflow`` routing-fitness integration.

Verifies the KiCad-2 (Issue #2720) outer-loop swap: when
``WorkflowConfig.use_routing_fitness`` is True, the workflow constructs and
injects a ``CppAstarRoutingEvaluator`` into the placement GA.

The default (off) path must remain a no-op so existing production runs are
unchanged.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from kicad_tools.optim.workflow import OptimizationWorkflow, WorkflowConfig

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


class TestWorkflowConfigDefaults:
    def test_use_routing_fitness_defaults_false(self):
        """Default off — production runs are unchanged until A/B validates."""
        c = WorkflowConfig()
        assert c.use_routing_fitness is False

    def test_pcb_path_for_routing_defaults_none(self):
        c = WorkflowConfig()
        assert c.pcb_path_for_routing is None


# ---------------------------------------------------------------------------
# _build_routing_evaluator branching
# ---------------------------------------------------------------------------


class TestBuildRoutingEvaluator:
    def test_returns_none_when_flag_off(self):
        """The workflow does NOT construct an evaluator when the flag is off."""
        pcb = MagicMock()
        pcb._path = "/some/board.kicad_pcb"
        wf = OptimizationWorkflow(pcb=pcb, config=WorkflowConfig(use_routing_fitness=False))
        assert wf._build_routing_evaluator() is None

    def test_returns_none_when_no_pcb_path(self):
        """Without a resolvable PCB path the factory cannot be built."""
        pcb = MagicMock()
        pcb._path = None
        wf = OptimizationWorkflow(pcb=pcb, config=WorkflowConfig(use_routing_fitness=True))
        assert wf._build_routing_evaluator() is None

    def test_builds_evaluator_when_flag_on(self):
        """When flag is on AND a path is available, evaluator is constructed."""
        pcb = MagicMock()
        pcb._path = "/some/board.kicad_pcb"
        wf = OptimizationWorkflow(pcb=pcb, config=WorkflowConfig(use_routing_fitness=True))

        # Patch the factory builder so we don't need a real PCB on disk.
        sentinel_factory = MagicMock(name="router_factory")
        with patch(
            "kicad_tools.optim.router_factory.build_pcb_router_factory",
            return_value=sentinel_factory,
        ) as mock_build:
            evaluator = wf._build_routing_evaluator()

        # The factory builder was called once with the PCB path.
        mock_build.assert_called_once()
        # The returned evaluator structurally satisfies the protocol.
        assert evaluator is not None
        assert hasattr(evaluator, "evaluate_routability")
        assert callable(evaluator.evaluate_routability)

    def test_uses_explicit_pcb_path_override(self):
        """``pcb_path_for_routing`` overrides ``pcb._path`` when set."""
        pcb = MagicMock()
        pcb._path = "/wrong/board.kicad_pcb"
        wf = OptimizationWorkflow(
            pcb=pcb,
            config=WorkflowConfig(
                use_routing_fitness=True,
                pcb_path_for_routing="/right/board.kicad_pcb",
            ),
        )
        with patch(
            "kicad_tools.optim.router_factory.build_pcb_router_factory",
            return_value=MagicMock(),
        ) as mock_build:
            wf._build_routing_evaluator()
        # The override is what got passed.
        args, _ = mock_build.call_args
        assert str(args[0]) == "/right/board.kicad_pcb"

    def test_returns_none_when_factory_load_fails(self):
        """If the factory raises (e.g. invalid PCB), evaluator is silently None."""
        pcb = MagicMock()
        pcb._path = "/some/board.kicad_pcb"
        wf = OptimizationWorkflow(pcb=pcb, config=WorkflowConfig(use_routing_fitness=True))

        with patch(
            "kicad_tools.optim.router_factory.build_pcb_router_factory",
            side_effect=RuntimeError("PCB load failed"),
        ):
            assert wf._build_routing_evaluator() is None


# ---------------------------------------------------------------------------
# _run_evolutionary integration: evaluator is threaded through to the
# EvolutionaryPlacementOptimizer.from_pcb call.
# ---------------------------------------------------------------------------


class TestRunEvolutionaryRoutingFitness:
    def test_evaluator_passed_to_from_pcb_when_flag_on(self):
        """``from_pcb`` receives the constructed evaluator via kwarg."""
        pcb = MagicMock()
        pcb._path = "/some/board.kicad_pcb"
        wf = OptimizationWorkflow(
            pcb=pcb,
            config=WorkflowConfig(
                strategy="evolutionary",
                use_routing_fitness=True,
                generations=1,
                population=2,
            ),
        )

        sentinel_eval = MagicMock(name="evaluator")
        sentinel_eval.evaluate_routability = MagicMock(return_value=0.5)

        # Patch the evaluator-construction side, then patch the optimizer.
        with patch.object(wf, "_build_routing_evaluator", return_value=sentinel_eval), patch(
            "kicad_tools.optim.EvolutionaryPlacementOptimizer.from_pcb"
        ) as mock_from_pcb, patch(
            "kicad_tools.optim.add_keepout_zones"
        ):
            # The mocked optimizer needs an .optimize() that returns a stub
            # individual + matching attributes for the result-building code.
            mock_optimizer = MagicMock()
            mock_optimizer.components = []
            mock_optimizer.total_wire_length.return_value = 0.0
            mock_optimizer.compute_energy.return_value = 0.0
            mock_optimizer.optimize.return_value = MagicMock(fitness=42.0)
            mock_from_pcb.return_value = mock_optimizer

            wf._run_evolutionary()

        mock_from_pcb.assert_called_once()
        _, kwargs = mock_from_pcb.call_args
        assert kwargs.get("routing_evaluator") is sentinel_eval

    def test_evaluator_is_none_when_flag_off(self):
        """``from_pcb`` receives ``routing_evaluator=None`` when flag is off."""
        pcb = MagicMock()
        pcb._path = "/some/board.kicad_pcb"
        wf = OptimizationWorkflow(
            pcb=pcb,
            config=WorkflowConfig(
                strategy="evolutionary",
                use_routing_fitness=False,
                generations=1,
                population=2,
            ),
        )

        with patch(
            "kicad_tools.optim.EvolutionaryPlacementOptimizer.from_pcb"
        ) as mock_from_pcb, patch("kicad_tools.optim.add_keepout_zones"):
            mock_optimizer = MagicMock()
            mock_optimizer.components = []
            mock_optimizer.total_wire_length.return_value = 0.0
            mock_optimizer.compute_energy.return_value = 0.0
            mock_optimizer.optimize.return_value = MagicMock(fitness=0.0)
            mock_from_pcb.return_value = mock_optimizer

            wf._run_evolutionary()

        _, kwargs = mock_from_pcb.call_args
        assert kwargs.get("routing_evaluator") is None

    def test_evolutionary_config_carries_flag(self):
        """The EvolutionaryConfig handed to the optimizer has the flag set."""
        pcb = MagicMock()
        pcb._path = "/some/board.kicad_pcb"
        wf = OptimizationWorkflow(
            pcb=pcb,
            config=WorkflowConfig(
                strategy="evolutionary",
                use_routing_fitness=True,
                generations=1,
                population=2,
            ),
        )

        sentinel_eval = MagicMock(name="evaluator")

        with patch.object(wf, "_build_routing_evaluator", return_value=sentinel_eval), patch(
            "kicad_tools.optim.EvolutionaryPlacementOptimizer.from_pcb"
        ) as mock_from_pcb, patch("kicad_tools.optim.add_keepout_zones"):
            mock_optimizer = MagicMock()
            mock_optimizer.components = []
            mock_optimizer.total_wire_length.return_value = 0.0
            mock_optimizer.compute_energy.return_value = 0.0
            mock_optimizer.optimize.return_value = MagicMock(fitness=0.0)
            mock_from_pcb.return_value = mock_optimizer

            wf._run_evolutionary()

        _, kwargs = mock_from_pcb.call_args
        config = kwargs["config"]
        assert getattr(config, "use_routing_fitness", False) is True


# ---------------------------------------------------------------------------
# _run_hybrid: same plumbing as _run_evolutionary.
# ---------------------------------------------------------------------------


class TestRunHybridRoutingFitness:
    def test_hybrid_threads_evaluator_through(self):
        pcb = MagicMock()
        pcb._path = "/some/board.kicad_pcb"
        wf = OptimizationWorkflow(
            pcb=pcb,
            config=WorkflowConfig(
                strategy="hybrid",
                use_routing_fitness=True,
                generations=1,
                population=2,
                iterations=1,
            ),
        )

        sentinel_eval = MagicMock(name="evaluator")

        with patch.object(wf, "_build_routing_evaluator", return_value=sentinel_eval), patch(
            "kicad_tools.optim.EvolutionaryPlacementOptimizer.from_pcb"
        ) as mock_from_pcb, patch("kicad_tools.optim.add_keepout_zones"):
            mock_evo_opt = MagicMock()
            mock_evo_opt.components = []
            mock_phys_opt = MagicMock()
            mock_phys_opt.components = []
            mock_phys_opt.total_wire_length.return_value = 0.0
            mock_phys_opt.compute_energy.return_value = 0.0
            mock_evo_opt.optimize_hybrid.return_value = mock_phys_opt
            mock_from_pcb.return_value = mock_evo_opt

            wf._run_hybrid()

        _, kwargs = mock_from_pcb.call_args
        assert kwargs.get("routing_evaluator") is sentinel_eval


# ---------------------------------------------------------------------------
# Force-directed strategy is unaffected by the flag (sanity guard).
# ---------------------------------------------------------------------------


class TestForceDirectedUnaffected:
    def test_force_directed_does_not_construct_evaluator(self):
        """Setting the flag while running force-directed must not crash."""
        pcb = MagicMock()
        pcb._path = "/some/board.kicad_pcb"
        # Force-directed never reads use_routing_fitness — assert that the
        # evaluator construction is not even reached.
        wf = OptimizationWorkflow(
            pcb=pcb,
            config=WorkflowConfig(
                strategy="force-directed",
                use_routing_fitness=True,
                iterations=1,
            ),
        )

        # _build_routing_evaluator is only called from _run_evolutionary /
        # _run_hybrid — patch at the workflow boundary to guard the contract.
        with patch.object(
            wf, "_build_routing_evaluator", return_value=None
        ) as mock_build, patch(
            "kicad_tools.optim.PlacementOptimizer.from_pcb"
        ) as mock_from_pcb, patch("kicad_tools.optim.add_keepout_zones"):
            mock_optimizer = MagicMock()
            mock_optimizer.components = []
            mock_optimizer.total_wire_length.return_value = 0.0
            mock_optimizer.compute_energy.return_value = 0.0
            mock_optimizer.run.return_value = 1
            mock_from_pcb.return_value = mock_optimizer

            wf._run_force_directed(callback=None)

        # The evaluator builder is NOT called for force-directed.
        mock_build.assert_not_called()
