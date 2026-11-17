"""
Project cleanup command for kicad-tools.

Cleans up old/orphaned files from KiCad projects including intermediate
PCB versions, stale reports, and backup files.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["main", "CleanResult", "find_cleanable_files"]


@dataclass
class CleanableFile:
    """A file that can be cleaned up."""

    path: Path
    category: str  # "pcb_version", "stale_report", "backup"
    reason: str
    size_bytes: int

    @property
    def size_kb(self) -> float:
        """Size in kilobytes."""
        return self.size_bytes / 1024

    @property
    def size_str(self) -> str:
        """Human-readable size string."""
        if self.size_bytes < 1024:
            return f"{self.size_bytes} B"
        elif self.size_bytes < 1024 * 1024:
            return f"{self.size_bytes / 1024:.1f} KB"
        else:
            return f"{self.size_bytes / (1024 * 1024):.1f} MB"


@dataclass
class ProtectedFile:
    """A file that will be kept."""

    path: Path
    reason: str


@dataclass
class CleanResult:
    """Result of analyzing a project for cleanup."""

    project_dir: Path
    project_name: str
    to_delete: list[CleanableFile] = field(default_factory=list)
    to_keep: list[ProtectedFile] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def total_size_bytes(self) -> int:
        """Total size of files to delete in bytes."""
        return sum(f.size_bytes for f in self.to_delete)

    @property
    def total_size_str(self) -> str:
        """Human-readable total size to delete."""
        size = self.total_size_bytes
        if size < 1024:
            return f"{size} B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        else:
            return f"{size / (1024 * 1024):.1f} MB"

    def by_category(self, category: str) -> list[CleanableFile]:
        """Get files to delete by category."""
        return [f for f in self.to_delete if f.category == category]


# Patterns for detecting old PCB versions
PCB_VERSION_PATTERNS = [
    # Versioned files: *-v1.kicad_pcb, *-v2.kicad_pcb, etc.
    re.compile(r"-v\d+\.kicad_pcb$", re.IGNORECASE),
    # Routed versions: *-routed.kicad_pcb, *-routed-v34.kicad_pcb
    re.compile(r"-routed(-v?\d+)?\.kicad_pcb$", re.IGNORECASE),
    # Autorouted: *-autorouted.kicad_pcb
    re.compile(r"-autorouted\.kicad_pcb$", re.IGNORECASE),
    # Generated: *-generated.kicad_pcb
    re.compile(r"-generated\.kicad_pcb$", re.IGNORECASE),
    # Draft versions: *-draft.kicad_pcb
    re.compile(r"-draft\.kicad_pcb$", re.IGNORECASE),
    # Old versions: *-old.kicad_pcb
    re.compile(r"-old\.kicad_pcb$", re.IGNORECASE),
    # Backup copies: *-backup.kicad_pcb
    re.compile(r"-backup\.kicad_pcb$", re.IGNORECASE),
    # Copy files: *-copy.kicad_pcb, * copy.kicad_pcb
    re.compile(r"[-_ ]copy\.kicad_pcb$", re.IGNORECASE),
]

# Patterns for detecting stale reports
REPORT_PATTERNS = [
    # DRC reports with version suffix
    re.compile(r"drc[-_]?v?\d+\.(txt|rpt|json)$", re.IGNORECASE),
    # ERC reports with version suffix
    re.compile(r"erc[-_]?v?\d+\.(txt|rpt|json)$", re.IGNORECASE),
    # Generic old reports
    re.compile(r"drc[-_](old|prev|backup)\.(txt|rpt|json)$", re.IGNORECASE),
    re.compile(r"erc[-_](old|prev|backup)\.(txt|rpt|json)$", re.IGNORECASE),
]

# Patterns for detecting backup files
BACKUP_PATTERNS = [
    # .bak files
    re.compile(r"\.bak$", re.IGNORECASE),
    # ~ backup files
    re.compile(r"~$"),
    # KiCad backup format: *-bak.kicad_*
    re.compile(r"-bak\.kicad_", re.IGNORECASE),
    # Backup with date: *-backup-*.kicad_*
    re.compile(r"-backup-\d+\.kicad_", re.IGNORECASE),
    # KiCad autosave files
    re.compile(r"\.kicad_.*\.lck$", re.IGNORECASE),
    # Rescue files
    re.compile(r"-rescue\.kicad_", re.IGNORECASE),
]

# Additional patterns for deep clean mode
DEEP_CLEAN_PATTERNS = [
    # Gerber and drill files
    re.compile(r"\.(gbr|drl|gbl|gtl|gbs|gts|gbo|gto|gm1|gko|gpt|gpb)$", re.IGNORECASE),
    # Position/placement files
    re.compile(r"[-_]pos\.(csv|txt)$", re.IGNORECASE),
    re.compile(r"[-_]cpl\.(csv|txt)$", re.IGNORECASE),
    # BOM output files
    re.compile(r"[-_]bom\.(csv|xml|html)$", re.IGNORECASE),
    # Netlist exports
    re.compile(r"\.net$", re.IGNORECASE),
    # STEP/3D exports
    re.compile(r"\.(step|stp|wrl)$", re.IGNORECASE),
    # PDF exports
    re.compile(r"[-_](schematic|pcb|layout)\.pdf$", re.IGNORECASE),
]


def get_project_pcb_name(project_path: Path) -> str | None:
    """
    Get the main PCB filename from a project file.

    Args:
        project_path: Path to .kicad_pro file

    Returns:
        Expected main PCB filename (without path) or None if cannot determine
    """
    # The main PCB should match the project name
    return project_path.stem + ".kicad_pcb"


def get_project_schematic_name(project_path: Path) -> str | None:
    """
    Get the main schematic filename from a project file.

    Args:
        project_path: Path to .kicad_pro file

    Returns:
        Expected main schematic filename (without path) or None if cannot determine
    """
    return project_path.stem + ".kicad_sch"


def find_cleanable_files(
    project_path: Path,
    deep: bool = False,
) -> CleanResult:
    """
    Find files that can be cleaned up in a KiCad project directory.

    Args:
        project_path: Path to .kicad_pro file
        deep: If True, also include generated output files (gerbers, etc.)

    Returns:
        CleanResult with files categorized for deletion or keeping
    """
    if not project_path.exists():
        raise FileNotFoundError(f"Project file not found: {project_path}")

    if not project_path.suffix == ".kicad_pro":
        raise ValueError(f"Expected .kicad_pro file, got: {project_path}")

    project_dir = project_path.parent
    project_name = project_path.stem

    result = CleanResult(
        project_dir=project_dir,
        project_name=project_name,
    )

    # Get expected main files
    main_pcb = get_project_pcb_name(project_path)
    main_sch = get_project_schematic_name(project_path)

    # Add protected main files
    if main_pcb:
        main_pcb_path = project_dir / main_pcb
        if main_pcb_path.exists():
            result.to_keep.append(ProtectedFile(main_pcb_path, "main project PCB"))

    if main_sch:
        main_sch_path = project_dir / main_sch
        if main_sch_path.exists():
            result.to_keep.append(ProtectedFile(main_sch_path, "main schematic"))

    # Protect the project file itself
    result.to_keep.append(ProtectedFile(project_path, "project file"))

    # Scan directory for cleanable files
    protected_names = {p.path.name for p in result.to_keep}

    for file_path in project_dir.iterdir():
        if not file_path.is_file():
            continue

        filename = file_path.name

        # Skip protected files
        if filename in protected_names:
            continue

        # Check for old PCB versions
        if filename.endswith(".kicad_pcb"):
            matched = False
            for pattern in PCB_VERSION_PATTERNS:
                if pattern.search(filename):
                    try:
                        size = file_path.stat().st_size
                    except OSError:
                        size = 0
                    result.to_delete.append(
                        CleanableFile(
                            path=file_path,
                            category="pcb_version",
                            reason=f"matches pattern: {pattern.pattern}",
                            size_bytes=size,
                        )
                    )
                    matched = True
                    break

            if not matched and filename != main_pcb:
                # Check if it matches a backup pattern before categorizing as additional PCB
                is_backup = False
                for pattern in BACKUP_PATTERNS:
                    if pattern.search(filename):
                        try:
                            size = file_path.stat().st_size
                        except OSError:
                            size = 0
                        result.to_delete.append(
                            CleanableFile(
                                path=file_path,
                                category="backup",
                                reason=f"backup file: {pattern.pattern}",
                                size_bytes=size,
                            )
                        )
                        is_backup = True
                        break

                if not is_backup:
                    # Truly an additional PCB file
                    try:
                        size = file_path.stat().st_size
                    except OSError:
                        size = 0
                    result.to_delete.append(
                        CleanableFile(
                            path=file_path,
                            category="pcb_version",
                            reason="additional PCB file (not main project PCB)",
                            size_bytes=size,
                        )
                    )
            continue

        # Check for stale reports
        for pattern in REPORT_PATTERNS:
            if pattern.search(filename):
                try:
                    size = file_path.stat().st_size
                except OSError:
                    size = 0
                result.to_delete.append(
                    CleanableFile(
                        path=file_path,
                        category="stale_report",
                        reason=f"versioned/backup report: {pattern.pattern}",
                        size_bytes=size,
                    )
                )
                break

        # Check for backup files
        for pattern in BACKUP_PATTERNS:
            if pattern.search(filename):
                try:
                    size = file_path.stat().st_size
                except OSError:
                    size = 0
                result.to_delete.append(
                    CleanableFile(
                        path=file_path,
                        category="backup",
                        reason=f"backup file: {pattern.pattern}",
                        size_bytes=size,
                    )
                )
                break

        # Deep clean patterns
        if deep:
            for pattern in DEEP_CLEAN_PATTERNS:
                if pattern.search(filename):
                    try:
                        size = file_path.stat().st_size
                    except OSError:
                        size = 0
                    result.to_delete.append(
                        CleanableFile(
                            path=file_path,
                            category="generated",
                            reason=f"generated output: {pattern.pattern}",
                            size_bytes=size,
                        )
                    )
                    break

    # Find most recent report of each type and protect it
    _protect_latest_reports(result, project_dir)

    return result


def _protect_latest_reports(result: CleanResult, project_dir: Path) -> None:
    """
    Protect the most recent DRC/ERC report of each type.

    Modifies result in place to move the newest report from to_delete to to_keep.
    """
    # Group stale reports by type (drc/erc)
    drc_reports: list[CleanableFile] = []
    erc_reports: list[CleanableFile] = []

    for f in result.to_delete:
        if f.category == "stale_report":
            name_lower = f.path.name.lower()
            if name_lower.startswith("drc"):
                drc_reports.append(f)
            elif name_lower.startswith("erc"):
                erc_reports.append(f)

    # Find and protect newest of each type
    for reports, report_type in [(drc_reports, "DRC"), (erc_reports, "ERC")]:
        if not reports:
            continue

        # Sort by modification time (newest first)
        try:
            reports_with_mtime = [(f, f.path.stat().st_mtime) for f in reports]
            reports_with_mtime.sort(key=lambda x: x[1], reverse=True)
            newest = reports_with_mtime[0][0]

            # Move newest to keep list
            result.to_delete.remove(newest)
            result.to_keep.append(ProtectedFile(newest.path, f"most recent {report_type} report"))
        except (OSError, IndexError):
            pass


def format_output_text(result: CleanResult, verbose: bool = False) -> str:
    """Format clean result as text output."""
    lines = []
    lines.append(f"Project cleanup: {result.project_dir.name}/")
    lines.append("═" * 55)
    lines.append("")

    # Group files to delete by category
    pcb_versions = result.by_category("pcb_version")
    stale_reports = result.by_category("stale_report")
    backups = result.by_category("backup")
    generated = result.by_category("generated")

    if pcb_versions:
        lines.append(f"Would delete ({len(pcb_versions)} old PCB versions):")
        for f in pcb_versions:
            lines.append(f"  ✗ {f.path.name} ({f.size_str})")
        lines.append("")

    if stale_reports:
        lines.append(f"Would delete ({len(stale_reports)} stale reports):")
        for f in stale_reports:
            lines.append(f"  ✗ {f.path.name}")
        lines.append("")

    if backups:
        lines.append(f"Would delete ({len(backups)} backup files):")
        for f in backups:
            lines.append(f"  ✗ {f.path.name} ({f.size_str})")
        lines.append("")

    if generated:
        lines.append(f"Would delete ({len(generated)} generated files):")
        for f in generated:
            lines.append(f"  ✗ {f.path.name} ({f.size_str})")
        lines.append("")

    if result.to_keep:
        lines.append("Would keep:")
        for f in result.to_keep:
            lines.append(f"  ✓ {f.path.name} ({f.reason})")
        lines.append("")

    if result.to_delete:
        lines.append(f"Space savings: {result.total_size_str}")
    else:
        lines.append("No files to clean up.")

    return "\n".join(lines)


def format_output_json(result: CleanResult) -> str:
    """Format clean result as JSON output."""
    data = {
        "project_dir": str(result.project_dir),
        "project_name": result.project_name,
        "to_delete": [
            {
                "path": str(f.path),
                "name": f.path.name,
                "category": f.category,
                "reason": f.reason,
                "size_bytes": f.size_bytes,
            }
            for f in result.to_delete
        ],
        "to_keep": [
            {
                "path": str(f.path),
                "name": f.path.name,
                "reason": f.reason,
            }
            for f in result.to_keep
        ],
        "total_size_bytes": result.total_size_bytes,
        "errors": result.errors,
    }
    return json.dumps(data, indent=2)


def delete_files(result: CleanResult, verbose: bool = False) -> tuple[int, int]:
    """
    Delete the files marked for cleanup.

    Args:
        result: CleanResult with files to delete
        verbose: Print each file as it's deleted

    Returns:
        Tuple of (files_deleted, bytes_freed)
    """
    deleted = 0
    freed = 0

    for f in result.to_delete:
        try:
            f.path.unlink()
            deleted += 1
            freed += f.size_bytes
            if verbose:
                print(f"  Deleted: {f.path.name}")
        except OSError as e:
            result.errors.append(f"Failed to delete {f.path.name}: {e}")

    return deleted, freed


def create_parser() -> argparse.ArgumentParser:
    """Create argument parser for clean command."""
    parser = argparse.ArgumentParser(
        prog="kicad-project-clean",
        description="Clean up old/orphaned files from KiCad projects",
    )
    parser.add_argument(
        "project",
        help="Path to .kicad_pro file",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be cleaned without deleting (default behavior)",
    )
    parser.add_argument(
        "--deep",
        action="store_true",
        help="Also delete generated output files (gerbers, BOM exports, etc.)",
    )
    parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Delete files without confirmation (for CI/automation)",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show detailed output",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Main entry point for project clean command."""
    parser = create_parser()
    args = parser.parse_args(argv)

    project_path = Path(args.project).resolve()

    try:
        result = find_cleanable_files(project_path, deep=args.deep)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # Output format
    if args.format == "json":
        print(format_output_json(result))
    else:
        print(format_output_text(result, verbose=args.verbose))

    # Force mode - delete without confirmation
    if args.force and result.to_delete:
        deleted, freed = delete_files(result, verbose=args.verbose)
        if args.format != "json":
            print(f"\nDeleted {deleted} files, freed {freed / 1024:.1f} KB")
        if result.errors:
            for err in result.errors:
                print(f"Error: {err}", file=sys.stderr)
            return 1
        return 0

    # Default is dry-run: show what would be cleaned but don't delete
    # Use --force to actually delete files
    return 0


if __name__ == "__main__":
    sys.exit(main())
