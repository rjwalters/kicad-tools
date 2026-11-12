"""``kct submit-jlcpcb``: agent-prepared JLCPCB PCBA submission (issues #8465, #8466).

Composes the existing submission plan / stock / upload / review pieces via
:mod:`kicad_tools.export.submission_workflow`. It never places an order.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..export.submission_plan import SubmissionError
from ..export.submission_workflow import (
    WorkflowError,
    format_stock_failures,
    run_submission,
)
from .format_options import add_format_flag


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("bundle", help="Tapeout bundle directory (contains manifest.json)")
    parser.add_argument("--board", required=True, help="Board .kicad_pcb the bundle was cut from")
    parser.add_argument("--schematic", required=True, help="Schematic .kicad_sch (under board dir)")
    parser.add_argument("--quantity", type=int, default=1, help="Boards to order (default: 1)")
    parser.add_argument("--output", "-o", required=True, help="New directory for plan and review")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preflight, prepare/verify, stock gate and review page only; no upload",
    )
    parser.add_argument(
        "--inventory-covered",
        metavar="C123,C456",
        default="",
        help="Catalog ids you explicitly hold in your JLC parts inventory (shown as covered)",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="Upload the exact Gerber bytes via the API (needs JLC credentials and --reviewer)",
    )
    parser.add_argument("--reviewer", help="Your name; confirms the upload checklist")
    site = parser.add_argument_group(
        "website leg (attended only; stops at the saved cart, never orders)"
    )
    site.add_argument(
        "--website",
        action="store_true",
        help="Drive your own signed-in Chrome (CDP) to a saved cart; needs a TTY and the fields below",
    )
    site.add_argument(
        "--guided",
        action="store_true",
        help="Print the manual website checklist instead (fallback / kill switch)",
    )
    site.add_argument("--cdp-port", type=int, default=9222, help="Chrome debugging port")
    site.add_argument(
        "--lock-dir",
        help="Directory for the one-driver-per-browser lock (default: ~/.kct/locks)",
    )
    site.add_argument("--thickness-mm", help="Board thickness, e.g. 1.6 (never guessed)")
    site.add_argument("--outer-copper-oz", help="Outer copper weight, e.g. 1")
    site.add_argument("--inner-copper-oz", help="Inner copper weight (4+ layer boards)")
    site.add_argument("--has-tht", action="store_true", help="BOM has THT lines (Standard PCBA)")
    site.add_argument(
        "--match-mapping",
        help="JSON file giving the column schema (SelectionMapping fields) of JLC's part-matching CSV; supplied up front, while the CSV itself is requested mid-run",
    )
    add_format_flag(parser)


def _website_leg(args: argparse.Namespace, result, out: Path) -> int:
    import json as _json

    from ..export.factory_selection import ExpectedPart, SelectionMapping
    from ..export.submission_review import render_review_page
    from ..manufacturers import jlc_site_driver as drv

    plan = result.plan
    core = _json.loads(plan.plan_bytes)
    sides = core["settings"]["assembly_sides"]
    side = "both" if len(sides) > 1 else sides[0]
    spec = drv.SiteOrderSpec(
        layers=core["settings"]["board_layers"],
        quantity=core["board_quantity"],
        thickness_mm=args.thickness_mm or "",
        outer_copper_oz=args.outer_copper_oz or "",
        inner_copper_oz=args.inner_copper_oz,
        assembly_side=side,
        has_tht=bool(args.has_tht),
    )
    names = {"gerber": "gerbers.zip", "bom": "bom.csv", "cpl": "cpl.csv"}
    inputs = drv.SiteInputs(
        gerber=plan.directory / names["gerber"],
        bom=plan.directory / names["bom"],
        cpl=plan.directory / names["cpl"],
        sha256={r: core["outputs"][n]["sha256"] for r, n in names.items()},
    )
    if args.guided:
        print("Guided checklist (do these yourself; stop at the saved cart):")
        for line in drv.guided_checklist(spec, inputs):
            print(f"  {line}")
        return 0
    if not args.match_mapping:
        print("REFUSED: --website needs --match-mapping", file=sys.stderr)
        return 2
    mapping = SelectionMapping(**_json.loads(Path(args.match_mapping).read_text()))
    expected = [
        ExpectedPart(reference=m["reference"], catalog_id=m["catalog_id"])
        for m in _json.loads((plan.directory / "expected-matches.json").read_text())
    ]

    def get_match_csv() -> bytes:
        path = input("Download JLC's part-matching export, then enter its path: ").strip()
        return Path(path).read_bytes()

    try:
        drv.require_attended()
        lock_dir = Path(args.lock_dir) if args.lock_dir else None
        with drv.browser_lock(args.cdp_port, lock_dir=lock_dir):
            page, close = drv.connect_attended_page(args.cdp_port)
            try:
                site = drv.run_driver(
                    page,
                    spec,
                    inputs,
                    expected,
                    mapping,
                    get_match_csv,
                    out / "site-screenshots",
                    fab_review_signed=result.fab_review_signoff is not None,
                )
            finally:
                close()
    except drv.DriverAbort as exc:
        print(f"ABORTED at {exc.step}: {exc.reason}", file=sys.stderr)
        if exc.screenshot:
            print(f"screenshot: {out / 'site-screenshots' / exc.screenshot}", file=sys.stderr)
        print("Nothing was submitted. Continue by hand (--guided):", file=sys.stderr)
        for line in drv.guided_checklist(spec, inputs):
            print(f"  {line}", file=sys.stderr)
        return 2
    page_out = render_review_page(
        plan,
        destination=out / "review-page-cart",
        stock=result.stock.as_dict(),
        site=site.as_dict(),
        fab_review_signoff=result.fab_review_signoff,
    )
    print(f"cart: {site.cart_url}  (state: {site.state})")
    print(f"review page: {page_out.directory}")
    return 0


def run(args: argparse.Namespace) -> int:
    from ..parts.jlcpcb_api import JLCCredentials, JLCOpenAPIClient
    from ..parts.lcsc_inventory import LCSCInventorySource

    if args.dry_run and args.website:
        # Issue #8465: a dry run never touches the website. Refuse before the
        # preflight so nothing is written and nothing is contacted.
        print(
            "REFUSED: --dry-run never touches the website; drop --dry-run to run "
            "--website (without --upload nothing is sent to the API), or use --guided",
            file=sys.stderr,
        )
        return 2
    credentials = JLCCredentials.from_env()
    inventory_source: JLCOpenAPIClient | LCSCInventorySource | None = None
    if credentials:
        inventory_source = JLCOpenAPIClient(credentials)
    else:
        # Issue #8539: no OpenAPI keys -> anonymous LCSC reads unblock the
        # dry-run / website / guided legs. They never satisfy --upload, which
        # keeps requiring credentials (enforced by run_submission).
        inventory_source = LCSCInventorySource()
    upload = bool(args.upload)
    covered = [c for c in args.inventory_covered.split(",") if c.strip()]
    try:
        result = run_submission(
            bundle_root=Path(args.bundle),
            board=Path(args.board),
            schematic=Path(args.schematic),
            quantity=args.quantity,
            output_dir=Path(args.output),
            dry_run=bool(args.dry_run),
            inventory_source=inventory_source,
            inventory_covered=covered,
            upload=upload,
            reviewer=args.reviewer,
            credentials=credentials,
        )
    except (WorkflowError, SubmissionError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    failures = list(format_stock_failures(result.stock))
    if args.guided:
        # The manual checklist never touches the site, so a blocked stock gate
        # does not stop it; the stock state is printed beside it instead.
        state = "BLOCKED" if result.stock.blocked else "pass"
        print(f"stock gate: {state}")
        for failure in failures:
            print(f"  STOCK: {failure}")
        return _website_leg(args, result, Path(args.output))
    if args.website and not result.stock.blocked:
        return _website_leg(args, result, Path(args.output))
    if args.website:
        print("website leg skipped: the stock gate is blocked", file=sys.stderr)
    if getattr(args, "format", "text") == "json":
        print(
            json.dumps(
                {
                    "states": dict(result.states),
                    "stock_failures": failures,
                    "review_page": str(result.page.directory),
                    "stock_blocked": bool(result.stock.blocked),
                },
                indent=2,
            )
        )
        return 1 if result.stock.blocked else 0
    for key, value in result.states.items():
        print(f"{key}: {value}")
    for failure in failures:
        print(f"  STOCK: {failure}")
    print(f"review page: {result.page.directory}")
    return 1 if result.stock.blocked else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kct submit-jlcpcb", description=__doc__)
    add_arguments(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
