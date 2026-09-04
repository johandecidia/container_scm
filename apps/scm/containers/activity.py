"""What has happened to the container *record*, as opposed to the container.

The Journey tab answers "what happened to the physical box". This module answers
the other question: what has this platform done with, and learned about, this
object. Two different histories, kept apart on purpose — a carrier discharging a
container and somebody editing its equipment type are not entries in the same list.

**Derived, never stored.** Every entry below is read off a record that already
exists for its own reasons: container movements, tracking sync runs, subscription
and association rows, ETA history, and the container's own timestamps. Nothing
here is written, and there is no activity table.

**What we cannot know, we do not claim.** The consequence of deriving is that this
is not an audit log:

* ``Container.updated_at`` says the row changed. It does not say which field
  changed, from what, or why — there is no field-level history in the schema, so
  an edit entry names the person and the time and stops there.
* Associations are dated by their link row's ``created_at``. A link that was later
  removed left no record behind, so undoing is invisible.
* Gate in and gate out only appear where a ``ContainerMovement`` recorded them.
  There is no gate workflow yet, so for most containers they are genuinely absent
  rather than missing from this view.

A real audit trail is a schema change and belongs to its own piece of work. Until
then this view shows what is true and the template says what it cannot show.

The entry shape itself lives in :mod:`apps.scm.activity`, shared with the other
workspaces that have an Activity tab.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from django.utils.translation import gettext_lazy as _

from apps.scm.activity import ActivityEntry

if TYPE_CHECKING:
    from apps.teams.models import Team

    from .models import Container
    from .workspace import ContainerWorkspace

# One screen of history. A display cap, not a claim about how much happened.
_ACTIVITY_LIMIT = 40

# How far apart `created_at` and `updated_at` must be before the row counts as
# having been edited. `auto_now_add` and `auto_now` are evaluated separately during
# the same insert, so a container that has never been touched still has an
# `updated_at` a few microseconds after its `created_at`. Without a threshold every
# container would report an edit it never had — which is exactly the kind of
# invented history this module exists to avoid.
_EDIT_THRESHOLD = timedelta(seconds=1)


def get_container_activity(team: Team, container: Container, workspace: ContainerWorkspace) -> list[ActivityEntry]:
    """Return this container's record history, newest first.

    Reads what ``workspace`` already loaded — movements, subscriptions, shipment
    links, delivery lines, sync runs — and adds only the ETA history, which the
    workspace does not need. Team-scoped through the workspace and through the one
    query added here.
    """
    entries: list[ActivityEntry] = []
    entries.extend(_movement_entries(workspace))
    entries.extend(_shipment_entries(workspace))
    entries.extend(_delivery_entries(workspace))
    entries.extend(_subscription_entries(workspace))
    entries.extend(_sync_run_entries(workspace))
    entries.extend(_eta_entries(team, container, workspace))
    entries.extend(_record_entries(container))

    entries.sort(key=lambda entry: entry.occurred_at, reverse=True)
    return entries[:_ACTIVITY_LIMIT]


def _movement_entries(workspace: ContainerWorkspace) -> list[ActivityEntry]:
    """Recorded physical movements between our own locations.

    The only place gate movements can appear: they exist here or they did not
    happen as far as this platform knows.
    """
    entries = []
    for movement in workspace.movements:
        origin = movement.from_location.name if movement.from_location_id else ""
        destination = movement.to_location.name if movement.to_location_id else ""
        detail = " → ".join(part for part in (origin, destination) if part)
        entries.append(
            ActivityEntry(
                occurred_at=movement.occurred_at,
                kind="movement",
                title=movement.get_movement_type_display(),
                detail=detail,
                actor=movement.get_source_display() if movement.source else "",
            )
        )
    return entries


def _shipment_entries(workspace: ContainerWorkspace) -> list[ActivityEntry]:
    """When this container was put on a shipment.

    Dated by the link row, which is when we recorded the association — not when the
    box was physically loaded. ``loaded_at`` answers that and is shown as detail
    where the carrier or the operator filled it in.
    """
    entries = []
    for link in workspace.shipment_containers:
        detail = ""
        if link.loaded_at:
            detail = str(_("Loaded %(when)s")) % {"when": link.loaded_at.date().isoformat()}
        entries.append(
            ActivityEntry(
                occurred_at=link.created_at,
                kind="shipment",
                title=_("Added to shipment"),
                detail=" · ".join(part for part in (str(link.shipment), detail) if part),
                url=_shipment_url(link.shipment_id),
            )
        )
    return entries


def _delivery_entries(workspace: ContainerWorkspace) -> list[ActivityEntry]:
    """When this container was linked to a supplier delivery, and so to a PO."""
    entries = []
    for line in workspace.supplier_delivery_lines:
        order_line = line.purchase_order_line if line.purchase_order_line_id else None
        order = order_line.purchase_order if order_line is not None else None
        detail_parts = [line.delivery.delivery_reference]
        if order is not None:
            detail_parts.append(str(_("PO %(number)s")) % {"number": order.po_number})
        entries.append(
            ActivityEntry(
                occurred_at=line.created_at,
                kind="delivery",
                title=_("Linked to supplier delivery"),
                detail=" · ".join(detail_parts),
                url=_delivery_url(line.delivery_id),
            )
        )
    return entries


def _subscription_entries(workspace: ContainerWorkspace) -> list[ActivityEntry]:
    """When a carrier became a tracking source for this container.

    A subscription is only ever created from carrier data, so this is the moment a
    carrier proved it knows the box — worth its own line.
    """
    entries = []
    for subscription in workspace.tracking_subscriptions:
        entries.append(
            ActivityEntry(
                occurred_at=subscription.created_at,
                kind="tracking",
                title=_("Tracking started"),
                detail=" · ".join(
                    part
                    for part in (
                        subscription.provider.name if subscription.provider_id else "",
                        subscription.tracking_reference,
                    )
                    if part
                ),
                url=_tracking_url(subscription.pk),
            )
        )
    return entries


def _sync_run_entries(workspace: ContainerWorkspace) -> list[ActivityEntry]:
    """Each recent attempt to ask a carrier, and what came back.

    SKIPPED runs are kept and labelled as skipped. They are neither a success nor a
    failure, and hiding them would make a container that has never actually been
    asked look like one that was asked and had nothing to report.
    """
    from apps.scm.tracking.models import TrackingSyncRun

    entries = []
    for run in workspace.recent_sync_runs:
        when = run.started_at or run.created_at
        if when is None:
            continue
        detail_parts = [run.provider.name] if run.provider_id else []
        if run.status != TrackingSyncRun.Status.SUCCESS:
            detail_parts.append(run.get_status_display())
        if run.error_type:
            detail_parts.append(run.get_error_type_display())
        entries.append(
            ActivityEntry(
                occurred_at=when,
                kind="refresh",
                title=_("Tracking refreshed"),
                detail=" · ".join(detail_parts),
                actor=str(_("System")),
            )
        )
    return entries


def _eta_entries(team: Team, container: Container, workspace: ContainerWorkspace) -> list[ActivityEntry]:
    """Every recorded move of the arrival forecast.

    ETA history is written against the shipment when there is one and against the
    container when the box is tracked on its own, so both are read here — a
    container's ETA changing is the same event to the reader either way.
    """
    from django.db.models import Q

    from apps.scm.tracking.models import ETAHistory

    shipment = workspace.active_shipment
    scope = Q(container=container)
    if shipment is not None:
        scope |= Q(shipment=shipment)

    entries = []
    for change in ETAHistory.objects.filter(team=team).filter(scope).order_by("-changed_at")[:_ACTIVITY_LIMIT]:
        entries.append(
            ActivityEntry(
                occurred_at=change.changed_at,
                kind="eta",
                title=_("ETA changed"),
                detail=_eta_detail(change),
                actor=change.source,
            )
        )
    return entries


def _eta_detail(change) -> str:
    """ "12 Aug → 4 Sep", or just the new date when there was nothing before it."""
    new = change.new_eta.isoformat() if change.new_eta else ""
    previous = change.previous_eta.isoformat() if change.previous_eta else ""
    if previous and new:
        return f"{previous} → {new}"
    return new or previous


def _record_entries(container: Container) -> list[ActivityEntry]:
    """The container row itself: created, and last edited.

    The edit entry names the time and the person and nothing else, because nothing
    else is recorded. See the module docstring.
    """
    entries = [
        ActivityEntry(
            occurred_at=container.created_at,
            kind="created",
            title=_("Container created"),
            actor=str(container.created_by) if container.created_by_id else "",
        )
    ]
    if container.updated_at and container.updated_at - container.created_at > _EDIT_THRESHOLD:
        entries.append(
            ActivityEntry(
                occurred_at=container.updated_at,
                kind="edited",
                title=_("Container record updated"),
                actor=str(container.updated_by) if container.updated_by_id else "",
            )
        )
    return entries


def _shipment_url(shipment_id) -> str:
    from django.urls import reverse

    return reverse("shipments:detail", kwargs={"pk": shipment_id})


def _delivery_url(delivery_id) -> str:
    from django.urls import reverse

    return reverse("supplier_deliveries:detail", kwargs={"delivery_id": delivery_id})


def _tracking_url(subscription_id) -> str:
    from django.urls import reverse

    return reverse("tracking:detail", kwargs={"pk": subscription_id})
