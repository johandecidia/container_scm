"""Celery tasks for container discovery.

The task is idempotent: re-running it for a team only processes PLANNED
containers, so already-detected containers are skipped automatically.
"""

from __future__ import annotations

import logging

from celery import shared_task

from apps.teams.models import Team

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def run_container_discovery_for_team_task(self, team_id: int) -> dict:
    """Run container discovery for all PLANNED containers on the given team.

    Idempotent: containers already DETECTED or RETIRED are not reprocessed.
    """
    try:
        team = Team.objects.get(pk=team_id)
    except Team.DoesNotExist:
        logger.warning("run_container_discovery_for_team_task: team %d not found", team_id)
        return {}

    try:
        from .discovery_service import run_container_discovery

        summary = run_container_discovery(team=team)
        logger.info("Discovery task completed for team %s: %s", team.slug, summary)
        return summary
    except Exception as exc:
        logger.exception("Discovery task failed for team %d: %s", team_id, exc)
        raise self.retry(exc=exc) from exc
