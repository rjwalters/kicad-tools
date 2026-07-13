"""
Offline JLCPCB parts catalog backed by the yaqwsx/jlcparts dataset.

This module provides a *local, offline* fallback for LCSC part lookups when
the live JLCPCB web API is unavailable (403 anti-bot enforcement, network
errors, or an explicit offline request). It consumes the publicly published
``yaqwsx/jlcparts`` SQLite dataset -- a mirror of the entire JLCPCB catalog --
downloaded on demand into the existing kicad-tools cache directory.

Two responsibilities live here:

* :func:`sync_catalog` -- download the split-zip SQLite dataset from
  ``https://yaqwsx.github.io/jlcparts/data/`` (``cache.zip`` + ``cache.z01``,
  ``cache.z02``, ... sequential parts), reassemble and unzip it into
  ``~/.cache/kicad-tools/jlcparts.sqlite3``. This is a large, user-invoked
  download -- it is *never* triggered automatically as a side effect of a
  lookup, and *never* runs in CI/tests (tests use a small hand-curated
  fixture SQLite instead).

* :class:`JlcpartsCatalog` -- a read-only reader over the downloaded
  ``jlcparts.sqlite3`` that translates ``jlc_components`` rows into the
  existing :class:`~kicad_tools.parts.models.Part` dataclass by exact
  ``lcsc`` part-number lookup.

Design notes
------------

* No ``PartProvider`` protocol / multi-backend abstraction is introduced
  (deferred per owner direction on #4108) -- the catalog is consulted as a
  fallback inside :class:`~kicad_tools.parts.lcsc.LCSCClient`.
* Only exact ``lcsc`` lookup is supported in this phase; full-catalog fuzzy
  matching is explicitly out of scope.
* The dataset is *never* committed to the repository; ``sync_catalog`` writes
  only into the cache directory.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path

from .cache import get_default_cache_path
from .lcsc import PARTS_INSTALL_HINT, _categorize_part, _guess_package_type
from .models import Part, PartPrice

logger = logging.getLogger(__name__)

# Public GitHub Pages location of the split-zip jlcparts SQLite dataset.
JLCPARTS_DATA_BASE = "https://yaqwsx.github.io/jlcparts/data"

# Filename of the reassembled catalog inside the cache directory.
CATALOG_FILENAME = "jlcparts.sqlite3"


def get_catalog_path() -> Path:
    """Return the path to the local jlcparts catalog inside the cache dir.

    Sits alongside the per-query ``parts.db`` produced by
    :func:`~kicad_tools.parts.cache.get_default_cache_path`. Does not require
    the file to exist -- callers check :meth:`Path.exists` themselves.
    """
    return get_default_cache_path().parent / CATALOG_FILENAME


class JlcpartsCatalog:
    """Read-only reader over a downloaded jlcparts SQLite catalog.

    Translates ``jlc_components`` rows into :class:`Part` objects by exact
    ``lcsc`` part-number lookup. If the backing file is absent the reader is a
    silent no-op: :meth:`lookup` returns ``None`` for every query so callers
    fall through to their existing "not found" behavior.

    Example::

        catalog = JlcpartsCatalog()
        if catalog.available:
            part = catalog.lookup("C123456")
    """

    def __init__(self, db_path: Path | None = None):
        """Initialize the catalog reader.

        Args:
            db_path: Path to the jlcparts SQLite file (default:
                ``~/.cache/kicad-tools/jlcparts.sqlite3``). The file is not
                required to exist.
        """
        self.db_path = db_path or get_catalog_path()

    @property
    def available(self) -> bool:
        """Whether the backing catalog file exists on disk."""
        return self.db_path.exists()

    def lookup(self, lcsc_part: str) -> Part | None:
        """Look up a single part by exact LCSC number.

        Args:
            lcsc_part: LCSC part number (e.g. ``"C123456"``). The numeric
                portion is matched against the ``lcsc`` column.

        Returns:
            A translated :class:`Part` if found, otherwise ``None`` (including
            when the catalog file does not exist).
        """
        if not self.available:
            return None

        lcsc_id = _normalize_lcsc_id(lcsc_part)
        if lcsc_id is None:
            return None

        try:
            with sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    "SELECT * FROM jlc_components WHERE lcsc = ?",
                    (lcsc_id,),
                )
                row = cursor.fetchone()
        except sqlite3.Error as e:
            logger.warning(f"jlcparts catalog read failed for {lcsc_part}: {e}")
            return None

        if row is None:
            return None

        return _row_to_part(row)

    def lookup_many(self, lcsc_parts: list[str]) -> dict[str, Part]:
        """Look up multiple parts by exact LCSC number.

        Args:
            lcsc_parts: LCSC part numbers to resolve.

        Returns:
            Dict mapping normalized (``C``-prefixed, upper-case) part numbers
            to :class:`Part`. Missing parts are omitted. An empty dict is
            returned when the catalog file does not exist.
        """
        if not self.available or not lcsc_parts:
            return {}

        # Map numeric id -> canonical requested key so results round-trip.
        id_to_key: dict[int, str] = {}
        for raw in lcsc_parts:
            lcsc_id = _normalize_lcsc_id(raw)
            if lcsc_id is not None:
                id_to_key[lcsc_id] = _canonical_lcsc(raw)

        if not id_to_key:
            return {}

        result: dict[str, Part] = {}
        try:
            with sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True) as conn:
                conn.row_factory = sqlite3.Row
                placeholders = ",".join("?" * len(id_to_key))
                cursor = conn.execute(
                    f"SELECT * FROM jlc_components WHERE lcsc IN ({placeholders})",
                    list(id_to_key.keys()),
                )
                for row in cursor:
                    part = _row_to_part(row)
                    key = id_to_key.get(int(row["lcsc"]), part.lcsc_part)
                    result[key] = part
        except sqlite3.Error as e:
            logger.warning(f"jlcparts catalog bulk read failed: {e}")
            return {}

        return result

    def count(self) -> int:
        """Return the number of components in the catalog (0 if absent)."""
        if not self.available:
            return 0
        try:
            with sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True) as conn:
                return int(conn.execute("SELECT COUNT(*) FROM jlc_components").fetchone()[0])
        except sqlite3.Error:
            return 0


def _canonical_lcsc(lcsc_part: str) -> str:
    """Normalize a part number to canonical ``C<digits>`` upper-case form."""
    normalized = lcsc_part.strip().upper()
    if not normalized.startswith("C"):
        normalized = f"C{normalized}"
    return normalized


def _normalize_lcsc_id(lcsc_part: str) -> int | None:
    """Extract the integer ``lcsc`` id from a ``C123456``-style part number.

    The jlcparts ``jlc_components.lcsc`` column stores the numeric id without
    the ``C`` prefix, so ``"C123456"`` maps to ``123456``.

    Returns:
        The integer id, or ``None`` if the input has no numeric portion.
    """
    digits = _canonical_lcsc(lcsc_part)[1:]
    if not digits.isdigit():
        return None
    return int(digits)


def _row_to_part(row: sqlite3.Row) -> Part:
    """Translate a ``jlc_components`` row into a :class:`Part`.

    Maps the stable jlcparts schema columns onto the existing ``Part``
    dataclass. The ``price`` column is a JSON array of price breaks
    (``[{"qFrom": n, "price": p}, ...]``); ``library_type`` distinguishes
    JLCPCB Basic/Preferred parts.
    """
    keys = set(row.keys())

    def col(name: str, default: object = None) -> object:
        return row[name] if name in keys else default

    def col_str(name: str) -> str:
        value = col(name)
        return str(value) if value not in (None, "") else ""

    def col_int(name: str, default: int = 0) -> int:
        value = col(name)
        if value in (None, ""):
            return default
        try:
            return int(str(value))
        except (TypeError, ValueError):
            return default

    lcsc_id = col("lcsc")
    lcsc_part = f"C{lcsc_id}" if lcsc_id is not None else ""

    description = col_str("description")
    package = col_str("package")
    library_type = col_str("library_type").lower()

    prices = _parse_price_json(col("price"))

    return Part(
        lcsc_part=lcsc_part,
        mfr_part=col_str("mfr"),
        manufacturer=col_str("manufacturer"),
        description=description,
        category=_categorize_part(description, package),
        package=package,
        package_type=_guess_package_type(package),
        stock=col_int("stock", 0),
        min_order=col_int("min_order", 1),
        prices=prices,
        is_basic=library_type == "basic",
        is_preferred=library_type == "preferred",
        datasheet_url=col_str("datasheet"),
        product_url=f"https://jlcpcb.com/partdetail/{lcsc_part}" if lcsc_part else "",
        fetched_at=datetime.now(),
    )


def _parse_price_json(raw: object) -> list[PartPrice]:
    """Parse the jlcparts ``price`` JSON column into price breaks.

    The column holds a JSON array of ``{"qFrom": <int>, "price": <float>}``
    objects (``qTo`` may also be present). Malformed or empty values yield an
    empty list.
    """
    if not raw:
        return []

    try:
        data = json.loads(raw) if isinstance(raw, str | bytes | bytearray) else raw
    except (json.JSONDecodeError, TypeError):
        return []

    if not isinstance(data, list):
        return []

    prices: list[PartPrice] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        qty = entry.get("qFrom") or entry.get("startNumber") or entry.get("quantity") or 0
        unit_price = entry.get("price") or entry.get("productPrice") or entry.get("unit_price") or 0
        try:
            qty_i = int(qty)
            price_f = float(unit_price)
        except (TypeError, ValueError):
            continue
        if qty_i > 0 and price_f > 0:
            prices.append(PartPrice(quantity=qty_i, unit_price=price_f))

    prices.sort(key=lambda p: p.quantity)
    return prices


def _discover_split_parts(base_url: str) -> list[str]:
    """Return the ordered list of split-zip URLs to download.

    The dataset is published as ``cache.zip`` (the final segment of a split
    archive) plus sequential ``cache.z01``, ``cache.z02``, ... segments. The
    number of ``.zNN`` segments changes as the catalog grows, so probe
    sequentially until a segment 404s.

    Note the split-archive convention: the ``.zNN`` parts come *first* and
    ``cache.zip`` is the *last* segment. This helper returns URLs in the
    correct concatenation order (``z01``, ``z02``, ..., then ``cache.zip``).
    """
    import requests  # type: ignore[import-untyped]

    part_urls: list[str] = []
    index = 1
    while True:
        url = f"{base_url}/cache.z{index:02d}"
        resp = requests.head(url, timeout=30, allow_redirects=True)
        if resp.status_code == 404:
            break
        resp.raise_for_status()
        part_urls.append(url)
        index += 1
        # Safety bound: the dataset is ~12-20 segments; stop well before runaway.
        if index > 100:
            break

    part_urls.append(f"{base_url}/cache.zip")
    return part_urls


def sync_catalog(
    dest: Path | None = None,
    *,
    base_url: str = JLCPARTS_DATA_BASE,
    force: bool = False,
    progress: bool = True,
) -> Path:
    """Download and assemble the jlcparts catalog into the cache directory.

    Downloads the split-zip SQLite dataset from ``base_url`` (``cache.z01`` ..
    ``cache.zNN`` + ``cache.zip``), concatenates the segments, unzips the
    resulting archive, and writes the extracted SQLite database to ``dest``
    (default ``~/.cache/kicad-tools/jlcparts.sqlite3``).

    This is a *large* download (hundreds of MB) and must only ever be invoked
    explicitly by a user or CI job -- never as a side effect of a lookup, and
    never in the test suite.

    Args:
        dest: Destination path for the assembled SQLite catalog.
        base_url: Base URL of the published dataset (override for testing).
        force: Re-download even if ``dest`` already exists.
        progress: Print progress to stdout.

    Returns:
        The path to the assembled catalog.

    Raises:
        ImportError: If the ``requests`` dependency (``[parts]`` extra) is
            missing.
        RuntimeError: If the assembled archive does not contain the expected
            SQLite database.
    """
    try:
        import requests
    except ImportError as e:
        raise ImportError(
            f"Downloading the jlcparts catalog requires the 'requests' library. "
            f"{PARTS_INSTALL_HINT}"
        ) from e

    dest = dest or get_catalog_path()
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists() and not force:
        if progress:
            print(f"Catalog already present: {dest}")
            print("Use --force to re-download.")
        return dest

    def _log(msg: str) -> None:
        if progress:
            print(msg)

    _log(f"Discovering dataset segments at {base_url} ...")
    part_urls = _discover_split_parts(base_url)
    _log(f"Found {len(part_urls)} archive segment(s).")

    # Assemble the split archive into a single temporary .zip next to dest.
    combined_zip = dest.with_suffix(".download.zip")
    total_bytes = 0
    try:
        with combined_zip.open("wb") as out:
            for idx, url in enumerate(part_urls, start=1):
                _log(f"  [{idx}/{len(part_urls)}] {url}")
                with requests.get(url, stream=True, timeout=120) as resp:
                    resp.raise_for_status()
                    for chunk in resp.iter_content(chunk_size=1 << 20):
                        if chunk:
                            out.write(chunk)
                            total_bytes += len(chunk)
        _log(f"Downloaded {total_bytes / (1 << 20):.1f} MiB across {len(part_urls)} segment(s).")

        _log("Extracting SQLite database ...")
        _extract_catalog(combined_zip, dest)
    finally:
        combined_zip.unlink(missing_ok=True)

    count = JlcpartsCatalog(dest).count()
    _log(f"Catalog ready: {dest} ({count:,} components)")
    return dest


def _extract_catalog(archive: Path, dest: Path) -> None:
    """Extract the SQLite database from a (reassembled) jlcparts zip archive.

    Picks the first ``*.sqlite3``/``*.sqlite``/``*.db`` member, or -- if the
    archive contains a single member -- that member. Writes it to ``dest``.

    Raises:
        RuntimeError: If no plausible SQLite member is found.
    """
    with zipfile.ZipFile(archive) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        sqlite_members = [n for n in names if n.lower().endswith((".sqlite3", ".sqlite", ".db"))]
        if sqlite_members:
            member = sqlite_members[0]
        elif len(names) == 1:
            member = names[0]
        else:
            raise RuntimeError(
                f"Could not locate a SQLite database in the jlcparts archive; members: {names}"
            )

        # Extract to a sibling temp file and promote atomically so a killed
        # or failed extraction can never leave a truncated catalog that
        # later lookups would silently trust.
        tmp_dest = dest.with_suffix(".extract.tmp")
        try:
            with zf.open(member) as src, tmp_dest.open("wb") as out:
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
            os.replace(tmp_dest, dest)
        finally:
            tmp_dest.unlink(missing_ok=True)
