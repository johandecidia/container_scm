import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task
def sync_due_tracking_subscriptions() -> dict:
    """Celery task: sync all subscriptions that are due for a tracking update."""
    from .sync import sync_due_tracking_subscriptions as _sync

    return _sync()


@shared_task
def sync_single_tracking_subscription(subscription_id: int) -> bool:
    """Celery task: sync a single tracking subscription by ID."""
    from .models import TrackingSubscription
    from .sync import sync_tracking_subscription

    try:
        subscription = TrackingSubscription.objects.select_related("provider", "team").get(pk=subscription_id)
    except TrackingSubscription.DoesNotExist:
        logger.warning("sync_single_tracking_subscription: subscription %s not found.", subscription_id)
        return False

    return sync_tracking_subscription(subscription)


@shared_task
def cleanup_old_tracking_raw_payloads(days: int = 90) -> int:
    """Celery task: delete raw payloads older than `days` days to control storage growth."""
    from django.utils import timezone

    from .models import TrackingRawPayload

    cutoff = timezone.now() - timezone.timedelta(days=days)
    deleted_count, _ = TrackingRawPayload.objects.filter(received_at__lt=cutoff).delete()
    logger.info("cleanup_old_tracking_raw_payloads: deleted %d records older than %d days.", deleted_count, days)
    return deleted_count
