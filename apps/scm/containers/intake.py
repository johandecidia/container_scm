"""Container intake — the one path from a container number to a Container row.

Typing one number, pasting a column out of Excel and uploading a CSV all end up
here, so a number accepted by one of them is accepted by all three and none of
them can drift into its own idea of what a valid container is.

Nothing about ISO 6346 is re-implemented: the shape and the check digit stay in
:mod:`apps.scm.containers.utils`, and writes stay in
:func:`apps.scm.containers.services.create_container`. What this module adds is
what those two do not cover — normalising messy input, telling *new* from
*already exists* before anything is written, and surviving a bad row without
losing the good ones.

Carrier is optional and never inferred: an ISO owner prefix says who owns the box,
not who is moving it. When one is chosen it is recorded as the carrier to *ask* —
not as one that tracks the box, which only the carrier's own data can establish.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils.translation import gettext_lazy as _

from apps.teams.models import Team
from apps.users.models import CustomUser

from .models import Container
from .selectors import get_default_equipment_type
from .services import create_container
from .utils import parse_container_id, validate_container_id

# Separators accepted in a pasted list: newlines, commas, semicolons, tabs and
# plain spaces, so a column copied straight out of Excel needs no cleaning up.
_SEPARATOR_RE = re.compile(r"[\s,;]+")
_WHITESPACE_RE = re.compile(r"\s+")

CSV_NUMBER_COLUMN = "container_number"
CSV_CARRIER_COLUMN = "carrier"

# Row outcomes, shared by the preview and the import result so both read the same.
NEW = "new"
EXISTS = "exists"
INVALID = "invalid"


def normalise_container_number(raw: str) -> str:
    """Return ``raw`` trimmed, stripped of inner whitespace and uppercased."""
    return _WHITESPACE_RE.sub("", raw or "").upper()


def parse_and_validate_container_number(raw: str) -> dict:
    """Return the four ISO 6346 parts of ``raw``, or raise ValidationError.

    The single, paste and CSV paths all go through this, so "valid" means exactly
    one thing across the feature.
    """
    parts = parse_container_id(normalise_container_number(raw))
    validate_container_id(
        parts["owner_code"],
        parts["category_id"],
        parts["serial_number"],
        parts["check_digit"],
    )
    return parts


def split_container_numbers(text: str) -> list[str]:
    """Split pasted text into unique, normalised container numbers, order preserved."""
    numbers: list[str] = []
    seen: set[str] = set()
    for token in _SEPARATOR_RE.split(text or ""):
        number = normalise_container_number(token)
        if number and number not in seen:
            seen.add(number)
            numbers.append(number)
    return numbers


def entries_from_text(text: str, carrier: str = "") -> list[tuple[str, str]]:
    """Turn pasted text plus one chosen carrier into (number, carrier) entries."""
    return [(number, carrier) for number in split_container_numbers(text)]


def entries_from_csv(file_obj) -> list[tuple[str, str]]:
    """Turn an uploaded CSV into (number, carrier) entries.

    Requires a ``container_number`` column; ``carrier`` is optional. Reuses the
    import app's CSV reader rather than opening a second one.
    """
    from apps.scm.imports.parsers import parse_csv_rows

    try:
        rows = parse_csv_rows(file_obj)
    except UnicodeDecodeError:
        raise ValidationError(_("The file could not be read as text. Save it as UTF-8 CSV and try again.")) from None

    if not rows:
        raise ValidationError(_("The file contains no rows."))

    if not any(CSV_NUMBER_COLUMN in {key.strip().lower() for key in row} for row in rows[:1]):
        raise ValidationError(
            _("The CSV needs a '%(column)s' column.") % {"column": CSV_NUMBER_COLUMN},
        )

    entries: list[tuple[str, str]] = []
    for row in rows:
        lowered = {key.strip().lower(): (value or "") for key, value in row.items() if key}
        number = normalise_container_number(lowered.get(CSV_NUMBER_COLUMN, ""))
        if not number:
            continue
        entries.append((number, lowered.get(CSV_CARRIER_COLUMN, "").strip()))
    return entries


@dataclass(frozen=True)
class IntakeRow:
    """One container number and what would happen to it."""

    number: str
    state: str
    carrier: str = ""
    error: str = ""
    parts: dict | None = None

    @property
    def is_new(self) -> bool:
        return self.state == NEW

    @property
    def is_invalid(self) -> bool:
        return self.state == INVALID


@dataclass(frozen=True)
class IntakePreview:
    """What a paste or a CSV would do, before anything is written."""

    rows: list[IntakeRow] = field(default_factory=list)

    @property
    def new_rows(self) -> list[IntakeRow]:
        return [row for row in self.rows if row.state == NEW]

    @property
    def new_count(self) -> int:
        return len(self.new_rows)

    @property
    def existing_count(self) -> int:
        return sum(1 for row in self.rows if row.state == EXISTS)

    @property
    def invalid_count(self) -> int:
        return sum(1 for row in self.rows if row.state == INVALID)

    @property
    def total(self) -> int:
        return len(self.rows)


@dataclass(frozen=True)
class IntakeResult:
    """What an import actually did."""

    created: list[str] = field(default_factory=list)
    existed: list[str] = field(default_factory=list)
    invalid: list[IntakeRow] = field(default_factory=list)

    @property
    def created_count(self) -> int:
        return len(self.created)

    @property
    def existed_count(self) -> int:
        return len(self.existed)

    @property
    def invalid_count(self) -> int:
        return len(self.invalid)


def preview_containers(*, team: Team, entries: list[tuple[str, str]]) -> IntakePreview:
    """Classify each entry as new, already existing or invalid, without writing.

    Duplicates within the input collapse to a single row, so a pasted list that
    repeats a number does not offer to create it twice.
    """
    rows: list[IntakeRow] = []
    seen: set[str] = set()
    parsed: list[tuple[str, str, dict]] = []

    for raw, carrier in entries:
        number = normalise_container_number(raw)
        if not number or number in seen:
            continue
        seen.add(number)
        try:
            parts = parse_and_validate_container_number(number)
        except ValidationError as exc:
            rows.append(IntakeRow(number=number, state=INVALID, carrier=carrier, error=" ".join(exc.messages)))
            continue
        parsed.append((number, carrier, parts))
        rows.append(IntakeRow(number=number, state=NEW, carrier=carrier, parts=parts))

    existing = _existing_keys(team, [parts for _, _, parts in parsed])
    if existing:
        rows = [
            IntakeRow(number=row.number, state=EXISTS, carrier=row.carrier, parts=row.parts)
            if row.parts is not None and _key(row.parts) in existing
            else row
            for row in rows
        ]
    return IntakePreview(rows=rows)


def create_or_get_container(
    *,
    team: Team,
    user: CustomUser,
    number: str,
    carrier: str = "",
) -> tuple[Container, bool]:
    """Return this team's container for ``number``, creating it if it is new.

    Raises ValidationError for a number that is not a valid ISO 6346 ID, and when
    no equipment type is configured to fall back on.
    """
    parts = parse_and_validate_container_number(number)
    lookup = {
        "team": team,
        "owner_code": parts["owner_code"],
        "category_id": parts["category_id"],
        "serial_number": parts["serial_number"],
    }

    container = Container.objects.filter(**lookup).first()
    created = False
    if container is None:
        equipment_type = get_default_equipment_type()
        if equipment_type is None:
            raise ValidationError(_("No equipment types are configured, so containers cannot be created yet."))
        try:
            # Its own transaction: a number that lost a race must not poison a
            # surrounding bulk import.
            with transaction.atomic():
                container = create_container(
                    team=team,
                    user=user,
                    # status and condition are left to the model defaults
                    # (Available / Good) — quick registration asks for neither.
                    data={**parts, "equipment_type": equipment_type},
                )
            created = True
        except IntegrityError:
            container = Container.objects.get(**lookup)

    if carrier:
        link_container_carrier(team=team, container=container, carrier=carrier)
    return container, created


def bulk_create_containers(*, team: Team, user: CustomUser, entries: list[tuple[str, str]]) -> IntakeResult:
    """Create every valid, new container in ``entries`` and report what happened.

    Duplicates — inside the input or against the team's containers — are counted,
    not created, and one bad entry never stops the rest.
    """
    preview = preview_containers(team=team, entries=entries)
    created: list[str] = []
    existed: list[str] = []
    invalid: list[IntakeRow] = [row for row in preview.rows if row.state == INVALID]

    for row in preview.rows:
        if row.state == INVALID:
            continue
        try:
            _, was_created = create_or_get_container(team=team, user=user, number=row.number, carrier=row.carrier)
        except ValidationError as exc:
            invalid.append(
                IntakeRow(number=row.number, state=INVALID, carrier=row.carrier, error=" ".join(exc.messages))
            )
            continue
        (created if was_created else existed).append(row.number)

    return IntakeResult(created=created, existed=existed, invalid=invalid)


def link_container_carrier(*, team: Team, container: Container, carrier: str) -> None:
    """Record a manually chosen carrier as the one to ask about a container.

    Container has no carrier column and does not need one. The choice is kept as a
    planned-container entry, which already means exactly this — "this number, at
    this carrier, not confirmed there yet" — so "Refresh tracking" on the container
    knows who to ask, and discovery keeps looking in the background.

    Deliberately *not* a TrackingSubscription: that asserts a carrier is tracking
    the box, which only the carrier's own data can establish. Someone typing
    "Maersk" into a form has not made Maersk answer. An unrecognised carrier is
    ignored rather than guessed at.
    """
    from apps.scm.integrations.carriers.registry import resolve_carrier_code

    from .discovery import add_planned_container

    code = resolve_carrier_code(carrier)
    if not code:
        return
    planned = add_planned_container(team=team, container_number=container.container_id, carrier=code)
    if planned.container_id != container.pk:
        planned.container = container
        planned.save(update_fields=["container", "updated_at"])


def carrier_choices() -> list[tuple[str, str]]:
    """Return the optional carrier choices for intake forms, from the carrier registry."""
    from apps.scm.integrations.carriers.registry import list_carriers

    return [("", _("— No carrier —"))] + [
        (definition.provider_code, definition.name) for definition in sorted(list_carriers(), key=lambda d: d.name)
    ]


def _key(parts: dict) -> tuple[str, str, str]:
    return (parts["owner_code"], parts["category_id"], parts["serial_number"])


def _existing_keys(team: Team, parts_list: list[dict]) -> set[tuple[str, str, str]]:
    """Return the keys of the team's containers that already cover ``parts_list``."""
    if not parts_list:
        return set()
    serials = {parts["serial_number"] for parts in parts_list}
    wanted = {_key(parts) for parts in parts_list}
    found = Container.objects.filter(team=team, serial_number__in=serials).values_list(
        "owner_code", "category_id", "serial_number"
    )
    return {key for key in found if key in wanted}
