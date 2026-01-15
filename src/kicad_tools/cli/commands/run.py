"""Run Python scripts with the kicad-tools interpreter.

This command solves the problem of running board generation scripts when
kicad-tools is installed via pipx. Instead of:

    python generate_design.py  # Fails: ModuleNotFoundError: kicad_tools

Users can run:

    kct run generate_design.py  # Works: uses pipx-installed Python
"""

import subprocess
import sys
from pathlib import Path

__all__ = ["run_run_command"]


def run_run_command(args) -> int:
    """Execute a Python script using the kicad-tools Python interpreter.

    This allows board generation scripts to import kicad_tools modules
    even when kicad-tools is installed in an isolated environment (pipx).
    """
    script_path = Path(args.run_script)

    if not script_path.exists():
        print(f"Error: Script not found: {script_path}", file=sys.stderr)
        return 1

    if script_path.suffix != ".py":
        print(f"Error: Not a Python script: {script_path}", file=sys.stderr)
        return 1

    # Build command: use current Python interpreter
    cmd = [sys.executable, str(script_path.resolve())]

    # Add any additional arguments passed to the script
    if args.run_args:
        cmd.extend(args.run_args)

    # Run the script, inheriting stdio
    result = subprocess.run(cmd)

    return result.returncode
