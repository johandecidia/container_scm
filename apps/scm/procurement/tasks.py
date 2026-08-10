import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def sync_purchase_orders_from_bc(self, team_id: int) -> None:
    """Sync purchase orders from Business Central for the given team.

    Resolves the team's active Business Central integration and runs an
    incremental sync. Permanent configuration problems (no integration, disabled
    sync, missing credentials) are logged and skipped; transient failures are
    retried. The full per-integration scheduled task + dispatcher is milestone 2.
    """
    from apps.scm.integrations.business_systems.business_central.exceptions import (
        BusinessCentralConfigurationError,
        BusinessCentralError,
        BusinessCentralSyncInProgressError,
    )
    from apps.scm.integrations.business_systems.business_central.sync import (
        sync_purchase_orders_from_business_central,
    )
    from apps.scm.integrations.models import Integration
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

    try:
        run = sync_purchase_orders_from_business_central(integration)
        logger.info(
            "sync_purchase_orders_from_bc: team %s — %s (created=%d updated=%d unchanged=%d failed=%d).",
            team.slug,
            run.status,
            run.records_created,
            run.records_updated,
            run.records_unchanged,
            run.records_failed,
        )
    except (BusinessCentralConfigurationError, BusinessCentralSyncInProgressError) as exc:
        # Permanent / not-our-turn — do not retry.
        logger.info("sync_purchase_orders_from_bc: team %s skipped: %s", team.slug, exc)
    except BusinessCentralError as exc:
        # Transient (auth/connection/rate-limit/server) — retry.
        logger.warning("sync_purchase_orders_from_bc: team %s transient failure: %s", team.slug, exc)
        raise self.retry(exc=exc) from exc
    except Exception as exc:
        logger.exception("sync_purchase_orders_from_bc: team %s failed: %s", team_id, exc)
        raise self.retry(exc=exc) from exc
