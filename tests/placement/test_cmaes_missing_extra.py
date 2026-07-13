"""Tests for the actionable ImportError message when the ``cmaes`` extra is absent.

Issue #4100: on a base install (no ``placement``/``dev``/``all`` extra) the
``cmaes`` package is not present, and ``kct optimize-placement`` must fail with
a clear, actionable message naming the ``placement`` extra -- not a bare
``pip install cmaes`` hint or a raw traceback.

These tests must run regardless of whether ``cmaes`` is installed, so they do
NOT ``importorskip("cmaes")``. They simulate the missing-dependency condition
by forcing the lazy import inside :func:`_create_strategy` to raise.
"""

from __future__ import annotations

import builtins

import pytest


def test_install_hint_names_placement_extra():
    """The canonical install hint must reference the 'placement' extra.

    Guards against a regression back to the misleading bare
    ``pip install cmaes`` message (issue #4100).
    """
    # Imported lazily so this test is collectible even without cmaes; the
    # constant is defined before the guarded ``from cmaes import CMAwM`` so
    # importing it does not require cmaes to be installed... but the module
    # top-level import guard runs on import, so only assert if importable.
    pytest.importorskip("cmaes", reason="constant lives in a cmaes-guarded module")
    from kicad_tools.placement.cmaes_strategy import CMAES_INSTALL_HINT

    assert "placement" in CMAES_INSTALL_HINT
    assert "kicad-tools[placement]" in CMAES_INSTALL_HINT
    # Must not be a bare, extra-less pip install of the raw package.
    assert "pip install cmaes" not in CMAES_INSTALL_HINT


def test_create_strategy_surfaces_actionable_message_when_cmaes_missing(monkeypatch):
    """When importing the cmaes strategy fails, the CLI helper re-raises clearly.

    Simulates a base install (no ``placement`` extra) by making the lazy
    ``from kicad_tools.placement.cmaes_strategy import CMAESStrategy`` inside
    :func:`_create_strategy` raise an ``ImportError`` whose message names the
    extra, then asserts the message propagates so the CLI can print it and
    exit non-zero (rather than emit a raw traceback).
    """
    from kicad_tools.cli.optimize_placement_cmd import _create_strategy

    real_import = builtins.__import__
    hint = (
        "The 'cmaes' package is required for CMAESStrategy. "
        "Install it with the 'placement' extra: uv sync --extra placement "
        '(or: pip install "kicad-tools[placement]").'
    )

    def fake_import(name, *args, **kwargs):
        if name == "kicad_tools.placement.cmaes_strategy" or name.endswith("cmaes_strategy"):
            raise ImportError(hint)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(ImportError) as excinfo:
        _create_strategy("cmaes")

    message = str(excinfo.value)
    assert "placement" in message
    assert "kicad-tools[placement]" in message
