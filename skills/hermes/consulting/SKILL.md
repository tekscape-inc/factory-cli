---
name: consulting
description: Turn a consulting request (Telegram intake) into a linted, price-validated SOW in ~/factory-portfolio/sows/<id>.md, an optional tekdeck deck with an independent review, and ONE Outlook draft in John's own mailbox. John sends it himself. Use for any "SOW", "proposal" or "quote" request, and for a portfolio row with kind consulting.
---

# consulting

You are the consulting lane. You produce **documents and one draft**. You never send email. Sending is R3 and it
belongs to John. Scripts live in `~/dev/factory-standard/scripts/` (below as `S=~/dev/factory-standard/scripts`).

## Steps

1. **Intake → SOW.** Copy `templates/SOW.md` (factory-standard) to `~/factory-portfolio/sows/<id>.md`. The `<id>` is
   the portfolio row id with `kind: consulting`. Keep its header block (`Status`, `To`, `Subject`) above the first `---` line.
   Fill in only what the request states. **Every unknown is tagged `[CONFIRM]`**: scope boundaries, dates, rates,
   quantities, customer names. Never guess a figure.
2. **Confirm.** Send **one** Telegram message that batches the questions: one `clarify` per `[CONFIRM]`, each with
   2–3 concrete options. Replace each tag only with the chosen label, then commit the SOW in `factory-portfolio`.
   Never resolve a `[CONFIRM]` yourself.
3. **Lint (hard gate).** Run `python3 $S/sow-lint.py ~/factory-portfolio/sows/<id>.md --json`. Exit 2 means stop, fix the
   named item, and lint again:
   - `confirms > 0` → go back to step 2;
   - `placeholders` (TBD, TODO, XXX, `<customer>`, lorem) → fill them in or `[CONFIRM]` them;
   - `totals.ok == false` → the `Totals` table's `Total` row must equal the sum of the line items.
4. **Validate the pricing** (a `data-validator` pass) before any draft. Run a second, separate turn:
   `hermes -p tekscape -z "<reconciliation prompt>" --provider <other vendor> -m <other model>`, or use the `delegate`
   tool. Reconciliation prompt: *"Recompute every line item (qty × rate), the Total and any discount from
   sows/<id>.md alone. List each figure as stated vs recomputed. Answer TIES or DOES NOT TIE, with the rows that differ."*
   Anything but TIES → fix the SOW, then run steps 3 and 4 again. Record `author_vendor` and `reviewer_vendor` in the
   evidence. **They must differ.**
5. **Deck (only if spike T6 is PASS and `~/.factory/opt/tekdeck/bin/tekdeck doctor | jq -e .pack_ok` is true).**
   `tekdeck build` → `tekdeck validate <deck.pptx>` → review by a model from **a different vendor** than the deck author,
   written to `review.json` with that reviewer's `reviewer_id` → `tekdeck review-finalize --deck <slug>`. `result` must
   be `PASS`. `REVIEW.INDEPENDENT` rejects reviewer == author, so never pass the author's id as `reviewer_id`. If the
   doctor fails, skip the deck. The deliverable is then the SOW document plus the step-4 review by a different vendor
   (ADR-0009 row 11).
6. **Draft (R1).** Run `python3 $S/outlook-draft.py --md ~/factory-portfolio/sows/<id>.md --to jclendennen@tekscape.com --json`.
   - The script refuses, with exit 2 and no Graph call, when the recipient is not in `~/.factory/outlook-allowlist.txt`
     (P4 holds John's mailbox only) or when the file still has `[CONFIRM]`. Do not work around a refusal; report it.
   - Success means `statusCode == 201`, `isDraft == true`, and the exact recipient. Keep the returned `id` in the
     portfolio row.
   - To replace a stale draft: `outlook-draft.py … --supersede <old id>`. It retitles the old draft
     `[SUPERSEDED - do not send] …` with PATCH. There is **no DELETE**.
7. **Tell John.** Run `python3 $S/notify.py --kind sow_draft --fields-json -` with `{"summary": "<id>: <one line>",
   "web_link": "<webLink>"}`. Only these two fields are accepted, and no SOW text goes to Telegram.
8. **Done.** The row becomes `done` (`python3 $S/portfolio.py status --id <id> done`) **only** when John replies "sent".
   In P4 the draft goes to his own mailbox and he replies "seen", which also closes the row. Until then the row stays
   `running`.

## Forbidden

- Any Graph path other than `POST /me/messages` (a draft), `GET /me/mailFolders/drafts/messages` (read-only, to check
  that exactly one live draft carries the subject) and `PATCH /me/messages/{id}` (retitle). In particular: no mail-send
  endpoint, no draft-send action, no DELETE, and no reply or forward endpoint. `outlook-draft.py` is the only write path.
  Never call the workspace wrapper with `--allow-writes` yourself.
- Customer addresses in P4. The recipient is always John's own Tekscape mailbox.
- Putting SOW bodies, prices, customer names or evidence into Telegram text, logs or argv. Use the `sow_draft` fields only.
- Resolving `[CONFIRM]`, approving pricing, or marking `done` on John's behalf.
- Editing the Hermes profile config, approvals, allowlists or tools (ADR-0004).

## Evidence (per SOW)

In `~/factory-portfolio/sows/<id>.evidence.md`: the sow-lint JSON, the validator verdict with both vendors, the deck
`review-finalize` JSON if there is a deck, and the outlook-draft JSON (`statusCode`, `isDraft`, `id`; `webLink` host
only).
