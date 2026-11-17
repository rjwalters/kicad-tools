"""``kct submit-jlcpcb``: agent-prepared JLCPCB PCBA submission (issues #8465, #8466).

Composes the existing submission plan / stock / upload / review pieces via
:mod:`kicad_tools.export.submission_workflow`. By default it never places an
order. The opt-in ``--checkout`` leg (issue #8935, attended only) continues from
the saved cart to a payment whose exact total the operator approves; see
:mod:`kicad_tools.manufacturers.jlc_payment`.
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
    pay = parser.add_argument_group(
        "checkout leg (opt-in, attended only, off by default; issue #8935)",
        "Continues --website from the saved cart to JLC's payment step. Needs an "
        "approval naming the exact total. Without --pay it is a rehearsal that stops "
        "before the pay button (run that first).",
    )
    pay.add_argument(
        "--checkout",
        action="store_true",
        help="Continue from the saved cart to the payment step (needs --website and an approval)",
    )
    approval = pay.add_mutually_exclusive_group()
    approval.add_argument(
        "--auto",
        action="store_true",
        help="Show the final total and ask you to type it back to approve it",
    )
    approval.add_argument(
        "--approve-total",
        metavar="'USD 123.45'",
        help="Approve exactly this total (currency code and amount); any other total stops",
    )
    pay.add_argument(
        "--pay",
        action="store_true",
        help="Press the pay button once after the approval matches (default: stop before it)",
    )
    pay.add_argument(
        "--card-file",
        metavar="PATH",
        help="Mode-600 card file outside every git repository (default: the saved site method)",
    )
    pay.add_argument(
        "--ledger-dir",
        help="Directory of the order ledger (default: ~/.kct/orders)",
    )
    pay.add_argument(
        "--clear-unconfirmed-payment",
        metavar="PLAN_SHA256",
        help="Record that you checked your account and an unconfirmed attempt was NOT charged",
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
        from ..parts.jlc_env import JLCContact

        for line in JLCContact.from_env().guided_lines(show_values=sys.stdout.isatty()):
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

    checkout_outcome: tuple[str, object] | None = None
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
                if getattr(args, "checkout", False):
                    checkout_outcome = _checkout_leg(args, plan, page, out)
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
        fab_dfm=result.fab_dfm,
    )
    print(f"cart: {site.cart_url}  (state: {site.state})")
    print(f"review page: {page_out.directory}")
    if checkout_outcome is not None:
        return _report_checkout(checkout_outcome, out)
    return 0


def _checkout_leg(args: argparse.Namespace, plan, page, out: Path) -> tuple[str, object]:
    """Run the opt-in checkout leg on the page the driver left at the saved cart."""
    from ..manufacturers import jlc_payment as pay
    from ..manufacturers.jlc_ledger import OrderLedger

    approval = pay.Approval(pay.parse_approval(args.approve_total) if args.approve_total else None)
    ledger = OrderLedger(Path(args.ledger_dir).expanduser() if args.ledger_dir else None)
    try:
        done = pay.run_checkout(
            page,
            plan_sha256=plan.sha256,
            approval=approval,
            ledger=ledger,
            ask=input,
            say=print,
            shots_dir=out / "site-screenshots",
            pay=bool(args.pay),
            card_file=Path(args.card_file).expanduser() if args.card_file else None,
        )
    except pay.PaymentUnconfirmed as exc:
        return ("unconfirmed", exc)
    except pay.CheckoutAbort as exc:
        return ("aborted", exc)
    return ("done", done)


def _report_checkout(outcome: tuple[str, object], out: Path) -> int:
    from ..manufacturers import jlc_payment as pay

    kind, value = outcome
    receipt = out / "checkout-receipt.json"
    if kind == "done":
        assert isinstance(value, pay.CheckoutResult)
        receipt.write_text(json.dumps(value.as_dict(), indent=2, sort_keys=True) + "\n")
        if value.state == "ordered":
            print(f"ORDERED: order {value.order_id}, charged {value.charged_total}")
        else:
            print(f"checkout rehearsed to the pay button; approved {value.approved_total}")
        print(f"checkout receipt: {receipt}")
        return 0
    assert isinstance(value, pay.CheckoutAbort | pay.PaymentUnconfirmed)
    state = "payment-unconfirmed" if kind == "unconfirmed" else "not-ordered"
    receipt.write_text(
        json.dumps(
            {
                "checkout_version": pay.CHECKOUT_VERSION,
                "state": state,
                "step": value.step,
                "reason": value.reason,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    if kind == "unconfirmed":
        print(f"PAYMENT UNCONFIRMED at {value.step}: {value.reason}", file=sys.stderr)
        print(f"checkout receipt: {receipt}", file=sys.stderr)
        return 3
    print(f"CHECKOUT STOPPED at {value.step}: {value.reason}", file=sys.stderr)
    print("Nothing was paid. The cart is saved; you can pay it yourself.", file=sys.stderr)
    return 2


def _checkout_preflight(args: argparse.Namespace) -> str | None:
    """Refusal text for an inconsistent or unattended checkout request, else None."""
    from ..manufacturers import jlc_payment as pay

    wants = args.auto or args.approve_total or args.pay or args.card_file
    if not args.checkout:
        if wants:
            return "--auto, --approve-total, --pay and --card-file only apply with --checkout"
        return None
    if not args.website or args.guided or args.dry_run:
        return "--checkout continues the --website leg; use it with --website only"
    if not (args.auto or args.approve_total):
        return "--checkout needs an approval: --auto (type the total) or --approve-total"
    if args.approve_total:
        try:
            pay.parse_approval(args.approve_total)
        except pay.MoneyError as exc:
            return f"--approve-total: {exc}"
    try:
        pay.require_attended_checkout()
        if args.card_file:
            pay.check_card_file(Path(args.card_file).expanduser())
    except pay.CheckoutAbort as exc:
        return exc.reason
    return None


def _clear_unconfirmed(args: argparse.Namespace) -> int:
    from ..manufacturers import jlc_payment as pay
    from ..manufacturers.jlc_ledger import OrderLedger, clear_unconfirmed

    try:
        pay.require_attended_checkout()
    except pay.CheckoutAbort as exc:
        print(f"REFUSED: {exc.reason}", file=sys.stderr)
        return 2
    ledger = OrderLedger(Path(args.ledger_dir).expanduser() if args.ledger_dir else None)
    if clear_unconfirmed(ledger, args.clear_unconfirmed_payment, input):
        print("Recorded: the unconfirmed attempt was not charged. A new checkout may run.")
        return 0
    print("Nothing recorded (no unresolved attempt for that plan, or not confirmed).")
    return 2


def run(args: argparse.Namespace) -> int:
    from ..parts.jlc_env import load_jlc_env
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
    if getattr(args, "clear_unconfirmed_payment", None):
        return _clear_unconfirmed(args)
    refusal = _checkout_preflight(args)
    if refusal:
        # Before the preflight: nothing is written and nothing is contacted.
        print(f"REFUSED: {refusal}", file=sys.stderr)
        return 2
    load_jlc_env(Path(args.board))
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
