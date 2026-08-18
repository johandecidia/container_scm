# Traqo Ocean — Phase 1 POC

Traqo is an external ocean tracking **aggregator**, not a carrier. It answers about
containers moving with Maersk, CMA CGM, COSCO and others, and the real carrier stays
identifiable through the `sealine` (SCAC) we ask with.

That is why this package is a sibling of `carriers/` and is **deliberately absent from
the carrier registry**. Registering it there would put Traqo into carrier discovery
sweeps, `list_carriers()` and the team carrier integration screens as though it moved
boxes itself.

```
Container SCM
  ├── Maersk Direct     (carriers/maersk, in the carrier registry)
  └── Traqo             (this package, not in the carrier registry)
        └── MAEU · MSCU · CMDU · HLCU · COSU · ONEY · YMLU · ZIMU · HDMU
```

## The path a Traqo response takes

```
TraqoClient.get_container()      → the original response envelope
store_verified_carrier_result()  → TrackingRawPayload (API_RESPONSE), then
map_traqo_container_payload()    → NormalisedTrackingEvent DTOs
persist_normalised_events()      → TrackingEvent (existing fingerprint + upsert)
                                 → existing timeline / position / ETA derivation
```

There is no second persistence path, no Traqo-specific event table, no Traqo columns on
`Container`, and **no migration**. The Phase 1 change set is this package plus one
opt-in hook in `carriers/http.py`, the `TRAQO_*` settings and the `traqo_test` command.

## Trying it

The sandbox is fixed demo data behind no credential, so it works out of the box:

```bash
make manage ARGS='traqo_test MRSU6859427 --sealine MAEU --sandbox'
make manage ARGS='traqo_test MRSU6859427 --sealine maersk --sandbox --dry-run'
```

Live calls need `TRAQO_ENABLED=true` and `TRAQO_API_KEY` in `.env`. `TRAQO_ENABLED`
gates production only — the sandbox is always reachable.

## Configuration, and why it is not per-team

Carrier credentials live on a team's `Integration` record because each team holds its
own agreement with the carrier. A Traqo account is one aggregator subscription for the
whole installation, so its credential is an environment setting. The consequence is
that Traqo calls write no `IntegrationRequestLog` row — there is no `Integration` to
scope one to.

## What the sandbox actually contains

Read from a live sandbox response, not from documentation:

```json
{"idx": 1, "location": "Mundra", "country": "India",
 "description": "Gate in full", "timestamp": "2026-03-01 00:00:00",
 "event_type": "EQUIPMENT", "event_code": "GTIN", "transport_type": "TRUCK",
 "is_actual": 1, "status": "CGI",
 "status_description": "Container arrival at first POL (Gate in)"}
```

`event_type`/`event_code` are DCSA-shaped, so the tracking layer's existing
`normalize_dcsa_event_type` classifies them with no Traqo mapping table at all.

Of the tables the Traqo docs mention, only `events_table` and `vessels_table` appear in
sandbox responses. `voyage_plan_table`, `containers_table`, `locations_table`,
`facilities_table`, `eta_history_table` and `route_json` were **not present**, so
nothing here maps them — writing extractors against guessed field names would be
untestable and would silently misread real data.

## Known gaps and the reasoning behind each

**No stable event ID.** `idx` is a position in the list, not an identity: an event
inserted mid-history shifts every `idx` after it. Using it as `source_event_id` would
make the ingestion layer overwrite one event with another. It is therefore left empty
and the existing field-based fingerprint identifies the event, with `idx` kept in
`raw_data`. Consequence: if Traqo corrects an event's **timestamp**, that is a new row
rather than an update. An extra row is recoverable; a silently rewritten history is not.

**Actual or forecast, and nothing finer.** `is_actual` is a boolean, so `1` → ACTUAL and
`0` → ESTIMATED. Traqo cannot express PLANNED or REQUESTED. An absent flag stays UNKNOWN
rather than being read as either.

**Naive timestamps.** Traqo sends `"YYYY-MM-DD HH:MM:SS"` with no offset. They are read
as UTC and `event_timezone` is left empty rather than claiming a zone Traqo did not
state. **This assumption needs production verification against a known movement.**

**No UN/LOCODE, coordinates or vessel on an event.** Sandbox events carry a place name
only. The mapper still looks for those fields so a richer production payload is not
dropped, but on today's payloads they yield nothing. `vessels_table` describes the
voyage, not the leg, so it is only attached to an event that names a `vessel_id` —
hanging the current vessel on a truck gate-in would claim the box was aboard a ship.

**Shipment-level ETA needs no new architecture.** Traqo's `data.eta` arrives on its own
as a forecast `ARRI` event, which the existing derivation already reads as the ETA
(`get_container_tracking_eta_event`) and feeds to `ETAHistory` through
`apply_tracking_to_shipment`. Nothing extra is synthesised — doing so would double-count
the same forecast. `eta_history_table` is not in the sandbox and is unmapped.

**Shipment-level position stays raw.** `data.latitude`/`longitude`/`route_json` are
provider observations of the voyage, not of the box. They are preserved in
`TrackingRawPayload` and assigned to nothing. There is no position model to put them in:
`tracking/positions.py` derives position from `TrackingEvent` alone, and inventing a
location-truth model is out of Phase 1 scope.

**Evergreen has no sealine.** Traqo's carrier list publishes 10 SCACs and Evergreen is
not among them, so `resolve_sealine("evergreen")` raises rather than inventing `EGLV`.
OOLU is the reverse case: Traqo supports it and Container SCM has no adapter.

## The architectural conflict Phase 1 does not resolve

`tracking/sync.py` resolves its client and parser through the **carrier** registry, and
`fetch_tracking()` has no `sealine` argument. A Traqo subscription therefore cannot be
driven by the scheduled poller or by the container refresh button — both would record a
SKIPPED run with `NOT_CONFIGURED`. Re-run `traqo_test` to refresh a Traqo subscription.

Fixing that needs one of:

1. a dispatch seam in `sync.py` that resolves a non-carrier tracking source, and
2. somewhere to persist the SCAC per subscription, since `TrackingSubscription` has no
   metadata field — which is a migration, and Phase 1 was asked to avoid one.

Both are Phase 2 decisions. Nothing here is pre-built for them.

## Error semantics

Statuses are mapped by consequence, onto the error hierarchy the sync layer already
classifies (see `errors.py`):

| Status | Error | Sync outcome |
| --- | --- | --- |
| 400 | `CarrierInvalidResponseError` | FAILED / invalid_response |
| 401 | `CarrierAuthenticationError` | FAILED / authentication |
| 402 `shipment_limit_reached` | `TraqoShipmentLimitReachedError` (rate-limit family) | FAILED / rate_limit, transient, honours Retry-After |
| 402 `payment_overdue` | `TraqoPaymentOverdueError` (configuration family) | SKIPPED / not_configured — needs a human |
| 403 | `TraqoDeveloperModeDisabledError` | SKIPPED / not_configured |
| 404 | `CarrierNoDataError` | SUCCESS with zero events |
| 429 | `CarrierRateLimitError` | FAILED / rate_limit, transient |
| 502 | `CarrierServerError` | FAILED / server_error, transient |

429 and 5xx are left to the shared transport on purpose: its retry-then-classify logic
is what keeps a temporary Traqo or upstream-carrier failure from marking a container
untrackable. A 402 with no stated reason is treated as the billing case, because that is
the one a retry cannot fix — assuming a quota would poll a suspended account forever.

A Traqo failure never deletes or invalidates a stored event: the fetch happens before
anything is written, so a failed call leaves the container exactly as it was.
