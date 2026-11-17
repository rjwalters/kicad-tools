"""Regression tests for nanobind dependency composition (Issue #4412).

The C++ router extension is built by the opt-in ``kct build-native`` step,
which requires an importable ``nanobind``. nanobind was declared only in the
standalone ``native`` optional-dependency extra, which was orphaned from every
set a default ``uv sync`` resolves. ``kct build-native`` then ad-hoc-installed
nanobind out of band, so an unrelated later ``uv sync`` pruned it and the next
rebuild failed on the missing import.

These tests pin the fix so it cannot silently regress:

* ``nanobind`` must appear in the default ``[dependency-groups] dev`` group
  (the group a bare ``uv sync`` installs) and in the ``all`` extra.
* ``_install_nanobind``'s failure message must name the ``native`` extra
  install hint rather than a bare ``pip install nanobind``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10 fallback
    import tomli as tomllib

import kicad_tools.cli.build_native_cmd as bnc

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _load_pyproject() -> dict:
    with _PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def _names(requirements: list[str]) -> set[str]:
    """Extract distribution names from a list of PEP 508 requirement strings."""
    names = set()
    for req in requirements:
        # Split off any version/marker specifiers; names are the leading token.
        token = req.split(";")[0].strip()
        for sep in ("[", ">", "<", "=", "!", "~", " ", "@"):
            token = token.split(sep)[0]
        names.add(token.strip().lower())
    return names


def test_nanobind_in_default_dev_dependency_group() -> None:
    """A default ``uv sync`` installs [dependency-groups].dev — nanobind must
    live there so it stays resolved and is not pruned by a later ``uv sync``."""
    data = _load_pyproject()
    dev_group = data["dependency-groups"]["dev"]
    assert "nanobind" in _names(dev_group), (
        "nanobind must be in [dependency-groups] dev so a default `uv sync` "
        "keeps it resolved (issue #4412)"
    )


def test_nanobind_in_all_extra() -> None:
    """The aggregate ``all`` extra must include nanobind for symmetry."""
    data = _load_pyproject()
    all_extra = data["project"]["optional-dependencies"]["all"]
    assert "nanobind" in _names(all_extra), "nanobind must be in the `all` extra (issue #4412)"


def test_native_extra_still_present() -> None:
    """The standalone ``native`` extra remains the public consumer surface."""
    data = _load_pyproject()
    native = data["project"]["optional-dependencies"]["native"]
    assert "nanobind" in _names(native)


def test_install_nanobind_failure_names_native_extra() -> None:
    """The failure hint must point at the `native` extra, not a bare install."""
    # Force the import to fail so _install_nanobind takes the install path,
    # and make every ad-hoc install command fail so it returns its error.
    with (
        mock.patch.dict(sys.modules, {"nanobind": None}),
        mock.patch.object(bnc.shutil, "which", return_value=None),
        mock.patch.object(
            bnc.subprocess,
            "run",
            side_effect=FileNotFoundError,
        ),
    ):
        ok, err = bnc._install_nanobind(verbose=False)

    assert ok is False
    assert err is not None
    assert "uv sync --extra native" in err
    assert "kicad-tools[native]" in err
    # And it should warn that a bare install is not lockfile-tracked.
    assert "pip install nanobind" in err
