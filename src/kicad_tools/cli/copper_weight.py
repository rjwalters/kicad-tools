"""Shared ``--copper`` (copper weight) argument parsing for CLI commands.

Extracted from :mod:`kicad_tools.cli.check_cmd` (Issue #4700) so ``kct
route`` and ``kct check`` parse the identical grammar and therefore resolve
the identical manufacturer design-rule key (``<layers>layer_<oz>oz``).  Any
divergence there is what let ``kct route --manufacturer jlcpcb`` emit copper
that the matching ``kct check --mfr jlcpcb --copper 2`` rejected.

Behavior is unchanged from the original ``check_cmd._parse_copper_weight_arg``
(Issue #4326); that name remains as an alias in ``check_cmd``.
"""

from __future__ import annotations

__all__ = ["parse_copper_weight_arg"]


def parse_copper_weight_arg(raw: str) -> tuple[float | None, float | None]:
    """Parse a ``--copper`` value into ``(outer_oz, inner_oz)`` (Issue #4326).

    Accepts two forms:

    - **Scalar** -- ``"2"`` / ``"1.0"`` -- applies to both the outer and
      inner layer classes, returning ``(oz, oz)``.
    - **Keyed** -- ``"outer=2,inner=0.5"`` -- sets each layer class
      independently.  A key omitted from the keyed form returns ``None`` for
      that class, meaning "fall back to the stackup / profile for that layer
      class" (so ``--copper outer=2`` overrides only the outer weight).

    Raises:
        ValueError: on empty input, unknown keys, duplicate keys, a
            non-numeric value, or a non-positive weight -- mirroring the
            ``--only`` / ``--skip`` category-validation contract (a clear
            ``Error:`` + exit 1 at the call site).
    """
    text = raw.strip()
    if not text:
        raise ValueError("--copper value is empty")

    if "=" not in text:
        # Scalar form: one number for both layer classes.
        try:
            oz = float(text)
        except ValueError:
            raise ValueError(
                f"invalid --copper value {raw!r} "
                "(expected a number like '2' or a keyed form 'outer=2,inner=0.5')"
            ) from None
        if oz <= 0:
            raise ValueError(f"--copper weight must be positive: {oz}")
        return (oz, oz)

    # Keyed form: comma-separated key=value tokens.
    outer: float | None = None
    inner: float | None = None
    seen: set[str] = set()
    for token in text.split(","):
        token = token.strip()
        if not token:
            continue
        if "=" not in token:
            raise ValueError(f"invalid --copper token {token!r} (expected 'key=value')")
        key, _, value = token.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if key not in ("outer", "inner"):
            raise ValueError(f"unknown --copper key {key!r} (expected 'outer' or 'inner')")
        if key in seen:
            raise ValueError(f"duplicate --copper key {key!r}")
        seen.add(key)
        try:
            oz = float(value)
        except ValueError:
            raise ValueError(
                f"invalid --copper {key} value {value!r} (expected a number)"
            ) from None
        if oz <= 0:
            raise ValueError(f"--copper {key} weight must be positive: {oz}")
        if key == "outer":
            outer = oz
        else:
            inner = oz

    if outer is None and inner is None:
        raise ValueError(f"--copper keyed form set no values: {raw!r}")
    return (outer, inner)
