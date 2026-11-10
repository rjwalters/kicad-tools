"""Route the test session's kicad-cli through the pinned image (#8779).

``KCT_KICAD_DOCKER=1`` only takes effect inside
``kicad_toolchain.evidence_toolchain()``; tests that call ``find_kicad_cli()``
directly never entered it, so they silently ran on the host's KiCad release.
``pinned_session()`` enters it once for the whole session.  Without the env var
it is a no-op, so CI and ordinary local runs are unchanged.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator


def _toolchain():
    from kicad_tools.cli import kicad_toolchain

    return kicad_toolchain


@contextlib.contextmanager
def pinned_session() -> Iterator[object | None]:
    """Yield the active ``EvidenceToolchain`` under ``KCT_KICAD_DOCKER=1``, else ``None``.

    Raises ``KiCadEnvironmentError`` (docker missing, wrong version, no
    kicad-cli) rather than letting tests fall back to the host binary.
    """
    tc = _toolchain()
    if not tc.docker_fallback_enabled():
        yield None
        return
    with tc.evidence_toolchain() as toolchain:
        yield toolchain


def report_header() -> list[str]:
    """Header lines naming the kicad-cli the session will use."""
    tc = _toolchain()
    if tc.docker_fallback_enabled():
        return [
            f"kicad-cli: {tc.KICAD_DOCKER_ENV}=1 -> pinned image {tc.docker_image()} "
            f"(version {tc.PINNED_KICAD_VERSION} enforced at session start; "
            "host kicad-cli is not used)"
        ]
    from kicad_tools.cli.runner import find_kicad_cli

    found = find_kicad_cli()
    find_kicad_cli.cache_clear()
    return [
        f"kicad-cli: {found or 'not found'} (host; set {tc.KICAD_DOCKER_ENV}=1 for the pinned image)"
    ]
