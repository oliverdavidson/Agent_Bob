# Phase 1: Bob, the email bookkeeper

Decisions agreed so far, and the build plan they lead to.

## Scope

- **One entity**, one QuickBooks Online company.
- **One way in:** email to `bob@bridgewerk.ca`. Float and CIBC (FTS or CMO file exports) plug in later as additional inputs without changing the core.
- **Model:** Claude through Microsoft Foundry, authenticated with the app's managed identity.
- **Hosting:** one Azure Container App (API and worker in one container, one replica), Postgres Flexible Server, Blob Storage, Key Vault.

## Migration and test period

1. **Opening position.** Run the Business Central trial balance at **September 30, 2026**. Post it to QBO as an opening journal entry, with open AP and AR as at that date. September will not be final in Business Central until the September close; post a preliminary opening entry and a true-up once final. The opening entries are prepared by script and signed off by a person.
2. **Q4 parallel run.** Business Central stays the official books for October to December. Bob processes Q4 email into QBO. At each month-end, compare QBO's trial balance to Business Central's, account by account. Every difference is either a Bob error or a Business Central quirk, which gives a measured accuracy figure.
3. **Cutover decision on January 1, 2027** based on the Q4 results.
4. **History as memory.** A Business Central transaction export (vendor, account, amount, date, memo) is loaded into Bob's database as coding context. It is never posted to QBO.

## How an email flows

1. **Ingest.** Poll the mailbox, store the email and original attachments (content-addressed, SHA-256), mark byte-identical files as duplicates, record the sender's trust level (internal, known, unknown), move the message to the Processed folder.
2. **Triage.** Claude classifies each attachment (and the body when it carries accounting content):

   | Type | Route |
   |---|---|
   | vendor_invoice, receipt, credit_note | `ready_to_code` |
   | vendor_statement, remittance, bank_statement | `evidence`: kept for matching, never posted |
   | gl_export | `migration`: handled by the one-off import, never posted as activity |
   | reply_to_bob | `instruction` |
   | other | `no_action` |
   | anything unclear | `needs_human`, with the question Bob would ask |

   Code enforces: duplicates never reach the model; postable items from unknown senders become `needs_human`; any attachment the model skipped is still recorded; a model refusal holds the email.
3. **Code** (next slice). Claude proposes vendor, account, tax code, service period and tags (acquisition-related, capex, related party, unusual), using the chart of accounts, a written coding policy, Business Central history and past corrections. Ambiguous items escalate to a second, independent classification; disagreement holds the item.
4. **Validate** (next slice). Code checks arithmetic and GST, that IDs exist in the QBO company, duplicates (vendor + invoice number, hash), the closing date, and hold rules.
5. **Post or hold** (next slice). Post-first: clear items post straight away; flagged items post and go on a confirm-before-close list; held items wait for one reply.
6. **Report** (next slice). A daily digest email, questions by email reply (internal addresses only), and one-click undo of anything Bob posted.

## Hard lines

- No payments, no payee or bank-detail changes. Cash stays with people in CMO and Float.
- Never post into a closed period (QBO closing date, set by a person after each close).
- Held before posting: amounts over the materiality threshold, intercompany or related-party items, acquisition and transaction costs, changed bank details, unclear entity or tax treatment, anything that feeds a payment.
- Posting has a kill switch and a daily cap. Every posted entry is tagged with its Bob proposal id and can be reversed by Bob.
- The audit trail is append-only (enforced by a database trigger).

## Build slices

| Slice | Contents | Status |
|---|---|---|
| 1 | Skeleton, Postgres schema and queue, mailbox ingestion, triage, Bicep, tests | Done |
| 1b | Validator; sender verification (SPF/DKIM/DMARC, lookalike domains); triage test set of 26 cases | Done |
| 2 | QBO OAuth and read sync (accounts, vendors, tax codes, closing date), write-guarded client | Built; needs an Intuit sandbox to test live |
| 3 | Coding step, posting to the sandbox, undo | Next |
| 4 | Daily digest and email reply loop (needs Mail.Send on the mailbox) | |
| 5 | Opening-balance import from the Sept 30 trial balance; Business Central history load | |
| 6 | Month-end comparison report against Business Central for the Q4 run | |

## Open items

- Materiality threshold (placeholder `BOB_MATERIALITY=25000`) and the hold list, with the accountant.
- QBO OAuth tokens are stored in Postgres; move them to Key Vault before production.

- Written coding policy: chart of accounts with plain-English descriptions and the rules the accountant uses.
- Business Central exports: Sept 30 trial balance, open AP/AR, transaction history.
- Foundry: Claude deployment name and the role assignment for Bob's managed identity.
