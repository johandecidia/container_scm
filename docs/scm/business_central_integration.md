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
- `business_central/lifecycle.py` — the run lifecycle shared by every BC resource:
  validation, per-resource lock, integration-wide source binding, the
  `IntegrationSyncRun` record and safe sync markers. A resource supplies only a
  step (fetch → map → validate → upsert) returning a `SyncOutcome`.
- `business_central/resources.py` — which resources are *defined*,
  *implemented* and *enabled*, and their intervals.
- `business_central/sync.py` — the purchase-order step.
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
  "scheduled_sync_enabled": false,
  "page_size": 100,
  "request_timeout_seconds": 30,
  "max_retries": 3,
  "initial_sync_days": 365
}
```

`tenant_id`, `environment`, and `company_id` (a GUID) are required for live
access. Prefer `setup_business_central_integration` (below) over editing this by
hand.

| Key | Default | Meaning |
| --- | --- | --- |
| `sync_enabled` | `true` (the setup command writes `false` unless `--enable-sync`) | Master switch: allows **any** sync of any resource, manual or scheduled. |
| `scheduled_sync_enabled` | `false` | Opt-in for Celery Beat (all resources). Only `true` (a JSON boolean) counts. The setup command always writes `false`. |
| `resources` | — | Per-resource settings, see *Resources* below. |
| `initial_sync_days` | `0` (full) | First sync only pulls orders modified in the last N days. Older, unmodified orders are then never pulled by incremental syncs — prefer a full first sync if the order count is modest. |
| `page_size` | `100` | Sent as `Prefer: odata.maxpagesize` (1–20000). Not `$top`. |
| `api_version` | `v2.0` | Only the `/api/{version}` segment; the leading `/v2.0` service segment is fixed. |

### Resources

`IntegrationSyncRun.resource_type` knows `purchase_orders`, `sales_orders`,
`purchase_receipts`, `sales_shipments` and `item_ledger_entries`. A resource is
synced on a schedule only when all of these hold:

1. it is **defined** (a `ResourceType`);
2. it is **implemented** (registered in `resources._SYNC_IMPLEMENTATIONS` —
   `purchase_orders` and, since stage 1a, `item_ledger_entries`);
3. it is **enabled** for the integration;
4. the integration has `sync_enabled` and `scheduled_sync_enabled: true`.

```json
{
  "resources": {
    "purchase_orders": {"enabled": true, "interval_minutes": 30, "failure_backoff_minutes": 15},
    "item_ledger_entries": {"enabled": false, "interval_minutes": 30}
  }
}
```

Without a `resources` entry, purchase orders are enabled and keep using
`purchase_order_sync_interval_minutes` / `purchase_order_sync_failure_backoff_minutes`
(a `resources.purchase_orders` value takes precedence). Every other resource is
disabled unless its `enabled` is literally `true`, and an enabled resource without
an implementation is never scheduled or run.

**Sync markers.** `watermark_from`/`watermark_to` carry timestamp markers; integer
markers (e.g. a ledger `entryNo`) go in `metadata["cursor_from"]`/`["cursor_to"]`
(`services.get_last_successful_cursor`). Both advance only on a `COMPLETED` run.

**Locks.** One lock per integration *and* resource (`{integration_id}:{resource}`,
unchanged for purchase orders): the same resource never runs twice, different
resources may run in parallel.

**API paths.** The client reaches only allowlisted APIs: `standard`
(`/api/{api_version}`) and `analytics` (`/api/microsoft/analytics/v1.0`).
Endpoints must be relative; a `@odata.nextLink` must stay on the BC host *and*
inside the API it was requested from. Only `GET` is ever sent.

### Credentials (secret — never in config)

Stored via the credential service (`integrations/credentials.py`), encrypted at
rest with Fernet. The setup command reads them from environment variables so
they never appear on the command line, in `config`, or in output:

```bash
export BC_CLIENT_ID='<entra application (client) id>'
read -rs BC_CLIENT_SECRET && export BC_CLIENT_SECRET   # paste, no echo, not in shell history
python manage.py setup_business_central_integration --team <slug> \
  --tenant-id <tenant-guid> --environment <env> --company-id <company-guid>
unset BC_CLIENT_SECRET
```

Programmatic equivalent (e.g. for rotation):
`set_integration_credentials(integration, IntegrationCredential.AuthType.OAUTH2, {"client_id": ..., "client_secret": ...})`.
Re-running the setup command without the env vars keeps the stored credentials.

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
- **First run**: full sync, or bounded by `initial_sync_days` if set. Watermarks are
  per data source (see *Sandbox / production isolation*).
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
- **Line changes**: `purchaseOrderLine` has no `lastModifiedDateTime` in API v2.0, so
  the watermark relies on the header's. Verify in the sandbox whether a line-only edit
  bumps the header timestamp; if not, a line-only change is picked up only when the
  header changes (or by reconciliation's full scan — which does not update values).

## API endpoints used (read-only)

All requests are `GET` against the standard API v2.0:

```
https://api.businesscentral.dynamics.com/v2.0/{tenant_id}/{environment}/api/v2.0/companies
https://api.businesscentral.dynamics.com/v2.0/{tenant_id}/{environment}/api/v2.0/companies({company_id})/purchaseOrders
https://api.businesscentral.dynamics.com/v2.0/{tenant_id}/{environment}/api/v2.0/companies({company_id})/purchaseOrders({id})/purchaseOrderLines
```

- Tenant and environment are URL-encoded; company and purchase-order ids must be GUIDs.
- `$select` uses the documented v2.0 property names (header: `requestedReceiptDate`,
  `fullyReceived`; line: `lineObjectNumber`, `lineType`). An unknown property makes BC
  reject the whole request.
- Paging uses `Prefer: odata.maxpagesize` and follows `@odata.nextLink` (only to the BC
  API host). `$top` is sent only for an explicit limit (inspection).

### Field mapping

| Business Central (API v2.0) | SCM |
| --- | --- |
| `purchaseOrder.id` | `PurchaseOrder.external_id` (required) |
| `number` | `po_number` (required) |
| `vendorNumber` / `vendorName` | `supplier_no` / `supplier_name` |
| `status` (+ `fullyReceived`) | `status`: `Draft`/`In Review` → `open`, `Open` → `released`, `fullyReceived=true` → `fully_received`; anything else → `open` + a warning |
| `requestedReceiptDate` | `expected_receipt_date` (header has no expected receipt date in v2.0) |
| `currencyCode` | `currency` (empty → `EUR`) |
| `lastModifiedDateTime` | `source_last_modified_at` + watermark |
| `purchaseOrderLine.id` | `PurchaseOrderLine.external_id` (required) |
| `sequence` | `line_no` |
| `lineObjectNumber` | `item_no` |
| `quantity` | `ordered_qty` |
| `directUnitCost` | `unit_price` |
| `expectedReceiptDate` | `expected_receipt_date` |

BC's blank date `0001-01-01` becomes `None`; datetimes are stored as UTC. The full
selected payload is kept in `raw_payload`. Received/invoiced quantities stay in
`raw_payload` only (receipts are out of scope). A record with a missing id/number or a
non-numeric quantity fails on its own (`records_failed`, partial run, watermark held).

## Sandbox / production isolation

A BC sandbox is usually a copy of production with the **same company id and record
GUIDs**, and `PurchaseOrder` has no environment column. Safeguards:

1. **One Team per environment.** A team has at most one BC integration
   (`unique (team, provider_code)`), and the setup command refuses to re-point a team at
   another environment/company.
2. **Source binding (integration-wide).** Every sync run stores `metadata["source"]` =
   tenant, environment, company. Once an integration has imported anything (completed
   or partial run of *any* resource), a sync or reconcile of any resource from another
   source — including runs recorded before sources were tracked, and the dummy client —
   is refused with *"Use a separate Team for this environment."* A run still in progress
   (started < 2 h ago) binds too, and the check and the run claim happen together
   under a row lock on the integration, so parallel resource syncs cannot read two
   sources. Failed runs do not bind.
3. **Watermarks per source.** Only completed runs of the same source supply a watermark.
4. **Ownership on upsert.** An existing PO from another `source_system` or another
   `source_company_id` is never overwritten; that record fails instead.
5. **Reconcile** additionally requires a completed sync from the same source.

## Scheduling controls

| Control | Scope | Default |
| --- | --- | --- |
| `SCM_BUSINESS_CENTRAL_DISPATCH_ENABLED` (env) | Pauses the whole Beat dispatcher | `True` |
| `config.scheduled_sync_enabled` | Per integration opt-in for Beat | `false` |
| `config.sync_enabled` | Any sync, manual or scheduled | `true` |

The dispatcher, a queued *scheduled* task (re-checked when it runs) and the legacy
`procurement.tasks.sync_purchase_orders_from_bc` all require `scheduled_sync_enabled`.
`bc_test_connection`, `bc_inspect_purchase_orders` and manual syncs do not.

## Permissions

The monitoring pages (`/scm/integrations/`) are visible to team members. **Test
connection**, **Sync purchase orders now** and the (not yet implemented)
create/update/delete endpoints require a team administrator
(`scm_team_admin_required`; members get 404 and do not see the buttons). Credentials
are never editable or shown in the UI.

## Dummy mode

`python manage.py bc_sync_purchase_orders --integration <id> --dummy` (and the admin
"Sync from Business Central (dummy data)" action) import the local fixtures without
credentials. Dummy data binds that integration to the dummy source, so a team used for
fixtures cannot later sync a live environment — use a separate team.

---

## Sandbox verification runbook (BC-SANDBOX-1)

All commands run in the Django environment whose database should receive the data
(`uv run manage.py …` locally, or `docker compose exec web python manage.py …`).
`SCM_INTEGRATION_ENCRYPTION_KEY` must be the same in every process that reads the
credentials (shell, web, Celery).

### 0. Prerequisites (NAB / Entra)

- [ ] Entra app registration with Business Central `API.ReadWrite.All` (application) and
      admin consent. (SCM only issues `GET` requests.)
- [ ] The app is registered **inside** Business Central (*Microsoft Entra Applications*),
      state *Enabled*, with read permission sets for the target company covering
      companies, purchase orders and purchase order lines (e.g. `D365 READ`).
- [ ] Tenant id, environment name and company id recorded; client secret available.

### 1. Create a dedicated Team

Create a team for the sandbox (e.g. slug `bc-sandbox`) through the normal UI. Never
point a team that holds production purchase orders at the sandbox.

### 2. Create the integration (sync disabled, schedule disabled)

```bash
export BC_CLIENT_ID='<entra application (client) id>'
read -rs BC_CLIENT_SECRET && export BC_CLIENT_SECRET
python manage.py setup_business_central_integration --team bc-sandbox \
  --tenant-id 609e5a65-e2b8-49d7-85d4-5cdadf170d85 \
  --environment Sandbox_eligra_261002 \
  --company-id 782d7133-5033-4c4b-9a29-83f7f44f062d
unset BC_CLIENT_SECRET BC_CLIENT_ID
```

Note the integration id it prints. It is created with `sync_enabled=false` and
`scheduled_sync_enabled=false`.

### 3. Test the connection

```bash
python manage.py bc_test_connection --integration <id>
```

Expected:

```
  OK  OAuth2 token acquired
  OK  Environment 'Sandbox_eligra_261002' reachable (N company/companies visible)
  OK  Company accessible: <company name>
  OK  Purchase orders readable
Business Central connection: OK
```

### 4. Inspect purchase orders (no import)

```bash
python manage.py bc_inspect_purchase_orders --integration <id> --limit 3
python manage.py bc_inspect_purchase_orders --integration <id> --limit 1 --show-fields
```

`--limit` (1–50) and `--line-limit` (1–200, default 10) are sent to BC as `$top`.
Nothing is written to procurement tables. Check that statuses, item numbers, quantities
and dates look right, and that no record reports *"Would NOT import"*.

### 5. Enable manual sync and run the first sync

```bash
python manage.py setup_business_central_integration --team bc-sandbox \
  --tenant-id 609e5a65-e2b8-49d7-85d4-5cdadf170d85 \
  --environment Sandbox_eligra_261002 \
  --company-id 782d7133-5033-4c4b-9a29-83f7f44f062d --enable-sync
python manage.py bc_sync_purchase_orders --integration <id>
```

(Re-running setup without the credential env vars keeps the stored credentials. Add
`--initial-sync-days N` only if you deliberately want to skip older orders.)

Expected: `Sync completed: fetched=N created=N updated=0 unchanged=0 failed=0`.
Run it a second time: `created=0 … unchanged=N` (idempotent).

### 6. Verify imported records

- `/scm/integrations/<id>/`: latest run *Completed*, counts, watermark.
- `/scm/procurement/purchase-orders/` (logged into the sandbox team).
- Django admin → *Integration sync runs*: `metadata.source.environment` is
  `sandbox_eligra_261002`; *Integration request logs*: sanitised GETs only.
- Shell:
  ```python
  from apps.scm.procurement.models import PurchaseOrder
  PurchaseOrder.objects.filter(team__slug="bc-sandbox").values("po_number", "status", "source_company_id")
  ```

### Troubleshooting

`bc_*` commands print a category and a sanitised message (BC's error code in
`[code=…]` when present). `Integration.last_error_message` and
`IntegrationRequestLog` carry the same; none contain tokens or secrets.

| Category | Likely cause | Action |
| --- | --- | --- |
| Configuration error | Missing tenant/environment/company, non-GUID company, missing credentials, or sync disabled | Re-run the setup command; `--enable-sync` for manual syncs |
| Authentication failure: *Token request failed (HTTP 400/401, error=invalid_client)* | Wrong client id/secret, expired secret | Re-check/rotate the secret |
| Authentication failure: *rejected the access token (401)* | Token OK, but the app is not registered/enabled in BC | NAB: *Microsoft Entra Applications* in BC |
| Missing Business Central permissions (403) | App lacks permission sets in BC | NAB: grant read permission sets for the company |
| Invalid environment | Environment name wrong (case/spelling) or deleted | Check the name in the BC admin center |
| Invalid company | Company id wrong, or not visible to the app | Compare with the company GUID; NAB checks company access |
| Network error | DNS/timeout/proxy | Retry; check outbound HTTPS to `api.businesscentral.dynamics.com` and `login.microsoftonline.com` |
| Unexpected API response (HTTP 400) | Usually a `$select` property the environment does not expose | Report the `[code=…]`; compare with the v2.0 reference |
| *already synced data from a different (or unrecorded) Business Central source* | Team previously synced another environment or dummy data | Use a fresh Team |

> **Security note:** this document contains no secrets. Never commit a client secret
> or a Fernet key, never pass secrets as command arguments, never paste them into
> `Integration.config`.

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
  it queues one task per *due* integration **and resource** that has opted in with
  `scheduled_sync_enabled: true` (see *Resources*: interval, failure backoff,
  skipping in-progress runs). Purchase orders keep
  `sync_business_central_purchase_orders_task`; other resources would use
  `sync_business_central_resource_task(integration_id, resource_type, trigger_type)`.
- Monitoring UI at `/scm/integrations/`: per-integration health, latest run counts,
  watermark, and — for team administrators — **Test connection** / **Sync purchase
  orders now** buttons (POST + CSRF, team-scoped, queue Celery tasks, never display
  credentials).
- The detail page also lists every resource type (`selectors.get_resource_sync_statuses`):
  implemented / enabled, latest run, last finished run counts, *Never synced*, and
  the error of a resource whose latest finished run failed or was partial. The list
  page flags *N resource error(s)*. All of it is read from `IntegrationSyncRun`; no
  extra status is stored.
- `Integration.status` / `last_error_message` are shared by every resource. A failed
  or partial run sets them as before; a completed run clears them only when no
  *other* resource has an open error — otherwise the integration stays `error` and
  the message lists the failing resources (`services.mark_integration_sync_success`).
  A successful connection test still clears them (connection health); the
  per-resource table remains the source of truth.

## Item ledger entries (stage 1a)

### API (verified read-only against the sandbox, 2026-10-25)

`GET …/v2.0/{tenant}/{environment}/api/microsoft/analytics/v1.0/companies({companyId})/itemLedgerEntries`
(Analytics API, query 36968). Standard API v2.0 has no serial numbers.

Fields: `entryNo` (integer), `itemNo`, `serialNo`, `lotNo`, `entryType`, `documentType`,
`documentNo`, `postingDate`, `quantity`, `remainingQuantity`, `invoicedQuantity`, `open`,
`positive`, `locationCode`, `sourceType`, `sourceNo`, `unitOfMeasureCode`,
`qtyPerUnitOfMeasure`, `costAmountActual`, `salesAmountActual`, `dimensionSetID`,
`expirationDate`. No etag, no last-modified field, no order / order-line references.

| OData feature | Result |
| --- | --- |
| `$filter=entryNo gt N` / `ge` | works |
| `$orderby=entryNo asc` | works |
| `$orderby=entryNo desc` | **HTTP 501** |
| `not …` in a filter | **HTTP 501** |
| `startswith(itemNo,'X')`, joined with `or` | works |
| `serialNo ne ''`, `open eq true`, `postingDate ge …` | works |
| `$count=true`, `$select` | works |
| filter + order + paging | works: `@odata.nextLink` keeps filter and order and pages by `$skiptoken=<last entryNo>` (keyset) |

Sandbox snapshot: 95 110 entries (entryNo 1…95 110, no gaps in the sampled tail);
1 869 with a serial — 1 860 on `CON*` items, 5 on `MAFI-CW` (real container numbers)
and 4 test serials on `P1`. A `CON` prefix filter would therefore drop container
serials. 377 serials occur in more than one entry and 26 on more than one item
(reclassification via `KORR`), so identity is the serial alone, not item + serial.

`entryNo` is unique per company and assigned in posting order. Posted entries are
never edited in their identity fields; corrections are new entries (e.g.
negative/positive adjustments). `open`, `remainingQuantity`, `invoicedQuantity` and the
amounts **do** change after posting (application, invoicing, cost adjustment). Deleted
ledger entries could not be observed through the API (BC does not delete them in
normal operation).

### Model

`BusinessCentralItemLedgerEntry` (`scm_integrations`): team, integration, `company_id`,
`entry_no`, item, raw + normalised serial, `serial_status`
(`valid_iso` / `invalid_iso` / `missing_serial`), `item_classification` (always
`unclassified` in stage 1a), entry/document type and no., posting date, quantity,
location, source type/no., `raw_payload`, `sync_hash`, `synced_at`. Unique on
`(integration, company_id, entry_no)`. Fields BC changes later stay in `raw_payload`
only. No relation to `Container`; nothing creates or changes containers.

Serials are normalised with `containers.intake.normalise_container_number` (trim,
inner whitespace, upper case) and checked with `parse_and_validate_container_number`;
a wrong check digit is never corrected. Every entry is stored whatever its serial.

### Sync (`item_ledger_sync.py`)

Off by default. Enable per integration (manual runs need only this; scheduled runs
also need `scheduled_sync_enabled: true`):

```jsonc
{"resources": {"item_ledger_entries": {
  "enabled": true,
  "item_no_prefixes": [],          // default: every item; e.g. ["CON", "MAFI"]
  "serial_only": false,            // only entries with a serial number
  "max_entries_per_run": 5000,     // 1–100 000
  "interval_minutes": 30
}}}
```

- **Incremental by `entryNo`.** Each run requests `entryNo gt <cursor>` in ascending
  order and stores whole pages until `max_entries_per_run` is reached
  (`metadata["has_more"]` is then true). The cursor (`metadata["cursor_to"]`) is the
  last entry stored and is kept only when the run completes fully.
- **Initial import** is a series of such runs:
  `python manage.py bc_sync_item_ledger_entries --integration <id> --until-complete`
  (bounded by `--max-runs`; `--dummy` uses the fixtures). Nothing starts on its own.
- **Resumption.** Each page is upserted in its own transaction. A run that fails
  part-way keeps those rows; the next run starts from the old cursor and reports them
  as unchanged. A record without an integer `entryNo` fails, makes the run partial and
  holds the cursor. A response that is not strictly ascending fails the run.
- **Idempotent** on `(integration, company_id, entry_no)`; an unchanged row (same
  `sync_hash`) only gets `synced_at` bumped, a changed row is rewritten.
- **Scoped cursor.** The filter (`item_no_prefixes`, `serial_only`) is stored as
  `metadata["scope"]`, and a cursor is only reused within the same scope; widening the
  filter re-reads from the start (existing rows come back unchanged).
- **Monitoring.** Standard run counts plus `metadata["quality"]`
  (`valid_iso`, `invalid_iso`, `missing_serial`, `unclassified`) on the integration
  detail page. Invalid serials are queryable on the model (`serial_status`) and in
  Django admin (read-only).
- **No side effects.** No containers, movements, receipts, shipments or tracking.

### Reconciliation (proposal, not implemented)

Incremental sync never re-reads an entry once the cursor has passed it, so the
fields BC updates after posting (`open`, `remainingQuantity`, `invoicedQuantity`,
amounts) go stale in `raw_payload`. Identity fields do not change. Proposed: a
periodic, separate pass that re-reads `open eq true` entries (verified filter) and,
less often, a full re-scan of the selected scope through the same idempotent upsert;
entries missing from a full scan would be flagged, never deleted. Until then, use the
columns (immutable) rather than the mutable values in `raw_payload`.

## Container identity (stage 1b)

### Item classification

`BusinessCentralItemRule` (per integration, edited by Team Administrators under
*Settings → Integrations → Business Central → Item classification*,
`/scm/integrations/<id>/items/`): match type (`exact` / `prefix`), item no., optional
variant (exact rules only), classification (`container` / `not_container`) and, for
containers, the Pegasus `EquipmentType`.

Resolution (`item_rules.ItemRuleSet`), most specific first: exact item + variant →
exact item → longest prefix → unclassified. An exact `not_container` rule therefore
overrides a `CON` prefix. Item numbers are compared case-insensitively. Nothing is
inferred from the item number: an item without a rule is unclassified.

Variants: the Analytics API sends no `variantCode` (verified). An item whose exact
rules all name a variant is reported as needing a variant rather than guessed.

The page also lists every item number in the synced entries with its current
classification, unclassified items first.

### Linking entries to containers (operation A)

Identity is the team's `Container` with the entry's valid ISO 6346 number (owner,
category, serial) — never the item number. `container_links.match_item_ledger_entries`
(`bc_match_item_ledger_entries --integration <id>`, or the Celery task
`match_business_central_item_ledger_entries_task`) recomputes, for every entry, the
nullable `container` FK, `link_status` and `item_classification` from the current
rules. It only reads containers: nothing is created, edited or moved, and Business
Central is not called. It runs separately from the sync, can be repeated after rule
changes, and only rows whose link changed are written (`link_updated_at`).

| `link_status` | Meaning |
| --- | --- |
| `linked` | Linked to the team's container |
| `equipment_type_conflict` | Linked, but the item's mapped type differs from the container's (the container is not changed) |
| `container_not_found` | Valid container item, not in Pegasus — a creation candidate |
| `missing_equipment_type` | Container item without a mapped type and no existing container |
| `unclassified_item` / `non_container_item` / `variant_unresolved` | Item not (yet) a container item |
| `invalid_serial` / `missing_serial` | No usable ISO number |
| `pending` | Not checked yet |

The status is the entry's current state (no duplicates across passes). A deleted
container leaves its entries with `container = NULL`; commercial history is never
deleted. The BC sync never writes these fields.

## Technical debt left after stage 0

- `schemas.NormalizedCustomerOrder(Line)` (plain dataclasses) and
  `BusinessCentralMapper.map_sales_order` (raises `NotImplementedError`) are unused
  placeholders. Replace them with Pydantic schemas when sales orders are built
  (stage 5).
- `test_connection` only proves purchase-order read access; each new resource needs
  its own permission check (e.g. item ledger entries, analytics API).
- Reconciliation (`reconcile.py`) and **Sync purchase orders now** are
  purchase-order only.
- The client issues one `requests.get` per call (there is no shared
  `requests.Session`); retry, 401 refresh, logging and pagination are shared in
  `_request` / `_fetch_all_pages`.
- `procurement.tasks.sync_purchase_orders_from_bc` is kept only as a thin,
  team-based wrapper (Celery Beat uses the database scheduler, so a stored entry
  might still reference it). Remove it once no Beat entry points at it.
- The *stale running* threshold (2 h) is shared by the dispatcher and the source
  binding.

