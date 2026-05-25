from celery import shared_task


@shared_task
def update_shipment_tracking(shipment_id: int) -> None:
    """Background task to update shipment tracking information."""
    from .models import Shipment

    shipment = Shipment.objects.get(pk=shipment_id)
    # TODO: implement tracking update logic
    _ = shipment
