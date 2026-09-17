"""The exceptions queue: what currently needs operational attention.

Not a log of what has ever happened. An item is here because the supply chain is in
a state somebody has to do something about, and it leaves when that state changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from apps.teams.models import Team

from ..read_models import ObjectKind
from ..selectors import filter_by_eta_window, list_visibility_objects, matches_search
from .choices import issue_choices, text_choices
from .issues import QueueItem, build_queue_item


@dataclass
class ExceptionQueueFilters:
    """The exceptions queue's filter state, parsed once from the query string."""

    issue: str = ""
    carrier: str = ""
    kind: str = ""
    eta_window: str = ""
    search: str = ""

    @property
    def is_active(self) -> bool:
        return bool(self.issue or self.carrier or self.kind or self.eta_window or self.search)


@dataclass
class ExceptionQueue:
    """Everything the exceptions page renders."""

    items: list[QueueItem] = field(default_factory=list)
    filters: ExceptionQueueFilters = field(default_factory=ExceptionQueueFilters)
    carrier_choices: list[str] = field(default_factory=list)
    issue_choices: list[tuple[str, str]] = field(default_factory=list)
    # How many items need attention before any filter — the number the page header
    # and the Control Tower are talking about.
    total: int = 0

    @property
    def kind_choices(self):
        return ObjectKind.choices


def parse_exception_queue_filters(params) -> ExceptionQueueFilters:
    return ExceptionQueueFilters(
        issue=(params.get("issue") or "").strip(),
        carrier=(params.get("carrier") or "").strip(),
        kind=(params.get("kind") or "").strip(),
        eta_window=(params.get("eta") or "").strip(),
        search=(params.get("search") or "").strip(),
    )


def get_exception_queue(team: Team, filters: ExceptionQueueFilters | None = None) -> ExceptionQueue:
    """Return what currently needs operational attention for *team*.

    One pass over the team's visibility objects, so the query count is the overview's
    and does not grow with the number of items in the queue.
    """
    filters = filters or ExceptionQueueFilters()
    objects = list_visibility_objects(team)
    items = [item for item in (build_queue_item(obj) for obj in objects) if item.issues]
    items.sort(key=lambda item: item.sort_key)

    return ExceptionQueue(
        items=_filter_items(items, filters),
        filters=filters,
        carrier_choices=text_choices(
            {item.object.carrier_name for item in items if item.object.carrier_name}, filters.carrier
        ),
        issue_choices=issue_choices(items, filters.issue),
        total=len(items),
    )


def _filter_items(items: list[QueueItem], filters: ExceptionQueueFilters) -> list[QueueItem]:
    result = items
    if filters.issue:
        result = [item for item in result if filters.issue in item.issue_types]
    if filters.carrier:
        result = [item for item in result if item.object.carrier_name == filters.carrier]
    if filters.kind:
        result = [item for item in result if item.object.kind == filters.kind]
    if filters.eta_window:
        # Reuses the Control Tower's window rule, applied to the rows' objects.
        in_window = {obj.key for obj in filter_by_eta_window([item.object for item in result], filters.eta_window)}
        result = [item for item in result if item.key in in_window]
    if filters.search:
        needle = filters.search.lower()
        result = [item for item in result if matches_search(item.object, needle)]
    return result
