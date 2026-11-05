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

1. **Preflight (offline, before any network call).** Refuses on: a `manifest.json` file whose sha256/size no longer matches; no `tapeout.human_signoff` recorded in the manifest; a board file whose sha256 differs from `tapeout.board_sha256`.
2. **Prepare + verify.** Layer count (board copper layers) and assembly sides (CPL `Layer` column) come from the board/bundle, never guessed; quantity is `--quantity`.
3. **Stock gate.** Live `refresh_inventory` at `per_board × quantity`. Every short line is listed with need vs stock; unknown stock blocks. Lines you hold in your JLC parts inventory are passed explicitly with `--inventory-covered C123,C456` and shown as covered; this is never inferred.
4. **Optional API leg** (`--upload --reviewer NAME`, credentials in the environment): uploads the exact Gerber bytes, fetches the audit preview and records the receipt in `upload-ledger.jsonl`. Refused when the stock gate is blocked.
5. **Review page** under `<output>/review-page`, with distinct states prepared → stock-checked → uploaded.

```bash
kct submit-jlcpcb <dir> --board <pcb> --schematic <sch> \
    --quantity 5 -o <dir>-submit --dry-run
```

`--dry-run` runs steps 1–3 and 5 and never contacts the upload API or the website. Exit codes: 0 ok, 1 stock gate blocked, 2 refused.

## Hard limits

- No code path calls checkout, payment or parts purchase. Do not add one.
- Never set or claim an "ordered" state; hand the review page to the human.
- Website/browser automation and factory matching are separate work (#8465 children 2 and 3).
