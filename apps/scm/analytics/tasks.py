from celery import shared_task

from apps.scm.monitoring import get_scm_logger, log_analytics_failed

logger = get_scm_logger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=120)
def compute_analytics_snapshot(self, team_id: int) -> dict:
    """Compute and persist a daily analytics snapshot for a team.

    Idempotent: uses update_or_create, so running multiple times is safe.
    Retries up to 3 times with 2-minute backoff on transient errors.
    """
    from apps.teams.models import Team

    from .services import create_or_update_snapshot

    try:
        team = Team.objects.get(pk=team_id)
    except Team.DoesNotExist:
        logger.warning("compute_analytics_snapshot: team %s not found — skipping.", team_id)
        return {"status": "skipped", "reason": "team not found"}

    logger.info("compute_analytics_snapshot: starting for team %s.", team.slug)
    try:
        snapshot = create_or_update_snapshot(team)
        logger.info(
            "compute_analytics_snapshot: completed for team %s (date=%s, shipments=%s).",
            team.slug,
            snapshot.date,
            snapshot.total_shipments,
        )
        return {"status": "ok", "date": str(snapshot.date), "team_id": team_id}
    except Exception as exc:
        log_analytics_failed(logger, team_id=team_id, error=str(exc))
        raise self.retry(exc=exc) from exc
