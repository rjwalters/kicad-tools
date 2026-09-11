"""Operating-state component electrical-stress gate (MOSFET VDS / VGS).

Advisory-only analyzer (issue #5039) that answers a question no existing check
in this repository asks: **is the device itself rated for the potential
difference its own terminals will see, in each operating state the designer has
declared?**

Why the existing checks structurally miss this
----------------------------------------------

* :mod:`kicad_tools.erc` checks pin-type/connectivity rule violations, not
  device absolute-maximum ratings.
* :mod:`kicad_tools.drc` / :mod:`kicad_tools.creepage` check **PCB geometry**.
  A creepage PASS says the *copper* is far enough apart; it says nothing about
  whether the *device* bridging those two nodes can stand the voltage. A
  footprint-level creepage waiver therefore can never suppress a finding from
  this module -- the two are computed from disjoint inputs.
* :mod:`kicad_tools.analysis.electrical_rating` is the closest precedent (it
  already does sourced-rating-vs-stress for capacitors) but is structurally
  **single-state and single-terminal**: ``infer_rail_voltage()`` derives one
  voltage per net from the net *name*. It cannot represent a net that is +90V
  in one state and -169V in another, and has no notion of a *differential*
  terminal-to-terminal stress across two independently-swinging nodes.

Model
-----

Stress is always evaluated as a **terminal-to-terminal differential inside a
single declared state**::

    VDS = V(drain net)  - V(source net)
    VGS = V(gate net)   - V(source net)

Both potentials come from the *same* entry of an explicit, user-authored
operating-state manifest, so the analyzer can never combine one net's extreme
with another net's unrelated extreme. Because both terminals are read from one
state, translating a whole floating driver domain (shifting D, G and S by the
same offset) leaves the computed differentials unchanged -- by construction.

**No automatic circuit-state inference is performed.** The manifest is a
required, reviewed input; the analyzer never synthesizes a state on its own.
A (component, state) pair the manifest does not cover is ``UNRESOLVED`` for
that state -- never silently omitted, and never a PASS.

Ratings are read from **schematic symbol fields only** (``Vds_max`` /
``Vgs_max``, case-insensitively) together with a source citation
(``Rating_Source`` / ``Datasheet``). There is no built-in rating table and no
default: a missing, unparseable or uncited rating is ``UNRESOLVED``.

Pin roles (D / G / S) resolve, in order, from explicit ``Pin_D`` / ``Pin_G`` /
``Pin_S`` symbol fields, then the embedded library symbol's pin *names*, then a
``..._GDS``-style pin-order suffix on the ``lib_id``. The resolved mapping is
cached per reference under an identity that includes the MPN and the footprint,
so swapping either invalidates the cached mapping instead of carrying a stale
pinout across the part swap.

This module is advisory only: :meth:`ComponentStressAnalyzer.analyze` **never
raises** -- every failure path degrades to an ``UNRESOLVED`` census row with a
human-readable reason (mirroring the advisory contract of
:mod:`kicad_tools.analysis.electrical_rating` and
:mod:`kicad_tools.analysis.current_sense`).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kicad_tools.operations.netlist import build_netlist_from_schematic
from kicad_tools.spec.units import parse_unit_value

if TYPE_CHECKING:
    from kicad_tools.operations.netlist import Netlist
    from kicad_tools.schema.library import LibrarySymbol
    from kicad_tools.schema.symbol import SymbolInstance

__all__ = [
    "CHECK_VDS",
    "CHECK_VGS",
    "REQUIRED_COVERAGE_STATES",
    "STATUS_FAIL",
    "STATUS_PASS",
    "STATUS_UNRESOLVED",
    "ComponentStressAnalyzer",
    "ComponentStressResult",
    "OperatingState",
    "OperatingStateManifest",
    "PinRoleCache",
    "PinRoleMap",
    "normalize_state_name",
]

# Status vocabulary. UNRESOLVED (rather than the sibling analyzers' SKIP) is
# deliberate: the issue requires unknown states/ratings to stay *visible*
# release blockers, not quiet omissions.
STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_UNRESOLVED = "UNRESOLVED"

CHECK_VDS = "vds"
CHECK_VGS = "vgs"

# The coverage checklist required by issue #5039. A MOSFET that the manifest
# does not place in one of these states is UNRESOLVED for that state. Override
# per project with a ``required_states:`` key in the manifest.
REQUIRED_COVERAGE_STATES: tuple[str, ...] = (
    "startup",
    "precharge",
    "mains_positive",
    "mains_negative",
    "support",
    "trip",
    "loss_of_drive",
)

# Terminal pair evaluated by each check: (check, high-side role, low-side role).
_CHECK_TERMINALS: tuple[tuple[str, str, str], ...] = (
    (CHECK_VDS, "D", "S"),
    (CHECK_VGS, "G", "S"),
)

# Rating field consulted per check, in preference order.
_CHECK_RATING_FIELDS: dict[str, tuple[str, ...]] = {
    CHECK_VDS: ("Vds_max", "VDSS_max", "VDSS", "Vds"),
    CHECK_VGS: ("Vgs_max", "VGS_max", "Vgs"),
}

# Symbol fields carrying the manufacturer part number, in preference order.
_MPN_FIELDS: tuple[str, ...] = (
    "MPN",
    "Manufacturer_Part_Number",
    "Manufacturer Part Number",
    "Part_Number",
    "PartNumber",
    "LCSC",
)

# Symbol fields carrying the provenance of the ratings above.
_SOURCE_FIELDS: tuple[str, ...] = (
    "Rating_Source",
    "Ratings_Source",
    "Rating_Datasheet",
    "Datasheet",
)

# Accepted spellings of each terminal role, used when reading library pin names
# and explicit ``Pin_*`` symbol fields.
_ROLE_ALIASES: dict[str, frozenset[str]] = {
    "D": frozenset({"D", "DRAIN"}),
    "G": frozenset({"G", "GATE"}),
    "S": frozenset({"S", "SOURCE"}),
}

# Library parts recognised as MOSFETs by lib_id. Kept as substring hints so the
# check fires on the dominant Device:Q_NMOS_* / Q_PMOS_* conventions without a
# hard dependency on one symbol library.
_MOSFET_LIB_HINTS: tuple[str, ...] = ("NMOS", "PMOS", "MOSFET", "NFET", "PFET")

# ``Q_NMOS_GDS`` -> pins 1,2,3 are G,D,S. Only a full permutation of the three
# role letters is honoured.
_PIN_ORDER_SUFFIX_RE = re.compile(r"_([DGS]{3})$", re.IGNORECASE)

_STATE_NAME_RE = re.compile(r"[^a-z0-9]+")

# Assumptions that hold for every row this module emits. Surfaced per-row so a
# reviewer never has to infer what the number does and does not cover.
_MODEL_ASSUMPTIONS: tuple[str, ...] = (
    "declared steady-state node potentials only; transients, ringing, dv/dt and "
    "avalanche are not modelled",
    "stress magnitude compared against the absolute-maximum rating; SOA, thermal "
    "and repetitive-pulse limits are not checked",
)


def normalize_state_name(name: str) -> str:
    """Normalize an operating-state name for matching.

    ``"Mains-Negative"``, ``"mains negative"`` and ``"mains_negative"`` all
    normalize to ``"mains_negative"``. Never raises.
    """
    if not name:
        return ""
    return _STATE_NAME_RE.sub("_", str(name).strip().lower()).strip("_")


def _normalize_net_name(name: str | None) -> str:
    """Normalize a net name for tolerant manifest lookup."""
    if not name:
        return ""
    return str(name).strip().lstrip("/").upper()


def _parse_voltage(raw: object) -> float | None:
    """Parse a voltage-ish value to volts, or ``None``. Never raises.

    Accepts finite plain numbers (``-169.3``), voltage unit strings
    (``"100V"``) and the ``±20V`` / ``+/-20V`` absolute-maximum notation
    datasheets use for VGS. Non-voltage units and non-finite values are invalid.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        try:
            value = float(raw)
        except OverflowError:
            return None
        return value if math.isfinite(value) else None
    text = str(raw).strip()
    if not text:
        return None
    for marker in ("±", "+/-", "+-"):
        if text.startswith(marker):
            text = text[len(marker) :].strip()
            break
    try:
        try:
            value = float(text)
        except ValueError:
            parsed = parse_unit_value(text)
            if parsed.unit != "V":
                return None
            value = float(parsed.value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _get_field(sym: SymbolInstance, *names: str) -> str | None:
    """Case-insensitively fetch the first present symbol field value.

    ``SymbolInstance.get_property`` is exact-match; designers may write
    ``Vds_max`` / ``VDS_MAX`` / ``vds_max``, so fall back to a case-insensitive
    scan of the symbol's properties (mirrors ``electrical_rating._get_field``).
    """
    for name in names:
        try:
            val = sym.get_property(name)
        except Exception:
            val = None
        if val is not None and str(val).strip():
            return str(val)
    wanted = {n.lower() for n in names}
    try:
        props = sym.properties
    except Exception:
        return None
    for prop_name, prop in props.items():
        if prop_name.lower() in wanted:
            value = getattr(prop, "value", None)
            if value is not None and str(value).strip():
                return str(value)
    return None


# ---------------------------------------------------------------------------
# Operating-state manifest
# ---------------------------------------------------------------------------
@dataclass
class OperatingState:
    """One declared operating state: a correlated snapshot of node potentials.

    Attributes:
        name: Normalized state name (see :func:`normalize_state_name`).
        raw_name: The name exactly as written in the manifest.
        potentials: Net name -> node potential in volts. All entries belong to
            the *same* instant, which is what makes a differential stress
            meaningful.
        description: Optional human-readable description.
        assumptions: Optional reviewer note carried onto every row for the
            state (e.g. "steady state, 10% mains high line").
        source: Optional provenance for the declared potentials.
    """

    name: str
    raw_name: str
    potentials: dict[str, float] = field(default_factory=dict)
    description: str | None = None
    assumptions: str | None = None
    source: str | None = None

    def potential(self, net_name: str | None) -> float | None:
        """Return the declared potential for *net_name*, or ``None``.

        Matching is exact first, then tolerant of a leading ``/`` and of case,
        so a manifest written as ``SRC_POS`` also matches a netlist's
        ``/SRC_POS``.
        """
        if not net_name:
            return None
        if net_name in self.potentials:
            return self.potentials[net_name]
        wanted = _normalize_net_name(net_name)
        for key, value in self.potentials.items():
            if _normalize_net_name(key) == wanted:
                return value
        return None


@dataclass
class OperatingStateManifest:
    """A reviewed set of operating states, loaded from YAML or JSON.

    The manifest is the *only* source of node potentials -- this module never
    infers a state, a rail voltage, or a net's potential from its name.

    Schema (both forms accepted)::

        version: 1
        required_states: [startup, precharge, ...]   # optional override
        states:
          startup:
            description: "bank empty, drives off"
            source: "architecture rev-B section 3"
            assumptions: "steady state"
            nets:
              SRC_POS: 0
              BANK_POS: 0
              GATE_A: 0
          mains_negative:                            # shorthand: net -> volts
            SRC_POS: -169.3
            BANK_POS: 90.0
            GATE_A: 89.8
    """

    states: dict[str, OperatingState] = field(default_factory=dict)
    required_states: tuple[str, ...] = REQUIRED_COVERAGE_STATES
    source_path: str | None = None

    # -- construction -------------------------------------------------
    @classmethod
    def from_dict(cls, data: Any, source_path: str | None = None) -> OperatingStateManifest:
        """Build a manifest from an already-parsed mapping.

        Raises:
            ValueError: If the document is not a mapping, has no ``states``
                mapping, or declares a non-numeric node potential. The manifest
                is an explicit user input, so a malformed one is reported
                loudly rather than silently treated as "no states declared".
        """
        if not isinstance(data, dict):
            raise ValueError("operating-state manifest must be a mapping at the top level")

        raw_states = data.get("states")
        if raw_states is None:
            raise ValueError("operating-state manifest has no 'states' mapping")
        if not isinstance(raw_states, dict):
            raise ValueError("operating-state manifest 'states' must be a mapping")

        states: dict[str, OperatingState] = {}
        for raw_name, body in raw_states.items():
            name = normalize_state_name(str(raw_name))
            if not name:
                raise ValueError(
                    f"operating-state manifest has an unusable state name: {raw_name!r}"
                )
            if not isinstance(body, dict):
                raise ValueError(f"state {raw_name!r} must be a mapping of nets to potentials")

            if "nets" in body:
                raw_nets = body.get("nets") or {}
                if not isinstance(raw_nets, dict):
                    raise ValueError(f"state {raw_name!r}: 'nets' must be a mapping")
                description = body.get("description")
                assumptions = body.get("assumptions")
                source = body.get("source")
            else:
                raw_nets = body
                description = None
                assumptions = None
                source = None

            potentials: dict[str, float] = {}
            for net, value in raw_nets.items():
                volts = _parse_voltage(value)
                if volts is None:
                    raise ValueError(
                        f"state {raw_name!r}: net {net!r} has a non-numeric potential {value!r}"
                    )
                potentials[str(net)] = volts

            states[name] = OperatingState(
                name=name,
                raw_name=str(raw_name),
                potentials=potentials,
                description=None if description is None else str(description),
                assumptions=None if assumptions is None else str(assumptions),
                source=None if source is None else str(source),
            )

        required = data.get("required_states")
        if required is None:
            required_states = REQUIRED_COVERAGE_STATES
        else:
            if not isinstance(required, (list, tuple)):
                raise ValueError("'required_states' must be a list of state names")
            required_states = tuple(
                normalize_state_name(str(item)) for item in required if str(item)
            )

        return cls(states=states, required_states=required_states, source_path=source_path)

    @classmethod
    def load(cls, path: str | Path) -> OperatingStateManifest:
        """Load a manifest from a ``.yaml`` / ``.yml`` / ``.json`` file.

        Raises:
            FileNotFoundError: If *path* does not exist.
            ValueError: If the document cannot be parsed or is malformed.
        """
        manifest_path = Path(path)
        if not manifest_path.exists():
            raise FileNotFoundError(f"Operating-state manifest not found: {manifest_path}")

        text = manifest_path.read_text(encoding="utf-8")
        data: Any
        if manifest_path.suffix.lower() == ".json":
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON in {manifest_path}: {exc}") from exc
        else:
            import yaml  # type: ignore[import-untyped]

            try:
                data = yaml.safe_load(text)
            except yaml.YAMLError as exc:
                raise ValueError(f"Invalid YAML in {manifest_path}: {exc}") from exc

        return cls.from_dict(data, source_path=str(manifest_path))

    # -- queries ------------------------------------------------------
    def get(self, state_name: str) -> OperatingState | None:
        """Return the declared state, or ``None`` if the manifest omits it."""
        return self.states.get(normalize_state_name(state_name))

    def coverage_states(self) -> list[str]:
        """Return every state to emit rows for: required first, then extras.

        The required checklist always appears even when the manifest omits it --
        that gap is the finding, so it must never be silently dropped.
        """
        ordered: list[str] = []
        for name in self.required_states:
            if name and name not in ordered:
                ordered.append(name)
        for name in sorted(self.states):
            if name not in ordered:
                ordered.append(name)
        return ordered


# ---------------------------------------------------------------------------
# Pin-role resolution
# ---------------------------------------------------------------------------
@dataclass
class PinRoleMap:
    """Resolved D/G/S terminal roles for one component.

    Attributes:
        roles: Role letter (``"D"`` / ``"G"`` / ``"S"``) -> pin number.
        origin: How the mapping was derived, surfaced as a row assumption.
    """

    roles: dict[str, str] = field(default_factory=dict)
    origin: str = ""

    def pin(self, role: str) -> str | None:
        return self.roles.get(role)

    def complete(self) -> bool:
        return all(role in self.roles for role in ("D", "G", "S"))


class PinRoleCache:
    """Per-reference pin-role cache keyed on the component's part identity.

    The cache key includes the MPN and the footprint, and the cached entry for
    a reference is **dropped** as soon as either changes. A part swap therefore
    can never carry a stale D/G/S mapping forward (issue #5039 acceptance
    criterion), which a cache keyed on the reference designator alone would.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, ...], PinRoleMap | None] = {}
        self._identity_by_ref: dict[str, tuple[str, ...]] = {}
        self.hits = 0
        self.misses = 0
        self.invalidations = 0

    @staticmethod
    def identity(sym: SymbolInstance) -> tuple[str, ...]:
        """Return the part-identity tuple a cached mapping is valid for."""
        explicit_parts: list[str] = []
        for role in sorted(_ROLE_ALIASES):
            names = _pin_field_names(role)
            explicit_parts.append(f"{role}={_get_field(sym, *names) or ''}")
        explicit = "|".join(explicit_parts)
        return (
            str(getattr(sym, "reference", "") or ""),
            str(getattr(sym, "lib_id", "") or ""),
            _get_field(sym, *_MPN_FIELDS) or "",
            str(getattr(sym, "footprint", "") or ""),
            explicit,
        )

    def resolve(
        self,
        sym: SymbolInstance,
        lib_symbol: LibrarySymbol | None,
    ) -> PinRoleMap | None:
        """Return the component's pin-role map, using the cache when valid."""
        ref = str(getattr(sym, "reference", "") or "")
        key = self.identity(sym)

        previous = self._identity_by_ref.get(ref)
        if previous is not None and previous != key:
            # MPN / footprint / lib_id / explicit-field change -> the cached
            # mapping describes a different part. Drop it outright.
            self._entries.pop(previous, None)
            self.invalidations += 1

        if key in self._entries:
            self.hits += 1
            return self._entries[key]

        self.misses += 1
        resolved = resolve_pin_roles(sym, lib_symbol)
        self._entries[key] = resolved
        self._identity_by_ref[ref] = key
        return resolved


def resolve_pin_roles(
    sym: SymbolInstance,
    lib_symbol: LibrarySymbol | None,
) -> PinRoleMap | None:
    """Resolve a MOSFET's D/G/S pin numbers, or ``None`` if indeterminate.

    Resolution order (first complete mapping wins):

    1. Explicit ``Pin_D`` / ``Pin_G`` / ``Pin_S`` symbol fields.
    2. The embedded library symbol's pin *names* (``D``/``DRAIN`` etc.).
    3. A ``..._GDS``-style pin-order suffix on the ``lib_id`` (3-pin symbols).

    Never raises; an ambiguous or incomplete mapping returns ``None`` so the
    caller reports UNRESOLVED rather than guessing a pinout.
    """
    try:
        explicit = _roles_from_fields(sym)
        if explicit is not None:
            return explicit

        by_name = _roles_from_pin_names(lib_symbol)
        if by_name is not None:
            return by_name

        return _roles_from_lib_id_suffix(sym, lib_symbol)
    except Exception:
        return None


def _pin_field_names(role: str) -> tuple[str, ...]:
    """Return the accepted explicit pin-role field names for *role*.

    ``"D"`` -> ``("Pin_D", "Pin_Drain")``.
    """
    return tuple(f"Pin_{alias.title()}" for alias in sorted(_ROLE_ALIASES[role], key=len))


def _roles_from_fields(sym: SymbolInstance) -> PinRoleMap | None:
    roles: dict[str, str] = {}
    for role in _ROLE_ALIASES:
        value = _get_field(sym, *_pin_field_names(role))
        if value:
            roles[role] = str(value).strip()
    if len(roles) == 3:
        return PinRoleMap(roles=roles, origin="explicit Pin_D/Pin_G/Pin_S symbol fields")
    return None


def _roles_from_pin_names(lib_symbol: LibrarySymbol | None) -> PinRoleMap | None:
    if lib_symbol is None:
        return None
    pins = getattr(lib_symbol, "pins", None) or []
    roles: dict[str, str] = {}
    for role, aliases in _ROLE_ALIASES.items():
        matches = [
            str(p.number)
            for p in pins
            if str(getattr(p, "name", "") or "").strip().upper() in aliases
        ]
        if len(matches) == 1:
            roles[role] = matches[0]
        elif len(matches) > 1:
            # Ambiguous (e.g. a multi-source power FET): refuse to guess.
            return None
    if len(roles) == 3:
        return PinRoleMap(roles=roles, origin="library symbol pin names")
    return None


def _roles_from_lib_id_suffix(
    sym: SymbolInstance,
    lib_symbol: LibrarySymbol | None,
) -> PinRoleMap | None:
    lib_id = str(getattr(sym, "lib_id", "") or "")
    part = lib_id.split(":")[-1]
    match = _PIN_ORDER_SUFFIX_RE.search(part)
    if not match:
        return None
    letters = match.group(1).upper()
    if set(letters) != {"D", "G", "S"}:
        return None
    if lib_symbol is not None and len(getattr(lib_symbol, "pins", None) or []) != 3:
        return None
    roles = {letter: str(index) for index, letter in enumerate(letters, start=1)}
    return PinRoleMap(roles=roles, origin=f"lib_id pin-order suffix '_{letters}'")


# ---------------------------------------------------------------------------
# Result rows
# ---------------------------------------------------------------------------
@dataclass
class ComponentStressResult:
    """One census row for a (component, operating state, terminal pair).

    Attributes:
        reference: Component reference designator (e.g. ``Q1A``).
        mpn: Manufacturer part number read from the symbol, or ``None``.
        state: Normalized operating-state name this row was evaluated in.
        check: ``"vds"`` or ``"vgs"``; ``"analysis"`` identifies a schematic
            or netlist load failure (reference ``"?"``, empty state).
        status: ``"PASS"``, ``"FAIL"`` or ``"UNRESOLVED"``. UNRESOLVED means
            the data needed to judge the terminal pair was absent -- it is
            never a silent pass.
        high_terminal / low_terminal: Terminal roles the differential is taken
            between (``"D"`` minus ``"S"``, ``"G"`` minus ``"S"``).
        high_net / low_net: Nets bound to those terminals, or ``None``.
        high_potential_v / low_potential_v: Declared potentials for those nets
            in this state (volts), or ``None``.
        stress_v: Signed differential ``V(high) - V(low)`` (volts).
        rated_v: Sourced absolute-maximum rating (volts), or ``None``.
        margin_v: ``rated_v - abs(stress_v)``; negative on a FAIL.
        assumptions: Model/state assumptions that qualify this row.
        source: Provenance of the rating (and the state, when declared).
        reason: Why the row is UNRESOLVED, or ``None``.
    """

    reference: str
    state: str
    check: str
    status: str
    mpn: str | None = None
    high_terminal: str | None = None
    low_terminal: str | None = None
    high_net: str | None = None
    low_net: str | None = None
    high_potential_v: float | None = None
    low_potential_v: float | None = None
    stress_v: float | None = None
    rated_v: float | None = None
    margin_v: float | None = None
    assumptions: list[str] = field(default_factory=list)
    source: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to a JSON-serializable dict (mirrors sibling analyzers)."""

        def _r(value: float | None, ndigits: int = 3) -> float | None:
            return None if value is None else round(value, ndigits)

        out: dict[str, Any] = {
            "reference": self.reference,
            "mpn": self.mpn,
            "state": self.state,
            "check": self.check,
            "status": self.status,
            "high_terminal": self.high_terminal,
            "low_terminal": self.low_terminal,
            "high_net": self.high_net,
            "low_net": self.low_net,
            "high_potential_v": _r(self.high_potential_v),
            "low_potential_v": _r(self.low_potential_v),
            "stress_v": _r(self.stress_v),
            "rated_v": _r(self.rated_v),
            "margin_v": _r(self.margin_v),
            "assumptions": list(self.assumptions),
            "source": self.source,
        }
        if self.reason is not None:
            out["reason"] = self.reason
        return out


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------
class ComponentStressAnalyzer:
    """Per-state MOSFET VDS/VGS stress gate over a schematic.

    Advisory only; :meth:`analyze` never raises. Construct with a reviewed
    :class:`OperatingStateManifest`, then call :meth:`analyze` with a
    ``.kicad_sch`` path.
    """

    def __init__(
        self,
        manifest: OperatingStateManifest,
        require_rating_source: bool = True,
    ) -> None:
        """Initialize the analyzer.

        Args:
            manifest: The reviewed operating-state manifest. Required -- this
                analyzer performs no automatic circuit-state inference.
            require_rating_source: When ``True`` (default) a rating without a
                ``Rating_Source`` / ``Datasheet`` citation is UNRESOLVED, per
                the issue's "source-backed ratings only" requirement. Set
                ``False`` to accept uncited symbol ratings (the row still
                records that the citation was missing).
        """
        self.manifest = manifest
        self.require_rating_source = require_rating_source
        self.pin_role_cache = PinRoleCache()

    def analyze(self, sch_path: str | Path) -> list[ComponentStressResult]:
        """Return one census row per (MOSFET, state, terminal pair).

        Never raises: unreadable schematics and netlist failures yield an
        UNRESOLVED analysis-error row, distinct from a valid empty census.
        """

        def load_error(exc: Exception) -> list[ComponentStressResult]:
            return [
                ComponentStressResult(
                    reference="?",
                    state="",
                    check="analysis",
                    status=STATUS_UNRESOLVED,
                    reason=f"schematic/netlist analysis failed: {exc}",
                    assumptions=list(_MODEL_ASSUMPTIONS),
                )
            ]

        try:
            netlist = build_netlist_from_schematic(sch_path)
        except Exception as exc:
            return load_error(exc)

        try:
            from kicad_tools.schema.schematic import Schematic

            sch = Schematic.load(sch_path)
            symbols = [s for s in sch.symbols if s.reference and not s.reference.startswith("#")]
        except Exception as exc:
            return load_error(exc)

        states = self.manifest.coverage_states()
        results: list[ComponentStressResult] = []
        for sym in symbols:
            try:
                if not _is_mosfet(sym):
                    continue
                results.extend(self._check_symbol(sym, sch, netlist, states))
            except Exception:
                ref = str(getattr(sym, "reference", "") or "?")
                for state in states:
                    for check, high, low in _CHECK_TERMINALS:
                        results.append(
                            ComponentStressResult(
                                reference=ref,
                                state=state,
                                check=check,
                                status=STATUS_UNRESOLVED,
                                high_terminal=high,
                                low_terminal=low,
                                reason="analysis error",
                                assumptions=list(_MODEL_ASSUMPTIONS),
                            )
                        )
        results.sort(key=lambda r: (r.reference, states.index(r.state), r.check))
        return results

    # ------------------------------------------------------------------
    # Topology helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _pin_net_map(netlist: Netlist, ref: str) -> dict[str, str]:
        """Return ``{pin number: net name}`` for one component reference."""
        mapping: dict[str, str] = {}
        for net in netlist.nets:
            for node in net.nodes:
                if node.reference == ref and node.pin:
                    mapping.setdefault(str(node.pin), net.name)
        return mapping

    # ------------------------------------------------------------------
    # Per-component evaluation
    # ------------------------------------------------------------------
    def _check_symbol(
        self,
        sym: SymbolInstance,
        sch: Any,
        netlist: Netlist,
        states: list[str],
    ) -> list[ComponentStressResult]:
        ref = sym.reference
        mpn = _get_field(sym, *_MPN_FIELDS)
        rating_source = _get_field(sym, *_SOURCE_FIELDS)

        try:
            lib_symbol = sch.get_lib_symbol_resolved(sym.lib_id)
        except Exception:
            lib_symbol = None

        roles = self.pin_role_cache.resolve(sym, lib_symbol)
        pin_nets = self._pin_net_map(netlist, ref)

        rows: list[ComponentStressResult] = []
        for state_name in states:
            state = self.manifest.get(state_name)
            for check, high_role, low_role in _CHECK_TERMINALS:
                rows.append(
                    self._evaluate(
                        sym=sym,
                        ref=ref,
                        mpn=mpn,
                        rating_source=rating_source,
                        roles=roles,
                        pin_nets=pin_nets,
                        state_name=state_name,
                        state=state,
                        check=check,
                        high_role=high_role,
                        low_role=low_role,
                    )
                )
        return rows

    def _evaluate(
        self,
        *,
        sym: SymbolInstance,
        ref: str,
        mpn: str | None,
        rating_source: str | None,
        roles: PinRoleMap | None,
        pin_nets: dict[str, str],
        state_name: str,
        state: OperatingState | None,
        check: str,
        high_role: str,
        low_role: str,
    ) -> ComponentStressResult:
        assumptions: list[str] = list(_MODEL_ASSUMPTIONS)
        if roles is not None and roles.origin:
            assumptions.append(f"pin roles from {roles.origin}")
        if state is not None and state.assumptions:
            assumptions.append(f"state '{state_name}': {state.assumptions}")

        sources: list[str] = []
        if rating_source:
            sources.append(f"rating: {rating_source}")
        if state is not None and state.source:
            sources.append(f"state '{state_name}': {state.source}")
        source = "; ".join(sources) if sources else None

        def row(
            status: str,
            reason: str | None = None,
            **kwargs: Any,
        ) -> ComponentStressResult:
            return ComponentStressResult(
                reference=ref,
                state=state_name,
                check=check,
                status=status,
                mpn=mpn,
                high_terminal=high_role,
                low_terminal=low_role,
                assumptions=assumptions,
                source=source,
                reason=reason,
                **kwargs,
            )

        # 1. Coverage gap: the manifest never declared this state.
        if state is None:
            return row(
                STATUS_UNRESOLVED,
                f"state '{state_name}' is not declared in the operating-state manifest",
            )

        # 2. Pin roles.
        if roles is None or not roles.pin(high_role) or not roles.pin(low_role):
            return row(
                STATUS_UNRESOLVED,
                (
                    f"could not resolve {high_role}/{low_role} pin roles for {ref} "
                    "(add Pin_D/Pin_G/Pin_S symbol fields)"
                ),
            )

        # 3. Terminal -> net binding.
        high_pin = roles.pin(high_role)
        low_pin = roles.pin(low_role)
        high_net = pin_nets.get(str(high_pin))
        low_net = pin_nets.get(str(low_pin))
        unbound = [role for role, net in ((high_role, high_net), (low_role, low_net)) if not net]
        if unbound:
            return row(
                STATUS_UNRESOLVED,
                f"terminal {'/'.join(unbound)} is not bound to a net",
                high_net=high_net,
                low_net=low_net,
            )

        # 4. Declared potentials for BOTH terminals, in THIS state.
        high_v = state.potential(high_net)
        low_v = state.potential(low_net)
        missing = [net for net, volts in ((high_net, high_v), (low_net, low_v)) if volts is None]
        if missing:
            return row(
                STATUS_UNRESOLVED,
                (
                    f"net {' and '.join(repr(n) for n in missing)} has no declared "
                    f"potential in state '{state_name}'"
                ),
                high_net=high_net,
                low_net=low_net,
                high_potential_v=high_v,
                low_potential_v=low_v,
            )

        assert high_v is not None and low_v is not None
        stress = high_v - low_v
        if not all(math.isfinite(v) for v in (high_v, low_v, stress)):
            return row(
                STATUS_UNRESOLVED,
                "terminal potentials and differential stress must be finite voltages",
                high_net=high_net,
                low_net=low_net,
            )

        # 5. Sourced absolute-maximum rating. Never a built-in default.
        rating_fields = _CHECK_RATING_FIELDS[check]
        raw_rating = _get_field(sym, *rating_fields)
        if raw_rating is None:
            return row(
                STATUS_UNRESOLVED,
                f"no {rating_fields[0]} symbol field (no built-in default is assumed)",
                high_net=high_net,
                low_net=low_net,
                high_potential_v=high_v,
                low_potential_v=low_v,
                stress_v=stress,
            )
        rated = _parse_voltage(raw_rating)
        if rated is None or rated <= 0:
            return row(
                STATUS_UNRESOLVED,
                f"unparseable {rating_fields[0]} field ({raw_rating!r})",
                high_net=high_net,
                low_net=low_net,
                high_potential_v=high_v,
                low_potential_v=low_v,
                stress_v=stress,
            )
        if self.require_rating_source and not rating_source:
            return row(
                STATUS_UNRESOLVED,
                (
                    f"{rating_fields[0]}={raw_rating} is not source-backed "
                    "(add a Rating_Source or Datasheet field)"
                ),
                high_net=high_net,
                low_net=low_net,
                high_potential_v=high_v,
                low_potential_v=low_v,
                stress_v=stress,
                rated_v=rated,
            )
        if not rating_source:
            assumptions.append("rating has no source citation (--allow-uncited-ratings)")

        margin = rated - abs(stress)
        status = STATUS_FAIL if abs(stress) > rated else STATUS_PASS
        return row(
            status,
            None,
            high_net=high_net,
            low_net=low_net,
            high_potential_v=high_v,
            low_potential_v=low_v,
            stress_v=stress,
            rated_v=rated,
            margin_v=margin,
        )


def _is_mosfet(sym: SymbolInstance) -> bool:
    """Return True when *sym* should be evaluated as a MOSFET.

    Recognised by lib_id hint (``Device:Q_NMOS_GDS`` and friends) **or** by the
    presence of an explicit ``Vds_max``/``Vgs_max``/``Pin_D`` field -- so a
    vendor-specific symbol still participates once it carries ratings, while a
    bipolar ``Q`` with no MOSFET fields is not dragged into the census.
    """
    lib_id = str(getattr(sym, "lib_id", "") or "")
    part = lib_id.split(":")[-1].upper()
    if any(hint in part for hint in _MOSFET_LIB_HINTS):
        return True
    return any(
        _get_field(sym, name) is not None for name in ("Vds_max", "Vgs_max", "Pin_D", "Pin_S")
    )
