"""UUIDv7-based display ID generation using Crockford's Base32.

Format: ``{PREFIX}-{12 chars}``  e.g. ``INV-9TZRHT4WYN7F``

Algorithm:
1. Use the investigation UUID (ideally UUIDv7 for time-ordering; UUID4 also works).
2. Take the first 60 bits of the UUID bytes.
3. Encode as 12-character Crockford's Base32 string (5 bits per char, 12 × 5 = 60).

Display IDs are globally unique (derived from UUID) and human-friendly:
no ambiguous characters (no I/L/O/U), uppercase, fixed length.
"""

from __future__ import annotations

import uuid as _uuid

# Crockford's Base32 alphabet — excludes I, L, O, U to avoid ambiguity
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def generate_display_id(
    prefix: str = "INV", investigation_uuid: str | None = None
) -> tuple[str, str]:
    """Generate a UUID and its display ID.

    Args:
        prefix: Display ID prefix (INV, EXP, JOB, ART).
        investigation_uuid: If provided, derive display ID from this UUID
            instead of generating a new one.

    Returns:
        Tuple of (uuid_str, display_id).
        e.g. ("0190d4a2-7c3f-7b8a-9e1f-3a2b4c5d6e7f", "INV-9TZRHT4WYN7F")
    """
    u = _uuid.UUID(investigation_uuid) if investigation_uuid else _uuid.uuid4()

    display_id = uuid_to_display_id(u, prefix)
    return str(u), display_id


def uuid_to_display_id(u: _uuid.UUID, prefix: str = "INV") -> str:
    """Compute display ID from a UUID.

    Takes the first 60 bits of the UUID, encodes as 12-char Crockford Base32.
    """
    # First 8 bytes = 64 bits; shift right 4 to get first 60 bits
    val = int.from_bytes(u.bytes[:8], "big") >> 4
    encoded = _int_to_crockford(val, 12)
    return f"{prefix}-{encoded}"


def _int_to_crockford(val: int, length: int) -> str:
    """Encode an integer as a fixed-length Crockford Base32 string."""
    chars = []
    for _ in range(length):
        chars.append(_CROCKFORD[val & 0x1F])
        val >>= 5
    return "".join(reversed(chars))
