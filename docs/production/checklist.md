# Production Deployment Checklist

Work through this list before going live and after each major deploy.

---

## Environment Variables

- [ ] `SECRET_KEY` — long random string, not the dev default
- [ ] `DATABASE_URL` — injected by Railway Postgres service
- [ ] `REDIS_URL` — injected by Railway Redis service
- [ ] `ALLOWED_HOSTS` — `your-app.up.railway.app,yourdomain.com`
- [ ] `CSRF_TRUSTED_ORIGINS` — `https://your-app.up.railway.app,https://yourdomain.com`
- [ ] `CORS_ALLOWED_ORIGINS` — same as CSRF_TRUSTED_ORIGINS
- [ ] `SENTRY_DSN` — paste from Sentry project settings
- [ ] `SENTRY_ENVIRONMENT` — `production` (or `staging`)
- [ ] `DEFAULT_FROM_EMAIL` / `SERVER_EMAIL` — valid sender addresses
- [ ] `USE_S3_MEDIA=True` + `AWS_*` credentials — required (Railway filesystem is ephemeral)
- [ ] `STRIPE_LIVE_MODE=True` (only if taking live payments)
- [ ] `STRIPE_LIVE_PUBLIC_KEY` / `STRIPE_LIVE_SECRET_KEY` — if live mode

---

## Domain & SSL

- [ ] Custom domain added in Railway dashboard
- [ ] DNS CNAME / A record points to Railway
- [ ] SSL certificate provisioned by Railway (automatic with Let's Encrypt)
- [ ] `https://` redirects work (SECURE_SSL_REDIRECT=True in production settings)
- [ ] HSTS header present (`Strict-Transport-Security: max-age=60`)

---

## Database

- [ ] Railway Postgres service linked to web service
- [ ] `DATABASE_URL` auto-injected and visible in service variables
- [ ] Migrations applied (`python manage.py migrate --check` returns exit 0)
- [ ] Django sites table has the correct domain (`python manage.py shell -c "from django.contrib.sites.models import Site; print(Site.objects.all())"`)

---

## Redis

- [ ] Railway Redis service linked to web service
- [ ] `REDIS_URL` auto-injected and visible in service variables
- [ ] `/health/redis/` returns `{"redis": "ok"}`

---

## Sentry

- [ ] `SENTRY_DSN` set
- [ ] `SENTRY_ENVIRONMENT=production`
- [ ] Test: trigger a 500 error and confirm it appears in Sentry
- [ ] Celery task errors appear in Sentry (CeleryIntegration enabled)

---

## Health Checks

- [ ] `GET /health/` → HTTP 200, `{"status": "ok"}`
- [ ] `GET /health/db/` → HTTP 200, `{"database": "ok"}`
- [ ] `GET /health/redis/` → HTTP 200, `{"redis": "ok"}`
- [ ] Railway service health check configured to `/health/` (already in `railway.json`)

---

## Backup

- [ ] Neon PITR retention confirmed (check plan)
- [ ] Take a manual `pg_dump` before first production data entry
- [ ] S3 bucket versioning enabled (protects media files)

---

## Admin User

- [ ] Superuser created (`python manage.py createsuperuser`)
- [ ] Django admin accessible at `/admin/`
- [ ] Admin login redirects to `/accounts/login/` (not the Django default)

---

## Tenant Isolation Tests

Run before and after each deploy:

```bash
python manage.py test apps.scm.tests.test_tenant_isolation --keepdb
```

- [ ] All tenant isolation tests pass

---

## Smoke Test After Deploy

Walk through these steps in a browser after each deploy:

- [ ] Homepage loads
- [ ] Sign up / log in works
- [ ] Create a team
- [ ] Navigate to `/a/<team-slug>/` — dashboard loads
- [ ] Open Containers list — no 500 errors
- [ ] Open Shipments list — no 500 errors
- [ ] Open Imports list — no 500 errors
- [ ] Upload a small CSV import — job created, status progresses
- [ ] Check `/health/`, `/health/db/`, `/health/redis/` — all green
- [ ] Check Sentry — no new unexpected errors

---

## Security

- [ ] `python manage.py check --deploy` — no warnings
- [ ] `DEBUG=False` confirmed (check response headers: no `X-Debug-*`)
- [ ] `X-Frame-Options: DENY` header present
- [ ] `Strict-Transport-Security` header present
- [ ] CSRF protection active (login/form POST requires CSRF token)
