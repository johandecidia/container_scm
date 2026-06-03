# Shipment services — all business logic and write operations.
# Views must not contain business logic; call these functions instead.
from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

if TYPE_CHECKING:
    from django_stubs_ext import StrOrPromise

from apps.scm.audit_log.models import SCMAuditLog
from apps.scm.audit_log.services import log_scm_action
from apps.scm.containers.models import Container
from apps.teams.models import Team
from apps.users.models import CustomUser

from .models import Shipment, ShipmentContainer, ShipmentEvent


def create_shipment(team: Team, user: CustomUser, data: dict) -> Shipment:
    """Create a new shipment and record a CREATED timeline event."""
    shipment = Shipment.objects.create(team=team, created_by=user, **data)
    create_shipment_event(
        shipment=shipment,
        event_type=ShipmentEvent.EventType.CREATED,
        description=_("Shipment created."),
        user=user,
    )
    log_scm_action(
        team=team,
        action=SCMAuditLog.Action.SHIPMENT_CREATED,
        object_type="Shipment",
        object_id=str(shipment.pk),
        object_repr=str(shipment),
        metadata={"status": shipment.status},
        actor=user,
    )
    return shipment


def update_shipment(shipment: Shipment, user: CustomUser, data: dict) -> Shipment:
    """Update shipment fields.

    Status changes must go through change_shipment_status instead.
    ETA changes automatically record an ETA_UPDATED timeline event.
    """
    data = {k: v for k, v in data.items() if k != "status"}

    old_eta = shipment.eta
    for field, value in data.items():
        setattr(shipment, field, value)
    shipment.save()

    if "eta" in data and data["eta"] != old_eta:
        create_shipment_event(
            shipment=shipment,
            event_type=ShipmentEvent.EventType.ETA_UPDATED,
            description=_("ETA updated."),
            user=user,
            metadata={"eta": str(data["eta"]) if data.get("eta") else None},
        )

    return shipment


def change_shipment_status(shipment: Shipment, user: CustomUser, new_status: str) -> Shipment:
    """Change shipment status and record a STATUS_CHANGED (or terminal) event."""
    old_status = shipment.status
    if old_status == new_status:
        return shipment

    shipment.status = new_status
    shipment.save(update_fields=["status", "updated_at"])

    if new_status == Shipment.Status.DELIVERED:
        event_type = ShipmentEvent.EventType.DELIVERED
    elif new_status == Shipment.Status.CANCELLED:
        event_type = ShipmentEvent.EventType.CANCELLED
    else:
        event_type = ShipmentEvent.EventType.STATUS_CHANGED

    create_shipment_event(
        shipment=shipment,
        event_type=event_type,
        description=f"Status changed from {old_status} to {new_status}.",
        user=user,
        metadata={"old_status": old_status, "new_status": new_status},
    )
    log_scm_action(
        team=shipment.team,
        action=SCMAuditLog.Action.SHIPMENT_STATUS_CHANGED,
        object_type="Shipment",
        object_id=str(shipment.pk),
        object_repr=str(shipment),
        metadata={"old_status": old_status, "new_status": new_status},
        actor=user,
    )
    return shipment


def cancel_shipment(shipment: Shipment, user: CustomUser) -> Shipment:
    """Cancel a shipment. Prefer this over hard deletion."""
    return change_shipment_status(shipment, user, Shipment.Status.CANCELLED)


def add_container_to_shipment(
    team: Team,
    shipment: Shipment,
    container: Container,
    user: CustomUser,
    data: dict | None = None,
) -> ShipmentContainer:
    """Add a container to a shipment.

    Raises ValidationError if shipment or container do not belong to the team,
    or if the container is already linked to this shipment.
    """
    if shipment.team_id != team.pk:
        raise ValidationError(_("Shipment does not belong to this team."))
    if container.team_id != team.pk:
        raise ValidationError(_("Container does not belong to this team."))

    sc = ShipmentContainer.objects.create(
        shipment=shipment,
        container=container,
        **(data or {}),
    )
    create_shipment_event(
        shipment=shipment,
        event_type=ShipmentEvent.EventType.CONTAINER_ADDED,
        description=f"Container {container} added.",
        user=user,
        metadata={"container_id": container.pk, "container_str": str(container)},
    )
    return sc


def remove_container_from_shipment(
    team: Team,
    shipment: Shipment,
    shipment_container: ShipmentContainer,
    user: CustomUser,
) -> None:
    """Remove a container from a shipment and record a CONTAINER_REMOVED event."""
    if shipment.team_id != team.pk:
        raise ValidationError(_("Shipment does not belong to this team."))

    container_str = str(shipment_container.container)
    container_pk = shipment_container.container_id
    shipment_container.delete()

    create_shipment_event(
        shipment=shipment,
        event_type=ShipmentEvent.EventType.CONTAINER_REMOVED,
        description=f"Container {container_str} removed.",
        user=user,
        metadata={"container_id": container_pk, "container_str": container_str},
    )


def update_shipment_eta(
    shipment: Shipment,
    eta_date,
    source: str,
    confidence: str = "medium",
    user: CustomUser | None = None,
) -> Shipment:
    """Update the shipment ETA and record a timeline event.

    Sets original_eta on first update, always updates current eta and meta fields.
    """
    if shipment.original_eta is None and eta_date is not None:
        shipment.original_eta = eta_date

    old_eta = shipment.eta
    shipment.eta = eta_date
    shipment.eta_source = source
    shipment.eta_confidence = confidence
    shipment.eta_last_updated = timezone.now()
    shipment.save(
        update_fields=["eta", "original_eta", "eta_source", "eta_confidence", "eta_last_updated", "updated_at"]
    )

    if eta_date != old_eta:
        from apps.scm.tracking.models import ETAHistory

        create_shipment_event(
            shipment=shipment,
            event_type=ShipmentEvent.EventType.ETA_UPDATED,
            description=f"ETA updated to {eta_date} (source: {source}, confidence: {confidence}).",
            user=user,
            metadata={"eta": str(eta_date) if eta_date else None, "source": source, "confidence": confidence},
        )
        ETAHistory.objects.create(
            team=shipment.team,
            shipment=shipment,
            previous_eta=old_eta,
            new_eta=eta_date,
            changed_at=shipment.eta_last_updated,
            source=source,
        )

    return shipment


def calculate_shipment_status(shipment: Shipment) -> str:
    """Derive a shipment status from its linked data.

    Logic (first match wins):
      EXCEPTION  — any EXCEPTION event
      CANCELLED  — current status is CANCELLED
      DELIVERED  — actual_arrival_at set and all supplier deliveries received
      PARTIALLY_RECEIVED — some supplier deliveries received but not all
      ARRIVED    — actual_arrival_at set
      IN_TRANSIT — actual_departure_at set
      BOOKED     — at least one container linked
      DRAFT      — fallback
    """
    from apps.scm.supplier_deliveries.models import SupplierDelivery, SupplierDeliveryStatus

    # CANCELLED is sticky
    if shipment.status == Shipment.Status.CANCELLED:
        return Shipment.Status.CANCELLED

    # EXCEPTION event present
    if shipment.events.filter(event_type=ShipmentEvent.EventType.EXCEPTION).exists():
        return Shipment.Status.EXCEPTION

    # Arrival / delivery logic
    if shipment.actual_arrival_at is not None:
        container_ids = shipment.shipment_containers.values_list("container_id", flat=True)
        deliveries = SupplierDelivery.objects.filter(
            team=shipment.team, lines__container_id__in=container_ids
        ).distinct()
        if deliveries.exists():
            total = deliveries.count()
            received = deliveries.filter(status=SupplierDeliveryStatus.RECEIVED).count()
            if received >= total:
                return Shipment.Status.DELIVERED
            if received > 0:
                return Shipment.Status.PARTIALLY_RECEIVED
        return Shipment.Status.ARRIVED

    if shipment.actual_departure_at is not None:
        return Shipment.Status.IN_TRANSIT

    if shipment.shipment_containers.exists():
        return Shipment.Status.BOOKED

    return Shipment.Status.DRAFT


def recalculate_and_save_shipment_status(shipment: Shipment) -> Shipment:
    """Recalculate status and save if it has changed."""
    new_status = calculate_shipment_status(shipment)
    if shipment.status != new_status:
        shipment.status = new_status
        shipment.save(update_fields=["status", "updated_at"])
    return shipment


def create_shipment_event(
    shipment: Shipment,
    event_type: str,
    description: StrOrPromise,
    user: CustomUser | None = None,
    metadata: dict | None = None,
    occurred_at=None,
) -> ShipmentEvent:
    """Create a timeline event for a shipment."""
    return ShipmentEvent.objects.create(
        shipment=shipment,
        event_type=event_type,
        description=str(description),
        created_by=user,
        metadata=metadata or {},
        occurred_at=occurred_at or timezone.now(),
    )
