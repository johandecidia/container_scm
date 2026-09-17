# CMA CGM Track & Trace integration

Live against CMA CGM's **public Track & Trace events** endpoint:

```
GET https://apis.cma-cgm.net/operation/trackandtrace/v1/events?equipmentReference=<container>
keyId: <api key>
```

| | |
|---|---|
| API | CMA CGM Track & Trace |
| Standard | DCSA Track & Trace 2.2.0 (CMA API 1.2.9) |
| Base URL | `https://apis.cma-cgm.net` |
| Path | `/operation/trackandtrace/v1/events` |
| Public auth | `keyId` header |
| Credential key | `api_key` |

CMA CGM runs on the shared DCSA pipeline (`carriers/dcsa/client.py` and
`carriers/dcsa/carrier_parser.py`), the same one Maersk uses. This package holds only
CMA CGM's identity, capabilities and verified endpoint settings — no HTTP handling, no
retry policy, no auth flow and no parser of its own. The configuration keys below are
the shared DCSA ones and apply to any DCSA carrier.

The client has no hardcoded endpoint. Every endpoint value comes from the team's
`Integration.config`, and a missing one raises `CarrierConfigurationError`, which the
sync layer records as a `SKIPPED` run (never as "synced, no events").

## Enabling a team

The verified settings live in code as
`carriers.cma_cgm.client.PUBLIC_TRACK_AND_TRACE_CONFIG` — configuration data, not a
secret. Apply them and store the API key with:

```bash
export CMA_CGM_API_KEY='<api key>'
python manage.py setup_cma_cgm_integration --team <team-slug> \
    --test-reference <a container the account can see>
```

The command reads the key from the environment, writes it through the credential
service (encrypted at rest) and never echoes it. The API key comes from the CMA CGM
developer portal; it is never committed, never put in `.env`, and never passed on the
command line.

Verify with a live read-only call:

```bash
python manage.py test_carrier_tracking <container> --provider cma_cgm --team <team-slug>
```

Under Docker Compose:

```bash
docker compose exec web python manage.py test_carrier_tracking <container> \
    --provider cma_cgm --team <team-slug>
```

It prints the event count, the first and latest event, the classifier, location and
vessel — and no credential.

The public endpoint authenticates on the API key alone, so CMA CGM is **not** marked
`requires_account_number`. Contracted products that do need an account number carry it
in their own `Integration.config`.

Still worth confirming per account:

- **Rate limits.** Set `min_poll_interval_minutes` to the contractual limit; the
  polling policy treats it as a hard floor.
- **Page size.** `pagination.page_size` ships at the documented default of 100.

## Reference mapping

| Container SCM reference kind | CMA CGM query parameter |
|---|---|
| `container_number` | `equipmentReference` |
| `booking_number` | `carrierBookingReference` |
| `bill_of_lading_number` (transport document) | `transportDocumentReference` |

A reference kind absent from `reference_params` is reported as unsupported rather than
guessed.

**One reference per call.** `carriers/base.py::resolve_tracking_reference` deliberately
allows exactly one, and CMA CGM does not bend it. CMA CGM's `/events` *does* accept
`equipmentReference` and `carrierBookingReference` together, which would pin a
container to a single commercial cycle instead of returning every cycle it has been
part of — useful, because a box is reused across bookings over its life. Enabling that
means widening the shared carrier contract for every carrier (a `TrackingReference`
set rather than one value, plus capability flags for which combinations a carrier
accepts), not special-casing it here. Tracked as a follow-up in
`CmaCgmClient`'s docstring.

## Configuration

Stored on the team's `Integration` (`provider_family=carrier`, `provider_code=cma_cgm`).

```jsonc
{
  "base_url": "https://apis.cma-cgm.net",
  "tracking_path": "/operation/trackandtrace/v1/events",
  "auth_style": "api_key_header",              // or "oauth2_client_credentials"

  // Query parameter CMA CGM expects for each reference kind. At least one is required.
  "reference_params": {
    "container_number": "equipmentReference",
    "bill_of_lading_number": "transportDocumentReference",
    "booking_number": "carrierBookingReference"
  },

  // auth_style = api_key_header — exactly the header the Swagger's ApiKeyAuth names.
  "api_key_header_name": "keyId",

  // Non-secret headers sent with every request.
  "extra_headers": { "Accept": "application/json" },

  // Cursor pagination. Both cursor_param and next_page_header are required together;
  // omit the whole block for a carrier that answers in one page.
  "pagination": {
    "cursor_param": "cursor",
    "next_page_header": "Next-Page",
    "limit_param": "limit",
    "page_size": 100,
    "max_pages": 20
  },

  // Connectivity check. Not shipped in code — a reference known to an account belongs
  // to that account. Without it, test_connection() reports the missing key.
  "test_connection_reference": "<container the account can see>",

  // Optional transport tuning
  "request_timeout_seconds": 30,
  "max_retries": 3,
  "retry_backoff_seconds": 0.5,
  "min_poll_interval_minutes": 60,
  "no_data_statuses": [404]
}
```

## Credentials

Written through `apps.scm.integrations.credentials.set_integration_credentials`, which
encrypts them at rest. They are never in settings, logs, raw payload metadata, error
messages or Git.

- `auth_style = api_key_header` → `{"api_key": "<api key>"}`

## Pagination

CMA CGM pages `/events` with `limit` and `cursor`, advertising the next cursor in a
`Next-Page` response header. The shared DCSA client follows it:

```
GET /operation/trackandtrace/v1/events?equipmentReference=…&limit=100
  → 200, Next-Page: cursor123
GET /operation/trackandtrace/v1/events?equipmentReference=…&limit=100&cursor=cursor123
  → 200, no Next-Page → done
```

The header value is sent back verbatim as the `cursor` parameter. Every page's events
are concatenated in carrier order into one payload, which is stored and parsed as a
single response, so the tracking timeline sees each event exactly once.

Guards, all in the shared client rather than here:

- **`max_pages`** (default 20) caps the loop, and the truncation is logged as a
  warning rather than passing silently as a complete history.
- **A repeated cursor** stops pagination instead of looping over the same events.
- **A failure on a later page** raises, so a partial history is never mistaken for a
  complete one.
- **Half-configured pagination** (a cursor parameter with no header, or the reverse)
  is a `CarrierConfigurationError` at config-resolution time.

## Behaviour

- One reference per call, validated against the carrier's capabilities.
- Retries with exponential backoff on timeouts and 5xx; honours `Retry-After` on 429.
- `200` with `[]` → a successful call with zero events. Never an error.
- `404` (configurable) → `CarrierNoDataError`, a successful sync with zero events.
- `401`/`403` → `CarrierAuthenticationError` (permanent). `401` triggers one token
  refresh first, which is a no-op for a static API key.
- Malformed JSON, or a body that is neither object nor array →
  `CarrierInvalidResponseError`. Never silently an empty event list.
- Missing configuration → `CarrierConfigurationError`, recorded as `SKIPPED`.
- Every request is logged to `IntegrationRequestLog` with the path only — no query
  string, no headers, no body — so a credential cannot reach the log table.
- Responses are stored in `TrackingRawPayload` before parsing and only marked parsed
  once parsing succeeds.

## Preserved carrier data

CMA CGM sends a `carrierSpecificData` object (`internalEventCode`,
`internalEventLabel`, `internalLocationCode`, `internalFacilityCode`,
`bookingExportVoyageReference`, `transportationPhase`, `shipmentLocationType`,
`numberOfUnits`), plus location and vessel fields the normalised model has no column
for (`facilityCode`, `facilityTypeCode`, `vesselFlag`, `vesselCallSignNumber`,
`vesselOperatorCarrierCode`).

None of it gets a CMA-specific database column. It survives verbatim on the stored raw
payload and on each normalised event's `raw_payload`, available for debugging and for
extracting into the normalised model later if a feature needs it.

What *is* normalised (and therefore available to the Container Timeline, Container Map,
Shipment Map, ETA and Current Location features) is the shared DCSA set: event type,
code and classifier, timestamp, location name, UN/LOCODE, facility name, latitude,
longitude, vessel name, IMO, voyage number, transport mode and the container, booking
and transport-document references.

## Out of MVP scope

Deliberately not implemented, and deliberately not prevented:

- **OAuth2 / private events.** The Swagger also offers client credentials at
  `https://auth.cma-cgm.com/as/token.oauth2` with scopes `tandtpublic:read:be` and
  `tandtcommercial:read:be`. The shared DCSA client already supports
  `auth_style: "oauth2_client_credentials"`, so enabling it later is a change to a
  team's `Integration.config` plus `{"client_id", "client_secret"}` credentials — no
  new code here. Commercial-scope event enrichment is a separate piece of work.
- **Webhooks and subscriptions.** `capabilities.supports_webhooks` and
  `supports_subscriptions` are `True` because, as everywhere in this layer, the flags
  describe what the carrier's API offers on paper — Maersk declares the same and its
  `webhook.py` is likewise empty. Neither is implemented for any carrier yet.
- CMA CGM Shipment and Booking APIs, schedules, scraping.

## Tests

`apps/scm/integrations/tests/test_cma_cgm_client.py` covers the architecture (that CMA
CGM subclasses the shared client and parser and reimplements neither), the shipped
public config (`keyId`, `equipmentReference`, the events URL), all three reference
mappings, configuration validation, pagination (cursor following, merged events,
case-insensitive header, max-pages cap, repeated cursor, mid-pagination failure,
half-configured refusal), empty results, 401/403, 429 with `Retry-After`, 5xx,
timeouts, malformed JSON, DCSA parsing of EQUIPMENT `LOAD`/`DISC` and TRANSPORT
`DEPA`/`ARRI` with locations, coordinates and vessels, `carrierSpecificData` survival,
request-log redaction, cross-team credential isolation and container discovery.

`apps/scm/tracking/tests/test_cma_cgm_pipeline.py` drives the real sync engine end to
end: factory resolution, carrier call, stored raw payload, normalised and persisted
events, paged ingestion, the timeline layer, idempotent re-import, and the error
classifications.

The fixture is `tests/fixtures/carriers/cma_cgm_events_response.json` (a bare event
array, as `/events` returns). Every test uses an injected fake session; the suite makes
no live calls, and `test_carrier_no_live_api.py` enforces that independently.
