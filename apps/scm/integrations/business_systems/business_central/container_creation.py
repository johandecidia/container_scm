"""Controlled creation of containers from Business Central item ledger entries (operation B).

Nothing here runs on its own: no sync or matching pass creates a container. A Team
Administrator reviews candidates and approves specific serial numbers, and only
when the integration's creation mode is ``create_approved``.

A serial number is a candidate only if, re-checked at approval time:

  - it is a valid ISO 6346 number (entries already classified ``VALID_ISO``);
  - at least one entry is ``container_not_found`` — a container item by explicit
    rule, with a mapped equipment type, and no container in the team;
  - all such entries map to exactly one equipment type (otherwise ambiguous).

Creation goes through ``containers.intake.create_or_get_container`` with the mapped
equipment type and nothing else: no carrier (so no tracking), no location (so no
movement), status left to the model default. An existing container — including
one created by a concurrent approval — is reused, never changed. The entries are
then re-matched so they link to it.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.scm.containers.intake import create_or_get_container
from apps.scm.containers.models import EquipmentType
from apps.scm.integrations.models import BusinessCentralItemLedgerEntry, Integration, IntegrationSyncRun
from apps.users.models import CustomUser

from .container_links import match_item_ledger_entries
from .exceptions import BusinessCentralConfigurationError
from .item_rules import ItemRuleSet, entry_variant_code
from .resources import get_resource_config

Entry = BusinessCentralItemLedgerEntry

MATCH_ONLY = "match_only"
REVIEW = "review"
CREATE_APPROVED = "create_approved"
CREATION_MODES = [
    (MATCH_ONLY, _("Match existing containers only")),
    (REVIEW, _("Review creation candidates")),
    (CREATE_APPROVED, _("Create approved containers")),
]
_CONFIG_KEY = "container_creation"
_ILE = IntegrationSyncRun.ResourceType.ITEM_LEDGER_ENTRIES


@dataclass
class Candidate:
    serial_no: str
    entries: int = 0
    item_nos: set[str] = field(default_factory=set)
    equipment_types: dict[str, EquipmentType] = field(default_factory=dict)
    last_posting_date: date | None = None
    last_document_no: str = ""

    @property
    def equipment_type(self) -> EquipmentType | None:
        """The single mapped type, or None when the entries disagree (ambiguous)."""
        return next(iter(self.equipment_types.values())) if len(self.equipment_types) == 1 else None

    @property
    def ambiguous(self) -> bool:
        return len(self.equipment_types) > 1


@dataclass
class CreationResult:
    created: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    refused: dict[str, str] = field(default_factory=dict)


def get_container_creation_mode(integration: Integration) -> str:
    """The integration's creation mode; anything unknown or absent is ``match_only``."""
    mode = get_resource_config(integration, _ILE).get(_CONFIG_KEY)
    return mode if mode in dict(CREATION_MODES) else MATCH_ONLY


def set_container_creation_mode(integration: Integration, mode: str) -> None:
    if mode not in dict(CREATION_MODES):
        raise ValueError(f"unknown container creation mode: {mode!r}")
    config = dict(integration.config or {})
    resources = dict(config.get("resources") or {})
    resources[_ILE] = {**dict(resources.get(_ILE) or {}), _CONFIG_KEY: mode}
    config["resources"] = resources
    integration.config = config
    integration.save(update_fields=["config", "updated_at"])


def get_creation_candidates(
    integration: Integration, *, serial_numbers: Iterable[str] | None = None
) -> list[Candidate]:
    """Serial numbers that could become containers, most recently posted first.

    Built from entries the last matching pass marked ``container_not_found``, with
    the equipment type resolved from the *current* rules.
    """
    entries = Entry.objects.filter(
        integration=integration,
        link_status=Entry.LinkStatus.CONTAINER_NOT_FOUND,
        serial_status=Entry.SerialStatus.VALID_ISO,
    ).only("serial_no", "item_no", "posting_date", "document_no", "raw_payload")
    if serial_numbers is not None:
        entries = entries.filter(serial_no__in=list(serial_numbers))
    rules = ItemRuleSet.for_integration(integration)
    candidates: dict[str, Candidate] = defaultdict(lambda: Candidate(serial_no=""))
    for entry in entries.iterator(chunk_size=1000):
        candidate = candidates[entry.serial_no]
        candidate.serial_no = entry.serial_no
        candidate.entries += 1
        candidate.item_nos.add(entry.item_no)
        resolution = rules.resolve(entry.item_no, entry_variant_code(entry))
        if resolution.classification == Entry.ItemClassification.CONTAINER and resolution.equipment_type:
            candidate.equipment_types[resolution.equipment_type.pk] = resolution.equipment_type
        if entry.posting_date and (
            candidate.last_posting_date is None or entry.posting_date > candidate.last_posting_date
        ):
            candidate.last_posting_date, candidate.last_document_no = entry.posting_date, entry.document_no
    # Rules may have changed since the last pass: a candidate needs a type now.
    found = [c for c in candidates.values() if c.equipment_types]
    return sorted(found, key=lambda c: (c.last_posting_date or date.min, c.serial_no), reverse=True)


def create_approved_containers(
    integration: Integration, serial_numbers: Iterable[str], user: CustomUser
) -> CreationResult:
    """Create containers for the approved ``serial_numbers`` that are still valid candidates.

    Raises BusinessCentralConfigurationError unless the creation mode is
    ``create_approved``. Each serial is re-checked; refused ones are reported with
    the reason and nothing is created for them.
    """
    if get_container_creation_mode(integration) != CREATE_APPROVED:
        raise BusinessCentralConfigurationError("container creation is not enabled for this integration")
    requested = sorted({s.strip().upper() for s in serial_numbers if s and s.strip()})
    result = CreationResult()
    if not requested:
        return result

    # Re-check against the current rules and containers before creating anything.
    match_item_ledger_entries(integration, serial_numbers=requested)
    candidates = {c.serial_no: c for c in get_creation_candidates(integration, serial_numbers=requested)}
    approved: list[str] = []
    for serial_no in requested:
        candidate = candidates.get(serial_no)
        if candidate is None:
            result.refused[serial_no] = str(_("not a creation candidate (already linked, or no longer qualifies)"))
            continue
        if candidate.equipment_type is None:
            result.refused[serial_no] = str(_("the entries map to different equipment types"))
            continue
        try:
            _container, created = create_or_get_container(
                team=integration.team,
                user=user,
                number=serial_no,
                attributes={"equipment_type": candidate.equipment_type},
            )
        except ValidationError as exc:
            result.refused[serial_no] = " ".join(exc.messages)
            continue
        (result.created if created else result.existing).append(serial_no)
        approved.append(serial_no)

    if approved:
        match_item_ledger_entries(integration, serial_numbers=approved)
    return result
