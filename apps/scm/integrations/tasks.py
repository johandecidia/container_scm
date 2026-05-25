from celery import shared_task


@shared_task
def sync_integration(integration_id: int) -> None:
    """Background task to sync data for an integration."""
    from .models import Integration

    integration = Integration.objects.get(pk=integration_id)
    # TODO: implement sync logic per provider (use clients/ for API calls)
    _ = integration
