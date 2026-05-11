"""Tests for router determinism via ``--seed`` (Issue #2589).

Verifies that seeding Python's global ``random`` module makes the
unseeded escape-strategy callsites in
``kicad_tools.router.algorithms.negotiated`` (and the MST trial shuffle
in ``kicad_tools.router.core``) deterministic.

Rationale
=========

The CI-visible symptom on issue #2589 was board 03 producing 0-3 DRC
errors run-to-run with ``kct route --backend python``.  The root cause
was unseeded ``random.shuffle`` / ``random.sample`` calls in:

* ``algorithms/negotiated.py::_escape_shuffle_order``
* ``algorithms/negotiated.py::_escape_random_subset``
* ``algorithms/negotiated.py::_escape_full_reorder``
* ``core.py`` MST fine-grid trial loop

These calls all consume the process-wide ``random`` state, which is
otherwise seeded from ``os.urandom`` and varies per process.

This test suite verifies that:

1. ``random.seed(N)`` makes ``random.shuffle`` reproducible on a fixed
   input -- a direct check that the primary mechanism works.
2. The ``kct route`` CLI exposes ``--seed`` and the value reaches the
   ``main()`` function (smoke test of the wiring).

The full end-to-end "two ``kct route`` invocations produce identical
output" verification is left to manual / CI testing because routing
even a small board takes 30+ seconds, which is too slow for the unit
test suite.  The smoke test below at least confirms the seed is
plumbed through to ``route_main``.
"""

from __future__ import annotations

import random
from pathlib import Path
from unittest.mock import patch


def test_global_random_seed_is_reproducible():
    """``random.seed(N)`` makes ``random.shuffle`` byte-identical.

    This is the underlying mechanism the ``--seed`` flag relies on:
    after ``random.seed(42)``, two calls to ``random.shuffle`` on the
    same input produce the same permutation.  If this ever breaks
    (e.g., Python changes its PRNG implementation in a way that
    invalidates seed reproducibility), the determinism guarantee of
    ``kct route --seed`` no longer holds.
    """
    items_a = list(range(20))
    items_b = list(range(20))

    random.seed(42)
    random.shuffle(items_a)

    random.seed(42)
    random.shuffle(items_b)

    assert items_a == items_b, (
        "random.seed(N) must make random.shuffle reproducible -- "
        "this is the core mechanism --seed relies on"
    )


def test_global_random_seed_differs_between_seeds():
    """Different seeds produce different shuffles.

    This guards against the seed value being ignored (e.g., if the CLI
    layer reads ``--seed`` but never calls ``random.seed``).  If two
    different seed values shuffled the same list to the same
    permutation, the seed parameter would be a no-op.
    """
    items_a = list(range(20))
    items_b = list(range(20))

    random.seed(42)
    random.shuffle(items_a)

    random.seed(7)
    random.shuffle(items_b)

    assert items_a != items_b, (
        "different seeds should produce different shuffles on a non-trivial input (20 items)"
    )


def test_negotiated_random_callsites_use_global_random():
    """The four escape/MST random callsites import the ``random`` module.

    This test guards against accidental refactoring that introduces a
    per-instance ``random.Random()`` without also threading the seed
    through.  If any escape strategy is rewritten to use a local
    ``random.Random(seed=...)``, the ``--seed`` CLI flag would silently
    stop working for that strategy -- the test fails fast and the
    refactor must also update the CLI plumbing.

    The check is intentionally lightweight (substring match): a deeper
    AST inspection would be brittle to formatting changes.
    """
    from kicad_tools.router import core
    from kicad_tools.router.algorithms import negotiated

    neg_src = Path(negotiated.__file__).read_text(encoding="utf-8")
    core_src = Path(core.__file__).read_text(encoding="utf-8")

    # The four sites the curator's investigation identified.
    assert "random.shuffle(shuffled)" in neg_src, (
        "expected _escape_shuffle_order to call random.shuffle(shuffled); "
        "if you moved this to a per-instance RNG, update route_cmd.py too"
    )
    assert "random.sample(nets_to_reroute" in neg_src, (
        "expected _escape_random_subset to call "
        "random.sample(nets_to_reroute, ...); if you moved this to a "
        "per-instance RNG, update route_cmd.py too"
    )
    assert "random.shuffle(remaining)" in neg_src, (
        "expected _escape_full_reorder to call random.shuffle(remaining); "
        "if you moved this to a per-instance RNG, update route_cmd.py too"
    )
    assert "random.shuffle(trial_pads)" in core_src, (
        "expected MST trial loop in core.py to call "
        "random.shuffle(trial_pads); if you moved this to a "
        "per-instance RNG, update route_cmd.py too"
    )


def test_route_cmd_accepts_seed_argument():
    """``kct route --seed N`` is accepted by the argparse parser.

    Tests the CLI surface: the route command's argparse accepts a
    ``--seed`` integer.  Uses a malformed PCB path so the parse
    succeeds but the load fails fast -- we only want to exercise the
    argparse layer.
    """
    from kicad_tools.cli.route_cmd import main as route_main

    # Use a non-existent pcb path; main() will fail at the
    # "file not found" check, but parse_args must accept --seed first.
    # If --seed weren't registered, argparse would exit(2) before the
    # path check.
    rc = route_main(["/nonexistent/path/file.kicad_pcb", "--seed", "42"])
    # Expected: rc == 1 (file not found), NOT exit code 2 (parse error)
    assert rc == 1, (
        f"expected route main() to return 1 (file not found) after "
        f"accepting --seed; got {rc}.  If rc == 2, argparse rejected "
        f"--seed -- check the add_argument call in route_cmd.py."
    )


def test_route_cmd_seed_is_optional():
    """``--seed`` is not required.

    Default behaviour (no ``--seed``) must continue to work, otherwise
    every existing caller of ``kct route`` would suddenly need to pass
    a seed.
    """
    from kicad_tools.cli.route_cmd import main as route_main

    rc = route_main(["/nonexistent/path/file.kicad_pcb"])
    # Same as above: rc == 1 (file not found), not 2 (parse error)
    assert rc == 1, (
        f"expected route main() to accept invocation with no --seed; "
        f"got rc={rc} (parse error would be 2)"
    )


def test_route_cmd_calls_random_seed_when_seed_provided():
    """When ``--seed N`` is passed, ``random.seed(N)`` is called.

    Patches ``random.seed`` and verifies it's invoked with the right
    integer.  This is the direct check that the CLI wiring connects
    ``--seed`` to the global RNG -- not just that the argument is
    accepted.
    """
    from kicad_tools.cli import route_cmd

    with patch.object(route_cmd.random, "seed") as mock_seed:
        # Invoke with a bogus PCB so we bail out early (after the
        # seed call but before any router work).
        route_cmd.main(["/nonexistent/path/file.kicad_pcb", "--seed", "12345"])

    # Verify random.seed was called at least once with 12345.
    seed_calls = [c.args[0] for c in mock_seed.call_args_list if c.args]
    assert 12345 in seed_calls, (
        f"expected random.seed(12345) call when --seed 12345 is passed; "
        f"observed seed calls: {seed_calls}"
    )


def test_route_cmd_does_not_call_random_seed_when_seed_omitted():
    """When ``--seed`` is omitted, ``random.seed`` is NOT called.

    Preserves existing run-to-run variance behaviour for users who
    don't opt into determinism.  If we silently seeded on every run,
    the route output would be reproducible by accident -- but only
    until someone reordered the route command's initialization,
    making the regression hard to spot.
    """
    from kicad_tools.cli import route_cmd

    with patch.object(route_cmd.random, "seed") as mock_seed:
        route_cmd.main(["/nonexistent/path/file.kicad_pcb"])

    assert mock_seed.call_count == 0, (
        f"expected random.seed to NOT be called when --seed is omitted; "
        f"got {mock_seed.call_count} call(s).  This would silently make "
        f"every route run deterministic, masking future regressions in "
        f"the unseeded code path."
    )
