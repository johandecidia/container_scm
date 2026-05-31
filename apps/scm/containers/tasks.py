import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task
def sync_container_status(container_id: int) -> None:
    """Background task to sync container status from external source."""
    from .models import Container

    try:
        container = Container.objects.get(pk=container_id)
    except Container.DoesNotExist:
        logger.warning("sync_container_status: container %s not found — skipping.", container_id)
        return

    # TODO: implement status sync logic
    logger.info("sync_container_status: container %s — sync not yet implemented.", container.pk)
