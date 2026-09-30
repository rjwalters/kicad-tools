#!/usr/bin/env python3
"""Report drift between the ecosystem registry and upstream reality.

Issue #5839.  Polls each pollable project's forge and classifies what has
changed since the ``last_verified`` date recorded in
``src/kicad_tools/ecosystem/data/projects.toml``.

**This script never writes the registry.**  It prints the TOML a human can
apply, the same discipline as the ``verified <date>`` comments in
``benchmarks/external/boards.toml`` and the same reporting posture as
``scripts/changelog_gap_report.py``.  Facts in that file are human-ratified by
construction: an automated edit would defeat the point of recording who
checked them.

Drift classes, in severity order:

===================  ==========================================================
``license-changed``  SPDX id differs. Recomputes code-reuse rights -- an
                     MIT -> AGPL flip retroactively forbids reuse.  ERROR
``renamed``          Upstream moved; the recorded URL is now a redirect and
                     will eventually rot.  ERROR
``archived``         Upstream archived; the verdict and framing need
                     rewording.  ERROR
``license-missing``  No LICENSE file upstream; treat as all-rights-reserved
                     regardless of what the README claims.  WARN
``reeval-trigger``   The entry records a re-evaluation trigger and an
                     error-class fact moved; a human should re-read it.  WARN
``stale-facts``      stars / last_push drifted, or last_verified is older
                     than --max-age days.  INFO
===================  ==========================================================

Usage::

    uv run python scripts/ecosystem_refresh.py                 # human report
    uv run python scripts/ecosystem_refresh.py --format json   # machine report
    uv run python scripts/ecosystem_refresh.py --strict        # exit 1 on ERROR

Requires the ``gh`` CLI for GitHub projects (it reuses your existing auth).
GitLab projects are polled over the public API with no auth.  A project with
``vcs = "other"`` has no API and is reported as ``unpollable`` rather than
failing the run.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from kicad_tools.ecosystem import (  # noqa: E402  (path bootstrap above)
    EcosystemProject,
    load_registry,
)

#: Drift class -> severity.
SEVERITY: dict[str, str] = {
    "license-changed": "error",
    "renamed": "error",
    "archived": "error",
    "license-missing": "warn",
    "reeval-trigger": "warn",
    "stale-facts": "info",
    "unpollable": "info",
    "probe-failed": "info",
}

#: Default age (days) past which `last_verified` is itself reported.
DEFAULT_MAX_AGE_DAYS = 90


@dataclass
class Finding:
    """One drift observation about one project."""

    project_id: str
    drift_class: str
    detail: str
    suggested_toml: list[str] = field(default_factory=list)

    @property
    def severity(self) -> str:
        """Severity for this drift class."""
        return SEVERITY.get(self.drift_class, "info")

    def to_dict(self) -> dict[str, Any]:
        """Convert to a JSON-serializable dictionary."""
        return {
            "project_id": self.project_id,
            "class": self.drift_class,
            "severity": self.severity,
            "detail": self.detail,
            "suggested_toml": self.suggested_toml,
        }


@dataclass
class UpstreamFacts:
    """What a forge reports about a project right now."""

    license: str | None = None
    stars: int | None = None
    last_push: str | None = None
    archived: bool = False
    full_name: str | None = None
    error: str | None = None


def _run_gh(args: list[str]) -> tuple[int, str, str]:
    """Run a ``gh`` command, returning ``(returncode, stdout, stderr)``."""
    try:
        proc = subprocess.run(
            ["gh", *args], capture_output=True, text=True, timeout=30, check=False
        )
    except FileNotFoundError:
        return 127, "", "gh CLI not found on PATH"
    except subprocess.TimeoutExpired:
        return 124, "", "gh timed out"
    return proc.returncode, proc.stdout, proc.stderr


def probe_github(slug: str) -> UpstreamFacts:
    """Fetch current facts for a GitHub project via ``gh api``."""
    code, out, err = _run_gh(
        [
            "api",
            f"repos/{slug}",
            "--jq",
            '{license: (.license.spdx_id // "NONE"), stars: .stargazers_count, '
            "pushed_at: .pushed_at, archived: .archived, full_name: .full_name}",
        ]
    )
    if code != 0:
        return UpstreamFacts(error=(err or out).strip() or f"gh exited {code}")
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        return UpstreamFacts(error=f"unparseable gh output: {exc}")

    return UpstreamFacts(
        license=payload.get("license") or "NONE",
        stars=payload.get("stars"),
        last_push=_to_date(payload.get("pushed_at")),
        archived=bool(payload.get("archived")),
        full_name=payload.get("full_name"),
    )


def probe_gitlab(slug: str) -> UpstreamFacts:
    """Fetch current facts for a GitLab project over the public API."""
    encoded = urllib.parse.quote(slug, safe="")
    url = f"https://gitlab.com/api/v4/projects/{encoded}?license=true"
    request = urllib.request.Request(url, headers={"User-Agent": "kicad-tools-ecosystem"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return UpstreamFacts(error=f"gitlab probe failed: {exc}")

    licence = payload.get("license") or {}
    return UpstreamFacts(
        license=(licence.get("key") or "NONE").upper() if licence else "NONE",
        stars=payload.get("star_count"),
        last_push=_to_date(payload.get("last_activity_at")),
        archived=bool(payload.get("archived")),
        full_name=payload.get("path_with_namespace"),
    )


def _to_date(timestamp: str | None) -> str | None:
    """Normalize an ISO-8601 timestamp to ``YYYY-MM-DD``."""
    if not timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc).date().isoformat()


def compare(
    project: EcosystemProject,
    facts: UpstreamFacts,
    today: date,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> list[Finding]:
    """Classify the difference between a registry entry and upstream facts.

    Pure: no network, no clock. This is the unit under test -- the probes are
    mocked and this function is fed recorded payloads.
    """
    findings: list[Finding] = []

    if not project.is_pollable:
        return [
            Finding(
                project.project_id,
                "unpollable",
                f'vcs="{project.vcs}" has no API to poll; facts are human-maintained',
            )
        ]

    if facts.error:
        return [Finding(project.project_id, "probe-failed", facts.error)]

    if facts.full_name and project.slug and facts.full_name.lower() != project.slug.lower():
        findings.append(
            Finding(
                project.project_id,
                "renamed",
                f"upstream is now {facts.full_name!r}, registry says {project.slug!r}",
                [
                    f'slug = "{facts.full_name}"',
                    f'repo_url = "https://github.com/{facts.full_name}"',
                ],
            )
        )

    if facts.archived:
        findings.append(
            Finding(
                project.project_id,
                "archived",
                "upstream repository is archived; re-word the verdict and summary",
            )
        )

    upstream_license = facts.license or "NONE"
    if upstream_license != project.license:
        findings.append(
            Finding(
                project.project_id,
                "license-changed",
                (
                    f"license is now {upstream_license!r}, registry says "
                    f"{project.license!r} -- re-derive license_compat "
                    f"(currently {project.license_compat!r})"
                ),
                [f'license = "{upstream_license}"'],
            )
        )
    elif upstream_license == "NONE" and project.license_compat != "unlicensed":
        findings.append(
            Finding(
                project.project_id,
                "license-missing",
                (
                    "upstream commits no LICENSE but license_compat is "
                    f"{project.license_compat!r}; all-rights-reserved applies"
                ),
                ['license_compat = "unlicensed"'],
            )
        )

    stale: list[str] = []
    suggested: list[str] = []
    if facts.stars is not None and facts.stars != project.stars:
        stale.append(f"stars {project.stars} -> {facts.stars}")
        suggested.append(f"stars = {facts.stars}")
    if facts.last_push and facts.last_push != project.last_push:
        stale.append(f"last_push {project.last_push} -> {facts.last_push}")
        suggested.append(f'last_push = "{facts.last_push}"')

    age = (today - date.fromisoformat(project.last_verified)).days
    if age > max_age_days:
        stale.append(f"last_verified is {age} days old (>{max_age_days})")

    if stale:
        suggested.append(f'last_verified = "{today.isoformat()}"')
        findings.append(Finding(project.project_id, "stale-facts", "; ".join(stale), suggested))

    # Only an error-class change (license flip, rename, archive) is worth
    # re-reading a trigger for. Firing on a star-count delta would make the
    # warn tier noise, and a noisy warn tier gets ignored.
    if project.reeval_trigger and any(f.severity == "error" for f in findings):
        findings.append(
            Finding(
                project.project_id,
                "reeval-trigger",
                "entry records a re-evaluation trigger and an error-class fact moved: "
                + " ".join(project.reeval_trigger.split()),
            )
        )

    return findings


def probe(project: EcosystemProject) -> UpstreamFacts:
    """Dispatch to the right forge probe for ``project``."""
    if project.vcs == "github":
        return probe_github(project.slug)
    if project.vcs == "gitlab":
        return probe_gitlab(project.slug)
    return UpstreamFacts(error=f'no probe for vcs="{project.vcs}"')


def main(argv: list[str] | None = None) -> int:
    """Poll every pollable project and report drift."""
    parser = argparse.ArgumentParser(description="Report ecosystem registry drift")
    parser.add_argument("--format", choices=["text", "json"], default="text", help="Output format")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 when any error-class drift is present (default: always exit 0)",
    )
    parser.add_argument(
        "--max-age",
        type=int,
        default=DEFAULT_MAX_AGE_DAYS,
        help=f"Days before last_verified is itself reported (default: {DEFAULT_MAX_AGE_DAYS})",
    )
    parser.add_argument("--only", help="Poll a single project id")
    args = parser.parse_args(argv)

    registry = load_registry()
    projects = registry.projects
    if args.only:
        projects = [p for p in projects if p.project_id == args.only]
        if not projects:
            print(f"ERROR: unknown project id {args.only!r}", file=sys.stderr)
            return 2

    today = datetime.now(timezone.utc).date()
    findings: list[Finding] = []
    for project in projects:
        findings.extend(compare(project, probe(project), today, args.max_age))

    by_severity = {
        severity: [f for f in findings if f.severity == severity]
        for severity in ("error", "warn", "info")
    }

    if args.format == "json":
        json.dump(
            {
                "checked": len(projects),
                "counts": {sev: len(items) for sev, items in by_severity.items()},
                "findings": [f.to_dict() for f in findings],
            },
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
    else:
        _print_text_report(projects, by_severity, findings)

    if args.strict and by_severity["error"]:
        return 1
    return 0


def _print_text_report(
    projects: list[EcosystemProject],
    by_severity: dict[str, list[Finding]],
    findings: list[Finding],
) -> None:
    """Print the human-readable drift report."""
    print(f"Ecosystem drift report: {len(projects)} project(s) checked")
    print(
        f"  errors: {len(by_severity['error'])}  "
        f"warnings: {len(by_severity['warn'])}  "
        f"info: {len(by_severity['info'])}"
    )

    if not findings:
        print("\nNo drift. Registry matches upstream.")
        return

    for severity in ("error", "warn", "info"):
        items = by_severity[severity]
        if not items:
            continue
        print(f"\n{severity.upper()}")
        for finding in items:
            print(f"  [{finding.drift_class}] {finding.project_id}: {finding.detail}")

    suggestions = [f for f in findings if f.suggested_toml]
    if suggestions:
        print("\nSuggested registry edits (apply by hand -- this script never writes):")
        for finding in suggestions:
            print(f"\n  [projects.{finding.project_id}]")
            for line in finding.suggested_toml:
                print(f"  {line}")

    print(
        "\nThis report is advisory. A human ratifies every fact in "
        "src/kicad_tools/ecosystem/data/projects.toml."
    )


if __name__ == "__main__":
    sys.exit(main())
