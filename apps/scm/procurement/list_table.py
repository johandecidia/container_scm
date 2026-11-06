"""The purchase order list's table state: what the query string asks for, validated.

Parsing, chips and column headers for :func:`~apps.scm.procurement.selectors.filter_purchase_orders`.
The generic half — sort cycling, links that keep the other parameters, hidden
fields — is :mod:`apps.scm.tables`; this module only says which columns there are
and what each one filters on.

Every value is checked against an explicit allowance: a status or source that is not
one of the model's choices, a sort key that is not a column, a bound that is not a
number — each is dropped, so a hand-edited URL shows a wider list, never an error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from django.utils.translation import gettext as _

from apps.scm.tables import (
    DIRECTION_PARAM,
    SORT_PARAM,
    FilterChip,
    TableSort,
    hidden_fields,
    parse_decimal,
    parse_sort,
    sort_url,
    table_url,
)

from .models import PurchaseOrderSource, PurchaseOrderStatus
from .selectors import PURCHASE_ORDER_SORTS

# Free-text filters are substring matches; anything longer than this is not a PO
# number or a supplier anybody typed.
MAX_TEXT_LENGTH = 100

RANGE_COLUMNS = ("ordered", "shipped", "received", "remaining")


@dataclass(frozen=True)
class PurchaseOrderListFilters:
    po: str = ""
    supplier: str = ""
    statuses: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    # column -> (minimum, maximum); either bound may be None.
    ranges: dict[str, tuple[Decimal | None, Decimal | None]] = field(default_factory=dict)
    sort: TableSort = field(default_factory=TableSort)

    @property
    def is_active(self) -> bool:
        """True when anything narrows the list. Sorting only reorders it."""
        return bool(self.po or self.supplier or self.statuses or self.sources or self.ranges)


def parse_purchase_order_list_filters(params) -> PurchaseOrderListFilters:
    ranges = {}
    for column in RANGE_COLUMNS:
        low, high = parse_decimal(params.get(f"{column}_min")), parse_decimal(params.get(f"{column}_max"))
        if low is not None or high is not None:
            ranges[column] = (low, high)
    return PurchaseOrderListFilters(
        po=_text(params.get("po")),
        supplier=_text(params.get("supplier")),
        statuses=_choices(params.getlist("status"), PurchaseOrderStatus.values),
        sources=_choices(params.getlist("source"), PurchaseOrderSource.values),
        ranges=ranges,
        sort=parse_sort(params, PURCHASE_ORDER_SORTS),
    )


def filter_chips(filters: PurchaseOrderListFilters, params) -> list[FilterChip]:
    """One chip per active filter, each removing only its own parameters."""
    chips = []
    if filters.po:
        chips.append(FilterChip(_("PO: %(value)s") % {"value": filters.po}, table_url(params, drop=("po",))))
    if filters.supplier:
        chips.append(
            FilterChip(_("Supplier: %(value)s") % {"value": filters.supplier}, table_url(params, drop=("supplier",)))
        )
    if filters.statuses:
        labels = ", ".join(str(PurchaseOrderStatus(value).label) for value in filters.statuses)
        chips.append(FilterChip(_("Status: %(value)s") % {"value": labels}, table_url(params, drop=("status",))))
    if filters.sources:
        labels = ", ".join(str(PurchaseOrderSource(value).label) for value in filters.sources)
        chips.append(FilterChip(_("Source: %(value)s") % {"value": labels}, table_url(params, drop=("source",))))
    for column, (low, high) in filters.ranges.items():
        label = str(_column_labels()[column])
        if low is not None and high is not None:
            text = f"{label} {_number(low)}–{_number(high)}"
        elif low is not None:
            text = f"{label} ≥ {_number(low)}"
        elif high is not None:
            text = f"{label} ≤ {_number(high)}"
        else:  # parse_purchase_order_list_filters only keeps a range with a bound
            continue
        chips.append(FilterChip(text, table_url(params, drop=(f"{column}_min", f"{column}_max"))))
    return chips


def clear_all_url(params) -> str:
    """Every filter gone; the sort stays, because it is not a filter."""
    keep = {key: params[key] for key in (SORT_PARAM, DIRECTION_PARAM) if params.get(key)}
    return table_url(params.__class__(""), **keep)


def column_headers(filters: PurchaseOrderListFilters, params) -> dict[str, dict]:
    """What each header cell needs: label, sort link and arrow, and its filter form."""
    labels = _column_labels()
    headers = {
        "po_number": _header(
            "po_number",
            labels["po_number"],
            filters,
            params,
            own=("po", "source"),
            text=("po", filters.po, _("PO number contains…")),
            choices=("source", _options(PurchaseOrderSource.choices, filters.sources)),
            active=bool(filters.po or filters.sources),
        ),
        "supplier": _header(
            "supplier",
            labels["supplier"],
            filters,
            params,
            own=("supplier",),
            text=("supplier", filters.supplier, _("Supplier contains…")),
            active=bool(filters.supplier),
        ),
        "status": _header(
            "status",
            labels["status"],
            filters,
            params,
            own=("status",),
            choices=("status", _options(PurchaseOrderStatus.choices, filters.statuses)),
            active=bool(filters.statuses),
        ),
    }
    for column in RANGE_COLUMNS:
        low, high = filters.ranges.get(column, (None, None))
        headers[column] = _header(
            column,
            labels[column],
            filters,
            params,
            own=(f"{column}_min", f"{column}_max"),
            range_=(column, _number(low) if low is not None else "", _number(high) if high is not None else ""),
            active=column in filters.ranges,
            numeric=True,
        )
    return headers


def _header(key, label, filters, params, *, own, text=None, choices=None, range_=None, active, numeric=False):
    return {
        "key": key,
        "label": label,
        "sort_url": sort_url(params, filters.sort, key),
        "direction": filters.sort.direction_for(key),
        "numeric": numeric,
        "active": active,
        "text": text,
        "choices": choices,
        "range": range_,
        "hidden": hidden_fields(params, drop=own),
        "clear_url": table_url(params, drop=own),
    }


def _column_labels() -> dict[str, str]:
    return {
        "po_number": _("PO Number"),
        "supplier": _("Supplier"),
        "status": _("Status"),
        "ordered": _("Ordered"),
        "shipped": _("Shipped"),
        "received": _("Received"),
        "remaining": _("Remaining"),
    }


def _options(choices, selected) -> list[tuple[str, str, bool]]:
    return [(value, str(label), value in selected) for value, label in choices]


def _text(value) -> str:
    return (value or "").strip()[:MAX_TEXT_LENGTH]


def _choices(values, allowed) -> tuple[str, ...]:
    """The allowed values among *values*, in the model's own order, each once."""
    wanted = set(values)
    return tuple(value for value in allowed if value in wanted)


def _number(value: Decimal) -> str:
    """A bound as somebody would type it: 5 rather than 5.000, 2.5 rather than 2.50."""
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text
