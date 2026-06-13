"""
Composition-based part model with parametric search.

Provides a structured, composition-based part model that separates electrical
definition (Unit/Entity) from physical packaging (Package) and supplier data.
This allows parts to share definitions across packages and inherit from base
variants.

Key concepts:
- **Unit**: A logical group of pins (e.g., one gate of a quad op-amp).
- **Entity**: One or more Units forming a complete logical device.
- **Package**: Physical footprint binding with pad-to-pin mapping.
- **ComposedPart**: Top-level binding of Entity + Package + MPN + parametric data,
  with optional single-parent inheritance for variant derivation.

Example - Creating a composed part::

    from kicad_tools.parts.composition import (
        ComposedPart, Entity, PinDirection, Unit, UnitPin,
    )

    # Define a simple resistor unit with two pins
    resistor_unit = Unit(
        id="resistor",
        name="Resistor",
        pins=[
            UnitPin(name="1", number="1", direction=PinDirection.PASSIVE),
            UnitPin(name="2", number="2", direction=PinDirection.PASSIVE),
        ],
    )

    # Wrap in an entity (single gate)
    resistor_entity = Entity(
        id="resistor",
        name="Resistor",
        units=[resistor_unit],
    )

    # Create a base 0402 resistor
    base_resistor = ComposedPart(
        id="resistor-0402-base",
        entity=resistor_entity,
        package="0402",
        category="resistor",
    )

    # Derive a specific variant via inheritance
    r10k = ComposedPart(
        id="resistor-0402-10k-1pct",
        entity=resistor_entity,
        package="0402",
        category="resistor",
        mpn="RC0402FR-0710KL",
        manufacturer="Yageo",
        base_part=base_resistor,
        params={"resistance": "10000", "tolerance": "1%"},
    )

Example - Parametric search::

    from kicad_tools.parts.composition import ComposedPartStore

    store = ComposedPartStore(db_path)
    store.save(r10k)
    results = store.find_parts(category="resistor", package="0402",
                               params={"resistance": "10000"})
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

# ---------------------------------------------------------------------------
# Pin / Unit / Entity models
# ---------------------------------------------------------------------------


class PinDirection(Enum):
    """Electrical direction of a pin."""

    INPUT = "input"
    OUTPUT = "output"
    BIDIRECTIONAL = "bidirectional"
    PASSIVE = "passive"
    POWER_IN = "power_in"
    POWER_OUT = "power_out"
    OPEN_COLLECTOR = "open_collector"
    OPEN_EMITTER = "open_emitter"
    UNSPECIFIED = "unspecified"


@dataclass
class UnitPin:
    """
    A single pin within a Unit.

    Attributes:
        name: Human-readable pin name (e.g., "VCC", "IN+").
        number: Pin number as it appears on the physical part (e.g., "1", "A3").
        direction: Electrical direction.
        alternate_names: Optional alternative names for the pin.
    """

    name: str
    number: str
    direction: PinDirection = PinDirection.UNSPECIFIED
    alternate_names: list[str] = field(default_factory=list)


@dataclass
class Unit:
    """
    A logical group of pins that forms one functional block.

    For a quad op-amp this would be one amplifier section.  A device that
    is not sub-divided into gates has a single Unit containing all pins.

    Attributes:
        id: Stable identifier (e.g., "opamp-gate").
        name: Human-readable label.
        pins: Ordered list of pins belonging to this unit.
    """

    id: str
    name: str
    pins: list[UnitPin] = field(default_factory=list)

    @property
    def pin_count(self) -> int:
        """Number of pins in this unit."""
        return len(self.pins)

    def get_pin(self, number: str) -> UnitPin | None:
        """Look up a pin by its number."""
        for pin in self.pins:
            if pin.number == number:
                return pin
        return None

    def get_pins_by_name(self, name: str) -> list[UnitPin]:
        """Get all pins matching *name*."""
        return [p for p in self.pins if p.name == name]


@dataclass
class Entity:
    """
    A complete logical device composed of one or more Units (gates).

    Attributes:
        id: Stable identifier.
        name: Human-readable label (e.g., "LM324 Quad Op-Amp").
        units: Ordered list of units (gates) in this entity.
    """

    id: str
    name: str
    units: list[Unit] = field(default_factory=list)

    @property
    def total_pins(self) -> int:
        """Total pins across all units."""
        return sum(u.pin_count for u in self.units)


# ---------------------------------------------------------------------------
# ComposedPart (top-level binding)
# ---------------------------------------------------------------------------


@dataclass
class ComposedPart:
    """
    A fully-specified part binding Entity + Package + supplier data.

    Supports single-parent inheritance via ``base_part``.  When
    ``resolve()`` is called the returned dict merges fields from the
    inheritance chain with the most-derived value winning.

    Attributes:
        id: Unique identifier for this part variant.
        entity: The logical device definition.
        package: Physical package name (e.g., "0402", "SOIC-8").
        category: Part category string (e.g., "resistor", "capacitor").
        mpn: Manufacturer part number.
        manufacturer: Manufacturer name.
        description: Human-readable description.
        base_part: Optional parent part for field inheritance.
        params: Parametric specifications as key-value pairs.
            Keys should be lowercase, e.g. ``resistance``, ``capacitance``,
            ``voltage``, ``tolerance``.
        tags: Flat list of tags for classification / search.
        lcsc_part: Optional LCSC part number linking to supplier data.
        datasheet_url: URL to part datasheet.
        pad_to_pin: Mapping from physical pad name to logical pin number.
        created_at: Timestamp when this record was created.
    """

    id: str
    entity: Entity
    package: str = ""
    category: str = ""
    mpn: str = ""
    manufacturer: str = ""
    description: str = ""
    base_part: ComposedPart | None = None
    params: dict[str, str] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    lcsc_part: str = ""
    datasheet_url: str = ""
    pad_to_pin: dict[str, str] = field(default_factory=dict)
    created_at: datetime | None = None

    # ------------------------------------------------------------------
    # Inheritance helpers
    # ------------------------------------------------------------------

    def _inheritance_chain(self) -> list[ComposedPart]:
        """
        Walk the inheritance chain from most-derived to base.

        Raises ``ValueError`` on circular inheritance.
        """
        chain: list[ComposedPart] = []
        seen: set[str] = set()
        current: ComposedPart | None = self
        while current is not None:
            if current.id in seen:
                raise ValueError(
                    f"Circular inheritance detected: {current.id} appears twice in the chain"
                )
            seen.add(current.id)
            chain.append(current)
            current = current.base_part
        return chain

    def resolve(self) -> dict[str, object]:
        """
        Resolve the inheritance chain and return merged fields.

        Fields from the most-derived part override those from ancestors.
        ``params`` dicts are merged (most-derived wins per key).
        ``tags`` lists are unioned.

        Returns:
            A flat dict of resolved field values.
        """
        chain = self._inheritance_chain()

        merged_params: dict[str, str] = {}
        merged_tags: set[str] = set()

        # Start with base (last in chain) and overlay toward self
        result: dict[str, object] = {}
        for part in reversed(chain):
            for attr in (
                "id",
                "package",
                "category",
                "mpn",
                "manufacturer",
                "description",
                "lcsc_part",
                "datasheet_url",
            ):
                val = getattr(part, attr)
                if val:
                    result[attr] = val
            merged_params.update(part.params)
            merged_tags.update(part.tags)

        result["params"] = dict(merged_params)
        result["tags"] = sorted(merged_tags)
        # Entity always comes from self (not inherited)
        result["entity"] = self.entity
        return result


# ---------------------------------------------------------------------------
# Serialisation helpers (Unit / Entity / ComposedPart <-> dict/JSON)
# ---------------------------------------------------------------------------


def _pin_to_dict(pin: UnitPin) -> dict:
    d: dict[str, object] = {
        "name": pin.name,
        "number": pin.number,
        "direction": pin.direction.value,
    }
    if pin.alternate_names:
        d["alternate_names"] = pin.alternate_names
    return d


def _pin_from_dict(d: dict) -> UnitPin:
    return UnitPin(
        name=d["name"],
        number=d["number"],
        direction=PinDirection(d.get("direction", "unspecified")),
        alternate_names=d.get("alternate_names", []),
    )


def _unit_to_dict(unit: Unit) -> dict:
    return {
        "id": unit.id,
        "name": unit.name,
        "pins": [_pin_to_dict(p) for p in unit.pins],
    }


def _unit_from_dict(d: dict) -> Unit:
    return Unit(
        id=d["id"],
        name=d["name"],
        pins=[_pin_from_dict(p) for p in d.get("pins", [])],
    )


def _entity_to_dict(entity: Entity) -> dict:
    return {
        "id": entity.id,
        "name": entity.name,
        "units": [_unit_to_dict(u) for u in entity.units],
    }


def _entity_from_dict(d: dict) -> Entity:
    return Entity(
        id=d["id"],
        name=d["name"],
        units=[_unit_from_dict(u) for u in d.get("units", [])],
    )


# ---------------------------------------------------------------------------
# SQLite-backed store with parametric search
# ---------------------------------------------------------------------------


class ComposedPartStore:
    """
    SQLite-backed store for composed parts with parametric search.

    Schema uses three tables:
    - ``composed_parts`` -- one row per part
    - ``composed_part_params`` -- key/value parametric data (indexed)
    - ``composed_part_tags`` -- flat tag list (indexed)

    Example::

        store = ComposedPartStore(Path("/tmp/parts.db"))
        store.save(my_part)

        hits = store.find_parts(category="resistor",
                                package="0402",
                                params={"resistance": "10000"})
    """

    SCHEMA_VERSION = 1

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._init_db()

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS composed_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );

                CREATE TABLE IF NOT EXISTS composed_parts (
                    id TEXT PRIMARY KEY,
                    entity_json TEXT NOT NULL,
                    package TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '',
                    mpn TEXT NOT NULL DEFAULT '',
                    manufacturer TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    base_part_id TEXT,
                    lcsc_part TEXT NOT NULL DEFAULT '',
                    datasheet_url TEXT NOT NULL DEFAULT '',
                    pad_to_pin_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_cp_category
                    ON composed_parts(category);
                CREATE INDEX IF NOT EXISTS idx_cp_package
                    ON composed_parts(package);
                CREATE INDEX IF NOT EXISTS idx_cp_mpn
                    ON composed_parts(mpn);
                CREATE INDEX IF NOT EXISTS idx_cp_manufacturer
                    ON composed_parts(manufacturer);
                CREATE INDEX IF NOT EXISTS idx_cp_lcsc
                    ON composed_parts(lcsc_part);

                CREATE TABLE IF NOT EXISTS composed_part_params (
                    part_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    PRIMARY KEY (part_id, key),
                    FOREIGN KEY (part_id)
                        REFERENCES composed_parts(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_cpp_key_value
                    ON composed_part_params(key, value);

                CREATE TABLE IF NOT EXISTS composed_part_tags (
                    part_id TEXT NOT NULL,
                    tag TEXT NOT NULL,
                    PRIMARY KEY (part_id, tag),
                    FOREIGN KEY (part_id)
                        REFERENCES composed_parts(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_cpt_tag
                    ON composed_part_tags(tag);
            """)

            cur = conn.execute("SELECT value FROM composed_meta WHERE key = 'schema_version'")
            row = cur.fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO composed_meta (key, value) VALUES ('schema_version', ?)",
                    (str(self.SCHEMA_VERSION),),
                )

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def save(self, part: ComposedPart) -> None:
        """Insert or replace a composed part and its params/tags."""
        with self._connect() as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            # Delete existing params/tags if replacing
            conn.execute("DELETE FROM composed_part_params WHERE part_id = ?", (part.id,))
            conn.execute("DELETE FROM composed_part_tags WHERE part_id = ?", (part.id,))

            conn.execute(
                """
                INSERT OR REPLACE INTO composed_parts (
                    id, entity_json, package, category, mpn, manufacturer,
                    description, base_part_id, lcsc_part, datasheet_url,
                    pad_to_pin_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    part.id,
                    json.dumps(_entity_to_dict(part.entity)),
                    part.package,
                    part.category,
                    part.mpn,
                    part.manufacturer,
                    part.description,
                    part.base_part.id if part.base_part else None,
                    part.lcsc_part,
                    part.datasheet_url,
                    json.dumps(part.pad_to_pin),
                    (part.created_at or datetime.now()).isoformat(),
                ),
            )

            if part.params:
                conn.executemany(
                    "INSERT INTO composed_part_params (part_id, key, value) VALUES (?, ?, ?)",
                    [(part.id, k, v) for k, v in part.params.items()],
                )

            if part.tags:
                conn.executemany(
                    "INSERT INTO composed_part_tags (part_id, tag) VALUES (?, ?)",
                    [(part.id, t) for t in part.tags],
                )

    def get(self, part_id: str) -> ComposedPart | None:
        """Retrieve a single composed part by ID."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM composed_parts WHERE id = ?", (part_id,)).fetchone()
            if row is None:
                return None
            return self._row_to_part(conn, row)

    def delete(self, part_id: str) -> bool:
        """Delete a composed part. Returns True if it existed."""
        with self._connect() as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("DELETE FROM composed_part_params WHERE part_id = ?", (part_id,))
            conn.execute("DELETE FROM composed_part_tags WHERE part_id = ?", (part_id,))
            cur = conn.execute("DELETE FROM composed_parts WHERE id = ?", (part_id,))
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Parametric search
    # ------------------------------------------------------------------

    def find_parts(
        self,
        *,
        category: str | None = None,
        package: str | None = None,
        manufacturer: str | None = None,
        mpn: str | None = None,
        tags: list[str] | None = None,
        params: dict[str, str] | None = None,
        limit: int = 100,
    ) -> list[ComposedPart]:
        """
        Search for composed parts by parametric criteria.

        All supplied filters are ANDed together.

        Args:
            category: Exact category match (e.g., "resistor").
            package: Exact package match (e.g., "0402").
            manufacturer: Exact manufacturer match.
            mpn: Exact MPN match.
            tags: Parts must have **all** listed tags.
            params: Parts must have **all** listed param key=value pairs.
            limit: Maximum results to return.

        Returns:
            List of matching ``ComposedPart`` objects.
        """
        clauses: list[str] = []
        bindings: list[object] = []

        if category is not None:
            clauses.append("cp.category = ?")
            bindings.append(category)
        if package is not None:
            clauses.append("cp.package = ?")
            bindings.append(package)
        if manufacturer is not None:
            clauses.append("cp.manufacturer = ?")
            bindings.append(manufacturer)
        if mpn is not None:
            clauses.append("cp.mpn = ?")
            bindings.append(mpn)

        # Tag filtering: require all tags present
        if tags:
            for tag in tags:
                clauses.append(
                    "EXISTS (SELECT 1 FROM composed_part_tags t "
                    "WHERE t.part_id = cp.id AND t.tag = ?)"
                )
                bindings.append(tag)

        # Param filtering: require all key=value pairs
        if params:
            for key, value in params.items():
                clauses.append(
                    "EXISTS (SELECT 1 FROM composed_part_params p "
                    "WHERE p.part_id = cp.id AND p.key = ? AND p.value = ?)"
                )
                bindings.append(key)
                bindings.append(value)

        where = " AND ".join(clauses) if clauses else "1=1"
        query = f"SELECT * FROM composed_parts cp WHERE {where} LIMIT ?"
        bindings.append(limit)

        with self._connect() as conn:
            rows = conn.execute(query, bindings).fetchall()
            return [self._row_to_part(conn, r) for r in rows]

    def find_by_tag(self, tag: str) -> list[ComposedPart]:
        """Convenience: find all parts with a specific tag."""
        return self.find_parts(tags=[tag])

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, object]:
        """Return basic statistics about the store."""
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM composed_parts").fetchone()[0]
            categories: dict[str, int] = {}
            for row in conn.execute(
                "SELECT category, COUNT(*) FROM composed_parts GROUP BY category"
            ):
                categories[row[0]] = row[1]
            tag_count = conn.execute(
                "SELECT COUNT(DISTINCT tag) FROM composed_part_tags"
            ).fetchone()[0]
        return {
            "total": total,
            "categories": categories,
            "distinct_tags": tag_count,
            "db_path": str(self.db_path),
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _row_to_part(self, conn: sqlite3.Connection, row: sqlite3.Row) -> ComposedPart:
        """Convert a DB row (plus params/tags sub-queries) to ComposedPart."""
        part_id: str = row["id"]

        # Params
        params: dict[str, str] = {}
        for pr in conn.execute(
            "SELECT key, value FROM composed_part_params WHERE part_id = ?",
            (part_id,),
        ):
            params[pr["key"]] = pr["value"]

        # Tags
        tags: list[str] = []
        for tr in conn.execute(
            "SELECT tag FROM composed_part_tags WHERE part_id = ?",
            (part_id,),
        ):
            tags.append(tr["tag"])

        # Entity
        entity = _entity_from_dict(json.loads(row["entity_json"]))

        # Base part (lazy -- load only the ID, not recursively)
        base_part: ComposedPart | None = None
        if row["base_part_id"]:
            base_row = conn.execute(
                "SELECT * FROM composed_parts WHERE id = ?",
                (row["base_part_id"],),
            ).fetchone()
            if base_row:
                base_part = self._row_to_part(conn, base_row)

        created_at = None
        if row["created_at"]:
            created_at = datetime.fromisoformat(row["created_at"])

        return ComposedPart(
            id=part_id,
            entity=entity,
            package=row["package"] or "",
            category=row["category"] or "",
            mpn=row["mpn"] or "",
            manufacturer=row["manufacturer"] or "",
            description=row["description"] or "",
            base_part=base_part,
            params=params,
            tags=tags,
            lcsc_part=row["lcsc_part"] or "",
            datasheet_url=row["datasheet_url"] or "",
            pad_to_pin=json.loads(row["pad_to_pin_json"]) if row["pad_to_pin_json"] else {},
            created_at=created_at,
        )
