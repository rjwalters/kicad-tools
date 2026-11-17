"""``--seed`` does not change lattice copper, and says so (issue #7580).

The lattice engine's negotiation draws no random numbers: boards 03 and 06b
routed with ``--seed 1`` and ``--seed 2`` gave identical tracks and vias (only
the UUIDs differ).  A seed sweep on it is one sample repeated, so the flag's
help says so rather than letting a sweep pass for variation.
"""

from __future__ import annotations

from kicad_tools.cli.route_cmd import _route_parser


def _seed_help() -> str:
    for action in _route_parser()._actions:
        if "--seed" in action.option_strings:
            return " ".join(str(action.help).split())
    raise AssertionError("kct route has no --seed flag")


def test_seed_help_says_lattice_copper_does_not_depend_on_it() -> None:
    text = _seed_help()
    assert "With --route-engine lattice, --seed does not change the copper" in text
    assert "only the UUIDs differ" in text


def test_lattice_pathfinder_draws_no_random_numbers() -> None:
    # The claim the help makes, held at the source: nothing in the lattice
    # engine imports ``random`` (or numpy's generator), so there is nothing
    # for ``--seed`` to seed.  Wiring a seed in would need real randomness.
    from pathlib import Path

    import kicad_tools.router.lattice as lattice

    root = Path(lattice.__file__).parent
    for path in sorted(root.rglob("*.py")):
        src = path.read_text()
        assert "import random" not in src, path
        assert "np.random" not in src and "default_rng" not in src, path
