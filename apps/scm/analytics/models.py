from django.db import models

from apps.teams.models import BaseTeamModel

# Analytics models — lightweight aggregation snapshots.
# Heavy computation belongs in services.py or Celery tasks.
