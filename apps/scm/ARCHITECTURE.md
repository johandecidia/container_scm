# SCM Domain — Architecture

## Multi-tenancy

All SCM customer data is scoped to a **Team** (Pegasus tenant model).
No SCM model may store customer data without a team relationship.

### Base model

All SCM models inherit from `BaseTeamModel` (not `BaseModel` or `models.Model` directly):

```python
from apps.teams.models import BaseTeamModel

class MyModel(BaseTeamModel):
    ...
```

`BaseTeamModel` provides:
- `team` — FK to `teams.Team`, CASCADE on delete
- `created_at` — auto timestamp
- `updated_at` — auto timestamp
- `objects` — unfiltered manager (Django admin, migrations, background tasks)
- `for_team` — `TeamScopedManager`, auto-filters to the current team context

### Do NOT

- Create a manual `team = models.ForeignKey(...)` in SCM models — it is inherited.
- Use `BaseModel` or `models.Model` directly for models that hold customer data.
- Create alternative base classes (e.g. `TeamOwnedModel`) that duplicate `BaseTeamModel`.

## Selectors

All user-facing read operations must accept `team` as an argument and apply a team filter:

```python
# Correct — team-scoped
def get_team_containers(team: Team) -> QuerySet[Container]:
    return Container.for_team.filter(team=team).order_by("-created_at")

# Correct — specific lookup, still team-scoped
def get_container_by_number(team: Team, container_number: str) -> Container:
    return Container.for_team.get(team=team, container_number=container_number)
```

### Cross-team selectors (internal use only)

Background tasks (Celery) may query across teams using `Model.objects` directly.
These functions must be clearly documented and must never be called from user-facing views:

```python
def get_pending_import_jobs() -> QuerySet[ImportJob]:
    # Cross-team — Celery workers only. Use get_team_import_jobs() in views.
    return ImportJob.objects.filter(status=ImportJob.Status.PENDING)
```

## App structure

Each SCM sub-app follows the same layout:

```
apps/scm/<app>/
    models.py     # BaseTeamModel subclasses only
    selectors.py  # Read operations — all user-facing selectors take `team`
    services.py   # Write operations
    views.py
    forms.py
    urls.py
    admin.py
    tasks.py      # Celery tasks (may use cross-team selectors)
```

## Current SCM models

| App           | Model        | Base           |
|---------------|--------------|----------------|
| containers    | Container    | BaseTeamModel  |
| shipments     | Shipment     | BaseTeamModel  |
| rates         | Rate         | BaseTeamModel  |
| imports       | ImportJob    | BaseTeamModel  |
| integrations  | Integration  | BaseTeamModel  |
| analytics     | *(none yet)* | —              |
