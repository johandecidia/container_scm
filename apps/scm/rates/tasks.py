from celery import shared_task


@shared_task
def refresh_market_rates(team_id: int) -> None:
    """Background task to refresh market rates for a team."""
    # TODO: implement market rate refresh logic
    pass
