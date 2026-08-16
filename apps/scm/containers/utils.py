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

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from .choices import ContainerCategory

_CATEGORY_IDS = "".join(ContainerCategory.values)
_CONTAINER_ID_RE = re.compile(rf"^([A-Z]{{3}})([{_CATEGORY_IDS}])(\d{{6}})(\d)$")

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
