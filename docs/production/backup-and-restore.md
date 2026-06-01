# Backup and Restore

## Overview

The database (PostgreSQL) is hosted on **Neon** via Railway. Neon provides continuous WAL archiving and point-in-time recovery (PITR) by default.

Media files (uploads) are stored on **AWS S3** (`USE_S3_MEDIA=True`) and are managed by S3's native versioning and redundancy.

---

## Neon — Automatic Backup (PITR)

Neon archives WAL continuously. The retention window depends on your Neon plan:

| Plan       | PITR window |
|------------|-------------|
| Free       | 24 hours    |
| Launch     | 7 days      |
| Scale/Business | 30 days |

No additional configuration is required — Neon handles this automatically.

---

## Manual Backup (pg_dump)

Useful before risky operations (migrations, bulk imports, schema changes).

```bash
pg_dump "$DATABASE_URL" \
  --format=custom \
  --no-acl \
  --no-owner \
  -f "backup_$(date +%Y%m%d_%H%M%S).dump"
```

Store the dump in a safe location (e.g. S3, local machine). Do **not** commit it to Git.

---

## Restore from Neon PITR

1. Go to the [Neon Console](https://console.neon.tech).
2. Select your project → **Branches**.
3. Click **Restore** on the main branch.
4. Select the target timestamp.
5. Neon creates a restore branch. You can either:
   - Point the Railway `DATABASE_URL` to the restore branch temporarily to verify data.
   - Promote the restore branch to be the new primary.

---

## Restore from a pg_dump file

```bash
# Drop and recreate the database (Railway / Neon: create a new branch instead)
pg_restore \
  --dbname "$DATABASE_URL" \
  --no-acl \
  --no-owner \
  --clean \
  backup_YYYYMMDD_HHMMSS.dump
```

---

## Post-Restore Checklist

After any restore, verify the following before bringing the app back online:

- [ ] Run `python manage.py migrate --check` — no pending migrations.
- [ ] Run `python manage.py check --deploy` — no configuration errors.
- [ ] Hit `/health/db/` — returns `{"database": "ok"}`.
- [ ] Log in as a known user and verify team data is present.
- [ ] Check Celery beat tasks are running (`django_celery_beat` tables populated).
- [ ] Verify media files are accessible (if using S3, no config change needed).
- [ ] Check Sentry for any post-restore errors.

---

## Handling Migrations After a Restore

If you restore to a point before a recent migration was applied:

1. Restore the database.
2. Deploy the code version that matches the restored schema (check `git log` for the migration commit).
3. Incrementally re-apply migrations if moving forward in time: `python manage.py migrate`.

If you restore to a point *after* the current code's migration state, roll back the code to match.

---

## S3 Media Files

Media files are stored in S3 (`AWS_STORAGE_BUCKET_NAME`). S3 does not require manual backup under normal usage — files are replicated across availability zones.

To protect against accidental deletion, enable **S3 Versioning** on the bucket in the AWS console.

To restore a deleted file, use the S3 console or CLI:

```bash
# List versions of a specific object
aws s3api list-object-versions \
  --bucket "$AWS_STORAGE_BUCKET_NAME" \
  --prefix "media/path/to/file.csv"

# Restore a specific version
aws s3api copy-object \
  --copy-source "$AWS_STORAGE_BUCKET_NAME/media/path/to/file.csv?versionId=<version_id>" \
  --bucket "$AWS_STORAGE_BUCKET_NAME" \
  --key "media/path/to/file.csv"
```
