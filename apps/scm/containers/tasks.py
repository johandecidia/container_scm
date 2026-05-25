from celery import shared_task


@shared_task
def sync_container_status(container_id: int) -> None:
    """Background task to sync container status from external source."""
    from .models import Container

    container = Container.objects.get(pk=container_id)
    # TODO: implement status sync logic
    _ = container
