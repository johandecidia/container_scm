import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def sync_container_status(self, container_id: int) -> None:
    """Background task to sync container status from external source."""
    from .models import Container

    try:
        container = Container.objects.get(pk=container_id)
    except Container.DoesNotExist:
        logger.warning("sync_container_status: container %s not found — skipping.", container_id)
        return

    try:
        # TODO: implement status sync logic
        logger.info("sync_container_status: container %s — sync not yet implemented.", container.pk)
    except Exception as exc:
        logger.exception("sync_container_status: container %s failed: %s", container_id, exc)
        raise self.retry(exc=exc) from exc
