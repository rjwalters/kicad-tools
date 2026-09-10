"""Scoped Pages credentials must pass identity and project-access checks."""

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/deploy-site.sh"


@pytest.mark.parametrize(
    "account,accessible,expected",
    [
        ("a" * 32, True, 0),
        ("b" * 32, True, 1),
        ("a" * 32, False, 1),
    ],
)
def test_scoped_pages_account_guard(tmp_path, account, accessible, expected):
    source = SCRIPT.read_text()
    guard = source[source.index("# --- Logging helpers") : source.index("# --- Parse flags")]
    # whoami intentionally cannot return account information with this token.
    mock = tmp_path / "wrangler"
    mock.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = whoami ]; then exit 99; fi\n'
        + ("echo kicad-tools\n" if accessible else "exit 1\n")
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
