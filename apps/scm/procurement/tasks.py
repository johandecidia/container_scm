import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def sync_purchase_orders_from_bc(self, team_id: int) -> None:
    """Sync purchase orders from Business Central for the given team.

    The real BC API client will be wired in the integrations layer.
    This stub calls the import service with an empty list until then.
    """
    from apps.teams.models import Team

    from .services import import_purchase_orders_from_bc

    try:
        team = Team.objects.get(pk=team_id)
    except Team.DoesNotExist:
        logger.warning("sync_purchase_orders_from_bc: team %s not found — skipping.", team_id)
        return

    try:
        # TODO: replace with real BC API client call from integrations layer
        purchase_orders_data: list = []
        orders = import_purchase_orders_from_bc(team=team, purchase_orders_data=purchase_orders_data)
        logger.info(
            "sync_purchase_orders_from_bc: team %s — synced %d orders.",
            team.slug,
            len(orders),
        )
    except Exception as exc:
        logger.exception("sync_purchase_orders_from_bc: team %s failed: %s", team_id, exc)
        raise self.retry(exc=exc) from exc
