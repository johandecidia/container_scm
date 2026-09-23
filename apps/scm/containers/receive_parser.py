"""Reading a pasted gate-in report into rows. No database, no decisions.

The report this is shaped for is John Evans' receive list, copied from its web page
as a Markdown-style table or out of a spreadsheet as tab-separated text:

.. code-block:: text

    | Prefix | Unit Number | Iso Size | Iso Type | Gate Date Time In | Site |
    | PSLU | 2913030 | 22 | 10 | 2026-09-17 16:00:51 | MCR AB - Oceanterminalen |

Each data line becomes a :class:`ReceiveRow`. What that row *means* — which
container, whether it is already received, what Confirm would do — is
:mod:`apps.scm.containers.receive`'s question, not this module's.

**Noise is ignored, malformed rows are not.** A copied page brings its header,
navigation and "Support" footer with it, and none of that is an error. A line whose
first cell is shaped like an ISO 6346 owner prefix and whose second starts with a
digit is a data row, though, and if the rest of it cannot be read it is reported
rather than silently dropped — a receive
list that loses a box without saying so is worse than one that refuses it.

The container number is validated by the intake module's own ISO 6346 check, so a
number accepted here is one every other intake path would accept.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext_lazy as _

from .choices import ContainerCategory
from .intake import normalise_container_number, parse_and_validate_container_number

# An owner code and a category letter: "PSLU". What makes a line a data row.
_PREFIX_RE = re.compile(rf"^[A-Z]{{3}}[{''.join(ContainerCategory.values)}]$", re.IGNORECASE)
# A Markdown table's header rule: "|---|:---:|".
_RULE_RE = re.compile(r"^[\s|:\-]+$")
# Spreadsheet text arrives tab-separated; text re-typed or re-rendered often turns
# the tabs into runs of spaces. Two or more, so the single space inside
# "2026-09-17 16:00:51" stays part of its cell.
_WIDE_GAP_RE = re.compile(r"\t| {2,}")
_WHITESPACE_RE = re.compile(r"\s+")

# The report's own column names, normalised, and the field each one fills.
_HEADER_FIELDS = {
    "prefix": "prefix",
    "unit number": "unit_number",
    "iso size": "iso_size",
    "iso type": "iso_type",
    "gate date time in": "occurred_at",
    "site": "source_site",
}
# The column order when a paste carries no header row.
_DEFAULT_COLUMNS = {"prefix": 0, "unit_number": 1, "iso_size": 2, "iso_type": 3, "occurred_at": 4, "source_site": 5}


@dataclass(frozen=True)
class ReceiveRow:
    """One line of a receive report, read and normalised."""

    line_number: int
    container_number: str
    parts: dict
    occurred_at: datetime
    iso_size: str = ""
    iso_type: str = ""
    source_site: str = ""

    @property
    def iso_code(self) -> str:
        """The size and type as one ISO 6346 code, "22G1", or "" when either is missing."""
        if not (self.iso_size and self.iso_type):
            return ""
        return f"{self.iso_size}{self.iso_type}".upper()


@dataclass(frozen=True)
class ReceiveParseError:
    """A line that looked like a data row and could not be read."""

    line_number: int
    raw: str
    error: str
    container_number: str = ""


@dataclass(frozen=True)
class ReceiveParseResult:
    rows: list[ReceiveRow] = field(default_factory=list)
    errors: list[ReceiveParseError] = field(default_factory=list)


def split_cells(line: str) -> list[str]:
    """Split one line into trimmed cells, as a Markdown table row or as TSV."""
    stripped = line.strip()
    if "|" in stripped:
        return [cell.strip() for cell in stripped.strip("|").split("|")]
    return [cell.strip() for cell in _WIDE_GAP_RE.split(stripped) if cell.strip()]


def _header_columns(cells: list[str]) -> dict[str, int] | None:
    """The column positions a header row names, or None when this is not a header."""
    names = [_WHITESPACE_RE.sub(" ", cell).strip().lower() for cell in cells]
    if "prefix" not in names or "unit number" not in names:
        return None
    return {_HEADER_FIELDS[name]: index for index, name in enumerate(names) if name in _HEADER_FIELDS}


def parse_occurred_at(value: str) -> datetime | None:
    """Read the report's gate-in time, in the active timezone when it carries none."""
    try:
        parsed = parse_datetime(value.strip()) if value else None
    except ValueError:  # Well-formed but impossible: "2026-13-45 10:00:00".
        return None
    if parsed is None:
        return None
    return timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed


def parse_receive_text(text: str) -> ReceiveParseResult:
    """Read every data row in *text*, and report every data row that cannot be read."""
    rows: list[ReceiveRow] = []
    errors: list[ReceiveParseError] = []
    columns = dict(_DEFAULT_COLUMNS)

    for line_number, line in enumerate((text or "").splitlines(), start=1):
        if not line.strip() or _RULE_RE.match(line):
            continue
        cells = split_cells(line)
        if (header := _header_columns(cells)) is not None:
            # A column the header does not name is absent, not in its default position.
            columns = {name: header.get(name, -1) for name in _DEFAULT_COLUMNS}
            continue

        prefix = _cell(cells, columns["prefix"])
        if not _PREFIX_RE.match(prefix) or not _cell(cells, columns["unit_number"])[:1].isdigit():
            continue  # Not a data row: page chrome, a footer ("Menu | Home"), a stray sentence.

        parsed = _parse_row(line_number, line, cells, columns)
        (rows if isinstance(parsed, ReceiveRow) else errors).append(parsed)
    return ReceiveParseResult(rows=rows, errors=errors)


def _parse_row(line_number: int, line: str, cells: list[str], columns: dict[str, int]):
    number = normalise_container_number(_cell(cells, columns["prefix"]) + _cell(cells, columns["unit_number"]))

    def error(message) -> ReceiveParseError:
        return ReceiveParseError(line_number=line_number, raw=line.strip(), error=str(message), container_number=number)

    try:
        parts = parse_and_validate_container_number(number)
    except ValidationError as exc:
        return error(" ".join(exc.messages))

    raw_time = _cell(cells, columns["occurred_at"])
    occurred_at = parse_occurred_at(raw_time)
    if occurred_at is None:
        if not raw_time:
            return error(_("No gate-in time."))
        return error(_("Gate-in time '%(value)s' is not a date and time.") % {"value": raw_time})

    return ReceiveRow(
        line_number=line_number,
        container_number=number,
        parts=parts,
        occurred_at=occurred_at,
        iso_size=_cell(cells, columns["iso_size"]),
        iso_type=_cell(cells, columns["iso_type"]),
        source_site=_cell(cells, columns["source_site"]),
    )


def _cell(cells: list[str], index: int) -> str:
    return cells[index] if 0 <= index < len(cells) else ""
