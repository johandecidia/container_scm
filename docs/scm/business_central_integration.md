# Business Central Integration — Purchase Order Sync

> **Scope: EPIC 1A, Milestones 1–2.** Live read-only Purchase Order sync (OAuth2 +
> OData), incremental watermark, sync-run tracking, versioned credential
> encryption, hardened locking, source metadata + deterministic sync hash,
> reconciliation/soft-delete, read-only enforcement, a scheduled dispatcher, and a
> monitoring UI. Serial numbers, warehouse receipts/shipments, sales orders and any
> write-back are **out of scope**.

## Architecture

- **Business Central is the system of record.** SCM only ever performs `GET`
  requests and never writes back.
- Each connection is an `Integration` row (`provider_family="business_system"`,
  `provider_code="business_central"`), scoped to a `Team`.
- `business_central/auth.py` — Entra ID OAuth2 client-credentials token.
- `business_central/client.py` — read-only OData v2.0 client (dummy + live).
- `business_central/mapper.py` + `schemas.py` — raw OData → normalised DTOs.
- `business_central/sync.py` — orchestrates an incremental, locked sync and
  records an `IntegrationSyncRun`.
- `procurement/services.py::upsert_purchase_orders` — idempotent write into
  `PurchaseOrder`/`PurchaseOrderLine` (the canonical write path for all sources).

### `PurchaseOrder.status` vs SCM logistics status

- **`PurchaseOrder.status`** holds the **Business Central document status**
  (`open`/`released`/…). It is written **only** by the BC mapper/sync.
- **SCM logistics status** (`not_started`/`partially_shipped`/`fully_shipped`/
  `arrived`/`partially_received`/`completed`/`exception`) is **computed** by
  `procurement/selectors.py::get_purchase_order_logistics_status` from
  fulfillment quantities. It is **not stored** and **never written by the BC
  mapper**.

## Configuration

### Integration.config (per team, non-secret)

```json
{
  "tenant_id": "<entra-tenant-guid>",
  "environment": "Production",
  "company_id": "<bc-company-guid>",
  "api_version": "v2.0",
  "sync_enabled": true,
  "page_size": 100,
  "request_timeout_seconds": 30,
  "max_retries": 3,
  "initial_sync_days": 365
}
```

`tenant_id`, `environment`, and `company_id` are required for live access.

### Credentials (secret — never in config)

Stored via the credential service (`integrations/credentials.py`), encrypted at
rest with Fernet:

```python
from apps.scm.integrations.credentials import set_integration_credentials
from apps.scm.integrations.models import IntegrationCredential
set_integration_credentials(
    integration,
    IntegrationCredential.AuthType.OAUTH2,
    {"client_id": "...", "client_secret": "..."},
)
```

### Environment variables

| Variable | Purpose | Default |
| --- | --- | --- |
| `SCM_INTEGRATION_ENCRYPTION_KEY` | Fernet key (url-safe base64, 32 bytes) for credential encryption. Set a stable value in production. | Derived from `SECRET_KEY` (dev only) |

Generate a production key:

```python
from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())
```

## OAuth2

Client-credentials flow against
`https://login.microsoftonline.com/<tenant_id>/oauth2/v2.0/token` with scope
`https://api.businesscentral.dynamics.com/.default`. The token is cached in
memory for the client's lifetime and refreshed shortly before expiry. Tokens are
never logged.

## Sync behaviour

- **Watermark**: each run stores `watermark_from`/`watermark_to`. The next run
  filters `lastModifiedDateTime gt <last completed watermark_to − 5 min overlap>`.
  The watermark advances **only** when a run completes fully — a failed or
  partially completed run leaves it unchanged so nothing is skipped.
- **First run**: full sync, or bounded by `initial_sync_days` if set.
- **Idempotent upsert**: re-syncing identical data reports records as
  *unchanged*, not *updated*. One bad record fails in isolation (per-PO
  transaction) and is reported in `records_failed` / `error_summary`.
- **Locking**: a per-integration cache lock prevents two concurrent PO syncs for
  the same integration; different teams/integrations sync in parallel.
- **Request logging**: every HTTP call writes a sanitised `IntegrationRequestLog`
  (endpoint without secrets, status, duration, correlation id).
- **Retry**: transient failures (timeout, connection error, HTTP 429/502/503/504)
  are retried with backoff; permanent ones (400/401-after-refresh/403/404/500)
  are not.

## Running a sync

Dummy mode (no credentials, fixtures):

```bash
python manage.py bc_sync_purchase_orders --integration <id> --dummy
```

Scheduled / programmatic:

```python
from apps.scm.integrations.business_systems.business_central.sync import (
    sync_purchase_orders_from_business_central,
)
sync_purchase_orders_from_business_central(integration)  # live
```

The Celery task `apps.scm.procurement.tasks.sync_purchase_orders_from_bc(team_id)`
resolves the team's active BC integration and runs a sync (full scheduled
dispatcher is Milestone 2).

---

## Live sandbox verification checklist

Run these once real sandbox credentials are available. All commands run in the
Django environment (`make manage ARGS='…'` or `python manage.py …`).

### BC administrator prerequisites

- [ ] App registration created in Entra ID (Azure AD).
- [ ] API permission **Dynamics 365 Business Central** granted.
- [ ] **Admin consent** granted for the permission.
- [ ] Client secret generated (note the value once — it is not shown again).
- [ ] The application is granted access inside Business Central (Microsoft Entra
      Applications / a BC user linked to the app).
- [ ] `company_id` (GUID), `environment` name, and `tenant_id` (GUID) recorded.

### Configure the integration

1. Create the integration (family `business_system`, provider `business_central`)
   with the config block above.
2. Store credentials with `set_integration_credentials` (see above).
3. Set `SCM_INTEGRATION_ENCRYPTION_KEY` in the environment.

### 1. Test the connection

```bash
python manage.py bc_test_connection --integration <id>
```

Expected success output:

```
Connected to Business Central
```

### 2. Perform a limited purchase-order sync

Set `initial_sync_days` small (e.g. `7`) to bound the first pull, then:

```bash
python manage.py bc_sync_purchase_orders --integration <id>
```

Expected output (counts will vary):

```
Sync completed: fetched=N created=N updated=0 unchanged=0 failed=0
```

Then confirm in the admin: `IntegrationSyncRun` shows a `completed` run with a
`watermark_to`, `IntegrationRequestLog` shows sanitised GET entries, and
`PurchaseOrder`/`PurchaseOrderLine` rows exist for the team.

### Diagnosing failures

The command prints the typed exception name; check `IntegrationRequestLog` and
`Integration.last_error_message` for detail. None of these expose tokens/secrets.

| Symptom | Likely cause | Action |
| --- | --- | --- |
| `BusinessCentralAuthenticationError` on connect | Wrong `client_id`/`client_secret`, or no admin consent | Re-check the secret; confirm admin consent was granted |
| `BusinessCentralConfigurationError` before any call | Missing `tenant_id`/`environment`/`company_id` or credentials | Fill the config / store credentials |
| HTTP 403 (`BusinessCentralResponseError`) | App lacks access **inside** Business Central | Grant the application/user access in BC |
| HTTP 404 on the company path | Wrong `company_id` or `environment` | Verify the company GUID and environment name |
| `BusinessCentralConnectionError` / timeout | Network / wrong tenant host | Check connectivity; retry (transient) |
| Auth OK but 0 orders | `initial_sync_days` too small, or no POs modified in range | Widen `initial_sync_days` / clear the watermark |

> **Security note:** the docs above contain no real secrets. Never commit a
> client secret or a Fernet key.

---

## Milestone 2 additions

### Credential storage & production key

- Stored credentials are versioned: `fernet:v1:<ciphertext>` for all new writes;
  `legacy:base64:<value>` (and unprefixed transitional values) are read-only. A
  `fernet:v1` decryption failure raises a sanitised `CredentialDecryptionError`
  and is never silently treated as legacy.
- Migrate legacy rows: `python manage.py migrate_integration_credentials [--dry-run]`
  (never prints secret values).
- In production, `SCM_INTEGRATION_ENCRYPTION_KEY` is **mandatory**: a system check
  (`scm_integrations.E001`) errors if it is missing, and the credential service
  refuses the SECRET_KEY-derived fallback.

### Source metadata & sync hash

- `PurchaseOrder` (and lines) carry `source_system` (`business_central` /
  `document_import` / `manual`), `source_company_id`, `source_last_modified_at`,
  `last_synced_at`, `raw_payload` (sanitised — never auth material), `sync_hash`,
  `source_active`, `source_deleted_at`.
- `sync_hash` is a deterministic SHA-256 over only the source-owned business
  content; it drives created/updated/unchanged. `last_synced_at` is technical and
  is bumped even for unchanged records without counting as a business update.

### Reconciliation vs incremental sync

- Incremental sync (default, watermark-bounded) never deactivates records for being
  absent from a bounded result.
- Full reconciliation soft-deletes records gone at source
  (`source_active=False` + `source_deleted_at`), never hard-deleting; run it
  deliberately: `python manage.py bc_reconcile_purchase_orders --integration <id>`.
- Three distinct concepts: `status=closed` (BC document closed, still present) ≠
  `source_active=False` (gone at source) ≠ computed SCM logistics `completed`.

### Read-only enforcement

- BC-sourced POs (`source_system=business_central`) are read-only in SCM: admin
  business/source fields are read-only and delete is blocked (PO and lines); the
  UI shows a "Managed by Business Central" badge. Manual / document-import POs
  remain editable.

### Scheduling & monitoring

- Celery Beat runs `sync_enabled_business_central_integrations_task` every 5 minutes;
  it queues one `sync_business_central_purchase_orders_task` per *due* integration
  (per-integration `purchase_order_sync_interval_minutes`, failure backoff via
  `purchase_order_sync_failure_backoff_minutes`, skipping in-progress runs).
- Monitoring UI at `/scm/integrations/`: per-integration health, latest run counts,
  watermark, and **Test connection** / **Sync purchase orders now** buttons (POST +
  CSRF, team-scoped, queue Celery tasks, never display credentials).
