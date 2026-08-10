"""Celery tasks for containers.

Tasks take IDs and load their data inside the task, so a queued job always works
against current, team-scoped state.
"""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def sync_container_status(self, container_id: int) -> None:
    """Background task to sync container status from external source."""
    from .models import Container

    try:
        container = Container.objects.get(pk=container_id)
    except Container.DoesNotExist:
        logger.warning("sync_container_status: container %s not found — skipping.", container_id)
        return

    try:
        # TODO: implement status sync logic
        logger.info("sync_container_status: container %s — sync not yet implemented.", container.pk)
    except Exception as exc:
        logger.exception("sync_container_status: container %s failed: %s", container_id, exc)
        raise self.retry(exc=exc) from exc


@shared_task
def dispatch_planned_container_discovery() -> dict:
    """Scheduled dispatcher: queue a discovery pass for each team with work to do.

    One task per team keeps teams isolated: a carrier outage or a slow adapter for
    one team cannot stall another team's discovery.
    """
    from .discovery import get_planned_containers_for_discovery

    # order_by() clears the queryset's ordering: an ordering column is added to the
    # SELECT list, which would make DISTINCT compare rows instead of team ids and
    # queue a team once per planned container.
    team_ids = list(get_planned_containers_for_discovery().order_by().values_list("team_id", flat=True).distinct())
    for team_id in team_ids:
        discover_planned_containers_for_team.delay(team_id)

    logger.info("Planned container discovery dispatcher: queued %d team(s).", len(team_ids))
    return {"queued": len(team_ids)}


@shared_task
def discover_planned_containers_for_team(team_id: int) -> dict:
    """Run one planned-container discovery pass for a team."""
    from apps.teams.models import Team

    from .discovery import run_discovery_for_team

    try:
        team = Team.objects.get(pk=team_id)
    except Team.DoesNotExist:
        logger.warning("discover_planned_containers_for_team: team %s not found — skipping.", team_id)
        return {"checked": 0, "detected": 0, "not_found": 0, "skipped": 0, "expired": 0, "errors": []}

    summary = run_discovery_for_team(team=team)
    logger.info(
        "Planned container discovery for team %s: checked=%s detected=%s not_found=%s skipped=%s expired=%s",
        team_id,
        summary["checked"],
        summary["detected"],
        summary["not_found"],
        summary["skipped"],
        summary["expired"],
    )
    return summary


@shared_task
def expire_stale_planned_containers() -> int:
    """Scheduled clean-up: expire planned numbers past their attempts or expiry.

    Runs independently of a discovery pass so a paused or backlogged queue is still
    tidied up.
    """
    from .discovery import expire_exhausted_planned_containers

    return expire_exhausted_planned_containers()
