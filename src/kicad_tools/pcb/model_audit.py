"""3D model coverage audit: classify every footprint, not just every model ref.

The older ``inspect_pcb_models`` walk only sees existing ``(model ...)`` nodes,
so a footprint with *no* model is invisible to it.  This module is the single
classifier behind ``kct check`` (``model_coverage``), ``kct pcb model-audit``,
``kct render`` and ``kct fleet status`` (issue #7682).  Each footprint gets
exactly one class from :data:`CLASSES`.

Classes
-------
``resolved``         an enabled model ref resolves to a file in the environment
``embedded``         ``kicad-embed://`` ref whose name exists in
                     ``embedded_files`` with data
``dangling``         the ref names a file that does not exist (including an
                     ``kicad-embed://`` ref with no data, and an empty path)
``no_ref``           the footprint carries no ``(model ...)`` node at all
``unresolved_var``   a ``${VAR}`` in the path is not defined in the environment
``hidden_only``      every model on the footprint is hidden
``lcsc_cache_miss``  the ref points into the LCSC STEP cache but the file is
                     not cached (``KCT_LCSC_3D_DIR`` / ``C<digits>.step``)
``lcsc_content_changed``  the ref points at a cached LCSC STEP whose sha256 no
                     longer matches the digest pinned when it was fetched
                     (the upstream body changed, or the cache was altered)
``placeholder``      the ref resolves, but to a stand-in body (see
                     :data:`PLACEHOLDER_NAME_RE`)
``exempt``           no 3D body is expected (see :data:`EXEMPT_ATTRS` and
                     :data:`EXEMPT_LIB_PATTERNS`)

``resolved`` and ``embedded`` count as *covered*; every other non-exempt class
is a gap.  Exempt footprints are excluded from the coverage denominator.

Resolution environments
-----------------------
``host``    today's render environment (``_render_env`` + native Configure
            Paths), i.e. what ``kct render`` would see on this machine.
``pinned``  the pinned KiCad toolchain image.  Its model set is not defined
            yet (issue #7683), so every model variable points at an empty
            directory: only ``${KIPRJMOD}``-relative files and embedded
            models resolve.
``dir``     a caller-supplied model directory (``--model-dir``).

Verified against host KiCad 10.0.x model libraries; the ``host`` totals depend
on the model directory installed on the machine that runs the audit.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "CLASSES",
    "COVERED_CLASSES",
    "EXEMPT_ATTRS",
    "EXEMPT_LIB_PATTERNS",
    "PLACEHOLDER_NAME_RE",
    "PINNED_MODEL_DIR",
    "FootprintCoverage",
    "ModelAuditReport",
    "audit_pcb_models",
    "build_environment",
    "classify_footprint",
    "expected_pin_count",
]

CLASSES: tuple[str, ...] = (
    "resolved",
    "embedded",
    "dangling",
    "no_ref",
    "unresolved_var",
    "hidden_only",
    "lcsc_cache_miss",
    "lcsc_content_changed",
    "placeholder",
    "exempt",
)

#: Classes that count toward coverage.
COVERED_CLASSES: frozenset[str] = frozenset({"resolved", "embedded"})

#: ``(attr ...)`` flags that mean "no 3D body expected" (data, documented).
EXEMPT_ATTRS: frozenset[str] = frozenset({"board_only", "exclude_from_bom"})

#: Case-insensitive patterns matched against the footprint ``lib_id``
#: (``Library:Name``).  These families carry no physical body.
EXEMPT_LIB_PATTERNS: tuple[str, ...] = (
    r"MountingHole",
    r"NetTie",
    r"TestPoint",
    r"Fiducial",
)

#: A resolved model whose file name matches this is a stand-in, not a body.
PLACEHOLDER_NAME_RE = re.compile(
    r"(^|[^a-z0-9])(placeholder|dummy|generic|proxy)([^a-z0-9]|$)", re.IGNORECASE
)

#: Model directory the pinned toolchain exposes.  ``None`` = it has none yet
#: (issue #7683 will supply one).
PINNED_MODEL_DIR: Path | None = None

_PINNED_SENTINEL = "/nonexistent/kct-pinned-3dmodels"

_VARIABLE = re.compile(r"\$\{([^}]+)\}|\$\(([^)]+)\)")
_EMBED_PREFIX = "kicad-embed://"
_LCSC_FILE = re.compile(r"^C\d+\.(step|stp|wrl)$", re.IGNORECASE)
# ``PinHeader_1x05_P2.54mm`` -> 1 row x 5 pins.  Anchored on a following
# ``_P<pitch>`` so ``SOIC-8_3.9x4.9mm_P1.27mm`` (a body size) never matches.
_PIN_GRID = re.compile(r"(?<![\d.])(\d{1,2})x(\d{1,3})(?=_P\d)")


@dataclass
class FootprintCoverage:
    """Coverage verdict for one footprint."""

    reference: str
    lib_id: str
    klass: str
    path: str | None = None
    reason: str = ""
    suspect: str | None = None
    #: The path variable whose model library is absent in this environment
    #: (undefined, or naming a directory that does not exist), when that --
    #: not the board -- is why the footprint is a gap.  ``None`` otherwise.
    missing_root: str | None = None

    @property
    def covered(self) -> bool:
        return self.klass in COVERED_CLASSES

    @property
    def is_gap(self) -> bool:
        return self.klass != "exempt" and not self.covered

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "reference": self.reference,
            "lib_id": self.lib_id,
            "class": self.klass,
            "path": self.path,
            "reason": self.reason,
        }
        if self.suspect:
            data["suspect"] = self.suspect
        if self.missing_root:
            data["missing_root"] = self.missing_root
        return data


@dataclass
class ModelAuditReport:
    """Coverage verdicts for every footprint on one board."""

    pcb: str
    env: str
    entries: list[FootprintCoverage] = field(default_factory=list)
    #: Per-model-ref detail, in file order: ``(reference, raw, class, path)``.
    #: Feeds the legacy ``inspect_pcb_models`` result.
    refs: list[tuple[str, str, str, str | None]] = field(default_factory=list)

    @property
    def gaps(self) -> list[FootprintCoverage]:
        return [e for e in self.entries if e.is_gap]

    @property
    def missing_roots(self) -> dict[str, list[FootprintCoverage]]:
        """Gaps caused by an absent model library, keyed by its variable."""
        roots: dict[str, list[FootprintCoverage]] = {}
        for entry in self.gaps:
            if entry.missing_root:
                roots.setdefault(entry.missing_root, []).append(entry)
        return roots

    @property
    def suspects(self) -> list[FootprintCoverage]:
        return [e for e in self.entries if e.suspect]

    def counts(self) -> dict[str, int]:
        counts = dict.fromkeys(CLASSES, 0)
        for entry in self.entries:
            counts[entry.klass] += 1
        return counts

    @property
    def total(self) -> int:
        """Non-exempt footprints (the coverage denominator)."""
        return sum(1 for e in self.entries if e.klass != "exempt")

    @property
    def covered(self) -> int:
        return sum(1 for e in self.entries if e.covered)

    @property
    def percent(self) -> float | None:
        """Covered share of non-exempt footprints; ``None`` when there are none."""
        if self.total == 0:
            return None
        return round(100.0 * self.covered / self.total, 1)

    def summary(self) -> dict[str, Any]:
        return {
            "env": self.env,
            "covered": self.covered,
            "total": self.total,
            "percent": self.percent,
            "counts": self.counts(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "pcb": self.pcb,
            **self.summary(),
            "footprints": [e.to_dict() for e in self.entries],
        }


def build_environment(
    env: str = "host",
    *,
    pcb_path: Path,
    model_dir: Path | None = None,
    kicad_cli: Path | None = None,
) -> dict[str, str]:
    """Return the variable map used to expand model paths for *env*.

    ``host`` mirrors ``inspect_pcb_models`` (render env + native Configure
    Paths); ``pinned`` exposes no model files; a *model_dir* maps every
    ``KICADn_3DMODEL_DIR`` variable to that directory.
    """
    # Imported lazily: ``cli.runner`` is heavy and imports back into ``pcb``.
    from kicad_tools.cli import runner
    from kicad_tools.pcb.lcsc_models import DEFAULT_CACHE_ENV_VAR

    if env not in ("host", "pinned", "dir"):
        raise ValueError(f"unknown model environment: {env!r}")
    if model_dir is not None:
        env = "dir"
    if env == "dir" and model_dir is None:
        raise ValueError("model_dir is required for the 'dir' environment")

    if env == "host":
        variables = runner._configured_model_variables(kicad_cli)
        variables.update(runner._render_env(kicad_cli) or os.environ)
    else:
        base = model_dir if model_dir is not None else (PINNED_MODEL_DIR or Path(_PINNED_SENTINEL))
        variables = {var: str(base) for var in runner.KICAD_3DMODEL_ENV_VARS}
        variables[DEFAULT_CACHE_ENV_VAR] = str(base / "lcsc")
    variables["KIPRJMOD"] = str(pcb_path.resolve().parent)
    return variables


def _expand(raw: str, variables: Mapping[str, str]) -> str:
    expanded = raw
    # Bounded so cyclic/unknown variables never look like resolved paths.
    for _ in range(8):
        updated = _VARIABLE.sub(
            lambda m: variables.get(m.group(1) or m.group(2), m.group(0)), expanded
        )
        if updated == expanded:
            break
        expanded = updated
    return expanded


def _embedded_names(node: Any) -> set[str]:
    """Names of ``embedded_files`` entries under *node* that carry data."""
    names: set[str] = set()
    for container in node.find_all("embedded_files"):
        for entry in container.find_all("file"):
            name = entry.get("name")
            data = entry.get("data")
            if name is None or data is None:
                continue
            if any(str(atom).strip() for atom in data.get_atoms()):
                names.add(str(name.get_first_atom()))
    return names


def _classify_ref(
    raw: str,
    variables: Mapping[str, str],
    pcb_dir: Path,
    embedded: set[str],
) -> tuple[str, str | None, str]:
    """Return ``(class, path, reason)`` for one enabled model ref."""
    if not raw:
        return "dangling", None, "empty model path"
    if raw.startswith(_EMBED_PREFIX):
        name = raw[len(_EMBED_PREFIX) :]
        if name in embedded:
            return "embedded", raw, "embedded in the PCB"
        return "dangling", raw, "embedded model has no data in embedded_files"

    expanded = _expand(raw, variables)
    base = Path(raw).name
    if _VARIABLE.search(expanded):
        from kicad_tools.pcb.lcsc_models import DEFAULT_CACHE_ENV_VAR

        if DEFAULT_CACHE_ENV_VAR in raw:
            return "lcsc_cache_miss", None, "LCSC cache directory is not configured"
        return "unresolved_var", None, "unresolved path variable"
    path = Path(expanded).expanduser()
    if not path.is_absolute():
        path = pcb_dir / path
    if path.is_file():
        if _LCSC_FILE.match(base):
            from kicad_tools.pcb.lcsc_models import check_cache_pin

            ok, message = check_cache_pin(base.rsplit(".", 1)[0].upper(), cache_dir=path.parent)
            if not ok:
                return "lcsc_content_changed", str(path), message or "LCSC body changed"
        if PLACEHOLDER_NAME_RE.search(base):
            return "placeholder", str(path), "model is a placeholder body"
        return "resolved", str(path), "model file found"
    if _LCSC_FILE.match(base) or "lcsc" in str(path.parent).lower():
        return "lcsc_cache_miss", str(path), "LCSC STEP not in the cache"
    return "dangling", str(path), "model file not found"


#: Gap classes an absent model library (rather than the board) can explain.
_ROOT_SENSITIVE = frozenset({"dangling", "unresolved_var", "lcsc_cache_miss"})


def _missing_root(raw: str, variables: Mapping[str, str]) -> str | None:
    """First variable in *raw* whose library is absent: undefined, or not a dir.

    ``KIPRJMOD`` is the board's own directory and never counts.
    """
    for match in _VARIABLE.finditer(raw):
        name = match.group(1) or match.group(2)
        if name == "KIPRJMOD":
            continue
        value = variables.get(name)
        if value is None:
            return name
        root = Path(_expand(value, variables)).expanduser()
        if _VARIABLE.search(str(root)) or not root.is_dir():
            return name
    return None


#: Worst-first: which ref decides the class when no ref is covered.
_GAP_PRIORITY = (
    "lcsc_content_changed",
    "lcsc_cache_miss",
    "unresolved_var",
    "dangling",
    "placeholder",
)


def _is_exempt(lib_id: str, attrs: set[str]) -> bool:
    if attrs & EXEMPT_ATTRS:
        return True
    return any(re.search(p, lib_id, re.IGNORECASE) for p in EXEMPT_LIB_PATTERNS)


def expected_pin_count(model_path: str) -> int | None:
    """Pin count implied by a ``..._<rows>x<cols>_P<pitch>...`` model name."""
    match = _PIN_GRID.search(Path(model_path).name)
    if match is None:
        return None
    return int(match.group(1)) * int(match.group(2))


def _pad_count(footprint: Any) -> int:
    numbers = set()
    for pad in footprint.find_all("pad"):
        if pad.get_string(1) == "np_thru_hole":
            continue
        number = pad.get_string(0)
        if number:
            numbers.add(number)
    return len(numbers)


def classify_footprint(
    footprint: Any,
    variables: Mapping[str, str],
    pcb_dir: Path,
    board_embedded: set[str],
    reference: str,
) -> tuple[FootprintCoverage, list[tuple[str, str, str, str | None]]]:
    """Classify one footprint node; also return its per-ref detail rows."""
    lib_id = str(footprint.get_first_atom() or "")
    attrs: set[str] = set()
    attr = footprint.find("attr")
    if attr is not None:
        attrs = {str(a) for a in attr.get_atoms()}
    embedded = board_embedded | _embedded_names(footprint)

    models = footprint.find_all("model")
    ref_rows: list[tuple[str, str, str, str | None]] = []
    verdicts: list[tuple[str, str | None, str, str]] = []  # (class, path, reason, raw)
    for model in models:
        hidden = model.get("hide")
        if hidden is not None and hidden.get_first_atom() != "no":
            continue
        raw = str(model.get_first_atom() or "")
        klass, path, reason = _classify_ref(raw, variables, pcb_dir, embedded)
        verdicts.append((klass, path, reason, raw))
        ref_rows.append((reference, raw, klass, path))

    if _is_exempt(lib_id, attrs):
        return FootprintCoverage(
            reference, lib_id, "exempt", reason="no 3D body expected"
        ), ref_rows
    if not models:
        return FootprintCoverage(
            reference, lib_id, "no_ref", reason="footprint has no model"
        ), ref_rows
    if not verdicts:
        return (
            FootprintCoverage(reference, lib_id, "hidden_only", reason="every model is hidden"),
            ref_rows,
        )

    chosen = next((v for v in verdicts if v[0] in COVERED_CLASSES), None)
    if chosen is None:
        for klass in _GAP_PRIORITY:
            chosen = next((v for v in verdicts if v[0] == klass), None)
            if chosen is not None:
                break
    assert chosen is not None
    klass, path, reason, raw = chosen
    entry = FootprintCoverage(reference, lib_id, klass, path=path or raw or None, reason=reason)
    if klass in _ROOT_SENSITIVE:
        entry.missing_root = _missing_root(raw, variables)
    if entry.covered:
        want = expected_pin_count(raw)
        have = _pad_count(footprint)
        if want is not None and have and want != have:
            entry.suspect = (
                f"model name implies {want} pins but the footprint has {have} pads "
                "(possible wrong-body proxy)"
            )
    return entry, ref_rows


def audit_pcb_models(
    pcb_path: Path,
    *,
    env: str = "host",
    model_dir: Path | None = None,
    kicad_cli: Path | None = None,
) -> ModelAuditReport:
    """Classify every footprint of *pcb_path* under the chosen environment."""
    from kicad_tools.schema.pcb import _is_footprint_tag
    from kicad_tools.sexp import parse_file

    doc = parse_file(pcb_path)
    if doc.name != "kicad_pcb":
        raise ValueError("Expected kicad_pcb root for model inspection")
    from kicad_tools.cli.runner import _get_fp_reference

    variables = build_environment(env, pcb_path=pcb_path, model_dir=model_dir, kicad_cli=kicad_cli)
    pcb_dir = pcb_path.resolve().parent
    board_embedded = _embedded_names(doc)

    report = ModelAuditReport(pcb=str(pcb_path), env="dir" if model_dir else env)
    for footprint in doc.children:
        if not _is_footprint_tag(footprint.name):
            continue
        reference = _get_fp_reference(footprint) or "<unknown>"
        entry, rows = classify_footprint(footprint, variables, pcb_dir, board_embedded, reference)
        report.entries.append(entry)
        report.refs.extend(rows)
    return report
