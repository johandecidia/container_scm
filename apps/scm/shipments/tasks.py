import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def update_shipment_tracking(self, shipment_id: int) -> None:
    """Background task to update shipment tracking information."""
    from .models import Shipment

    try:
        shipment = Shipment.objects.get(pk=shipment_id)
    except Shipment.DoesNotExist:
        logger.warning("update_shipment_tracking: shipment %s not found — skipping.", shipment_id)
        return

    try:
        # TODO: implement tracking update logic
        logger.info("update_shipment_tracking: shipment %s — sync not yet implemented.", shipment.pk)
    except Exception as exc:
        logger.exception("update_shipment_tracking: shipment %s failed: %s", shipment_id, exc)
        raise self.retry(exc=exc) from exc
