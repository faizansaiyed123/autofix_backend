"""Invoicing domain.

The bill the customer is asked to pay. An invoice is raised against a finished
repair order and carries the lines that were agreed on the estimate plus any
extra work the shop carried out, so the money on the invoice can always be
traced back to either the customer's approval or a note from the service desk.

Money is derived, never accepted: the header totals are recomputed from the
lines every time a line changes, and the balance is ``total - amount_paid``,
which is a property rather than a column so it cannot drift from the two numbers
it is made of. Payments themselves live in :mod:`app.payments`.
"""
