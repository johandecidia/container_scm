# SCM Domain — Architecture

This document defines the architecture conventions, naming rules, and responsibility
boundaries for the SCM domain (`apps/scm/`).

All contributors must follow these rules consistently across every SCM sub-app.

---

## Sub-apps

| App            | Purpose                                      |
|----------------|----------------------------------------------|
| `containers`   | Shipping container tracking                  |
| `shipments`    | End-to-end shipment lifecycle                |
| `rates`        | Freight rate management                      |
| `imports`      | File-based data import jobs                  |
| `integrations` | External system integrations (carriers, etc) |
| `analytics`    | Aggregations and reporting                   |
| `tracking`     | Carrier subscriptions, events, ETA, position |
| `visibility`   | Read-only composition layer + map GeoJSON    |

### Derived tracking reads

A container can have several verified tracking sources over one physical journey —
carriers that covered different legs, plus the operator's own physical record. The
derivations that hold that together live in `tracking`, beside the schema they read,
and own no writes:

```
apps/scm/tracking/
    journey.py    # The unified multi-source journey and the derived current location
    gaps.py       # The segment of a journey no source explains
    positions.py  # Where a container was last reported, and how well we know it
```

Nothing here persists a journey, a leg or a gap: all three are computed on read from
`TrackingEvent` and the container's own location record, so a new event changes the
answer immediately and there is nothing to reconcile.

---

## Composition layers

`visibility` is not a domain app. It owns no models, no data and no writes: it
reads containers, shipments, tracking subscriptions, tracking events and ETA
history, composes one answer to "where is everything, and how well do we know
it", and renders that as a page and as GeoJSON for Mapbox.

It therefore does **not** carry the standard file set below. It has no
`models.py`, `forms.py`, `services.py` or `admin.py`, and adding empty ones would
suggest this is somewhere data can originate — which is exactly what it must not
become. Its files are:

```
apps/scm/visibility/
    apps.py         # AppConfig only; no models
    selectors.py    # Read composition over the other apps' read models
    read_models.py  # VisibilityObject and its presentation groupings
    geojson.py      # The GeoJSON contract for Mapbox
    context.py      # Map context for the shipment and container detail pages
    mapbox.py       # Browser-side Mapbox configuration
    views.py        # HTTP and rendering
    urls.py
```

A new composition layer is the exception, not the pattern. Anything that stores
data is a domain app and follows the rules below.

---

## Multi-tenancy

All SCM customer data is scoped to a **Team** (the Pegasus tenant model).
No SCM model may store customer data without a `team` relationship.

### Base model

All SCM models inherit from `BaseTeamModel` — never from `BaseModel` or `models.Model`
directly when the model holds customer data:

```python
from apps.teams.models import BaseTeamModel

class Container(BaseTeamModel):
    container_number = models.CharField(max_length=20)
```

`BaseTeamModel` provides:

| Attribute     | Description                                                     |
|---------------|-----------------------------------------------------------------|
| `team`        | FK to `teams.Team`, CASCADE on delete                           |
| `created_at`  | Auto-set timestamp                                              |
| `updated_at`  | Auto-updated timestamp                                          |
| `objects`     | Unfiltered Django manager (admin, migrations, background tasks) |
| `for_team`    | `TeamScopedManager` — auto-filters to the active team context   |

### Do NOT

- Add a manual `team = models.ForeignKey(...)` in SCM models — it is inherited.
- Use `BaseModel` or `models.Model` directly for models that hold customer data.
- Create alternative base classes (e.g. `TeamOwnedModel`) — they duplicate `BaseTeamModel`
  and create two sources of truth for the same concept.

---

## App file structure

Each SCM sub-app follows this layout:

```
apps/scm/<app>/
    models.py       # Schema only — BaseTeamModel subclasses
    selectors.py    # All read/query operations
    services.py     # All write/mutation operations and business logic
    views.py        # Request handling, template rendering, HTMX partials
    forms.py        # Input validation and field cleaning
    urls.py         # URL patterns (urlpatterns + team_urlpatterns)
    admin.py        # Django admin registration
    tasks.py        # Celery background tasks
```

Integrations that require an external HTTP client also add:

```
apps/scm/integrations/
    clients/        # One module per external provider
```

---

## File responsibilities

### `models.py`

Allowed:
- Database schema (fields, Meta, indexes)
- Relation definitions (ForeignKey, ManyToMany)
- Small helper methods and `@property` values
- `__str__` and `get_absolute_url`
- Inner `TextChoices` / `IntegerChoices` classes

Not allowed:
- Large or complex querysets
- External API calls
- Business logic
- Import/export logic
- Analytics calculations

```python
# Good — schema and small helper only
class Shipment(BaseTeamModel):
    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        IN_TRANSIT = "in_transit", _("In Transit")

    reference = models.CharField(max_length=100, unique=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)

    @property
    def is_active(self) -> bool:
        return self.status == self.Status.IN_TRANSIT
```

---

### `selectors.py`

All read/query operations live here — no exceptions.

Rules:
- Every user-facing selector accepts `team` as its first argument.
- Always apply a team filter, using `for_team` manager where available.
- Apply `select_related` / `prefetch_related` when traversing relations.
- Aggregations and annotations belong here, not in views.

Naming prefix: `get_`, `list_`, `filter_`

```python
# selectors.py

def list_team_containers(team: Team) -> QuerySet[Container]:
    return Container.for_team.filter(team=team).order_by("-created_at")

def get_container_by_number(team: Team, container_number: str) -> Container:
    return Container.for_team.get(team=team, container_number=container_number)

def list_active_shipments(team: Team) -> QuerySet[Shipment]:
    return (
        Shipment.for_team
        .filter(team=team, status=Shipment.Status.IN_TRANSIT)
        .select_related("container")
    )
```

Cross-team selectors (Celery workers only):

```python
def get_pending_import_jobs() -> QuerySet[ImportJob]:
    # Cross-team — Celery workers only. Do NOT call from user-facing views.
    return ImportJob.objects.filter(status=ImportJob.Status.PENDING)
```

---

### `services.py`

All business logic and write operations live here.

Rules:
- Services create, update, or delete data.
- Services may call external APIs.
- Services may enqueue Celery tasks.
- Services should use selectors for read operations where it makes sense.
- Services must not render templates or return HTTP responses.

Naming: verb first — `create_`, `update_`, `delete_`, `sync_`, `process_`, `import_`

```python
# services.py

def create_container(team: Team, container_number: str, size: str = "") -> Container:
    return Container.objects.create(team=team, container_number=container_number, size=size)

def update_container_status(container: Container, status: str) -> Container:
    container.status = status
    container.save(update_fields=["status", "updated_at"])
    return container

def sync_tracking_data(container: Container) -> None:
    """Fetch latest tracking data from carrier API and update the container."""
    from apps.scm.integrations.clients import get_tracking_client
    data = get_tracking_client().fetch(container.container_number)
    update_container_status(container, data["status"])
```

---

### `views.py`

Views handle requests and delegate everything else.

Allowed:
- Parsing request data
- Calling selectors (read)
- Calling services (write)
- Instantiating and validating forms
- Rendering templates
- Returning HTMX partials

Not allowed:
- Business logic
- Large querysets built inline
- External API calls
- Long-running operations (use Celery tasks instead)

```python
# views.py

@login_and_team_required
def container_list(request, team_slug):
    team = request.team
    containers = list_team_containers(team)
    return render(request, "scm/containers/pages/list.html", {"containers": containers})

@login_and_team_required
def container_create(request, team_slug):
    form = ContainerForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        create_container(team=request.team, **form.cleaned_data)
        return redirect("containers:list", team_slug=team_slug)
    return render(request, "scm/containers/pages/create.html", {"form": form})
```

---

### `forms.py`

Forms handle input validation only.

Allowed:
- Field definitions and widgets
- `clean_<field>` validation
- Cross-field validation in `clean()`

Not allowed:
- Database writes
- Calls to services or external APIs
- Business logic

---

### `tasks.py`

All Celery background jobs live here.

Use for:
- Imports and bulk processing
- External syncs
- Analytics aggregations
- Any operation that may take more than a few seconds

Naming suffix: `_task`

```python
# tasks.py

@shared_task
def process_import_task(import_job_id: int) -> None:
    job = ImportJob.objects.get(pk=import_job_id)
    process_import_job(job)  # delegates to services.py

@shared_task
def sync_tracking_task(container_id: int) -> None:
    container = Container.objects.get(pk=container_id)
    sync_tracking_data(container)  # delegates to services.py
```

Views must never run long-running operations inline — always enqueue a task:

```python
# In views.py — correct
process_import_task.delay(import_job.pk)

# Wrong — blocks the request thread
process_import_job(import_job)
```

---

## Naming conventions

### Models

Singular noun, PascalCase:

| App            | Model              |
|----------------|--------------------|
| `containers`   | `Container`        |
| `shipments`    | `Shipment`         |
| `rates`        | `Rate`             |
| `imports`      | `ImportJob`        |
| `integrations` | `Integration`      |

### Selectors

```python
get_container_by_number(team, number)
list_team_containers(team)
filter_active_shipments(team, **kwargs)
```

### Services

```python
create_container(team, ...)
update_container(container, ...)
delete_container(container)
sync_tracking_data(container)
process_import_job(job)
```

### Celery tasks

```python
sync_tracking_task
process_import_task
send_shipment_notification_task
```

---

## Team isolation rules

**All SCM data must be filtered by team.** No exceptions in user-facing code.

```python
# Wrong — unscoped lookup, will leak data across teams
Container.objects.get(id=container_id)

# Wrong — no team check
Container.objects.filter(status="active")

# Correct — scoped via for_team manager
Container.for_team.get(team=team, id=container_id)

# Correct — explicit team filter
Container.objects.filter(team=team, id=container_id).first()
```

The `for_team` manager is preferred for user-facing code because it integrates with
Pegasus's team context middleware. The explicit `team=team` filter is acceptable and
required when writing cross-team selectors or background task code.

---

## URL conventions

Each sub-app provides two URL lists in `urls.py`:

```python
urlpatterns = [
    # Non-team views (if any)
]

team_urlpatterns = [
    # Team-scoped views — auto-prefixed with /a/<team_slug>/
    path("containers/", views.container_list, name="container-list"),
    path("containers/create/", views.container_create, name="container-create"),
    path("containers/<int:pk>/", views.container_detail, name="container-detail"),
]
```

All views in `team_urlpatterns` must accept `team_slug` as first argument and use
`@login_and_team_required` (or `@team_admin_required`).

---

## Template conventions

Templates live under:

```
templates/scm/<app_name>/
    pages/       # Full-page templates
    partials/    # HTMX partial responses
    components/  # Reusable includes
```

Example layout for `containers`:

```
templates/scm/containers/
    pages/
        list.html
        detail.html
        create.html
    partials/
        container_row.html
        status_badge.html
    components/
        container_card.html
```

Rules:
- Use `{% load i18n %}` and `{% translate %}` / `{% blocktranslate trimmed %}` for all
  user-facing strings.
- Indent with two spaces.
- Use DaisyUI components first; fall back to raw Tailwind v4 classes only when no DaisyUI
  component fits.
- Use `{% include %}` for reusable fragments — do not copy/paste template blocks.
- Use Alpine.js for browser-only interactions (toggling, local state).
- Use HTMX for interactions that require a server round-trip.
- Avoid inline `<script>` tags.

---

## Quick-reference: what goes where

| Scenario                                    | File            |
|---------------------------------------------|-----------------|
| Add a new database field                    | `models.py`     |
| Query containers filtered by status         | `selectors.py`  |
| Create a new shipment record                | `services.py`   |
| Validate uploaded file format               | `forms.py`      |
| Render container list page                  | `views.py`      |
| Call carrier API to fetch tracking update   | `services.py`   |
| Queue a bulk import to run in background    | `tasks.py`      |
| Aggregate shipment counts per route         | `selectors.py`  |
| Register model in Django admin              | `admin.py`      |
