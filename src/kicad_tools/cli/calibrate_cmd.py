"""
Performance calibration CLI command.

Provides command-line access to calibrate routing performance settings:

    kicad-tools calibrate               # Run CPU calibration and save
    kicad-tools calibrate --show        # Show current settings
    kicad-tools calibrate --quick       # Quick calibration
    kicad-tools calibrate --benchmark   # Full benchmark with details
    kicad-tools calibrate --gpu         # Run GPU benchmarks
    kicad-tools calibrate --all         # Run full calibration including GPU
    kicad-tools calibrate --show-gpu    # Show GPU capabilities
"""

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    """Main entry point for calibrate command.

    Args:
        argv: Command line arguments (defaults to sys.argv[1:]).

    Returns:
        Exit code (0 for success).
    """
    parser = argparse.ArgumentParser(
        prog="kicad-tools calibrate",
        description="Calibrate routing performance settings for your machine",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Show current performance configuration without running calibration",
    )
    parser.add_argument(
        "--show-gpu",
        action="store_true",
        help="Show GPU capabilities and current configuration",
    )
    parser.add_argument(
        "--gpu",
        action="store_true",
        help="Run GPU-specific benchmarks and determine optimal thresholds",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run full calibration including GPU benchmarks",
    )
    parser.add_argument(
        "--benchmark",
        action="store_true",
        help="Run full benchmarks with detailed output",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Run abbreviated calibration (faster but less accurate)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        help="Output path for configuration file",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output configuration as JSON",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show detailed progress information",
    )

    args = parser.parse_args(argv)

    # Handle --show-gpu: display GPU capabilities and config
    if args.show_gpu:
        from kicad_tools.calibration import show_gpu_config

        show_gpu_config(verbose=args.verbose)
        return 0

    # Handle --gpu: run GPU-specific benchmarks
    if args.gpu:
        from pathlib import Path

        from kicad_tools.calibration import run_gpu_calibration

        output_path = Path(args.output) if args.output else None
        verbose = args.verbose or args.benchmark

        config = run_gpu_calibration(
            output_path=output_path,
            verbose=verbose,
        )

        if args.json:
            import json

            print()
            print(json.dumps(config.to_dict(), indent=2))

        return 0

    # Handle --show: just display current config
    if args.show:
        from kicad_tools.calibration import show_current_config

        config = show_current_config(verbose=args.verbose)

        if args.json:
            import json

            print()
            print(json.dumps(config.to_dict(), indent=2))

        return 0

    # Run calibration
    from pathlib import Path

    from kicad_tools.calibration import calibrate_and_save

    output_path = Path(args.output) if args.output else None
    verbose = args.verbose or args.benchmark

    print("kicad-tools Performance Calibration")
    print("=" * 40)
    print()

    config = calibrate_and_save(
        output_path=output_path,
        verbose=verbose,
        quick=args.quick,
        include_gpu=args.all,
    )

    if args.json:
        import json

        print()
        print(json.dumps(config.to_dict(), indent=2))
    else:
        print()
        print("Calibration complete! Use --show to view saved settings.")
        print()
        print("To use high-performance mode when routing:")
        print("  kicad-tools route board.kicad_pcb --high-performance")

    return 0


if __name__ == "__main__":
    sys.exit(main())
