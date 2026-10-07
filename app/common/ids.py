"""UUID generation helpers.

The project uses time-ordered UUIDv7 primary keys so that rows insert in
index order (much better B-tree locality than random UUIDv4).

``uuid.uuid7`` only exists on CPython 3.14+. ``pyproject.toml`` declares
support for 3.11+, so this module provides a spec-compliant (RFC 9562)
fallback implementation used on older interpreters. Everything in the
codebase imports ``uuid7`` from here rather than calling ``uuid.uuid7``
directly.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable

__all__ = ["HAS_NATIVE_UUID7", "uuid7", "uuid7_str"]

HAS_NATIVE_UUID7 = hasattr(uuid, "uuid7")


def _uuid7_fallback() -> uuid.UUID:
    """Generate an RFC 9562 UUIDv7.

    Layout (128 bits):
        48 bits  unix timestamp in milliseconds
         4 bits  version (0b0111)
        12 bits  sub-millisecond randomness (rand_a)
         2 bits  variant (0b10)
        62 bits  randomness (rand_b)
    """
    unix_ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")

    rand_a = (rand >> 62) & 0x0FFF
    rand_b = rand & 0x3FFF_FFFF_FFFF_FFFF

    value = (unix_ms & 0xFFFF_FFFF_FFFF) << 80
    value |= 0x7 << 76
    value |= rand_a << 64
    value |= 0b10 << 62
    value |= rand_b

    return uuid.UUID(int=value)


uuid7: Callable[[], uuid.UUID] = getattr(uuid, "uuid7", _uuid7_fallback)


def uuid7_str() -> str:
    """Generate a UUIDv7 as a string."""
    return str(uuid7())
