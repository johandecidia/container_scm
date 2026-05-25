# Shipment services — all business logic and write operations.
from apps.teams.models import Team

from .models import Shipment


def create_shipment(team: Team, reference: str, **kwargs) -> Shipment:
    return Shipment.objects.create(team=team, reference=reference, **kwargs)


def update_shipment_status(shipment: Shipment, status: str) -> Shipment:
    shipment.status = status
    shipment.save(update_fields=["status", "updated_at"])
    return shipment


def delete_shipment(shipment: Shipment) -> None:
    shipment.delete()
