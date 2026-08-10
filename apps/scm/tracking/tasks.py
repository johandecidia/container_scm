"""Celery tasks for carrier tracking.

Tasks take IDs, never model instances, and load their data inside the task so a
queued job always works against current, team-scoped state.

The scheduled entry point is :func:`dispatch_due_tracking_subscriptions`, which
fans out one task per due subscription. Each subscription then syncs under its own
lock, so a slow carrier delays only its own subscription instead of the batch, and
two dispatch ticks overlapping cannot double-sync anything.
"""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task
def dispatch_due_tracking_subscriptions(limit: int | None = None) -> dict:
    """Scheduled dispatcher: queue one sync task per due subscription.

    ``limit`` caps how many are queued per tick so a backlog cannot flood the
    broker; it defaults to SCM_TRACKING_DISPATCH_LIMIT. When the cap bites it is
    logged and reported, so a truncated run never looks like a complete one — the
    remainder is picked up on the next tick.
    """
    from django.conf import settings

    from .selectors import get_due_tracking_subscriptions

    if limit is None:
        limit = getattr(settings, "SCM_TRACKING_DISPATCH_LIMIT", None)

    due_ids = list(get_due_tracking_subscriptions().values_list("pk", flat=True))
    total = len(due_ids)
    if limit is not None and total > limit:
        logger.warning(
            "Tracking dispatcher: %d subscriptions due, queueing the first %d (cap reached).",
            total,
            limit,
        )
        due_ids = due_ids[:limit]

    for subscription_id in due_ids:
        sync_single_tracking_subscription.delay(subscription_id)

    logger.info("Tracking dispatcher: queued %d of %d due subscription(s).", len(due_ids), total)
    return {"queued": len(due_ids), "due": total, "capped": len(due_ids) < total}


@shared_task
def sync_due_tracking_subscriptions() -> dict:
    """Sync every due subscription inline, in this worker.

    Used by management commands and tests. Scheduled runs use
    :func:`dispatch_due_tracking_subscriptions` instead, so one slow carrier cannot
    hold up everyone else's sync.
    """
    from .sync import sync_due_tracking_subscriptions as _sync

    return _sync()


@shared_task
def sync_single_tracking_subscription(subscription_id: int) -> dict:
    """Sync a single tracking subscription by ID.

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
def apply_tracking_raw_payload_retention() -> dict:
    """Scheduled retention pass over stored carrier responses.

    Archives bodies past the retention window (keeping hash and metadata) and, only
    if a deletion window is configured, removes records past that window.
    """
    from .retention import archive_old_raw_payloads, delete_expired_raw_payloads

    archived = archive_old_raw_payloads()
    deleted = delete_expired_raw_payloads()
    return {"archived": archived, "deleted": deleted}


@shared_task
def cleanup_old_tracking_raw_payloads(days: int = 90) -> int:
    """Archive raw payload bodies older than ``days``.

    Kept for existing callers. It now archives rather than deletes: the payload
    body is dropped but the record — including its hash — is retained, because it
    is often the only evidence of what the carrier said.
    """
    from .retention import archive_old_raw_payloads

    return archive_old_raw_payloads(days)
