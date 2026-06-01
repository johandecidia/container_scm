import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task
def compute_analytics_snapshot(team_id: int) -> None:
    """Background task to compute and cache analytics data for a team."""
    # TODO: implement analytics computation logic
    logger.info("compute_analytics_snapshot: team_id=%s — not yet implemented.", team_id)
