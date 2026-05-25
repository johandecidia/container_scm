from celery import shared_task


@shared_task
def compute_analytics_snapshot(team_id: int) -> None:
    """Background task to compute and cache analytics data for a team."""
    # TODO: implement analytics computation logic
    pass
