"""Server-side list tables: sorting, filter links and chips, read from the query string.

The convention any SCM list can adopt, kept deliberately small. The query string is
the only state — sort, direction, filters and page are plain parameters — so a view
can be linked, bookmarked and walked back through, and an HTMX swap and a full page
load always agree about what is on screen.

Nothing here knows about a model. A list names the sort keys it allows and maps
them to its own expressions; anything else in the URL is ignored rather than
handed to the ORM. Every link built here drops the page, because a different sort
or filter is a different list and its page 4 is not this one's.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

SORT_PARAM = "sort"
DIRECTION_PARAM = "direction"
PAGE_PARAM = "page"
ASC = "asc"
DESC = "desc"


@dataclass(frozen=True)
class TableSort:
    """The sort a request asked for; empty means the list's default order."""

    field: str = ""
    direction: str = ""

    def direction_for(self, field: str) -> str:
        """``asc``, ``desc`` or "" — how *field*'s header should show its arrow."""
        return self.direction if field == self.field else ""


@dataclass(frozen=True)
class FilterChip:
    """One active filter, worded for a reader, with the link that removes it."""

    label: str
    remove_url: str


def parse_sort(params, allowed: Iterable[str]) -> TableSort:
    """The requested sort, if *allowed* names it; otherwise the default order."""
    field = (params.get(SORT_PARAM) or "").strip()
    if field not in set(allowed):
        return TableSort()
    return TableSort(field=field, direction=DESC if params.get(DIRECTION_PARAM) == DESC else ASC)


def sort_url(params, sort: TableSort, field: str) -> str:
    """Where a click on *field*'s header goes: ascending, then descending, then default."""
    if sort.field != field:
        return table_url(params, drop=(SORT_PARAM, DIRECTION_PARAM), **{SORT_PARAM: field, DIRECTION_PARAM: ASC})
    if sort.direction == ASC:
        return table_url(params, drop=(SORT_PARAM, DIRECTION_PARAM), **{SORT_PARAM: field, DIRECTION_PARAM: DESC})
    return table_url(params, drop=(SORT_PARAM, DIRECTION_PARAM))


def table_url(params, *, drop: Iterable[str] = (), **updates: str) -> str:
    """``?query`` for *params* without *drop* or the page, with *updates* set.

    Relative on purpose: the list's own path is kept, so the same helper serves any
    list it is used on. Empty values are left out — a range form sends its blank
    bound, and carrying ``max=`` into every later link is noise.
    """
    query = params.copy()
    for key in (*drop, PAGE_PARAM):
        query.pop(key, None)
    for key in list(query):
        kept = [value for value in query.getlist(key) if value != ""]
        if kept:
            query.setlist(key, kept)
        else:
            del query[key]
    for key, value in updates.items():
        query[key] = value
    encoded = query.urlencode()
    return f"?{encoded}" if encoded else "?"


def hidden_fields(params, drop: Iterable[str] = ()) -> list[tuple[str, str]]:
    """The current parameters a column's filter form must carry along unchanged.

    Everything but that column's own fields and the page, so applying one filter keeps
    the others and the sort, and starts again from the first page.
    """
    dropped = {*drop, PAGE_PARAM}
    return [(key, value) for key, values in params.lists() if key not in dropped for value in values if value != ""]


def parse_decimal(value) -> Decimal | None:
    """A finite number from the query string, or None for anything else."""
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation, ValueError:
        return None
    return number if number.is_finite() else None
