# Evals

Test sets that measure Bob against the real model. Unit tests (`tests/`) check the code; these check the model's judgement.

## Triage

26 fictional emails covering the cases that matter: routine invoices, a statement that must not be posted, a credit note, receipts (PDF and a phone photo), a duplicate, Business Central exports, a staff instruction, marketing, and the fraud patterns (unknown sender, changed bank details, lookalike domain, spoofed internal sender, instructions aimed at an AI, an invoice addressed to another company). `cases.py` holds each email and the answer Bob should give.

```bash
pip install -e ".[dev]"
python -m evals.triage.build     # renders the documents into evals/triage/docs/
python -m evals.triage.run       # needs BOB_FOUNDRY_RESOURCE (and az login) or BOB_FOUNDRY_API_KEY
python -m evals.triage.run --only lookalike-domain,prompt-injection
```

Each run prints pass/fail per case with what Bob answered, and saves JSON to `evals/triage/results/`. A full run costs a few cents.

`tests/test_eval_harness.py` runs the same cases with a scripted model, so the harness itself and the rules that code enforces regardless of the model (duplicates, sender checks) are covered by the normal test suite.

Each invoice case also records the extraction ground truth (vendor, number, date, subtotal, tax, total, GST number). The coding step's eval will score against it.
