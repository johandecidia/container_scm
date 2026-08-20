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
`facilities_table`, `eta_history_table` and `route_json` were **not present**, so Phase 1
mapped none of them — writing extractors against guessed field names would be untestable
and would silently misread real data.

A production response carries more. `locations_table` is now read, because it is what
makes an event timestamp interpretable (below); the rest stay unmapped and preserved in
`TrackingRawPayload`. Sandbox payloads still work unchanged: an event with no
`location_id` simply has no zone to look up.

## Timezones

A production event names its place by `location_id`, and `locations_table` gives that
place an IANA zone:

```
event.location_id → locations_table row → timezone → aware local time → UTC
```

That chain is the only authority the mapper will accept. Where it breaks, the timestamp
is stored as sent and `event_timezone` is left **empty**, which is also how such events
are found later:

| Traqo sent | Stored `event_datetime` | `event_timezone` |
| --- | --- | --- |
| `2026-05-12 01:09:00`, Yantian, `Asia/Shanghai` | `2026-05-11 17:09Z` | `Asia/Shanghai` |
| `2026-07-01 15:09:00`, Goteborg, `Europe/Stockholm` | `2026-07-01 13:09Z` | `Europe/Stockholm` |
| `2026-07-13 09:30:05`, BORAAS, `timezone: null` | `2026-07-13 09:30:05Z` | *empty* |

The third row is the honest answer, not the best one. BORAAS is Boraas, Sweden, and
Maersk's own `SEBOS` event says so — but taking the zone from the country, the
coordinates, another provider or this server would put a **guessed** offset on a canonical
event timestamp with nothing to mark it as a guess. An unconverted timestamp is visibly
unconverted; a wrongly converted one is not. Every event also carries a
`_timestamp_normalisation` record in `raw_data` — the provider's original string, the zone
used, why it was or was not converted — so a later decision has the evidence to act on.

Traqo's `last_synced_at`, `last_updated_at` and `closed_at` arrive at UTC+05:30, which is
Traqo's own infrastructure. They are provider sync times and are never used to place an
event in time. Event time, provider sync time and Container SCM's `received_at` stay three
separate things.

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

**Local naive timestamps.** Traqo sends `"YYYY-MM-DD HH:MM:SS"` with no offset, and the
production benchmark settled what they mean: reading them as UTC put every Yantian event
exactly 8 h and every Gothenburg event exactly 2 h from Maersk's instant for the same
movement. They are **local times at the place the event happened**, and are converted
through the zone Traqo publishes for that place — see *Timezones* below.

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

## The architectural conflict, and how far Phase 2.1 takes it

`tracking/sync.py` resolves its client and parser through the **carrier** registry, and
`fetch_tracking()` has no `sealine` argument. A Traqo subscription therefore cannot be
*fetched* by the scheduled poller, and Phase 1 left it recording a SKIPPED run with
`NOT_CONFIGURED` — which also set `tracking_status = NOT_CONFIGURED`, telling the product
a container whose events were already stored and correct could not be tracked.

Phase 2.1 separates the two facts. `tracking/sources.py` knows which providers the
carrier sync drives, so:

- the scheduled poller never queues a Traqo subscription in the first place — a skip per
  cycle forever is correct and useless;
- a direct `sync_tracking_subscription()` call still skips safely, with the new
  `NOT_CARRIER_POLLED` error type, and leaves `tracking_status` exactly as it was;
- a genuinely misconfigured *carrier* still reports `NOT_CONFIGURED` and still degrades,
  which is the behaviour that surfaces real faults.

Fetching Traqo on a schedule remains out of scope, and still needs somewhere to persist
the SCAC per subscription — `TrackingSubscription` has no metadata field. Re-run
`traqo_test` to refresh a Traqo subscription. The container refresh button still reports
a Traqo source as "not configured": correct about the *action*, misleading about the
container, and a known gap rather than something Phase 2.1 redesigned the UI to fix.

Correcting how a *stored* response is read needs no fetch at all:

```bash
make manage ARGS='reparse_tracking_payloads --provider traqo --container CPWU2588297'
```

## Phase 2 — the benchmark (`benchmark/`)

Phase 1 asked whether Traqo can integrate cleanly. It can. Phase 2 asks a different
question — **is Traqo's data good enough to rely on** — and answers it by comparing what
two providers stored for the same real container.

```bash
# Read-only: compare what is already stored, no provider request at all.
make manage ARGS='traqo_test CPWU2588229 --sealine MAEU --compare --live --no-fetch'

# One live Traqo request, ingested through the Phase 1 pipeline, then compared.
make manage ARGS='traqo_test CPWU2588229 --sealine MAEU --compare --live'
make manage ARGS='traqo_test CPWU2588229 --sealine MAEU --compare --live --json --output run.json'
```

`--compare` demands an explicit `--live` or `--sandbox`. The sandbox returns the same
demo shipment whatever container you ask about, so comparing real carrier events against
it measures nothing — and silently choosing it would hide that.

### What the benchmark is, and is not

It is measurement apparatus: no model, no migration, no schedule, no UI. Delete
`benchmark/` and the tracking domain is untouched. The only write in the package is the
candidate's ordinary Phase 1 ingestion, which is how its data becomes canonical in the
first place.

The experiment is not "fixed" to make the candidate look good:

- no event of either provider is altered, merged or enriched from the other;
- a place *name* is never credited as a UN/LOCODE — Traqo saying "Rotterdam" earns it
  the name and nothing else;
- a shipment-level vessel is never attributed to an event that does not name it;
- ambiguity is reported, never resolved by guessing.

### Matching rules

`event_type` and `event_time_type` are hard partitions — an estimated arrival can never
pair with an actual one, which is the single most misleading thing this benchmark could
do. Within a partition, time proximity proposes (±24h by default, `--tolerance-hours`)
and identity disposes: disagreeing UN/LOCODEs or IMOs disqualify a pair outright.

A disagreeing place *name* does not disqualify. Spelling and choice of name ("Yantian"
versus "Shenzhen") is exactly the provider difference being measured, and treating it as
proof of two different events would manufacture a false `MAERSK_ONLY` **and** a false
`TRAQO_ONLY` from one real event. Those disagreements are counted and printed instead.

Every event lands in exactly one of `MATCHED`, `REFERENCE_ONLY`, `CANDIDATE_ONLY` or
`AMBIGUOUS`.

### Coverage, scoped twice

`benchmark event coverage` is matched events over the reference provider's *classified*
events, for **one container's journey against one reference provider**. It is not carrier
coverage and must never be quoted as such.

Document milestones Container SCM itself cannot classify (`SHIPMENT/DRFT`, `ISSU`,
`PENA`, `RELS`, `EQUIPMENT/PICK`, `DROP`) are excluded from that denominator, because no
aggregator claims to carry transport-document paperwork. The excluded codes are printed,
and `raw event coverage` over *every* reference event is printed beside it, so neither
number can flatter the candidate unnoticed.

### Freshness, and what one run may claim

`received_at` records when Container SCM learned of an event. On the run that first
ingests a provider it therefore records when the *experiment* started, not when the
provider knew — so those runs are flagged `first_observation` and their lag figures are
labelled backfill artefacts. Only repeated runs over days can measure real latency.

What a single run *can* measure is **milestone recency**: whose newest observed milestone
is more recent. That is reported separately and does indicate staleness. The provider's
own `last_updated_at` is reported separately again, and never written over a canonical
event timestamp.

The between-providers figure is called **provider observation lag**, deliberately — it is
a property of this installation's polling, not carrier latency.

### Live-run safety

Before a production request the command prints the container, the sealine, `Mode:
PRODUCTION` and a warning that a Traqo shipment slot may be consumed. It fetches once,
persists once, and compares locally; `--no-fetch` compares stored data without spending
a request. Without `TRAQO_API_KEY` a `--live` run fails with instructions and writes
nothing — it never falls back to the sandbox.

### One production change Phase 2 made

`get_container_tracking_eta_event(team, container, *, provider=None)` gained the optional
filter, so the benchmark can ask "what would this container's ETA be if only this
provider existed" through the canonical rule instead of restating it. Every production
caller passes nothing and behaves exactly as before.

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
