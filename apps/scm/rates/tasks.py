import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task
def refresh_market_rates(team_id: int) -> None:
    """Background task to refresh market rates for a team."""
    # TODO: implement market rate refresh logic
    logger.info("refresh_market_rates: team_id=%s — not yet implemented.", team_id)
