---
name: submit-jlcpcb
invocation: /kct:submit-jlcpcb
suggestedModel: sonnet
description: Prepare a JLCPCB PCBA submission from a signed-off tapeout bundle — offline preflight, frozen plan, live stock gate, optional API upload and a review page. Never checks out, pays or purchases parts; "ordered" is never set.
---

# Submit to JLCPCB (prepare only)

Drives `kct submit-jlcpcb`, which composes the existing submission pieces
(`export/submission_plan.py`, `submission_review.py`, `manufacturers/jlc_upload.py`)
through `export/submission_workflow.py`. It prepares and verifies; a human places the order.

## Arguments

**Arguments**: `$ARGUMENTS`

`$ARGUMENTS` is `<bundle> --board <pcb> --schematic <sch> [--quantity N] -o <new-dir> [--dry-run] [--inventory-covered C1,C2] [--upload --reviewer NAME]`. Default to `--dry-run` unless the human asked for the upload.

## Steps

1. **Preflight (offline, before any network call).** Refuses on: a `manifest.json` file whose sha256/size no longer matches; no `tapeout.human_signoff` recorded in the manifest (record it with `kct tapeout-sign-off <bundle> --board <board.kicad_pcb> --readiness <readiness.json> --reviewer "<name>"`); no readiness receipt hash (`tapeout.readiness_sha256`); a board file whose sha256 differs from `tapeout.board_sha256`.
2. **Prepare + verify.** Layer count (board copper layers) and assembly sides (CPL `Layer` column) come from the board/bundle, never guessed; quantity is `--quantity`.
3. **Stock gate.** Live `refresh_inventory` at `per_board × quantity`. Every short line is listed with need vs stock; unknown stock blocks. Lines you hold in your JLC parts inventory are passed explicitly with `--inventory-covered C123,C456` and shown as covered; this is never inferred.
4. **Optional API leg** (`--upload --reviewer NAME`, credentials in the environment): uploads the exact Gerber bytes, fetches the audit preview and records the receipt in `upload-ledger.jsonl`. Refused when the stock gate is blocked.

Without JLC credentials the stock gate reads stock from LCSC's anonymous public API (`provenance.source` is `lcsc-anonymous`), which is enough for `--dry-run`, `--website` and `--guided`; `--upload` still requires the OpenAPI credentials. `--guided` runs whatever the stock gate says and prints the stock state beside the checklist; `--website` stays gated.
5. **Review page** under `<output>/review-page`, with distinct states prepared → stock-checked → uploaded.

```bash
kct submit-jlcpcb <dir> --board <pcb> --schematic <sch> \
    --quantity 5 -o <dir>-submit --dry-run
```

`--dry-run` runs steps 1–3 and 5 and never contacts the upload API or the website. Exit codes: 0 ok, 1 stock gate blocked, 2 refused.

## Hard limits

- No code path calls checkout, payment or parts purchase. Do not add one.
- Never set or claim an "ordered" state; hand the review page to the human.
- The website driver is **attended only** (operator ruling on #8468): your own signed-in Chrome on a dedicated CDP profile, a TTY, you watching. Never run it headless, in CI or unattended; one action at a time, no retries.
- The driver never requests a robots.txt-disallowed path (`/api`, `/user-center/`, the `downloadNoMatchBomList` exports). You download the part-matching CSV yourself and hand it over.
- It stops at the saved cart. "ordered" is only ever set by you.

## Website leg (optional, after step 3)

```bash
kct submit-jlcpcb <dir> --board <pcb> --schematic <sch> -o <out> \
    --website --thickness-mm 1.6 --outer-copper-oz 1 [--inner-copper-oz 1] [--has-tht] \
    --match-mapping mapping.json
```

Start Chrome yourself with `--remote-debugging-port=9222 --user-data-dir=<dedicated dir>` (Chrome 136+ refuses the default profile), sign in, open the quote page. The driver uploads the frozen gerbers, sets fields (aborting on any it cannot map exactly), enables PCBA, uploads BOM/CPL, then asks for the path of JLC's part-matching export (the CSV itself is requested mid-run). `--match-mapping` is not that CSV: it is a small JSON file giving the CSV's column schema (`SelectionMapping` fields: `reference`, `selected`, `selected_values`, `unselected_values`, `catalog_id`, `identity`), and it is supplied up front. Any omitted, extra, substituted or unselected reference blocks the cart (`export/factory_selection.py`). The result is a second review page, `<out>/review-page-cart`. `--website` is refused with `--dry-run` (a dry run never touches the website); without `--upload` nothing goes to the API. Only one driver may hold a debug browser at a time: a lock in `~/.kct/locks` (`--lock-dir`) refuses a second run on the same `--cdp-port` until the first exits, or until the lock is two hours old or its process is dead.

**Kill switch:** `--guided` prints the same steps as a manual checklist, and any failed assertion drops into it. If JLC objects or restricts an account, disable the driver rather than patching it. Install the optional extra with `pip install 'kicad-tools[submit]'` for the CDP attach; `playwright install` is NOT needed (the driver attaches to your system Chrome over CDP). Selectors in `manufacturers/jlc_site_driver.py` are unverified against the live site until the first attended run.
