"""Purchase order domain.

What the shop has ordered from a supplier, and what has arrived against it.
Stock still moves through :mod:`app.inventory` — receiving files a ``RECEIPT``
transaction per line, so goods arriving from a supplier are on the same ledger
as everything else.
"""
