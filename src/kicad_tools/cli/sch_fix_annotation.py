#!/usr/bin/env python3
"""
Hierarchy-aware power/flag-symbol annotation repair with a net-neutrality gate.

This command walks a hierarchical KiCad schematic from its root and repairs
power/flag-symbol annotation problems that ``kicad-cli`` reports as
``schematic has annotation errors``:

- Un-numbered, net-name-styled power refs (``#GNDD``, ``#+3.3V``, ``#+5V``,
  ``#GNDA``) that neither ``re-annotate`` nor ``repair-instances`` recognize.
- Duplicate power refs across the flattened hierarchy (e.g. ``#GNDD`` placed
  in several sub-sheets).
- Inconsistent zero-padding on numbered power refs (``#PWR40`` vs ``#PWR040``).
- Missing ``(instances)`` blocks for power symbols.

Every ``power:``-lib_id symbol whose reference is not already in the canonical
``#PWR<dd>`` / ``#FLG<dd>`` shape is assigned a globally-unique designator.
Real-component references (``R1``, ``C3``, ``U2``) are never touched.

Because renumbering power symbols must be an electrically-neutral operation
(power symbols connect by *value*, not by designator), the command exports the
schematic netlist before and after the repair via ``kicad-cli`` and refuses to
write if net membership changes.  The comparison translates the *before*
snapshot's references through the rename mapping so a renamed symbol's own
nodes are compared apples-to-apples.

Usage:
    # Preview the repair plan (no netlist gate, no writes)
    kicad-tools sch fix-annotation root.kicad_sch --dry-run

    # Repair in place (runs the net-neutrality gate first)
    kicad-tools sch fix-annotation root.kicad_sch

    # Repair with backups
    kicad-tools sch fix-annotation root.kicad_sch --backup

    # Repair without the net-neutrality gate (unsafe; e.g. no kicad-cli)
    kicad-tools sch fix-annotation root.kicad_sch --skip-net-check
"""

import re
import shutil
import sys
import tempfile
from pathlib import Path

from kicad_tools.cli.export_netlist import Netlist, export_netlist, load_netlist
from kicad_tools.cli.modify_schematic import create_backup
from kicad_tools.cli.runner import find_kicad_cli
from kicad_tools.cli.sch_re_annotate import (
    _apply_uuid_reference_rename,
    _detect_project_info,
    _format_reference,
    _parse_reference,
)
from kicad_tools.cli.sch_repair_instances import (
    _find_sub_block_span,
    _find_symbol_block,
    _insert_project_instance,
    _scan_symbol_children,
)
from kicad_tools.cli.sch_set_footprint import _collect_schematic_files

# Whitespace token for regex: matches one or more tabs or spaces
_WS = r"[ \t]+"

# Canonical shapes for already-correct power/flag references: ``#PWR`` or
# ``#FLG`` followed by two-or-more digits (KiCad zero-pads to at least two).
_CANONICAL_POWER_RE = re.compile(r"^#(PWR|FLG)\d{2,}$")

# Reference prefix chosen for a ``power:`` symbol.  Flag symbols (PWR_FLAG and
# any lib_id containing ``FLAG``) get ``#FLG``; all other power symbols
# (grounds and rails alike) get ``#PWR``, matching KiCad convention.
_FLAG_PREFIX = "#FLG"
_POWER_PREFIX = "#PWR"


def _is_power_symbol(sym: dict) -> bool:
    """Return True if *sym* is a KiCad power/flag symbol."""
    return str(sym.get("lib_id", "")).startswith("power:")


def _is_flag_symbol(sym: dict) -> bool:
    """Return True if *sym* is a power *flag* symbol (gets ``#FLG``)."""
    lib_id = str(sym.get("lib_id", ""))
    return "FLAG" in lib_id.upper() or "PWR_FLAG" in lib_id.upper()


def _power_prefix_for(sym: dict) -> str:
    """Return the canonical reference prefix for a power/flag *sym*."""
    return _FLAG_PREFIX if _is_flag_symbol(sym) else _POWER_PREFIX


def _canonical_number(sym: dict) -> int | None:
    """Return the canonical number a power/flag *sym* already holds, else None.

    A reference is canonical only when it *exactly* round-trips through
    ``_format_reference`` for the prefix appropriate to the symbol kind.  This
    makes ``#PWR40`` canonical (number 40) but ``#PWR040`` non-canonical (that
    number formats to ``#PWR40``, not ``#PWR040``), catching the inconsistent
    zero-padding described in the issue.  Net-name-styled refs (``#GNDD``),
    unpadded single digits (``#PWR2``), and family mismatches (a flag symbol
    carrying ``#PWR01``) all return None.
    """
    ref = str(sym.get("reference", ""))
    m = _CANONICAL_POWER_RE.match(ref)
    if not m:
        return None
    number = int(ref[4:])
    expected = _power_prefix_for(sym)
    if _format_reference(expected, number) != ref:
        return None
    return number


def _needs_reassignment(sym: dict) -> bool:
    """Return True if a power/flag *sym*'s reference is not canonical."""
    return _canonical_number(sym) is None


def _existing_canonical_numbers(power_symbols: list[dict]) -> dict[str, set[int]]:
    """Collect the canonical numbers *kept* under first-occurrence-wins dedup.

    A reference reserves its number only when it is *already canonical and
    correctly-familied* AND it is the **first** symbol across the flattened
    hierarchy to hold that ``(prefix, number)`` pair.  Later symbols carrying
    the same canonical designator are cross-sheet duplicates that will be
    reassigned by ``build_rename_plan`` — so they do **not** reserve.

    Reserving via a plain set would collapse two ``#PWR40`` symbols into the
    single entry ``{40}`` and silently discard the collision, leaving both
    duplicates untouched.  Seeding from first-occurrence canonicals only keeps
    the reserved set consistent with the keep/reassign decision made in
    ``build_rename_plan`` (the first holder keeps ``40``; the ``_next()``
    machinery then hands the duplicate a fresh, non-reserved number).
    """
    reserved: dict[str, set[int]] = {_POWER_PREFIX: set(), _FLAG_PREFIX: set()}
    for sym in power_symbols:
        number = _canonical_number(sym)
        if number is None:
            continue
        prefix = _power_prefix_for(sym)
        if number in reserved[prefix]:
            # Duplicate canonical ref (cross-sheet collision): the first holder
            # already reserved this number; this one will be reassigned.
            continue
        reserved[prefix].add(number)
    return reserved


def build_rename_plan(ordered_power_symbols: list[dict]) -> dict[str, dict]:
    """Build the UUID-keyed rename plan for power/flag symbols.

    *ordered_power_symbols* is the flattened, hierarchy-ordered list of
    power/flag symbols (each dict as produced by
    ``_extract_symbols_with_instance_info`` plus a ``uuid`` key).  Globally
    unique ``#PWR<dd>`` / ``#FLG<dd>`` designators are assigned to every
    symbol whose reference is not already canonical, skipping numbers held by
    canonical symbols so no collision is introduced.

    Uniqueness is enforced **project-wide across the flattened hierarchy**
    using first-occurrence-wins: the first symbol to hold a canonical
    ``(prefix, number)`` keeps it, and any *later* symbol carrying the same
    canonical designator is treated as a cross-sheet duplicate and reassigned
    via ``_next()``.  This catches the case the old per-symbol canonicality
    check missed — two individually-canonical ``#PWR40`` symbols on different
    sheets are a real annotation error even though each looks fine in
    isolation.

    Returns a dict mapping symbol UUID -> ``{"old": str, "new": str}`` for
    every symbol that actually changes reference (identity renames are
    omitted).
    """
    reserved = _existing_canonical_numbers(ordered_power_symbols)
    counters: dict[str, int] = {_POWER_PREFIX: 1, _FLAG_PREFIX: 1}

    def _next(prefix: str) -> int:
        n = counters[prefix]
        while n in reserved[prefix]:
            n += 1
        reserved[prefix].add(n)
        counters[prefix] = n + 1
        return n

    # Canonical (prefix, number) pairs already kept, so a later symbol holding
    # the same canonical designator is recognised as a cross-sheet duplicate.
    kept_canonical: dict[str, set[int]] = {_POWER_PREFIX: set(), _FLAG_PREFIX: set()}

    plan: dict[str, dict] = {}
    for sym in ordered_power_symbols:
        prefix = _power_prefix_for(sym)
        number = _canonical_number(sym)
        if number is not None and number not in kept_canonical[prefix]:
            # First canonical holder of this (prefix, number): keep it as-is.
            kept_canonical[prefix].add(number)
            continue
        # Non-canonical ref, OR a later duplicate of an already-kept canonical:
        # assign a fresh, project-globally-unique number.
        new_ref = _format_reference(prefix, _next(prefix))
        old_ref = str(sym["reference"])
        if old_ref == new_ref:
            continue
        plan[str(sym["uuid"])] = {"old": old_ref, "new": new_ref}
    return plan


def _net_membership(netlist: Netlist) -> dict[frozenset[tuple[str, str]], int]:
    """Build a value-independent net-membership snapshot.

    Each net becomes a frozenset of ``(reference, pin)`` tuples; we key the
    snapshot on that membership set (counting duplicates) rather than on the
    net's display name/code, since renumbering can legitimately change a net's
    name while leaving the electrical grouping intact.

    ``#``-prefixed nodes (power/flag symbols) are **excluded** from the
    membership: power symbols connect by *value*, so their designators are
    electrically meaningless — the very thing this command renumbers.  Keeping
    them would break the gate on cross-sheet duplicates: ``ref_rename`` is keyed
    by the old reference *string*, so renaming one of two same-named ``#PWR40``
    nodes translates **both** before-snapshot nodes, producing a spurious diff
    that aborts an otherwise net-neutral repair.  Dropping power nodes compares
    only the real-component connectivity that must stay invariant.

    Returns a multiset ``{membership_frozenset: count}``.
    """
    snapshot: dict[frozenset[tuple[str, str]], int] = {}
    for net in netlist.nets:
        membership = frozenset(
            (node.reference, node.pin) for node in net.nodes if not node.reference.startswith("#")
        )
        snapshot[membership] = snapshot.get(membership, 0) + 1
    return snapshot


def _translate_membership(
    snapshot: dict[frozenset[tuple[str, str]], int],
    ref_rename: dict[str, str],
) -> dict[frozenset[tuple[str, str]], int]:
    """Translate a ``before`` snapshot's references through *ref_rename*.

    *ref_rename* maps old reference string -> new reference string.  Every
    ``(reference, pin)`` tuple whose reference was renamed is rewritten so the
    ``before`` snapshot can be compared against the ``after`` snapshot without
    spurious diffs on the renamed symbols' own nodes.
    """
    translated: dict[frozenset[tuple[str, str]], int] = {}
    for membership, count in snapshot.items():
        new_membership = frozenset((ref_rename.get(ref, ref), pin) for (ref, pin) in membership)
        translated[new_membership] = translated.get(new_membership, 0) + count
    return translated


def diff_net_membership(
    before: Netlist,
    after: Netlist,
    ref_rename: dict[str, str],
) -> list[str]:
    """Return a human-readable list of net-membership differences.

    Empty list means the repair was net-neutral.  The *before* snapshot's
    references are translated through *ref_rename* first so a renamed power
    symbol's own nodes compare equal.
    """
    before_snap = _translate_membership(_net_membership(before), ref_rename)
    after_snap = _net_membership(after)

    diffs: list[str] = []

    def _fmt(membership: frozenset[tuple[str, str]]) -> str:
        return "{" + ", ".join(sorted(f"{r}.{p}" for r, p in membership)) + "}"

    for membership, count in sorted(before_snap.items(), key=lambda kv: _fmt(kv[0])):
        after_count = after_snap.get(membership, 0)
        if after_count < count:
            diffs.append(f"  REMOVED (x{count - after_count}): {_fmt(membership)}")
    for membership, count in sorted(after_snap.items(), key=lambda kv: _fmt(kv[0])):
        before_count = before_snap.get(membership, 0)
        if before_count < count:
            diffs.append(f"  ADDED   (x{count - before_count}): {_fmt(membership)}")

    return diffs


def _update_instance_reference(text: str, sym_uuid: str, new_ref: str) -> str:
    """Update every ``(reference "...")`` inside a symbol's ``(instances)`` block.

    Used when a power symbol is renamed but already carries a well-formed
    instance for the project — the stale designator inside its instances block
    must be brought into line with the new ``(property "Reference")`` value.
    """
    bounds = _find_symbol_block(text, sym_uuid)
    if bounds is None:
        return text
    start, end = bounds
    block = text[start:end]

    instances_span, _ = _scan_symbol_children(block)
    if instances_span is None:
        return text
    inst_start, inst_end = instances_span
    inst_text = block[inst_start:inst_end]
    new_inst_text = re.sub(
        r'(\(reference ")[^"]+(")',
        r"\g<1>" + new_ref + r"\g<2>",
        inst_text,
    )
    new_block = block[:inst_start] + new_inst_text + block[inst_end:]
    return text[:start] + new_block + text[end:]


def _apply_plan_to_texts(
    all_files: list[Path],
    file_texts: dict[Path, str],
    file_symbols: dict[Path, list[dict]],
    plan: dict[str, dict],
    project_name: str,
    file_instance_paths: dict[Path, str],
) -> dict[Path, str]:
    """Apply the rename plan + instance-block repair to in-memory buffers.

    Returns a map of file -> modified text for every file that changed.
    Files whose power symbols need no change are omitted.
    """
    modified: dict[Path, str] = {}

    for sch_file in all_files:
        text = file_texts[sch_file]
        syms = file_symbols[sch_file]
        instance_path = file_instance_paths.get(sch_file, "")
        current = text
        changed = False

        for sym in syms:
            if not _is_power_symbol(sym):
                continue
            uuid = str(sym["uuid"])
            entry = plan.get(uuid)
            new_ref = entry["new"] if entry else str(sym["reference"])

            # Rename the (property "Reference") value (only when the plan
            # changes it).
            if entry:
                current = _apply_uuid_reference_rename(current, uuid, new_ref)
                changed = True

            # Ensure a correct (instances) block exists.  A power symbol needs
            # one when it has none, points at the wrong project, or carries
            # loose sibling project blocks.
            needs_instance = (
                not sym.get("has_project_instance")
                or sym.get("has_wrong_project")
                or sym.get("has_loose_project_blocks")
            )
            if needs_instance and instance_path:
                current = _insert_project_instance(
                    current,
                    uuid,
                    project_name,
                    instance_path,
                    new_ref,
                    replace_wrong_project=bool(sym.get("has_wrong_project")),
                    repair_loose_blocks=bool(sym.get("has_loose_project_blocks")),
                )
                changed = True
            elif entry and sym.get("has_project_instance"):
                # Renamed a symbol that already has a well-formed instance for
                # this project — the (reference "OLD") inside its instances
                # block is now stale and must be updated to the new ref, else
                # kicad-cli keeps reporting the old designator.
                current = _update_instance_reference(current, uuid, new_ref)
                changed = True

        if changed:
            modified[sch_file] = current

    return modified


def _run_net_gate(
    schematic_path: Path,
    all_files: list[Path],
    modified_files: dict[Path, str],
    ref_rename: dict[str, str],
    kicad_cli: Path,
) -> tuple[bool, list[str]]:
    """Run the net-neutrality gate.

    Exports the netlist from the unmodified tree, then from a temporary copy
    of the hierarchy with *modified_files* applied, and diffs net membership
    (translating the before snapshot through *ref_rename*).

    Returns ``(is_neutral, diffs)``.  ``is_neutral`` is False when an export
    fails (fail-closed) or when net membership changed.
    """
    # 1. Netlist of the current on-disk (unmodified) tree.
    with tempfile.TemporaryDirectory() as before_dir:
        before_out = Path(before_dir) / "before.net"
        ok, err = export_netlist(schematic_path, before_out, kicad_cli)
        if not ok:
            return False, [f"  netlist export (before) failed: {err}"]
        before_netlist = load_netlist(before_out)

    # 2. Copy the hierarchy to a temp dir, apply modifications, export.
    with tempfile.TemporaryDirectory() as work_dir:
        work_root = Path(work_dir)
        root_dir = schematic_path.parent
        file_map: dict[Path, Path] = {}
        for sch_file in all_files:
            rel = sch_file.resolve().relative_to(root_dir.resolve())
            dest = work_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if sch_file in modified_files:
                dest.write_text(modified_files[sch_file], encoding="utf-8")
            else:
                shutil.copy2(sch_file, dest)
            file_map[sch_file] = dest

        after_out = work_root / "after.net"
        ok, err = export_netlist(file_map[schematic_path], after_out, kicad_cli)
        if not ok:
            return False, [f"  netlist export (after) failed: {err}"]
        after_netlist = load_netlist(after_out)

    diffs = diff_net_membership(before_netlist, after_netlist, ref_rename)
    return (len(diffs) == 0), diffs


def run_fix_annotation(
    schematic_path: Path,
    dry_run: bool = False,
    backup: bool = True,
    format: str = "text",
    skip_net_check: bool = False,
) -> int:
    """Run hierarchy-aware power/flag annotation repair.

    Args:
        schematic_path: Path to the *root* .kicad_sch file.
        dry_run: If True, print the plan and make no changes (the
            net-neutrality gate is not run, since nothing is written).
        backup: If True, create ``.bak`` backups before writing.
        format: Output format ("text" or "json").
        skip_net_check: If True, skip the net-neutrality gate (unsafe;
            for environments without kicad-cli).

    Returns:
        0 on success, 1 on error, 2 if the net-neutrality gate fails.
    """
    if not schematic_path.exists():
        print(f"Error: File not found: {schematic_path}", file=sys.stderr)
        return 1

    all_files = _collect_schematic_files(schematic_path)
    if not all_files:
        print("Error: No schematic files found", file=sys.stderr)
        return 1

    project_name, root_uuid, file_instance_paths = _detect_project_info(schematic_path, all_files)
    if not root_uuid:
        print(
            "Error: Could not detect project UUID from root schematic",
            file=sys.stderr,
        )
        return 1

    # Phase 1: collect symbols across the hierarchy.
    file_texts: dict[Path, str] = {}
    file_symbols: dict[Path, list[dict]] = {}
    for sch_file in all_files:
        try:
            text = sch_file.read_text(encoding="utf-8")
        except OSError as e:
            print(f"Error reading {sch_file}: {e}", file=sys.stderr)
            return 1
        file_texts[sch_file] = text
        file_symbols[sch_file] = _extract_all_symbols(text, project_name)

    # Phase 2: build the global rename plan (hierarchy-ordered).
    ordered_power: list[dict] = []
    for sch_file in all_files:
        ordered_power.extend(s for s in file_symbols[sch_file] if _is_power_symbol(s))

    plan = build_rename_plan(ordered_power)

    # Identify power symbols that only need an instances-block repair (no
    # rename) so we report/act on them too.
    instance_only: list[dict] = []
    for sch_file in all_files:
        for sym in file_symbols[sch_file]:
            if not _is_power_symbol(sym):
                continue
            if str(sym["uuid"]) in plan:
                continue
            if (
                not sym.get("has_project_instance")
                or sym.get("has_wrong_project")
                or sym.get("has_loose_project_blocks")
            ):
                instance_only.append(sym)

    if not plan and not instance_only:
        if format == "json":
            import json

            print(json.dumps({"renames": [], "total": 0, "dry_run": dry_run}))
        else:
            print("No power/flag annotation errors found.")
        return 0

    # Phase 3: report the plan.
    ref_rename = {info["old"]: info["new"] for info in plan.values()}
    if format == "json":
        import json

        output = {
            "renames": [
                {"old": info["old"], "new": info["new"], "uuid": uid}
                for uid, info in sorted(plan.items(), key=lambda x: x[1]["new"])
            ],
            "instance_only": len(instance_only),
            "total": len(plan),
            "dry_run": dry_run,
        }
        print(json.dumps(output, indent=2))
    else:
        print(
            f"Power/flag annotation plan ({len(plan)} rename(s), "
            f"{len(instance_only)} instance-only repair(s)):"
        )
        print()
        for uid, info in sorted(plan.items(), key=lambda x: x[1]["new"]):
            print(f"    {info['old']} -> {info['new']}")
        if instance_only:
            print()
            print("  Instance-block repairs (no rename):")
            for sym in instance_only:
                print(f"    {sym['reference']}  ({sym['lib_id']})")
        print()

    if dry_run:
        if format == "text":
            print("Dry run: no changes made.")
        return 0

    # Phase 4: apply changes to in-memory buffers.
    modified_files = _apply_plan_to_texts(
        all_files,
        file_texts,
        file_symbols,
        plan,
        project_name,
        file_instance_paths,
    )

    if not modified_files:
        if format == "text":
            print("No files needed modification.")
        return 0

    # Phase 5: net-neutrality gate.
    if not skip_net_check:
        kicad_cli = find_kicad_cli()
        if kicad_cli is None:
            print(
                "Error: kicad-cli not found — required for the net-neutrality "
                "gate.\nInstall KiCad 8+ (https://www.kicad.org/download/) or "
                "re-run with --skip-net-check to proceed WITHOUT the safety "
                "gate (unsafe).",
                file=sys.stderr,
            )
            return 1

        is_neutral, diffs = _run_net_gate(
            schematic_path, all_files, modified_files, ref_rename, kicad_cli
        )
        if not is_neutral:
            print(
                "Net-neutrality gate FAILED: net membership would change. No files written.",
                file=sys.stderr,
            )
            for line in diffs:
                print(line, file=sys.stderr)
            return 2
        if format == "text":
            print("Net-neutrality gate passed (net membership unchanged).")
    elif format == "text":
        print("WARNING: --skip-net-check set; net-neutrality NOT verified.")

    # Phase 6: write the modified files.
    for sch_file, new_text in modified_files.items():
        if backup:
            bak = create_backup(sch_file)
            if format == "text":
                print(f"  Backup: {bak.name}")
        try:
            sch_file.write_text(new_text, encoding="utf-8")
        except OSError as e:
            print(f"Error writing {sch_file}: {e}", file=sys.stderr)
            return 1

    if format == "text":
        print(f"Repaired {len(plan)} power/flag reference(s) across {len(modified_files)} file(s).")

    return 0


def _extract_all_symbols(text: str, project_name: str) -> list[dict]:
    """Extract *all* symbols from *text* with per-project instance metadata.

    This mirrors ``sch_repair_instances._extract_symbols_with_instance_info``
    but deliberately does **not** skip well-formed power symbols — the
    fix-annotation workflow needs every ``power:`` symbol so it can renumber
    malformed refs and detect the ones missing instance blocks.  It reuses the
    same structural scanners (``_scan_symbol_children`` / ``_find_sub_block_span``)
    for symbol boundaries and instance-state classification.

    Each returned dict carries: reference, prefix, number, unit_suffix, lib_id,
    uuid, has_project_instance, has_wrong_project, has_loose_project_blocks,
    and loose_project_blocks — computed relative to *project_name*.
    """
    symbols: list[dict] = []
    ref_pattern = re.compile(r'\(property "Reference" "([^"]+)"')
    start_pattern = re.compile(
        r"(?:^|\n)(" + _WS + r")\(symbol\n" + _WS + r'\(lib_id "([^"]+)"\)',
        re.DOTALL,
    )

    for start_match in start_pattern.finditer(text):
        lib_id = start_match.group(2)
        paren_start = text.index("(symbol", start_match.start())
        block_end = _find_sub_block_span(text, paren_start)
        block = text[paren_start:block_end]

        ref_match = ref_pattern.search(block)
        if not ref_match:
            continue
        ref = ref_match.group(1)

        instances_span, loose_projects = _scan_symbol_children(block)
        has_instances_block = instances_span is not None
        if instances_span is not None:
            inst_start, inst_end = instances_span
            has_project = f'(project "{project_name}"' in block[inst_start:inst_end]
        else:
            has_project = False
        has_loose_project_blocks = bool(loose_projects)
        has_wrong_project = has_instances_block and not has_project and not has_loose_project_blocks

        # Locate the symbol-level uuid (the last uuid not inside a (pin ...)).
        all_uuids = list(re.finditer(r'\(uuid "([^"]+)"\)', block))
        sym_uuid = ""
        for uuid_m in reversed(all_uuids):
            preceding = block[: uuid_m.start()]
            last_paren_close = preceding.rfind(")")
            last_pin_open = preceding.rfind("(pin ")
            if last_pin_open > last_paren_close:
                continue
            sym_uuid = uuid_m.group(1)
            break
        if not sym_uuid and all_uuids:
            sym_uuid = all_uuids[-1].group(1)

        prefix, number, unit_suffix = _parse_reference(ref)

        symbols.append(
            {
                "reference": ref,
                "prefix": prefix,
                "number": number,
                "unit_suffix": unit_suffix,
                "lib_id": lib_id,
                "uuid": sym_uuid,
                "has_project_instance": has_project,
                "has_wrong_project": has_wrong_project,
                "has_loose_project_blocks": has_loose_project_blocks,
                "loose_project_blocks": loose_projects,
            }
        )

    return symbols


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description=("Hierarchy-aware power/flag annotation repair with a net-neutrality gate"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", type=Path, help="Path to root .kicad_sch file")
    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Preview changes without modifying files",
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        help="Create backup before modifying",
    )
    parser.add_argument(
        "--skip-net-check",
        action="store_true",
        help="Skip the net-neutrality gate (unsafe; e.g. no kicad-cli)",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )

    args = parser.parse_args(argv)

    return run_fix_annotation(
        schematic_path=args.schematic,
        dry_run=args.dry_run,
        backup=args.backup,
        format=args.format,
        skip_net_check=args.skip_net_check,
    )


if __name__ == "__main__":
    sys.exit(main())
