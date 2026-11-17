#!/usr/bin/env python3
"""Warn-only drift check: does the live kicad-tools.org match this repo? (Issue #7664)

The site is published by hand (site/README.md "Publish and verify public
bytes"), so nothing notices when it goes stale. This compares the public site
against the checkout it runs in, using only public URLs (no Cloudflare
credentials):

* footer ``Project version`` / ``Site commit`` vs. the latest ``v*`` tag / HEAD;
* per board, the gallery chip and the "Checked" evidence date vs.
  ``boards/<id>/output/readiness.json``;
* SHA-256 of the served ``board.kicad_pcb`` / ``manufacturing.zip`` vs. the
  committed sources, via ``verify_deployed_assets`` in
  ``scripts/lib/site-verify.sh`` (not re-implemented here).

It ALWAYS exits 0: drift is reported as ``::warning::`` lines plus a markdown
summary on stdout. Fetch failures and unparseable markup are findings, never
exceptions.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASE_URL = "https://kicad-tools.org"

_FOOTER_COMMIT = re.compile(r"Site commit:.*?<code[^>]*>([0-9a-f]+|unknown)</code>", re.S)
_FOOTER_VERSION = re.compile(r"Project version:\s*<code[^>]*>v?([^<\s]+)</code>", re.S)
_FOOTER_BUILT = re.compile(r"Site built\s*<time[^>]*datetime=\"([^\"]+)\"")
_CARD = re.compile(r"<a class=\"card\" href=\"/([^\"/]+)\" data-status=\"([a-z_]+)\"")
_CHECKED = re.compile(r"Checked\s*<time[^>]*datetime=\"([^\"]+)\"")

# Live chip variants that are consistent with a committed readiness status.
_ACCEPTED = {
    "ready": {"ready", "partial", "drc", "lvs", "no_artifacts"},
    "blocked": {"blocked", "development"},
}


def parse_footer(html: str) -> dict[str, str]:
    """Extract commit / version / built from the footer; missing keys are omitted."""
    found = {}
    for key, rx in (
        ("commit", _FOOTER_COMMIT),
        ("version", _FOOTER_VERSION),
        ("built", _FOOTER_BUILT),
    ):
        m = rx.search(html)
        if m:
            found[key] = m.group(1)
    return found


def parse_cards(html: str) -> dict[str, str]:
    """Map board slug -> live ``data-status`` chip variant from the gallery."""
    return dict(_CARD.findall(html))


def parse_checked(html: str) -> str | None:
    m = _CHECKED.search(html)
    return m.group(1) if m else None


def compare_footer(footer: dict[str, str], tag: str | None, head: str | None) -> list[str]:
    if not footer.get("version") or not footer.get("commit"):
        return ["footer unparseable: no 'Site commit' / 'Project version' found (markup changed?)"]
    out = []
    if tag and footer["version"] != tag.lstrip("v"):
        out.append(f"live Project version v{footer['version']} != latest release tag {tag}")
    if head and footer["commit"] != "unknown" and not head.startswith(footer["commit"]):
        out.append(f"live Site commit {footer['commit']} != checked-out commit {head[:8]}")
    if footer["commit"] == "unknown":
        out.append("live Site commit is 'unknown'")
    return out


def _same_instant(a: str, b: str) -> bool:
    """True when two ISO timestamps are the same instant ('Z' vs '+00:00' is not drift)."""
    try:
        from datetime import datetime

        return datetime.fromisoformat(a.replace("Z", "+00:00")) == datetime.fromisoformat(
            b.replace("Z", "+00:00")
        )
    except ValueError:
        return a == b


def compare_board(
    slug: str, readiness: dict | None, live_status: str | None, live_checked: str | None
) -> list[str]:
    if readiness is None:
        return []
    out = []
    want = readiness.get("status")
    if live_status is None:
        out.append(f"{slug}: not listed in the live gallery")
    elif want in _ACCEPTED and live_status not in _ACCEPTED[want]:
        out.append(
            f"{slug}: live readiness '{live_status}' but committed readiness.json says '{want}'"
        )
    want_date = readiness.get("checked_at")
    if live_checked is None:
        out.append(f"{slug}: live page shows no evidence date (committed checked_at {want_date})")
    elif want_date and not _same_instant(live_checked, want_date):
        out.append(f"{slug}: live evidence date {live_checked} != committed checked_at {want_date}")
    return out


def fetch(url: str, timeout: int = 30) -> tuple[str | None, str | None]:
    """Return (body, error). Never raises."""
    try:
        # Cloudflare 403s urllib's default User-Agent.
        req = urllib.request.Request(url, headers={"User-Agent": "kicad-tools-site-drift/1"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - public URL
            return resp.read().decode("utf-8", "replace"), None
    except Exception as exc:  # noqa: BLE001 - every failure is a finding
        return None, f"{url}: {exc}"


def stage_assets(boards_dir: Path, dest: Path) -> int:
    """Stage the PCB + manufacturing.zip exactly as site/scripts/copy-renders.mjs names them."""
    n = 0
    for bdir in sorted(p for p in boards_dir.iterdir() if (p / "output").is_dir()):
        out = bdir / "output"
        target = dest / bdir.name
        pcbs = sorted(f.name for f in out.glob("*.kicad_pcb"))
        chosen = next((f for f in pcbs if "_routed" in f), pcbs[0] if pcbs else None)
        for src, name in (
            (out / chosen if chosen else None, "board.kicad_pcb"),
            (out / "manufacturing.zip", "manufacturing.zip"),
        ):
            if src is not None and src.is_file():
                target.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, target / name)
                n += 1
    return n


def check_hashes(base_url: str, boards_dir: Path) -> list[str]:
    """Reuse verify_deployed_assets; its non-zero return is swallowed, MISMATCH lines are findings."""
    with tempfile.TemporaryDirectory() as tmp:
        if not stage_assets(boards_dir, Path(tmp)):
            return []
        script = (
            f'source "{REPO_ROOT}/scripts/lib/site-verify.sh"; verify_deployed_assets "$1" "$2"'
        )
        try:
            proc = subprocess.run(  # noqa: S603
                ["bash", "-c", script, "site-drift", base_url, tmp],
                capture_output=True,
                text=True,
                timeout=900,
                env={**os.environ, "VERIFY_RETRY_ATTEMPTS": "1", "VERIFY_RETRY_SLEEP_SECONDS": "0"},
            )
        except Exception as exc:  # noqa: BLE001
            return [f"hash check could not run: {exc}"]
        return [
            line.replace("[site-verify] ", "").strip()
            for line in proc.stderr.splitlines()
            if "MISMATCH" in line
        ]


def _git(*args: str) -> str | None:
    try:
        r = subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
        )
        return r.stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


def run(base_url: str, boards_dir: Path) -> list[str]:
    findings: list[str] = []
    home, err = fetch(base_url + "/")
    if home is None:
        return [f"site unreachable: {err}"]
    findings += compare_footer(
        parse_footer(home),
        _git("describe", "--tags", "--abbrev=0", "--match", "v*"),
        _git("rev-parse", "HEAD"),
    )
    cards = parse_cards(home)
    if not cards:
        findings.append("gallery unparseable: no board cards found (markup changed?)")
    for bdir in sorted(p for p in boards_dir.iterdir() if p.is_dir()):
        rpath = bdir / "output" / "readiness.json"
        if not rpath.is_file():
            continue
        try:
            readiness = json.loads(rpath.read_text())
        except ValueError as exc:
            findings.append(f"{bdir.name}: committed readiness.json unreadable: {exc}")
            continue
        page, perr = fetch(f"{base_url}/{bdir.name}")
        if page is None:
            findings.append(f"{bdir.name}: board page unreachable ({perr})")
            continue
        findings += compare_board(bdir.name, readiness, cards.get(bdir.name), parse_checked(page))
    findings += check_hashes(base_url, boards_dir)
    return findings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--boards-dir", type=Path, default=REPO_ROOT / "boards")
    args = ap.parse_args(argv)
    findings = run(args.base_url.rstrip("/"), args.boards_dir)
    print("## Site drift (kicad-tools.org vs. this checkout)\n")
    if not findings:
        print(
            f"No drift: {args.base_url} matches the latest tag, readiness evidence and committed bytes."
        )
    for f in findings:
        print(f"- {f}")
        print(f"::warning title=site drift::{f}", file=sys.stderr)
    return 0  # warn-only by design (#5318, #7664)


if __name__ == "__main__":
    sys.exit(main())
