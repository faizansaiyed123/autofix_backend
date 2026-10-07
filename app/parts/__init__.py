"""Parts catalog domain.

What the shop stocks, what each item costs, and what it sells for. Stock
quantities live here as a running balance, but they are never edited by hand:
every movement is written to ``inventory_transactions`` (see
:mod:`app.inventory`), which is the audit trail the balance is derived from.
"""
