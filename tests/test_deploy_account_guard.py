"""Scoped Pages credentials must pass identity and project-access checks."""

import hashlib
import os
import shlex
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/deploy-site.sh"


@pytest.mark.parametrize(
    "account,projects,expected",
    [
        ("a" * 32, "kicad-tools", 0),
        ("", "kicad-tools", 0),
        ("", "other-project", 0),
        ("a" * 32, "│ kicad-tools │ kicad-tools.pages.dev │", 0),
        ("b" * 32, "kicad-tools", 1),
        ("a" * 32, None, 1),
        ("a" * 32, "", 1),
        ("a" * 32, "other-project", 1),
        ("a" * 32, "kicad-tools-preview", 1),
        ("a" * 32, "old-kicad-tools", 1),
        ("a" * 32, "kicad-tools.pages.dev", 1),
    ],
)
def test_scoped_pages_account_guard(tmp_path, account, projects, expected):
    source = SCRIPT.read_text()
    guard = source[source.index("# --- Logging helpers") : source.index("# --- Parse flags")]
    # Scoped tokens cannot use whoami; OAuth falls back to it without an explicit account.
    mock = tmp_path / "wrangler"
    mock.write_text(
        "#!/bin/bash\n"
        + (
            'if [ "$1" = whoami ]; then exit 99; fi\n'
            if account
            else 'if [ "$1" = whoami ]; then echo ' + "a" * 32 + "; exit 0; fi\n"
        )
        + ("printf '%s\\n' " + shlex.quote(projects) + "\n" if projects is not None else "exit 1\n")
    )
    mock.chmod(0o700)
    env = {
        **os.environ,
        "CLOUDFLARE_ACCOUNT_ID": account,
        "EXPECTED_CLOUDFLARE_ACCOUNT_ID_SHA256": hashlib.sha256(("a" * 32).encode()).hexdigest(),
    }
    env.pop("EXPECTED_CLOUDFLARE_ACCOUNT_ID", None)
    result = subprocess.run(
        ["bash", "-c", guard + '\nassert_cloudflare_account "$1"', "guard-test", str(mock)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == expected, result.stdout + result.stderr
