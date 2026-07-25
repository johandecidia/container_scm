import logging
from datetime import timedelta

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task
def sync_due_tracking_subscriptions() -> dict:
    """Celery task: sync all subscriptions that are due for a tracking update."""
    from .sync import sync_due_tracking_subscriptions as _sync

    return _sync()


@shared_task
def sync_single_tracking_subscription(subscription_id: int) -> dict:
    """Celery task: sync a single tracking subscription by ID.

    Returns the run's status and error classification so a caller can tell a
    skipped run (nothing attempted) from a failed one.
    """
    from .models import TrackingSubscription
    from .sync import sync_tracking_subscription

    try:
        subscription = TrackingSubscription.objects.select_related("provider", "team", "shipment", "container").get(
            pk=subscription_id
        )
    except TrackingSubscription.DoesNotExist:
        logger.warning("sync_single_tracking_subscription: subscription %s not found.", subscription_id)
        return {"status": "not_found", "error_type": "", "events_created": 0}

    sync_run = sync_tracking_subscription(subscription)
    if sync_run is None:
        return {"status": "already_running", "error_type": "", "events_created": 0}
    return {
        "status": sync_run.status,
        "error_type": sync_run.error_type,
        "events_created": sync_run.events_created,
    }


@shared_task
def cleanup_old_tracking_raw_payloads(days: int = 90) -> int:
    """Celery task: delete raw payloads older than `days` days to control storage growth."""
    from django.utils import timezone

    from .models import TrackingRawPayload

    cutoff = timezone.now() - timedelta(days=days)
    deleted_count, _ = TrackingRawPayload.objects.filter(received_at__lt=cutoff).delete()
    logger.info("cleanup_old_tracking_raw_payloads: deleted %d records older than %d days.", deleted_count, days)
    return deleted_count
