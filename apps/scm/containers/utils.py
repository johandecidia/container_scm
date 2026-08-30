"""
ISO 6346 container identification helpers.

Container ID format: OOOOCU######C
  OOO  — owner code (3 letters)
  C    — category identifier (see ContainerCategory)
  ###### — serial number (6 digits)
  C    — check digit (1 digit)

Example: MSCU1234567
"""

import re
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from .choices import ContainerCategory

_CATEGORY_IDS = "".join(ContainerCategory.values)
_CONTAINER_ID_RE = re.compile(rf"^([A-Z]{{3}})([{_CATEGORY_IDS}])(\d{{6}})(\d)$")

# The same identity, typed as much of it as somebody has: an owner code and a
# category, optionally followed by any part of the serial number and the check
# digit. Anchored at the start because a container number is read left to right —
# nobody searches by the tail of a serial.
_CONTAINER_PREFIX_RE = re.compile(rf"^([A-Z]{{3}})([{_CATEGORY_IDS}])(\d{{0,7}})$")

# Typed off a container door or pasted out of a spreadsheet, a number picks up
# spaces and hyphens. They are not part of the identity.
_NOISE_RE = re.compile(r"[\s\-_./]")

# ISO 6346 assigns numeric values to letters A–Z.
# Starts at 10 for A and increments by 1, skipping any value that is a multiple of 11.
_LETTER_VALUES: dict[str, int] = {}
_val = 10
for _ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    _LETTER_VALUES[_ch] = _val
    _val += 1
    if _val % 11 == 0:
        _val += 1


def calculate_check_digit(owner_code: str, category_id: str, serial_number: str) -> int:
    """Return the ISO 6346 check digit for the given owner code, category, and serial."""
    chars = (owner_code + category_id + serial_number).upper()
    if len(chars) != 10:
        raise ValueError("Container ID prefix must be exactly 10 characters (owner + category + serial).")

    total = 0
    for i, ch in enumerate(chars):
        char_val = int(ch) if ch.isdigit() else _LETTER_VALUES[ch]
        total += char_val * (2**i)

    remainder = total % 11
    return 0 if remainder == 10 else remainder


def validate_container_id(owner_code: str, category_id: str, serial_number: str, check_digit: int) -> None:
    """Raise ValidationError if the check digit does not match the ISO 6346 calculation."""
    expected = calculate_check_digit(owner_code, category_id, serial_number)
    if int(check_digit) != expected:
        raise ValidationError(
            _("Invalid check digit: got %(got)s, expected %(expected)s.") % {"got": check_digit, "expected": expected}
        )


def parse_container_id(container_id_string: str) -> dict:
    """Parse a full container ID string into its component parts.

    Returns a dict with keys: owner_code, category_id, serial_number, check_digit.
    Raises ValidationError if the format is invalid.
    """
    s = container_id_string.strip().upper()
    m = _CONTAINER_ID_RE.match(s)
    if not m:
        raise ValidationError(_("Invalid container ID format. Expected format: ABCU123456C (e.g. MSCU1234567)."))
    return {
        "owner_code": m.group(1),
        "category_id": m.group(2),
        "serial_number": m.group(3),
        "check_digit": int(m.group(4)),
    }


@dataclass(frozen=True)
class ContainerNumberQuery:
    """How to find containers from a container number somebody typed.

    ``filters`` is a Q over Container's four identity columns. ``is_whole_number``
    is True only when the full eleven characters were given, which is the one case
    where the answer is a single container and the caller may treat it as exact.
    """

    filters: Q
    is_whole_number: bool


def container_number_query(text: str) -> ContainerNumberQuery | None:
    """Return how to match *text* as a container number, or None if it is not one.

    A container's ISO number is not stored: it is composed on read from
    ``owner_code``, ``category_id``, ``serial_number`` and ``check_digit``. So a
    substring match over those columns can never find a number typed whole — the
    string "MCUU2009300" appears in no column. This decomposes the query the same
    way the model composes the number, which is what lets a search for the number
    printed on the box find the box.

    Handles every length somebody reasonably types: "MCUU", "MCUU2009",
    "MCUU200930" and the full "MCUU2009300". Returns None for anything that is not
    shaped like the start of a container number, leaving the caller's own
    substring matching to answer.
    """
    candidate = _NOISE_RE.sub("", text).upper()
    match = _CONTAINER_PREFIX_RE.match(candidate)
    if match is None:
        return None

    owner_code, category_id, digits = match.groups()
    filters = Q(owner_code=owner_code, category_id=category_id)

    if len(digits) == 7:
        # Serial and check digit, whole. An invalid check digit is left to match
        # nothing rather than corrected: the number as typed is what was asked for.
        return ContainerNumberQuery(
            filters=filters & Q(serial_number=digits[:6], check_digit=int(digits[6])),
            is_whole_number=True,
        )
    if digits:
        return ContainerNumberQuery(filters=filters & Q(serial_number__startswith=digits), is_whole_number=False)
    return ContainerNumberQuery(filters=filters, is_whole_number=False)


def container_from_string(container_id_string: str):
    """Return an unsaved Container instance populated from a container ID string.

    The caller must set required fields (team, equipment_type, etc.) before saving.
    Raises ValidationError if the ID is invalid.
    """
    from .models import Container

    parts = parse_container_id(container_id_string)
    validate_container_id(
        parts["owner_code"],
        parts["category_id"],
        parts["serial_number"],
        parts["check_digit"],
    )
    return Container(**parts)
