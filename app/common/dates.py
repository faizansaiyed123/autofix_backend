"""The shop's calendar day.

Every date-only field in this application -- an invoice date, a purchase order
date, an estimate's ``valid_until``, a report's daily bucket -- is a **calendar
date chosen by the shop**, not an instant. A garage that opens at 8am writes
invoices dated that morning, and if the server is running on UTC then the UTC
date is the wrong answer for the first half of the working day.

The distinction matters because the alternative looks correct and is not:

- ``date.today()`` -- the shop's local day. What a date field should mean.
- ``datetime.now(UTC).date()`` -- the UTC day. Right for a machine that is always
  in the shop's timezone, wrong by up to a day for one that is not.

For a shop east of UTC the two disagree from local midnight until the following
local morning, and the disagreement is not cosmetic: an estimate with
``valid_until`` set to today expires up to half a day late, and a report's daily
buckets are each shifted, splitting a day's takings across two buckets.

This module is the single definition of that day, so the answer cannot drift
between modules again.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time


def today() -> date:
    """The shop's local calendar day.

    ``date.today()`` is deliberate: an invoice written at 8am must not carry
    yesterday's date because the server runs on UTC. Ruff's DTZ011 asks for
    ``datetime.now(tz).date()``, which would be the UTC date and the wrong answer
    for a shop that opens in the morning.
    """
    return date.today()  # noqa: DTZ011


def day_start(value: date) -> datetime:
    """The UTC instant at which ``value`` begins, in the shop's local time.

    Reports compare date columns against timestamp columns, so a bucket needs a
    datetime. Building it as UTC midnight instead would place the boundary in
    the wrong place for any shop that is not itself on UTC.
    """
    return datetime.combine(value, time.min).astimezone(UTC)


def day_end(value: date) -> datetime:
    """The last representable instant of ``value`` in the shop's local time."""
    return datetime.combine(value, time.max).astimezone(UTC)
