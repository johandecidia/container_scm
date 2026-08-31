"""What has happened to a purchase order *record*.

The same question the container workspace's Activity tab answers, asked of a
purchase order: not what happened to the goods — the progress figures and the
container states answer that — but what this platform has done with, and learned
about, this order.

**Be honest about how thin this is.** ``PurchaseOrderEvent`` exists and is read
first, but nothing in the application writes one today: the model and its service
are in place and no import, sync or view calls them. So for real data the timeline
is built almost entirely from records that exist for other reasons — the order's
own timestamps, its sync clock, the supplier deliveries raised against it and the
containers booked onto those deliveries.

That is deliberate. The alternative is a generic audit log invented for the tab,
which would look complete and be fiction. What is missing here is missing from the
schema, and the template says so.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.utils.translation import gettext_lazy as _

from apps.scm.activity import ActivityEntry

from .models import PurchaseOrderSource

if TYPE_CHECKING:
    from .workspace import PurchaseOrderWorkspace

# One screen of history. A display cap, not a claim about how much happened.
_ACTIVITY_LIMIT = 40


def get_purchase_order_activity(workspace: PurchaseOrderWorkspace) -> list[ActivityEntry]:
    """Return this purchase order's record history, newest first.

    Reads only what ``workspace`` already loaded — the order, its events, its
    deliveries and its container rows — so the Activity tab adds no queries to the
    page. Team-scoped through the workspace.
    """
    entries: list[ActivityEntry] = []
    entries.extend(_event_entries(workspace))
    entries.extend(_delivery_entries(workspace))
    entries.extend(_sync_entries(workspace))
    entries.extend(_record_entries(workspace))

    entries.sort(key=lambda entry: entry.occurred_at, reverse=True)
    return entries[:_ACTIVITY_LIMIT]


def _event_entries(workspace: PurchaseOrderWorkspace) -> list[ActivityEntry]:
    """The order's own timeline events, where any have been recorded."""
    return [
        ActivityEntry(
            occurred_at=event.timestamp,
            kind=_EVENT_KINDS.get(event.event_type, "event"),
            title=event.get_event_type_display(),
            detail=event.description,
        )
        for event in workspace.events
    ]


_EVENT_KINDS = {
    "CREATED": "created",
    "PARTIALLY_SHIPPED": "shipment",
    "FULLY_SHIPPED": "shipment",
    "LOADED": "shipment",
    "ARRIVED": "arrival",
    "RECEIVED": "received",
}


def _delivery_entries(workspace: PurchaseOrderWorkspace) -> list[ActivityEntry]:
    """When each supplier delivery was raised against this order, and what is on it.

    Dated by the delivery's ``created_at`` — when the batch was planned in SCM, not
    when it shipped. Its ship and arrival dates are shown as detail where they are
    recorded, so the two are never confused.
    """
    entries = []
    for row in workspace.delivery_rows:
        delivery = row.delivery
        detail_parts = [str(delivery.get_status_display())]
        if row.container_count:
            detail_parts.append(
                str(_("%(count)s containers")) % {"count": row.container_count}
                if row.container_count != 1
                else str(_("1 container"))
            )
        if row.ship_date:
            key = _("Shipped %(when)s") if row.ship_date_is_actual else _("Ship %(when)s")
            detail_parts.append(str(key) % {"when": row.ship_date.isoformat()})
        entries.append(
            ActivityEntry(
                occurred_at=delivery.created_at,
                kind="delivery",
                title=_("Supplier delivery %(reference)s") % {"reference": delivery.delivery_reference},
                detail=" · ".join(detail_parts),
                url=_delivery_url(delivery.pk),
            )
        )
    return entries


def _sync_entries(workspace: PurchaseOrderWorkspace) -> list[ActivityEntry]:
    """When the source system last changed this order, and when we last read it.

    Two different facts and two different lines. ``source_last_modified_at`` is the
    source system's own clock; ``last_synced_at`` is when SCM last asked. A manual
    order has neither, and gets neither line.
    """
    order = workspace.purchase_order
    if order.source_system == PurchaseOrderSource.MANUAL:
        return []

    entries = []
    if order.source_last_modified_at:
        entries.append(
            ActivityEntry(
                occurred_at=order.source_last_modified_at,
                kind="source",
                title=_("Changed in %(source)s") % {"source": order.get_source_system_display()},
                actor=order.get_source_system_display(),
            )
        )
    if order.last_synced_at:
        entries.append(
            ActivityEntry(
                occurred_at=order.last_synced_at,
                kind="refresh",
                title=_("Synced from %(source)s") % {"source": order.get_source_system_display()},
                actor=str(_("System")),
            )
        )
    return entries


def _record_entries(workspace: PurchaseOrderWorkspace) -> list[ActivityEntry]:
    """The order row itself: when SCM first recorded it.

    There is no updated entry. ``updated_at`` moves on every sync — including one
    that found nothing changed, which touches ``last_synced_at`` by design — so an
    "order updated" line would report edits that never happened. The sync line above
    already says when we last read the source.
    """
    order = workspace.purchase_order
    return [
        ActivityEntry(
            occurred_at=order.created_at,
            kind="created",
            title=_("Purchase order recorded in SCM"),
            actor=order.get_source_system_display(),
        )
    ]


def _delivery_url(delivery_id) -> str:
    from django.urls import reverse

    return reverse("supplier_deliveries:detail", kwargs={"delivery_id": delivery_id})
