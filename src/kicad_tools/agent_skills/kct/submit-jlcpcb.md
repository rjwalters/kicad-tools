---
name: submit-jlcpcb
invocation: /kct:submit-jlcpcb
suggestedModel: sonnet
description: Prepare a JLCPCB PCBA submission from a signed-off tapeout bundle — offline preflight, frozen plan, live stock gate, optional API upload and a review page. By default never checks out, pays or purchases parts and never sets "ordered"; the only exception is the opt-in, attended `--checkout` leg, which the operator runs in their own terminal and which pays only a total they approve exactly.
---

# Submit to JLCPCB (prepare; opt-in attended checkout)

Drives `kct submit-jlcpcb`, which composes the existing submission pieces
(`export/submission_plan.py`, `submission_review.py`, `manufacturers/jlc_upload.py`)
through `export/submission_workflow.py`. It prepares and verifies; by default a human places the order. The opt-in checkout leg (`--checkout`, issue #8935) is for the operator to run in their own terminal, never for an agent: see "Checkout leg" below.

## Arguments

**Arguments**: `$ARGUMENTS`

`$ARGUMENTS` is `<bundle> --board <pcb> --schematic <sch> [--quantity N] -o <new-dir> [--dry-run] [--inventory-covered C1,C2] [--upload --reviewer NAME]`. Default to `--dry-run` unless the human asked for the upload. An agent never passes `--checkout`, `--auto`, `--approve-total`, `--pay`, `--card-file` or `--clear-unconfirmed-payment` (see "Hard limits").

## Steps

1. **Preflight (offline, before any network call).** Refuses on: a `manifest.json` file whose sha256/size no longer matches; no `tapeout.human_signoff` recorded in the manifest (record it with `kct tapeout-sign-off <bundle> --board <board.kicad_pcb> --readiness <readiness.json> --reviewer "<name>"`); no readiness receipt hash (`tapeout.readiness_sha256`); a board file whose sha256 differs from `tapeout.board_sha256`; a gerber zip that differs from the sha256 either sign-off bound. `tapeout.human_signoff` is only the *release for upload* (gates pass; these bytes may be previewed at the fab). `--dry-run`, `--guided` and `--website` up to the factory match work with it alone.
2. **Prepare + verify.** Layer count (board copper layers) and assembly sides (CPL `Layer` column) come from the board/bundle, never guessed; quantity is `--quantity`.
3. **Stock gate.** Live `refresh_inventory` at `per_board × quantity`. Every short line is listed with need vs stock; unknown stock blocks. Lines you hold in your JLC parts inventory are passed explicitly with `--inventory-covered C123,C456` and shown as covered; this is never inferred.
4. **Optional API leg** (`--upload --reviewer NAME`, credentials in the environment, or `JLCPCB_*` keys in the project `.env`, which `submit-jlcpcb` loads without overriding the shell): uploads the exact Gerber bytes, fetches the audit preview and records the receipt in `upload-ledger.jsonl`. Refused when the stock gate is blocked.

Without JLC credentials the stock gate reads stock from LCSC's anonymous public API (`provenance.source` is `lcsc-anonymous`), which is enough for `--dry-run`, `--website` and `--guided`; `--upload` still requires the OpenAPI credentials. `--guided` runs whatever the stock gate says and prints the stock state beside the checklist; `--website` stays gated.
5. **Review page** under `<output>/review-page`, with distinct states prepared → stock-checked → uploaded.

```bash
kct submit-jlcpcb <dir> --board <pcb> --schematic <sch> \
    --quantity 5 -o <dir>-submit --dry-run
```

`--dry-run` runs steps 1–3 and 5 and never contacts the upload API or the website. Exit codes: 0 ok, 1 stock gate blocked, 2 refused (or checkout stopped before paying), 3 payment unconfirmed (check the account by hand).

## Hard limits

- Without `--checkout`, no code path calls checkout, payment or parts purchase, and nothing sets "ordered". The one payment path is the opt-in checkout leg (`manufacturers/jlc_payment.py`, issue #8935): attended only, PCB/PCBA saved cart only, and it pays only a total the operator approves exactly. There is no parts-purchase leg, no supplement payment, no monthly budget and no unattended, scheduled or batch checkout; do not add one without a new operator ruling (#9093 and #9094 track the parts and supplement legs).
- **Hard rules for agents (the checkout leg is the human's, never yours):**
  - Never run the checkout leg, in any form, including the rehearsal.
  - Never supply, suggest, compute or pre-fill an approval total (`--approve-total` or the typed `--auto` answer). Only the human names a total, after reading it on JLC's page.
  - Never write, read, locate or ask for a card file or any card details, a session cookie or a password.
  - Never work around the attended check: no pty wrappers (`script`, `expect`, `unbuffer`), no environment scrubbing (`env -i`, unsetting variables), no calling `run_checkout` or other `jlc_payment` functions directly, no replacing them in Python. The check is a heuristic that stops accidental and routine automated use, **not a security boundary**: defeating it is possible, and doing so is forbidden.
  - Never set or claim an "ordered" state; hand the review page to the human. "ordered" is set only by the human, or by the checkout leg from JLC's confirmation page of a completed payment.
- The website driver is **attended only** (operator ruling on #8468): your own signed-in Chrome on a dedicated CDP profile, a TTY, you watching. Never run it headless, in CI or unattended; one action at a time, no retries.
- The driver never requests a robots.txt-disallowed path (`/api`, `/user-center/`, the `downloadNoMatchBomList` exports). You download the part-matching CSV yourself and hand it over. The single exception (operator ruling 2026-11-17 on #8935): the checkout leg may be on `/user-center/` and the cart host, only inside an attended session in which the operator approves the exact total of that order; `/api` and the exports stay forbidden.
- It will not save the cart until a second attestation exists: review the fab's gerber viewer / DFM result, then run `kct tapeout-sign-off <bundle> --board <pcb> --stage fab-review --reviewer "<name>"` and re-run `--website` (issue #8818).
- **Fab DFM attestation (advisory, issue #8841).** After uploading/previewing at the fab, run JLCDFM (its own login), write the per-category counts to a JSON file `{"<category>": {"danger": n, "warning": n, "good": n}}` (all 14 JLCDFM categories as snake_case, see `DFM_CATEGORY_RULES`, each with all three levels; a missing category or level is refused, never filled with zero), then `kct tapeout-sign-off <bundle> --board <pcb> --stage fab-dfm --dfm-counts <counts.json> --kct-report <kct-check.json> --reviewer "<name>"`. It records `tapeout.fab_dfm` in the manifest (never `readiness.json`) with kct's per-rule counts and lists every category where the fab reports danger and kct found nothing (unmapped categories count as kct 0). The `kct check --output` report must carry a `pcb_sha256` equal to the board's (otherwise refused). It records only and never gates a new step, but a stale or malformed record makes `preflight` refuse: re-record it (or re-release the bundle) before the cart. **Order:** release sign-off → upload/preview → JLCDFM + `--stage fab-dfm` → `--stage fab-review` → cart; fab-dfm is recommended before fab-review but not required by it.
- Without `--checkout` it stops at the saved cart. "ordered" is then set only by the human operator after paying by hand, never by an agent.

## Website leg (optional, after step 3)

```bash
kct submit-jlcpcb <dir> --board <pcb> --schematic <sch> -o <out> \
    --website --thickness-mm 1.6 --outer-copper-oz 1 [--inner-copper-oz 1] [--has-tht] \
    --match-mapping mapping.json
```

Start Chrome yourself with `--remote-debugging-port=9222 --user-data-dir=<dedicated dir>` (Chrome 136+ refuses the default profile), sign in, open the quote page. The driver uploads the frozen gerbers, sets fields (aborting on any it cannot map exactly), enables PCBA, uploads BOM/CPL, then asks for the path of JLC's part-matching export (the CSV itself is requested mid-run). `--match-mapping` is not that CSV: it is a small JSON file giving the CSV's column schema (`SelectionMapping` fields: `reference`, `selected`, `selected_values`, `unselected_values`, `catalog_id`, `identity`), and it is supplied up front. Without `tapeout.fab_review_signoff` the run aborts after the factory match and before "save to cart" (`ABORTED at fab-review`): review the viewer, sign off with `--stage fab-review`, re-run. Any omitted, extra, substituted or unselected reference blocks the cart (`export/factory_selection.py`). The result is a second review page, `<out>/review-page-cart`. `--website` is refused with `--dry-run` (a dry run never touches the website); without `--upload` nothing goes to the API. Only one driver may hold a debug browser at a time: a lock in `~/.kct/locks` (`--lock-dir`) refuses a second run on the same `--cdp-port` until the first exits, or until the lock is two hours old or its process is dead.

**Kill switch:** `--guided` prints the same steps as a manual checklist, and any failed assertion drops into it. If JLC objects or restricts an account, disable the driver rather than patching it. Install the optional extra with `pip install 'kicad-tools[submit]'` for the CDP attach; `playwright install` is NOT needed (the driver attaches to your system Chrome over CDP). Selectors in `manufacturers/jlc_site_driver.py` are unverified against the live site until the first attended run.

## Checkout leg (opt-in, attended, operator only; issue #8935)

Off by default. The operator runs it in their own terminal after the website leg has saved the cart, in the same run:

```bash
# First run: rehearsal. Goes to the approved total and stops BEFORE the pay button.
kct submit-jlcpcb <dir> --board <pcb> --schematic <sch> -o <out> \
    --website --thickness-mm 1.6 --outer-copper-oz 1 --match-mapping mapping.json \
    --checkout --auto
# Only after a clean rehearsal: pay. --approve-total names the exact total instead of typing it.
kct submit-jlcpcb <dir> --board <pcb> --schematic <sch> -o <out> \
    --website --thickness-mm 1.6 --outer-copper-oz 1 --match-mapping mapping.json \
    --checkout --approve-total 'USD 123.45' --pay
```

Each run drives the website leg again, so give the second run a fresh `-o` directory (the plan, and so the ledger key, is the same for the same bundle and quantity). The paying run saves another cart line, so its checkout will probably list two lines and stop at `cart-lines` without paying (fail safe). To proceed, delete the rehearsal's line from the cart in your browser before the paying run, or when it stops, and run again.

- **Approval names the exact total.** `--auto` shows the final total (after the payment method is chosen, so shipping, tax, fees and coupons are in it) and you approve by typing that total back (`USD 123.45`; `y` is not an approval). `--approve-total` takes the currency code and amount up front. A total that differs by a cent, a different currency, or a total that changes between the quote and the pay click stops before paying, reports both numbers and records nothing. JLC's non-cancellable terms are shown before you approve.
- **Attended only (a heuristic, not a guarantee).** It stops unless stdin, stdout and stderr are an interactive terminal in the foreground, and stops when CI, scheduler (`INVOCATION_ID`, `JOURNAL_STREAM`), Loom (`LOOM_*`), agent-harness (`CLAUDECODE`, `CODEX_*`, `OPENCODE*`, `SQUAD_*`) or MCP (`MCP_*`, `KCT_MCP*`) environment markers are set, under pytest, or when Playwright debug logging (`DEBUG` with `pw:` or `*`, `PWDEBUG`) would print typed values. You also type `checkout` to start. No MCP tool or other kct command reaches it. This stops accidents and routine automation; it is not a security boundary (a pty wrapper with a scrubbed environment, or Python code replacing the check, gets past it). Run it yourself, from a plain terminal outside any agent session.
- **Payment method.** The saved site payment method is the default; kct types nothing. `--card-file PATH` is optional: a `KEY=VALUE` file with `KCT_CARD_NAME`, `KCT_CARD_NUMBER`, `KCT_CARD_EXPIRY` (MM/YY) and optional `KCT_CARD_CVC`, owned by you, mode exactly 600, not a symlink, and outside every git repository (otherwise refused). It is read only at the fill step and typed only into the jlcpcb.com page; no screenshot is taken once card data is in the page, and card values never appear in output, logs, receipts, the ledger, error messages, the environment or process arguments. After the fill kct drops its references to the values; Python cannot erase immutable strings, so they stay in kct's memory until garbage-collected, and they travel to Chrome over the CDP connection. An empty `KCT_CARD_CVC` means you type the security code into the browser yourself.
- **One pay click, never retried.** The total is re-read immediately before the click; the click itself is not bound to that read (Playwright cannot read and click atomically), so a few milliseconds separate them. `payment-attempted` is fsynced to the order ledger before the click. "ordered" is recorded only when the URL changed to a confirmation page showing the confirmation marker, a well-formed order number and a charged total equal to the approval, and you confirm that page. Anything else (a redirect, a timeout, a missing marker, a different charged total, your "no") is `payment-unconfirmed`, exit 3: check your JLCPCB account by hand. If you were not charged, record that with `--clear-unconfirmed-payment <plan-sha256>` (type `not charged`) before a new attempt.
- **Idempotent, as far as the ledger file goes.** The ledger is `~/.kct/orders/jlc-orders.jsonl` under your `$HOME`; no flag moves it. A plan already ordered, or with an unresolved attempt, stops before any click; it is checked at the start and again, under an exclusive file lock, immediately before the attempt is recorded, so two concurrent runs cannot both pay. Deleting the ledger, or running with a different `HOME`, removes this protection. It must stay owned by you and mode 600 (checked on every open).
- **Personal data kept.** The ledger holds the plan sha256, times, approved and charged totals, the JLC order number and the step and reason of a stopped or unconfirmed payment. Each checkout run's receipt (`checkout-receipt.json`, same fields) and its screenshots (which can show your address and a masked card, and are not taken after card entry) go to `~/.kct/orders/checkout/<plan>-<time>/` (mode 700), never into `-o`, which may be in a git repository.

**Unverified against the live site (stop and look at every step on first use).** Every checkout selector, the checkout and confirmation paths, the order-number pattern and the money formats in `manufacturers/jlc_payment.py` were written without a recorded page (#8629, #8478). While `CHECKOUT_SELECTORS_VERIFIED` is `False` the leg asks you to confirm the page before each step (open checkout, cart lines, payment method or card form, final total, pay) and the confirmation page after paying; anything but `y` stops before paying, or records the payment as unconfirmed after it. Card fields inside a third-party payment iframe are not handled (the leg aborts rather than type into another origin).
