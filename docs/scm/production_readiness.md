# SCM Production Readiness Runbook

This document covers operational procedures for the SCM module in production.

---

## 1. Monitoring & Alerting

### Health Endpoints

| Endpoint | Purpose |
|----------|---------|
| `/health/` | Overall app health (db, cache, queues) |
| `/health/db/` | Database connectivity |
| `/health/redis/` | Redis connectivity |
| `/health/scm/` | SCM-specific checks (imports, tracking sync, container discovery) |

`/health/scm/` returns JSON:

```json
{
  "status": "ok | warning | error",
  "checks": {
    "database": "ok",
    "imports": "ok",
    "tracking": "warning",
    "discovery": "ok"
  }
}
```

Returns HTTP 200 for `ok`/`warning`, HTTP 503 for `error`.

**Warning thresholds** (no data received within window):

| Check | Warning | Error |
|-------|---------|-------|
| imports | 48 h | — |
| tracking | 4 h | — |
| discovery | 24 h | — |

### Sentry

Set `SENTRY_DSN` in the environment to enable Sentry error reporting. Both Django and Celery integrations are active. SCM errors include `scm.team_id` and `scm.team_slug` tags for filtering.

### Structured Logging

All SCM components log under the `apps.scm.*` namespace. Key log events:

- `log_import_started / completed / failed` — import jobs
- `log_tracking_sync_started / completed / failed` — tracking syncs
- `log_carrier_api_failed` — external API failures
- `log_container_discovery_failed` — auto-discovery errors
- `log_analytics_failed` — analytics computation errors

Use `apps.scm.monitoring.get_scm_logger()` to get a correctly namespaced logger in new code.

---

## 2. Background Jobs (Celery)

All SCM tasks use `@shared_task(bind=True, max_retries=3, default_retry_delay=N)` with exponential back-off via `self.retry(exc=exc, countdown=...)`.

### Key Tasks

| Task | Module | Retry |
|------|--------|-------|
| `compute_analytics_snapshot` | `apps.scm.analytics.tasks` | 3× / 120 s |
| `run_tracking_sync` | `apps.scm.tracking.tasks` | 3× / 60 s |
| `discover_containers_for_team` | `apps.scm.integrations.tasks` | 3× / 60 s |
| `parse_import_job` | `apps.scm.imports.tasks` | 3× / 30 s |

### Beat Schedule

Periodic tasks are registered in `CELERY_BEAT_SCHEDULE` (see `container_scm/settings.py`). Confirm the Celery Beat process is running if scheduled jobs stop executing.

### Stuck Job Recovery

If a task is stuck in `PENDING`:

```bash
# Inspect active tasks
celery -A container_scm inspect active

# Revoke a stuck task
celery -A container_scm control revoke <task-id> --terminate

# Re-queue manually
python manage.py shell
>>> from apps.scm.analytics.tasks import compute_analytics_snapshot
>>> compute_analytics_snapshot.delay(team_id=<id>)
```

---

## 3. Database Backup & Recovery

### Backup Schedule

| Environment | Tool | Frequency | Retention |
|-------------|------|-----------|-----------|
| Railway (production) | Railway managed backups | Daily | 7 days |
| Self-hosted | `pg_dump` via cron | Daily | 30 days |

### Manual Backup (Railway)

```bash
railway run pg_dump $DATABASE_URL -Fc -f backup_$(date +%Y%m%d).dump
```

### Manual Backup (self-hosted)

```bash
pg_dump -U postgres -Fc container_scm > backup_$(date +%Y%m%d_%H%M).dump
```

### Restore

```bash
# Drop and recreate the database first if doing a full restore
pg_restore -U postgres -d container_scm --clean backup_20260101.dump

# Apply any missing migrations
python manage.py migrate
```

### SCM-Specific Tables

Critical SCM tables to verify after a restore:

- `scm_containers_container`
- `scm_shipments_shipment`
- `scm_procurement_purchaseorder` / `scm_procurement_purchaseorderline`
- `scm_supplier_deliveries_supplierdelivery`
- `scm_tracking_trackingsubscription` / `scm_tracking_trackingevent`
- `scm_audit_log_scmauditlog`

---

## 4. Data Recovery Procedures

### Re-importing Purchase Orders from Business Central

The BC import is fully idempotent. To re-sync all POs for a team:

```python
from apps.scm.procurement.services import import_purchase_orders_from_bc
from apps.teams.models import Team

team = Team.objects.get(slug="<team-slug>")
# Provide normalized BC data (list of dicts matching the import format)
import_purchase_orders_from_bc(team, bc_data)
```

Running this multiple times with the same data is safe — no duplicates will be created.

### Re-running Failed Import Jobs

```python
from apps.scm.imports.tasks import parse_import_job

parse_import_job.delay(import_job_id=<id>)
```

### Resetting a Stuck Tracking Subscription

```python
from apps.scm.tracking.models import TrackingSubscription, TrackingSubscriptionStatus

sub = TrackingSubscription.objects.get(pk=<id>)
sub.status = TrackingSubscriptionStatus.ACTIVE
sub.save(update_fields=["status"])
```

---

## 5. Security Checklist

- [ ] `SECRET_KEY` rotated and not in version control
- [ ] `DEBUG=False` in production
- [ ] `ALLOWED_HOSTS` set to production domains only
- [ ] `SENTRY_DSN` set for error visibility
- [ ] All SCM views are protected by `@scm_login_required`
- [ ] Cross-tenant isolation verified: all querysets filter by `team`
- [ ] Integration credentials stored in `IntegrationCredential` (encrypted), never in plain text
- [ ] `/health/scm/` not exposed to the public internet (internal load balancer only, or basic auth)

---

## 6. Audit Log

All key SCM state changes are recorded in `SCMAuditLog` (table: `scm_audit_log_scmauditlog`). This log is append-only (no update/delete in the admin).

To review recent actions for a team:

```python
from apps.scm.audit_log.models import SCMAuditLog
from apps.teams.models import Team

team = Team.objects.get(slug="<team-slug>")
SCMAuditLog.objects.filter(team=team).order_by("-created_at")[:50]
```

---

## 7. Deployment Checklist

Before each production deployment:

```bash
# Check for missing migrations
python manage.py makemigrations --check

# Apply migrations
python manage.py migrate

# Run Django system checks
python manage.py check --deploy

# Run full test suite
make test

# Verify health endpoint after deploy
curl https://<host>/health/scm/
```
