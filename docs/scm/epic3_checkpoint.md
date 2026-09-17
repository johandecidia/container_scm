# EPIC 3 — Final Checkpoint (feature freeze)

Date: 2026-09-13 · Branch: `phase2_scm` · Last feature commit: `TEAM-ADMIN-4`

**EPIC 3 is frozen.** No further visibility, tracking, location, map, queue or
analytics features. The next phase is Production Readiness / Live Pilot: operational
hardening, a real deployment, and defects found with real MCR data. Changes from here
should be production-blocking fixes, not refactors of working code.

Operational procedures are **not** repeated here — see
[`production_readiness.md`](production_readiness.md) for the runbook,
[`../production/checklist.md`](../production/checklist.md) for the deploy checklist and
[`../production/backup-and-restore.md`](../production/backup-and-restore.md) for
backups. This document is the state of the epic at the freeze.

---

## 1. What EPIC 3 now contains

### Containers and physical state
- Container master data on ISO 6346 identity, with single, pasted-list and CSV intake
  and a check-digit validator.
- **Container Workspace** — overview, journey, activity and related objects in one
  page. Carrier, tracking status, vessel, voyage and ETA are all *derived* from the
  container's shipment and tracking events, never stored on `Container`.
- Physical movements (gate in, gate out, depot receipt, transfer) with centralised
  precedence rules; `Container.current_location` is the projection of that history.
- Planned-container discovery: expected numbers polled until the carrier knows them.
- **Container conditions** as per-team master data (`ContainerCondition`), editable
  from Settings, seeded for every new team by a single signal.

### Canonical locations
- Three concepts kept apart by design — identity (`ContainerLocation`), evidence
  (`TrackingEvent` location fields, `LocationAlias`) and state (`Container.current_location`,
  `ContainerMovement`).
- One resolver (`containers/location_resolver.py`) owns every place-name rule; no
  fuzzy matching, and ambiguity resolves to `AMBIGUOUS` rather than to a guess.
- Location hierarchy, aliases, the Location Workspace, and a Data Quality page that
  turns unresolved carrier evidence into master-data tasks.

### Tracking
- One ingestion path for every provider (`tracking/ingestion.py`), with event
  fingerprinting and upsert, so no provider has its own event model.
- **Carrier resolution** (who is carrying the box) and **provider routing** (who to ask
  about it) are separate decisions in separate modules; activation is the only thing
  that writes.
- Multi-source journeys: a container may have several verified sources over one
  physical journey; gaps, positions, ETA history and delay/exception detection are all
  computed on read.
- Scheduled polling with a state-based interval, failure back-off and a dispatch cap.
- Raw payload retention: bodies archived after 90 days, records deleted only if a
  deletion window is explicitly configured.
- **Team tracking default** (`TeamTrackingSettings`, Traqo) and **per-container
  provider override** (`Container.tracking_provider_override`), integrated into
  `resolve_tracking_route`. An explicit choice never silently falls back.

### Visibility
- Control Tower with actionable KPI tiles, plus the Exceptions and Upcoming Arrivals
  work queues it drills into.
- Arrival lifecycle (`EXPECTED` → `ARRIVING` → arrived-on-physical-movement only).
- Map positions derived from tracking events, with position quality carried alongside
  the position.
- Global SCM search — a container number finds the container.

### Procurement and supply
- Purchase orders and lines, with a Purchase Order Workspace.
- Business Central sync (idempotent, watermarked, advisory-locked) plus CSV/XML/PDF
  import jobs.
- Supplier deliveries.

### Analytics and audit
- Analytics dashboard, saved filters, alerts, live stats.
- `SCMAuditLog` — append-only record of key state changes.

### Team administration (this epic's last feature)
- `/scm/settings/` — Members, Tracking integrations, Container settings. Every view is
  `@scm_team_admin_required`; navigation is hidden for non-admins but the decorator is
  what closes the area.
- Members: view, invite, resend/cancel invitation, change role, remove — all through
  the team app's own `Membership`, `Invitation` and role helpers. The final
  administrator can be neither removed nor demoted.
- Tracking: Maersk, CMA CGM and Hapag-Lloyd with active/inactive, credential entry and
  replacement, connection test, last success, last test and last error. Secrets are
  never rendered back.

---

## 2. Known defects and limitations

Nothing here is a regression; all of it is scope that was deliberately left out.

| Area | Limitation |
|---|---|
| Direct carriers | Only **Maersk, CMA CGM and Hapag-Lloyd** have working clients. MSC, COSCO, ONE, Evergreen, HMM, Yang Ming and ZIM are registered stubs whose clients raise; they are correctly excluded from Settings, discovery sweeps and the override list, but a user may still expect to see them. |
| Connection test | Needs a `test_connection_reference` — a container number the account can see. Maersk ships one; CMA CGM and Hapag-Lloyd deliberately do not, so the test button stays hidden until an admin supplies one. |
| Hapag-Lloyd | Configured for the IBM API Connect gateway (client id/secret headers). An OAuth product needs `HAPAG_TOKEN_URL` and the `setup_hapag_lloyd_integration` command, not the Settings UI. |
| Vizion | Carrier **identification only**. It is deliberately not schedulable — a reference is its billable unit, so polling it would buy one per cycle. Its stored events are correct but never refreshed. |
| Traqo | One installation-wide account (`TRAQO_API_KEY`), not a per-team credential, so it cannot be configured from Settings and every team shares the quota. |
| Team tracking default | Stored and read by routing, but Traqo is the only provider the scheduled sync can drive alone, so the Settings page displays it rather than offering a selector. |
| Container override | The option list needs the container's carrier to be established; until then only Traqo is offered. An override on a container whose carrier later changes shows as "(unavailable)" and must be corrected by hand — by design, so the misconfiguration is visible. |
| Provider switch | Superseded watches are paused, not resumed automatically if the override is cleared again. Reactivation is a manual refresh. |
| Webhooks | Maersk, CMA CGM and Hapag-Lloyd advertise webhooks and subscriptions; only pull is implemented. |
| Business Central | Purchase orders only. |
| PDF import | Requires an external FastAPI extraction service (`SCM_PDF_FASTAPI_BASE_URL`); unset means PDF import is unavailable. |
| Roles | Two roles, `admin` and `member`. No finer permissions, and that was a deliberate non-goal. |

---

## 3. Integrations that are actually configured and testable

| Integration | Credential source | State | How to verify |
|---|---|---|---|
| **Maersk** Track & Trace | per-team `IntegrationCredential` (API key), Settings → Tracking or `setup_maersk_integration` | Working client, DCSA 2.2, shipped config incl. test reference | Settings → Tracking → Test connection; `manage.py test_maersk_tracking <container> --team <slug>` |
| **CMA CGM** Track & Trace | per-team API key (`keyId` header) | Working client, DCSA 2.2 | Test connection **after** setting a test reference; `manage.py test_carrier_tracking` |
| **Hapag-Lloyd** Track & Trace | per-team client id + secret | Working client, IBM gateway style | As above; `setup_hapag_lloyd_integration` for the OAuth product |
| **Traqo Ocean** | installation-wide `TRAQO_API_KEY` + `TRAQO_ENABLED` | Working: carrier lookup, candidate probe, container tracking, scheduled refresh | `manage.py traqo_test` (sandbox needs no key) |
| **Vizion** | installation-wide `VIZION_API_KEY` + `VIZION_ENABLED` | Identification only, never polled | `manage.py vizion_test` — spends quota |
| **Business Central** | per-team `IntegrationCredential` | Working, scheduled dispatcher every 5 min | `manage.py bc_test_connection`; Integrations monitoring page |
| **PDF extraction** | `SCM_PDF_FASTAPI_BASE_URL` | External service, not deployed with this app | Upload a PDF import job |
| **Stripe / dj-stripe** | Pegasus default | Present, not part of EPIC 3 | — |

Tests never reach an aggregator: `settings.py` forces `TRAQO_ENABLED` and
`VIZION_ENABLED` off under `test`, and the carrier adapters cannot call anything
without a per-team integration.

---

## 4. Migrations and manual setup still required

### Migrations
43 migration files on this branch are not on `master`. The two added by TEAM-ADMIN are:

| Migration | Change |
|---|---|
| `scm_containers.0012_container_tracking_provider_override` | Adds `Container.tracking_provider_override` (nullable-free `CharField`, blank default). |
| `scm_tracking.0014_teamtrackingsettings` | Creates `TeamTrackingSettings`, one row per team, unique on `team`. |

Both are additive with safe defaults — no backfill, no data migration, no downtime.
`TeamTrackingSettings` rows are created on first read, so existing teams need nothing.

Earlier migrations on this branch that **are** data migrations, and matter on a
database that has production rows: `scm_containers.0007` (location name backfill),
`scm_containers.0010` (condition strings → rows), `scm_tracking.0004` (event
fingerprints), `scm_tracking.0012` (subscription carrier identity),
`scm_procurement.0005` (classify manual POs). These are the ones to watch on the first
production `migrate`.

### Environment variables

Required in production:

| Variable | Why |
|---|---|
| `SECRET_KEY`, `DATABASE_URL`, `REDIS_URL`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `FRONTEND_ADDRESS` | Standard |
| `SCM_INTEGRATION_ENCRYPTION_KEY` | **Set an explicit Fernet key** (url-safe base64, 32 bytes). Unset falls back to one derived from `SECRET_KEY`, so rotating `SECRET_KEY` would make every stored credential undecryptable. |
| `SCM_INTEGRATION_REQUIRE_ENCRYPTION_KEY=True` | Makes the fallback above an `ImproperlyConfigured` error instead of a silent risk. Off by default so development needs no key. |
| `SENTRY_DSN` | Error visibility |
| `DJANGO_VITE_DEV_MODE=False` | Built assets |

Optional per capability: `TRAQO_ENABLED` / `TRAQO_API_KEY`, `VIZION_ENABLED` /
`VIZION_API_KEY`, `SCM_PDF_FASTAPI_BASE_URL`, `SCM_TRACKING_DISPATCH_LIMIT`,
`SCM_TRACKING_RAW_PAYLOAD_RETENTION_DAYS`, `SCM_ARRIVAL_WINDOW_HOURS`,
`SCM_LOCATION_COORDINATE_RADIUS_KM`, `SCM_BUSINESS_CENTRAL_DISPATCH_ENABLED`.

### Manual setup, per team

1. Create the team; container conditions seed automatically.
2. Create canonical locations in the Locations pages. There is no production seeding
   command — `seed_locations` creates the Göteborg/Oceanterminalen *demo* example
   only, deliberately, because a team's places are its own master data. Without
   canonical locations the resolver has nothing to resolve carrier evidence *to*, and
   every reported place lands on the Data Quality page.
3. Connect carriers: Settings → Tracking, or the `setup_*_integration` commands when
   a non-default product or an OAuth flow is needed.
4. Add a `test_connection_reference` for CMA CGM and Hapag-Lloyd so the connection
   test works.
5. Business Central: create the integration and store credentials (no UI for this —
   `setup` path is the management command).
6. Equipment types: load `containers/fixtures/equipment_types.json` if absent.

### Infrastructure

- **Celery worker and beat must both run.** The Beat schedule lives in the database
  (`DatabaseScheduler`); `bootstrap_celery_tasks` runs in the release path in
  `Dockerfile.rlw`, so a deploy that adds a scheduled task starts running it. The
  worker/beat service must run the *same image* with its own start command.
- `railway.json` health-checks `/health/`. `/health/scm/` should not be public.

---

## 5. What blocks production

Ordered by severity. None of these is a code defect; all are configuration or
verification gaps that the Live Pilot phase exists to close.

1. **`SCM_INTEGRATION_ENCRYPTION_KEY` must be set before any credential is stored in
   production.** Storing credentials under the `SECRET_KEY`-derived fallback and
   rotating `SECRET_KEY` later loses them all. Set the key *and*
   `SCM_INTEGRATION_REQUIRE_ENCRYPTION_KEY=True`.
2. **Celery worker + beat not yet verified in production.** No scheduled tracking, no
   BC sync and no retention without them, and nothing in the app surfaces their
   absence beyond `/health/scm/` warning that tracking data is stale.
3. **No real carrier credentials exercised end-to-end in production.** The three direct
   integrations are covered by tests against recorded payloads and by live validation
   apparatus; they have not been run against a production account from the deployed
   environment.
4. **Traqo quota is installation-wide.** A live pilot polling real MCR containers
   consumes the single shared account; the polling cadence should be reviewed against
   the plan's limits before the first full sweep.
5. **Data migrations unrun against production-sized data.** The five listed above
   should be run on a restored copy of the intended production database first.
6. **Backup/restore untested for this schema.** Procedure is documented; a restore
   drill has not been performed.
7. **Tenant isolation verified by tests, not by a review.** Every SCM queryset filters
   by team and the suite covers it, but a deliberate cross-tenant pass over the newest
   surfaces (Settings, the override, the workspace) is worth doing once with real data.
8. **No load or volume testing.** Unknown behaviour at MCR's real container count on
   the Control Tower, the container list and the map.
