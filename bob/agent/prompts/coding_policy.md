# BridgeWerk coding policy (DRAFT: to be reviewed and replaced by the accountant)

This file is read by Bob on every coding decision. Edit it in plain English; changes take effect on the next deployment and are recorded in git history.

## Entity

- Everything is booked to BridgeWerk Capital Management Inc. (one QuickBooks company).
- An invoice addressed to a portfolio company, fund or other entity is not ours: mark it ambiguous and ask.

## Timing

- Book bills on the invoice date, not the date received.
- Services covering more than three months paid up front (insurance, annual software, retainers) go to Prepaid Expenses, with the service period recorded. Monthly or shorter service goes straight to expense.

## Capital items

- Equipment, furniture and computer hardware over $2,500 per item are capital (tag capex). Below that, expense to Office Supplies or Computer Equipment Expense.

## Deals and related parties

- Legal, advisory, diligence and travel costs tied to a named transaction or project (for example "Project Falcon") are deal costs: code to Deal Costs and tag acquisition_related.
- Anything involving a shareholder, director, employee personally, a portfolio company or a fund is related_party or intercompany. Tag it; do not guess the treatment.

## Tax

- Alberta suppliers charge GST only (5%). Out-of-province suppliers may charge HST; use the matching QBO tax code.
- Insurance premiums and most financial services are GST-exempt.
- Foreign suppliers that charge no Canadian tax: use the out-of-scope or zero tax code, never invent tax.
- Record the supplier's GST/HST registration number when shown.

## Card receipts

- Receipts already paid on a company card are expenses (kind "expense"), not bills.
