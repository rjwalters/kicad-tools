"""Shared guard for the optional/core ``shapely`` geometry backend.

``shapely`` is a **core** dependency of kicad-tools (declared in
``[project] dependencies`` in ``pyproject.toml``) because the project's
correctness-critical copper paths — zone-fill clearance carving and the
zone-vs-copper clearance DRC rules — cannot produce correct results
without it.

Historically ``shapely`` lived only under the optional ``geometry`` /
``dev`` extras, which produced two divergent and both-wrong conventions
across the codebase:

* **Degrade silently** — return ``0`` / no-op when ``shapely`` is
  missing.  This is *dangerous* on a correctness path because the caller
  cannot tell "nothing needed doing" from "we couldn't do anything", so
  a board with real shorts is reported as a clean fill.
* **Crash** — bare ``import shapely`` raising ``ModuleNotFoundError`` deep
  inside a DRC rule, so ``kct check`` / ``kct audit`` die with an opaque
  traceback instead of an actionable message.

This module is the single, honest convention every geometry-dependent
module should use:

* :func:`has_shapely` — cheap boolean probe (for fallback paths that have
  a legitimate pure-Python alternative, e.g. axis-aligned rect inset).
* :func:`require_shapely` — fail **loud** with an actionable install
  message when ``shapely`` is genuinely unavailable.  Use this on any
  path where there is no correct fallback, so a partial/broken
  environment never silently yields a non-clearance-correct result.

Even though ``shapely`` is now a core dependency, a broken or partial
install can still leave it unimportable, so these guards remain the
defense-in-depth layer that keeps a correctness path from ever *silently*
claiming success.
"""

from __future__ import annotations

# Friendly, actionable install hint reused everywhere a shapely-dependent
# feature is requested without shapely available.  shapely is a core
# dependency, so a missing import almost always means a broken/partial
# environment; reinstalling the package (or the geometry extra) repairs it.
SHAPELY_INSTALL_HINT = (
    "shapely is required for this geometry operation but is not importable. "
    "shapely is a core dependency of kicad-tools; reinstall it with: "
    "pip install 'kicad-tools[geometry]' (or 'pip install shapely>=2.0')."
)


_SHAPELY_AVAILABLE: bool | None = None


def has_shapely() -> bool:
    """Return ``True`` when the ``shapely`` backend is importable.

    The result is cached after the first probe.  Use this only for paths
    that have a *correct* pure-Python fallback; correctness-critical paths
    must use :func:`require_shapely` instead so a missing backend fails
    loud rather than silently degrading.
    """
    global _SHAPELY_AVAILABLE
    if _SHAPELY_AVAILABLE is None:
        try:
            import shapely  # type: ignore[import-untyped] # noqa: F401

            _SHAPELY_AVAILABLE = True
        except ImportError:
            _SHAPELY_AVAILABLE = False
    return _SHAPELY_AVAILABLE


def require_shapely(feature: str) -> None:
    """Raise :class:`ModuleNotFoundError` with an actionable hint if shapely is absent.

    Call this at the top of any function that cannot produce a correct
    result without ``shapely``.  ``feature`` names the operation so the
    error explains *what* could not run, e.g.::

        require_shapely("foreign-net pad clearance carving")

    Raising (rather than a silent ``return 0``) guarantees a correctness
    path never masquerades a skipped step as success.
    """
    if not has_shapely():
        raise ModuleNotFoundError(f"{feature}: {SHAPELY_INSTALL_HINT}")
