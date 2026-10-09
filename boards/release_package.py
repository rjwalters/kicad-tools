"""Shared plumbing for board release scripts (``boards/*/package_release.py``).

A board's shipped manufacturing package is rebuilt by the generic
``kct readiness --generate`` producer, so every gate (refill + save,
``kct check``, native KiCad DRC on saved and refilled copper, LVS, export,
drawings, full-bundle manifest, ``manufacturing.zip``, hash-bound
``output/readiness.json``) is actually re-run.  The board script adds only
what the generic producer cannot know, through the
:class:`kicad_tools.cli.readiness_cmd.Engines` board hooks:

* files the package ships besides the generic export (``package_extras``),
* board-specific checks (``extra_checks``),
* extra hashed inputs (``extra_inputs``).

Carry-forward discipline (Issue #6076): hand-curated evidence from the current
package is reused only while it is still valid, and :class:`Release` refuses
-- the readiness ``artifacts`` gate fails, nothing is published -- if the new
package would lack any file the current package ships.
"""

from __future__ import annotations

import json
import shutil
import sys
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from kicad_tools.cli import readiness_cmd
from kicad_tools.cli.readiness_cmd import CheckOutcome, Engines, ReadinessOptions

#: Fixed member timestamp for every ZIP a release script writes or normalises.
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


def write_zip(path: Path, members: dict[str, bytes]) -> None:
    """Write *members* as a byte-reproducible ZIP (sorted, fixed time/mode)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(members):
            info = zipfile.ZipInfo(name, date_time=ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, members[name])
    tmp.replace(path)


def normalize_zip(path: Path) -> None:
    """Rewrite an existing ZIP deterministically; member bytes are unchanged."""
    with zipfile.ZipFile(path) as zf:
        members = {i.filename: zf.read(i.filename) for i in zf.infolist() if not i.is_dir()}
    write_zip(path, members)


def board_content(data: bytes) -> str:
    """Canonical content of a KiCad board, independent of item UUIDs, of
    KiCad's UUID-ordered save and of child-node order within an item.

    Every ``(uuid ...)``/``(tstamp ...)`` node is dropped; each node keeps its
    atoms in order and its sub-lists sorted.  Two boards with equal content
    have identical copper, footprints, pads and graphics.
    """
    from kicad_tools.sexp import parse_string

    def canon(node) -> str:  # type: ignore[no-untyped-def]
        atoms = [repr(c.value) for c in node.children if c.is_atom]
        lists = sorted(
            canon(c) for c in node.children if not c.is_atom and c.name not in ("uuid", "tstamp")
        )
        return "(" + " ".join([str(node.name), *atoms, *lists]) + ")"

    return canon(parse_string(data.decode()))


def error_count(path: Path, *keys: str) -> int | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data if isinstance(data, int) else None


@dataclass
class Release:
    """One board's release recipe on top of ``kct readiness --generate``."""

    board_dir: Path
    manufacturer: str = "jlcpcb"
    #: Package files carried forward byte-for-byte from the CURRENT package
    #: (e.g. a hand-written procurement review), each with the package files
    #: whose bytes it vouches for; refused if any of those changed.
    reviewed: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: package-relative name -> callable(options) returning the bytes to ship.
    generated: dict[str, Callable[[ReadinessOptions], bytes]] = field(default_factory=dict)
    #: Board-specific checks, run once (lazily) before the package is finished.
    checks: Callable[[ReadinessOptions], list[CheckOutcome]] | None = None
    #: Files (relative to the board dir) hashed into readiness ``inputs``.
    inputs: Callable[[ReadinessOptions], Iterable[Path]] | None = None
    #: Extra README lines (board instructions), after the derived sign-off.
    readme_lines: tuple[str, ...] = ()
    #: Per-file validators for generated BOM/CPL changes vs the current
    #: package: name -> callable(old_bytes, new_bytes) raising on rejection.
    change_policy: dict[str, Callable[[bytes, bytes], None]] = field(default_factory=dict)

    _old: dict[str, bytes] = field(default_factory=dict, init=False)
    _old_archive: bytes | None = field(default=None, init=False)
    _outcomes: list[CheckOutcome] | None = field(default=None, init=False)

    @property
    def package_dir(self) -> Path:
        return self.board_dir / "output" / "manufacturing"

    def _snapshot(self) -> None:
        if not self.package_dir.is_dir():
            raise SystemExit(f"{self.package_dir} is missing; nothing to carry forward")
        self._old = {
            p.relative_to(self.package_dir).as_posix(): p.read_bytes()
            for p in sorted(self.package_dir.rglob("*"))
            if p.is_file()
        }
        archive = self.board_dir / "output" / "manufacturing.zip"
        self._old_archive = archive.read_bytes() if archive.is_file() else None

    def old(self, name: str) -> bytes:
        return self._old[name]

    # -- hooks -------------------------------------------------------------

    def _run_checks(self, options: ReadinessOptions) -> list[CheckOutcome]:
        if self._outcomes is None:
            self._outcomes = list(self.checks(options)) if self.checks else []
        return self._outcomes

    def _package_extras(self, options: ReadinessOptions) -> list[str]:
        out, evidence = options.output_dir, options.evidence_dir
        self._run_checks(options)  # board evidence must exist before it is packaged

        for name, policy in self.change_policy.items():
            if name in self._old and (out / name).read_bytes() != self._old[name]:
                policy(self._old[name], (out / name).read_bytes())
        for name, vouched in self.reviewed.items():
            changed = [v for v in vouched if (out / v).read_bytes() != self._old.get(v)]
            if changed:
                raise RuntimeError(
                    f"{', '.join(changed)} changed since {name} was written; re-review "
                    "instead of carrying the old review forward"
                )
            (out / name).write_bytes(self._old[name])
        # Gate evidence travels inside the bundle so a fab upload carries its sign-off.
        shutil.copyfile(evidence / "kct-check.json", out / "check-report.json")
        shutil.copyfile(evidence / "native-drc.json", out / "native-drc.json")
        for name, produce in self.generated.items():
            target = out / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(produce(options))
        for archive in ("kicad_project.zip", "gerbers/gerbers.zip"):
            if (out / archive).is_file():
                normalize_zip(out / archive)

        shipped = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()} | {
            "README.txt",
            "manifest.json",
        }
        dropped = sorted(set(self._old) - shipped)
        if dropped:
            raise RuntimeError(
                "refusing to drop files the current package ships: " + ", ".join(dropped)
            )

        kct_errors = error_count(evidence / "kct-check.json", "summary", "errors")
        native_saved = error_count(evidence / "native-drc.json", "error_count")
        native_refilled = error_count(
            evidence / "refilled-native" / "native-drc.json", "error_count"
        )
        lines = ["Sign-off", "--------"]
        if kct_errors == 0 and native_saved == 0 and native_refilled == 0:
            lines.append(
                "  Fresh kct check and native KiCad DRC (saved and refilled copper): zero errors."
            )
        else:
            lines.append(
                f"  kct check errors: {kct_errors}; native DRC errors (saved/refilled): "
                f"{native_saved}/{native_refilled}."
            )
        for outcome in self._outcomes or []:
            lines.append(f"  {outcome.name}: {outcome.status} -- {outcome.detail}")
        for name in self.reviewed:
            lines.append(
                f"  Procurement identities reviewed against supplier listings; see {name} "
                "and source project.kct."
            )
        return [*lines, *self.readme_lines]

    def _extra_inputs(self, options: ReadinessOptions) -> list[Path]:
        if self.inputs is None:
            return []
        return [options.board_dir / p for p in self.inputs(options)]

    # -- entry point -------------------------------------------------------

    def main(self) -> int:
        self._snapshot()
        manifest = self.package_dir / "manifest.json"
        # --generate refuses a recipe-finalised package; this script IS its recipe.
        if json.loads(manifest.read_text()).get("producer") != "kct readiness":
            shutil.rmtree(self.package_dir)
            (self.board_dir / "output" / "manufacturing.zip").unlink(missing_ok=True)
        engines = Engines(
            package_extras=self._package_extras,
            extra_checks=self._run_checks,
            extra_inputs=self._extra_inputs,
        )
        try:
            return readiness_cmd.main(
                [str(self.board_dir), "--generate", "--mfr", self.manufacturer], engines=engines
            )
        finally:
            if not self.package_dir.is_dir():
                # Nothing was published: restore the package we removed.
                for name, data in self._old.items():
                    target = self.package_dir / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                if self._old_archive is not None:
                    (self.board_dir / "output" / "manufacturing.zip").write_bytes(self._old_archive)
                print("Readiness did not publish; previous package restored.", file=sys.stderr)
