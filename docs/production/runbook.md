# Operations Runbook

Quick reference for operating Container SCM in production on Railway.

---

## Deploy to Railway

Railway deploys automatically on push to the connected branch (usually `master`).

To trigger a manual redeploy:

```bash
# Using Railway CLI
railway up

# Or via the Railway dashboard:
# Service → Deployments → Redeploy
```

The `Dockerfile.rlw` build process:
1. Builds frontend assets with Vite (Node 22)
2. Installs Python dependencies
3. Runs `collectstatic`
4. Starts Gunicorn via `/start.sh`

On container startup, `/start.sh` runs:
1. `python manage.py migrate --no-input`
2. `gunicorn container_scm.wsgi:application`

---

## Rollback

1. Go to Railway dashboard → Service → Deployments.
2. Find the last known good deployment.
3. Click **Redeploy** on that deployment.

If the rollback requires a database migration rollback, run:

```bash
# Connect to the Railway service shell
railway shell

# Roll back a specific migration
python manage.py migrate <app_name> <previous_migration>
```

---

## Migrations

Migrations run automatically on each deploy (see `/start.sh`).

To run a migration manually:

```bash
railway shell
python manage.py migrate
```

To check for unapplied migrations without applying them:

```bash
python manage.py migrate --check
```

---

## collectstatic

Static files are collected during the Docker build step. If you need to re-run it manually:

```bash
railway shell
python manage.py collectstatic --noinput
```

---

## Restart Web Process

From the Railway dashboard:
- Service → Settings → **Restart**

Or redeploy the current deployment (see above).

---

## Restart Celery Worker

If you run Celery as a separate Railway service:
- Open the Celery service → Settings → **Restart**

If Celery runs as part of the same container (not current setup), restart the web service.

---

## Health Checks

```bash
# Basic liveness
curl https://your-domain.com/health/

# Database
curl https://your-domain.com/health/db/

# Redis
curl https://your-domain.com/health/redis/
```

All should return HTTP 200 with `{"status": "ok"}` (or `{"database": "ok"}`, `{"redis": "ok"}`).

Railway uses `/health/` as the deployment health check with a 300-second timeout.

---

## Troubleshoot Import Jobs

Import jobs go through these states: `UPLOADED → PARSING → PARSED → VALIDATING → VALIDATED → IMPORTING → COMPLETED`

**Check job status:**
```
Django admin → SCM → Import Jobs → filter by status
```

**Common issues:**

| Symptom | Action |
|---------|--------|
| Job stuck in `PARSING` | Check Celery worker logs for `async_parse_import_job` |
| Job stuck in `VALIDATING` | Check Celery worker logs for `async_validate_import_job` |
| Job in `FAILED` | Check `error_rows` count and the import row error messages in admin |
| Celery worker not running | Restart the Celery service; check Redis connectivity |

**Re-run a failed import task:**
```bash
railway shell
python manage.py shell
>>> from apps.scm.imports.tasks import async_parse_import_job
>>> async_parse_import_job.delay(<job_id>)
```

---

## Troubleshoot Tracking Sync

Tracking subscriptions are synced by `sync_due_tracking_subscriptions` (periodic Celery task).

**Check subscription status:**
```
Django admin → SCM → Tracking Subscriptions → filter by status
```

**Common issues:**

| Symptom | Action |
|---------|--------|
| Subscriptions not syncing | Check Celery beat is running; check `next_sync_at` fields |
| Subscription in `FAILED` | Check `last_error_message` in admin |
| External API errors | Check Sentry; verify integration credentials |

**Manually trigger a sync:**
```bash
python manage.py shell
>>> from apps.scm.tracking.tasks import sync_single_tracking_subscription
>>> sync_single_tracking_subscription.delay(<subscription_id>)
```

---

## Where to Find Logs

| Location | How to access |
|----------|---------------|
| Railway web service logs | Railway dashboard → Service → Logs |
| Railway Celery worker logs | Railway dashboard → Celery service → Logs |
| Sentry errors | https://sentry.io — project: container-scm |
| Django admin | https://your-domain.com/admin/ |

---

## Django System Check

Run before deploying to catch configuration problems:

```bash
railway shell
python manage.py check --deploy
```

---

## Create Superuser

```bash
railway shell
python manage.py createsuperuser
```
