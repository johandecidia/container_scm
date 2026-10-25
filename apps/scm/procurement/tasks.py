import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def sync_purchase_orders_from_bc(self, team_id: int) -> None:
    """Legacy team-based entry point for the Business Central purchase-order sync.

    Not scheduled by this project — Celery Beat uses
    ``integrations.tasks.sync_enabled_business_central_integrations_task``. Kept
    (name unchanged) so an existing Beat entry or queued message still resolves.
    It only maps the team to its active BC integration and then runs the same
    task body as ``sync_business_central_purchase_orders_task`` as a *scheduled*
    run, so the same opt-in, resource checks, locking and retry rules apply.
    """
    from apps.scm.integrations.models import Integration, IntegrationSyncRun
    from apps.scm.integrations.tasks import run_business_central_resource_sync_task
    from apps.teams.models import Team

    try:
        team = Team.objects.get(pk=team_id)
    except Team.DoesNotExist:
        logger.warning("sync_purchase_orders_from_bc: team %s not found — skipping.", team_id)
        return

    integration = Integration.objects.filter(
        team=team,
        provider_code="business_central",
        provider_family=Integration.ProviderFamily.BUSINESS_SYSTEM,
        is_active=True,
    ).first()
    if integration is None:
        logger.info("sync_purchase_orders_from_bc: team %s has no active BC integration — skipping.", team.slug)
        return

    run_business_central_resource_sync_task(
        self, integration.id, IntegrationSyncRun.ResourceType.PURCHASE_ORDERS, "scheduled"
    )
