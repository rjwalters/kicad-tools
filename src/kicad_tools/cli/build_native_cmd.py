"""
Build native C++ router backend command.

Provides a simple way to build and install the C++ router extension
for 10-100x faster routing performance.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["main", "BuildResult"]


@dataclass
class BuildResult:
    """Result of a build operation."""

    success: bool
    backend_installed: bool = False
    so_path: Path | None = None
    error_message: str | None = None
    steps_completed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: bool = False

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON output."""
        return {
            "success": self.success,
            "backend_installed": self.backend_installed,
            "so_path": str(self.so_path) if self.so_path else None,
            "error_message": self.error_message,
            "steps_completed": self.steps_completed,
            "warnings": self.warnings,
            "skipped": self.skipped,
        }


def _check_cmake() -> tuple[bool, str | None]:
    """Check if cmake is available."""
    cmake_path = shutil.which("cmake")
    if not cmake_path:
        return (
            False,
            "cmake not found. Install with: brew install cmake (macOS) or apt install cmake (Linux)",
        )
    return True, cmake_path


def _check_compiler() -> tuple[bool, str | None]:
    """Check if a C++20 compiler is available."""
    # Check for clang++ or g++
    for compiler in ["clang++", "g++"]:
        path = shutil.which(compiler)
        if path:
            # Verify compiler can run
            try:
                subprocess.run(
                    [compiler, "--version"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=True,
                )
                return True, path
            except (subprocess.TimeoutExpired, FileNotFoundError, subprocess.CalledProcessError):
                continue
    return (
        False,
        "C++20 compiler not found. Install Xcode Command Line Tools (macOS) or build-essential (Linux)",
    )


def _get_package_root() -> Path:
    """Get the root directory of the installed package."""
    # This file is at src/kicad_tools/cli/build_native_cmd.py
    # Package root is src/kicad_tools/
    return Path(__file__).parent.parent


def _get_cpp_source_dir() -> Path | None:
    """Get the C++ source directory."""
    package_root = _get_package_root()
    cpp_dir = package_root / "router" / "cpp"
    if cpp_dir.exists():
        return cpp_dir
    return None


def _find_installed_so(router_dir: Path) -> Path | None:
    """Return the installed router_cpp extension (.so/.pyd), if any."""
    for pattern in ("router_cpp.*.so", "router_cpp.*.pyd"):
        for so_file in router_dir.glob(pattern):
            return so_file
    return None


def _newest_cpp_source_mtime(cpp_dir: Path) -> float | None:
    """Return the newest mtime among C++ source/header files under ``cpp_dir``.

    Scans recursively for ``*.cpp`` and ``*.hpp`` (plus the common ``*.cc``,
    ``*.h``, ``*.hxx`` variants and ``CMakeLists.txt``) so that any edit to the
    build inputs is detected.  Returns ``None`` when no source files are found.
    """
    newest: float | None = None
    patterns = ("*.cpp", "*.cc", "*.cxx", "*.hpp", "*.hxx", "*.h", "CMakeLists.txt")
    for pattern in patterns:
        for source in cpp_dir.rglob(pattern):
            try:
                mtime = source.stat().st_mtime
            except OSError:
                continue
            if newest is None or mtime > newest:
                newest = mtime
    return newest


def _is_so_stale(router_dir: Path) -> bool:
    """Decide whether the installed .so is older than its C++ sources.

    Returns ``True`` (rebuild needed) when the newest C++ source mtime is
    strictly greater than the installed extension's mtime.  Returns ``False``
    when the .so is up to date, when there is no .so to compare against, or
    when the source tree cannot be located (e.g. a pip-installed wheel without
    bundled sources) -- in those cases we defer to the existing
    version-matching guard rather than forcing a rebuild we cannot perform.
    """
    so_file = _find_installed_so(router_dir)
    if so_file is None:
        return False

    cpp_dir = _get_cpp_source_dir()
    if cpp_dir is None:
        return False

    newest_source = _newest_cpp_source_mtime(cpp_dir)
    if newest_source is None:
        return False

    try:
        so_mtime = so_file.stat().st_mtime
    except OSError:
        return False

    return newest_source > so_mtime


def _get_project_root() -> Path | None:
    """Get the project root directory (where CMakeLists.txt is)."""
    # Walk up from package root to find CMakeLists.txt
    current = _get_package_root()
    for _ in range(5):  # Limit search depth
        cmake_file = current / "CMakeLists.txt"
        if cmake_file.exists():
            return current
        current = current.parent
    return None


def _install_nanobind(verbose: bool = False) -> tuple[bool, str | None]:
    """Ensure nanobind is installed."""
    try:
        import nanobind

        return True, None
    except ImportError:
        pass

    # Try to install nanobind
    if verbose:
        print("  Installing nanobind...")

    # Try different installation methods
    install_commands = [
        # Try uv pip first (for uv-managed environments)
        ["uv", "pip", "install", "nanobind>=2.0"],
        # Standard pip
        [sys.executable, "-m", "pip", "install", "nanobind>=2.0"],
        # Fallback to pip directly
        ["pip", "install", "nanobind>=2.0"],
        # Try pip3
        ["pip3", "install", "nanobind>=2.0"],
    ]

    last_error = None
    for cmd in install_commands:
        try:
            # Check if command exists
            if cmd[0] not in ["uv", "pip", "pip3"] or shutil.which(cmd[0]):
                result = subprocess.run(
                    cmd,
                    capture_output=not verbose,
                    text=True,
                    timeout=120,
                )
                if result.returncode == 0:
                    # Verify installation
                    try:
                        import nanobind  # noqa: F401

                        return True, None
                    except ImportError:
                        continue  # Try next method
                last_error = result.stderr if result.stderr else "Unknown error"
        except subprocess.TimeoutExpired:
            last_error = "Timeout"
        except FileNotFoundError:
            continue  # Command not found, try next
        except Exception as e:
            last_error = str(e)

    return False, f"Failed to install nanobind. Last error: {last_error}"


def _get_nanobind_cmake_dir() -> Path | None:
    """Get the nanobind cmake directory."""
    try:
        import nanobind

        return Path(nanobind.cmake_dir())
    except (ImportError, AttributeError):
        return None


def build_native(
    verbose: bool = False,
    force: bool = False,
    jobs: int | None = None,
) -> BuildResult:
    """
    Build the C++ router backend.

    Args:
        verbose: Show detailed build output
        force: Force rebuild even if already installed
        jobs: Number of parallel jobs (default: auto)

    Returns:
        BuildResult with success status and details
    """
    result = BuildResult(success=False)
    router_dir = _get_package_root() / "router"

    # Check if already installed (unless force).
    #
    # Issue #3621: a matching-version .so short-circuits the build, but the
    # version guard offers no protection during development when the C++
    # source changes WITHOUT a version bump -- the most common iteration loop.
    # Compare the newest source mtime against the installed extension and fall
    # through to a real rebuild when the source is newer.  Only short-circuit
    # (and clearly report SKIPPED) when the .so is genuinely up to date.
    if not force:
        try:
            from kicad_tools.router.cpp_backend import is_cpp_available

            if is_cpp_available():
                if _is_so_stale(router_dir):
                    if verbose:
                        print("C++ source is newer than the installed extension -- rebuilding.")
                    result.steps_completed.append(
                        "C++ source newer than installed .so -- rebuilding"
                    )
                else:
                    result.success = True
                    result.backend_installed = True
                    result.skipped = True
                    result.steps_completed.append(
                        "C++ backend already installed (up to date) -- skipped rebuild"
                    )
                    # Find the .so file
                    result.so_path = _find_installed_so(router_dir)
                    return result
        except ImportError:
            pass

    # Step 1: Check prerequisites
    if verbose:
        print("Checking prerequisites...")

    cmake_ok, cmake_msg = _check_cmake()
    if not cmake_ok:
        result.error_message = cmake_msg
        return result
    result.steps_completed.append(f"cmake found: {cmake_msg}")
    if verbose:
        print(f"  cmake: {cmake_msg}")

    compiler_ok, compiler_msg = _check_compiler()
    if not compiler_ok:
        result.error_message = compiler_msg
        return result
    result.steps_completed.append(f"C++ compiler found: {compiler_msg}")
    if verbose:
        print(f"  C++ compiler: {compiler_msg}")

    # Step 2: Ensure nanobind is installed
    if verbose:
        print("Checking nanobind...")
    nanobind_ok, nanobind_err = _install_nanobind(verbose)
    if not nanobind_ok:
        result.error_message = nanobind_err
        return result
    result.steps_completed.append("nanobind available")

    nanobind_cmake = _get_nanobind_cmake_dir()
    if not nanobind_cmake:
        result.error_message = "Could not find nanobind cmake directory"
        return result
    if verbose:
        print(f"  nanobind cmake: {nanobind_cmake}")

    # Step 3: Find source directory
    # Try to find root CMakeLists.txt first (development checkout)
    project_root = _get_project_root()
    source_dir: Path

    if project_root:
        # Development checkout - use root CMakeLists.txt
        source_dir = project_root
    else:
        # Pip-installed package - use cpp directory's CMakeLists.txt directly
        cpp_dir = _get_cpp_source_dir()
        if not cpp_dir:
            result.error_message = (
                "C++ source not found. The package may have been installed without source files. "
                "Try reinstalling from source: pip install -e .[native]"
            )
            return result
        source_dir = cpp_dir

    cmake_file = source_dir / "CMakeLists.txt"
    if not cmake_file.exists():
        result.error_message = f"CMakeLists.txt not found in {source_dir}"
        return result
    result.steps_completed.append(f"Source found: {source_dir}")
    if verbose:
        print(f"  Source directory: {source_dir}")

    # Step 4: Configure with cmake
    if verbose:
        print("Configuring...")

    build_dir = Path(tempfile.mkdtemp(prefix="kicad_tools_build_"))
    try:
        cmake_args = [
            "cmake",
            "-B",
            str(build_dir),
            "-S",
            str(source_dir),
            f"-DPython_EXECUTABLE={sys.executable}",
            f"-Dnanobind_DIR={nanobind_cmake}",
            "-DCMAKE_BUILD_TYPE=Release",
        ]

        configure_result = subprocess.run(
            cmake_args,
            capture_output=not verbose,
            text=True,
            timeout=120,
            cwd=str(source_dir),
        )
        if configure_result.returncode != 0:
            error = configure_result.stderr if not verbose else "See output above"
            result.error_message = f"cmake configure failed: {error}"
            return result
        result.steps_completed.append("cmake configure")
        if verbose:
            print("  Configure: OK")

        # Step 5: Build
        if verbose:
            print("Building... (this may take 1-2 minutes)")

        build_args = ["cmake", "--build", str(build_dir), "--config", "Release"]
        if jobs:
            build_args.extend(["-j", str(jobs)])
        else:
            build_args.extend(["-j"])  # Auto-detect

        build_result = subprocess.run(
            build_args,
            capture_output=not verbose,
            text=True,
            timeout=600,  # 10 minute timeout
        )
        if build_result.returncode != 0:
            error = build_result.stderr if not verbose else "See output above"
            result.error_message = f"Build failed: {error}"
            return result
        result.steps_completed.append("cmake build")
        if verbose:
            print("  Build: OK")

        # Step 6: Find and copy the .so file
        so_files = list(build_dir.glob("**/router_cpp.*.so"))
        if not so_files:
            # Try .pyd for Windows
            so_files = list(build_dir.glob("**/router_cpp.*.pyd"))
        if not so_files:
            result.error_message = "Build succeeded but router_cpp extension not found"
            return result

        so_file = so_files[0]
        target_path = router_dir / so_file.name

        if verbose:
            print(f"Installing to {target_path}...")

        shutil.copy2(so_file, target_path)
        result.so_path = target_path
        result.steps_completed.append(f"Installed: {target_path}")

        # Verify the installation
        try:
            # Clear any cached imports
            #
            # Issue #2594: ``importlib.reload(cpp_module)`` alone is not
            # enough when the original ``from . import router_cpp`` failed
            # at startup (fresh checkout with no .so on disk).  Python
            # caches the negative result on the parent package's
            # ``FileFinder`` and may leave a partial / ``None`` entry in
            # ``sys.modules`` for ``kicad_tools.router.router_cpp``.  We
            # must drop that stale entry and invalidate the path-based
            # caches so the reloaded ``cpp_backend`` actually picks up
            # the freshly-written ``.so``.  ``sys`` is already imported
            # at module scope -- do NOT shadow it with a local import
            # here (would trigger ``UnboundLocalError`` on the earlier
            # ``sys.executable`` reference at the cmake configure step).
            import importlib

            sys.modules.pop("kicad_tools.router.router_cpp", None)
            importlib.invalidate_caches()

            import kicad_tools.router.cpp_backend as cpp_module

            importlib.reload(cpp_module)
            if cpp_module.is_cpp_available():
                result.backend_installed = True
                result.success = True
                if verbose:
                    print("  Verification: OK")
            else:
                result.warnings.append("Extension installed but not loading correctly")
                result.success = True  # Build succeeded, just verification failed
        except Exception as e:
            result.warnings.append(f"Could not verify installation: {e}")
            result.success = True  # Build succeeded

    finally:
        # Clean up build directory
        with contextlib.suppress(OSError):
            shutil.rmtree(build_dir)

    return result


def format_result_text(result: BuildResult) -> str:
    """Format build result as text."""
    lines = []

    if result.success:
        if result.skipped:
            lines.append(
                "C++ backend already installed -- SKIPPED rebuild (use --force to recompile)"
            )
            lines.append("")
            if result.so_path:
                lines.append(f"  Extension: {result.so_path.name}")
            lines.append("")
            lines.append("Run `kct route --backend cpp` to use the C++ backend.")
        elif result.backend_installed:
            lines.append("C++ backend installed successfully!")
            lines.append("")
            if result.so_path:
                lines.append(f"  Extension: {result.so_path.name}")
            lines.append("")
            lines.append("Run `kct route --backend cpp` to use the C++ backend.")
        else:
            lines.append("Build completed with warnings.")
            lines.append("")
            for warning in result.warnings:
                lines.append(f"  Warning: {warning}")
    else:
        lines.append("Build failed.")
        lines.append("")
        if result.error_message:
            lines.append(f"Error: {result.error_message}")
        lines.append("")
        lines.append("Steps completed:")
        for step in result.steps_completed:
            lines.append(f"  - {step}")

    return "\n".join(lines)


def create_parser() -> argparse.ArgumentParser:
    """Create argument parser for build-native command."""
    parser = argparse.ArgumentParser(
        prog="kct build-native",
        description="Build C++ router backend for 10-100x faster routing",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show detailed build output",
    )
    parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Force rebuild even if already installed",
    )
    parser.add_argument(
        "--jobs",
        "-j",
        type=int,
        default=None,
        help="Number of parallel build jobs (default: auto)",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Just check if C++ backend is available, don't build",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Main entry point for build-native command."""
    parser = create_parser()
    args = parser.parse_args(argv)

    # Check mode - just report status
    if args.check:
        try:
            from kicad_tools.router.cpp_backend import get_backend_info, is_cpp_available

            info = get_backend_info()
            if args.format == "json":
                print(json.dumps(info, indent=2))
            else:
                if is_cpp_available():
                    print(f"C++ backend: available (version {info['version']})")
                else:
                    print("C++ backend: not installed")
                    print("Run `kct build-native` to install.")
            return 0 if is_cpp_available() else 1
        except ImportError:
            if args.format == "json":
                print(json.dumps({"available": False, "error": "Module not found"}))
            else:
                print("C++ backend: not installed")
                print("Run `kct build-native` to install.")
            return 1

    # Build mode
    if args.verbose or args.format == "text":
        print("Building C++ router backend...")
        print("")

    result = build_native(
        verbose=args.verbose,
        force=args.force,
        jobs=args.jobs,
    )

    if args.format == "json":
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(format_result_text(result))

    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
