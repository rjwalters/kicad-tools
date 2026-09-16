"""Default-off CI entry point; forward the original workload argv verbatim."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def hex_identity(value):
    return value if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value or "") else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("group")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or not re.fullmatch(r"[a-z0-9-]+", args.group):
        parser.error("safe group name and workload command required")
    if os.environ.get("KCT_NATIVE_DIAGNOSTICS") != "true":
        os.execvp(command[0], command)
    root = Path(os.environ["RUNNER_TEMP"]) / "native-observer"
    root.mkdir(parents=True, exist_ok=True)
    checkout = Path.cwd().resolve()
    workspace = os.environ.get("GITHUB_WORKSPACE")
    if workspace and Path(workspace).resolve() != checkout:
        raise RuntimeError("Observer must run from the trusted checkout root")
    # checkout runs with a temporary HOME, while container Python may have
    # another UID/config context. Trust only this invocation's exact checkout;
    # never alter global/system config or use a wildcard safe.directory.
    source = subprocess.check_output(
        [
            "git",
            "-c",
            f"safe.directory={checkout}",
            "-C",
            str(checkout),
            "rev-parse",
            "--verify",
            "HEAD",
        ],
        text=True,
    ).strip()
    identity = {
        "group": args.group,
        "source_sha": hex_identity(source),
        "pr_head_sha": hex_identity(os.environ.get("KCT_OBSERVER_PR_HEAD")),
        "container_id": hex_identity(os.environ.get("KCT_OBSERVER_CONTAINER_ID")),
        "configured_image": "kicad/kicad:10.0",
        "image_digest": None,
        "image_digest_provenance": "Bind Initialize-container pull log to this run after execution; not available inside container",
        "run_id": os.environ.get("GITHUB_RUN_ID")
        if os.environ.get("GITHUB_RUN_ID", "").isdigit()
        else None,
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT")
        if os.environ.get("GITHUB_RUN_ATTEMPT", "").isdigit()
        else None,
        # Exact argv identity without retaining arbitrary argument contents.
        # The frozen workflow contains the invocation; resolve its runner-temp
        # substitutions before comparing this digest.
        "argv_sha256": hashlib.sha256(
            json.dumps(command, separators=(",", ":")).encode()
        ).hexdigest(),
        "workflow_sha256": hashlib.sha256(
            Path(".github/workflows/ci.yml").read_bytes()
        ).hexdigest(),
    }
    with (root / f"{args.group}-identity.json").open("x") as stream:
        json.dump(identity, stream, indent=2)
        stream.write("\n")
    observer = Path(__file__).with_name("native_observer.py")
    os.execv(
        sys.executable,
        [sys.executable, str(observer), "--output", str(root / args.group), "--", *command],
    )


if __name__ == "__main__":
    main()
