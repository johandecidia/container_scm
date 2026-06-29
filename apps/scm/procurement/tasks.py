import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def sync_purchase_orders_from_bc(self, team_id: int) -> None:
    """Sync purchase orders from Business Central for the given team."""
    from apps.scm.integrations.business_systems.business_central.sync import (
        sync_purchase_orders_from_business_central,
    )
    from apps.teams.models import Team

    try:
        team = Team.objects.get(pk=team_id)
    except Team.DoesNotExist:
        logger.warning("sync_purchase_orders_from_bc: team %s not found — skipping.", team_id)
        return

    try:
        orders = sync_purchase_orders_from_business_central(team=team)
        logger.info(
            "sync_purchase_orders_from_bc: team %s — synced %d orders.",
            team.slug,
            len(orders),
        )
    except NotImplementedError:
        # Live BC client is not yet configured — skip silently in dev.
        logger.info("sync_purchase_orders_from_bc: BC client not yet implemented for team %s — skipping.", team.slug)
    except Exception as exc:
        logger.exception("sync_purchase_orders_from_bc: team %s failed: %s", team_id, exc)
        raise self.retry(exc=exc) from exc
