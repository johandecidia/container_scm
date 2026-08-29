# Vizion — Phase 1 POC

Vizion is a container visibility **aggregator**, not a carrier. Like Traqo, this package is
therefore a sibling of `carriers/` and is **deliberately absent from the carrier registry**:
an entry there would put Vizion into carrier discovery sweeps, `list_carriers()` and the
team carrier integration screens as though it moved boxes itself.

What makes it worth a POC of its own is one capability Traqo does not have:

> **Auto Carrier Identification (ACI)** — create a reference with a container number and
> *no carrier*, and Vizion works out which line is carrying it.

```
Container SCM
  ├── Maersk Direct     (carriers/maersk, in the carrier registry)
  ├── Traqo             (integrations/traqo, not in the registry) — tracking, carrier lookup
  └── Vizion            (this package, not in the registry)      — tracking, ACI
```

## The architectural claim this package exists to defend

**Carrier resolution and tracking provider selection are different questions.** All three of
these must remain expressible:

```
Carrier: ONE      Resolved by: Vizion         Tracked by: Traqo
Carrier: Maersk   Resolved by: Container SCM  Tracked by: Maersk Direct
Carrier: ONE      Resolved by: Vizion         Tracked by: Vizion
```

So `service.py` exposes two functions that never call each other:

| | writes to Container SCM | writes at Vizion | answers |
| --- | --- | --- | --- |
| `resolve_carrier_via_aci()` | nothing | creates a reference | who is carrying this box |
| `ingest_vizion_container()` | events, subscription, sync run, raw payload | nothing | where the box has been |

Neither writes a routing decision, neither writes a carrier onto `Container`, and neither
feeds Vizion's answer into `carriers/carrier_discovery.py`. Vizion's ACI is a **fifth
signal** into the discovery model that already exists (chosen carrier, ISO 6346 prefix,
connected carriers, Traqo lookup) — not a replacement for it.

## The path a Vizion response takes

```
VizionClient.create_reference()   → POST /references with container_id only → ACI
VizionClient.get_reference()      → last_update_status → the ACI outcome
VizionClient.list_updates()       → the update envelopes
store_verified_carrier_result()   → TrackingRawPayload (API_RESPONSE), then
map_vizion_updates()              → NormalisedTrackingEvent DTOs
persist_normalised_events()       → TrackingEvent (existing fingerprint + upsert)
                                  → existing timeline / position / ETA derivation
read_vizion_eta_observation()     → ProviderEtaObservation → existing ETAHistory
```

There is no second persistence path, no Vizion-specific event table, no Vizion columns on
`Container` or `Shipment`, and **no migration**. The Phase 1 change set is this package, its
registration in `tracking/sources.py`, the `VIZION_*` settings, one additive hook in
`carriers/http.py`, and the `vizion_test` command.

## Trying it

There is no free sandbox. Vizion's demo environment is metered against the same account key
(15 000 calls/month, 50 active references), so `VIZION_ENABLED` gates **both** environments —
unlike `TRAQO_ENABLED`, which gates production only.

```bash
# The acceptance cases: container number only, no carrier hint of any kind.
make manage ARGS='vizion_test BBCU3273070 --resolve'
make manage ARGS='vizion_test BBCU3273090 --resolve'

# Resolve, then fetch and normalise, without writing anything.
make manage ARGS='vizion_test BBCU3273070 --resolve --track --dry-run'

# Resolve, track and ingest through the ordinary pipeline.
make manage ARGS='vizion_test BBCU3273070 --resolve --track --verbose-events'

# Re-observe later without spending a second reference.
make manage ARGS='vizion_test BBCU3273070 --reference <uuid> --track'

# Compare providers from stored rows. Fetches nothing, calls nobody.
make manage ARGS='vizion_test CPWU2588297 --compare'
```

`--resolve` **creates a Vizion reference**, which is Vizion's billable unit. The command
prints that before doing it.

## Cost, and why it is the central Phase 2 input

Traqo separates the two questions and charges differently for them: `/carriers/lookup` is
free and creates no shipment, `/container/{n}` spends a shipment slot. **Vizion does not.**
Creating the reference *is* what starts tracking, so identification and tracking are one
purchase.

The consequence is concrete: a future state of "resolved by Vizion, tracked by Traqo" means
paying Vizion for a reference and then not reading it, *and* paying Traqo for a shipment.
Whether that is worth it depends on numbers this POC cannot produce alone, and it is the
first thing Phase 2 should price.

## Configuration, and why it is not per-team

Carrier credentials live on a team's `Integration` record because each team holds its own
agreement with the carrier. A Vizion account is one aggregator subscription for the whole
installation, so its credential is an environment setting — the same reasoning as Traqo. The
consequence is that Vizion calls write no `IntegrationRequestLog` row: there is no
`Integration` to scope one to.

```
VIZION_ENABLED         gates both environments (default false)
VIZION_API_KEY         sent as X-API-Key; never logged, never in a query string
VIZION_BASE_URL        https://prod.vizionapi.com
VIZION_DEMO_BASE_URL   https://demo.vizionapi.com
```

References do not cross environments: one created in demo cannot be read from production.

## Why Vizion needs no mapping table

Vizion publishes a `journey_event` object on every milestone, and it is **DCSA**:

```json
"journey_event": {"journey_type": "TRANSPORT", "event_classifier": "EST",
                  "event_type": "ARRI", "transport_mode": "VESSEL",
                  "facility_type": "POTE"}
```

`journey_type` is the same SHIPMENT / EQUIPMENT / TRANSPORT partition
`tracking/statuses.normalize_dcsa_event_type` keys on, and `event_classifier` is the same
ACT / EST / PLN classifier. So the codes go through unchanged and the existing normalisers
classify them. `mapper.py` contains no Vizion mapping table at all — one would be
translating DCSA into DCSA.

This is a materially better fit than Traqo, which can only express actual-versus-estimated
as a boolean. Vizion distinguishes **PLANNED from ESTIMATED**, so all four canonical
`EventTimeType` values except REQUESTED are reachable from real provider data.

## Canonical mapping

| Vizion field | Canonical target | Quality | Loss |
| --- | --- | --- | --- |
| `payload.container_id` | `container_number` → `equipment_reference` | EXACT | none |
| `payload.bill_of_lading` | `bill_of_lading_number` | EXACT | none |
| `payload.booking_number` | `booking_number` | EXACT | none |
| `payload.carrier_scac` | *(provenance only)* | PROVIDER_ONLY | kept in raw payload — deliberately not written to `Container` |
| `journey_event.journey_type` | `event_type` (DTO) → `TrackingEvent.carrier_event_type` | EXACT | none |
| `journey_event.event_type` | `event_code` → `TrackingEvent.event_type` via DCSA map | EXACT where mapped | 20 of 35 DCSA codes are unmapped and stay UNKNOWN with the code preserved |
| `journey_event.event_classifier` | `event_time_type` | EXACT | none — ACT/EST/PLN all survive |
| `planned` (bool) | fallback classifier when `journey_event` absent | DERIVABLE | cannot express PLANNED; absent flag stays UNKNOWN |
| `timestamp` | `event_datetime` | EXACT | none — offset-aware, so no zone is ever guessed |
| `location.timezone` | `event_timezone` | EXACT | falls back to the stated offset |
| `description` | `description` / `status` | EXACT | none |
| `raw_description` | *(none)* | PROVIDER_ONLY | kept in raw payload; `carrier_description` holds the standardised text |
| `location.name` | `location_name` | EXACT | none |
| `location.unlocode` | `location_unlocode` | EXACT | none — never inferred from the name |
| `location.geolocation.latitude/longitude` | `location_latitude/longitude` | EXACT | none |
| `location.facility` | `facility_name` | EXACT | none |
| `location.city/state/country` | *(none)* | CANONICAL_GAP | kept in raw payload |
| `vessel` | `vessel_name` | EXACT | none |
| `vessel_imo` | `vessel_imo` | EXACT | none |
| `vessel_mmsi` | *(none)* | CANONICAL_GAP | kept in raw payload |
| `voyage` | `voyage_number` | EXACT | none — per event, so legs stay distinct |
| `journey_event.transport_mode` | `transport_mode` | EXACT | none |
| `mode` | `transport_mode` fallback | SEMANTIC_MISMATCH | Feeder and Trunk have no canonical value and become OTHER |
| `source` (carrier/ais/terminal/rail) | *(none)* | CANONICAL_GAP | kept in raw payload |
| `shipment_location.type_code` | *(none)* | CANONICAL_GAP | kept in raw payload — see the transshipment gap below |
| `journey_event.facility_type` / `empty_indicator` / `document_type` | *(none)* | CANONICAL_GAP | kept in raw payload |
| milestone `id` | *(deliberately unused)* | see *Event identity* | kept in raw payload |
| `payload.origin_port` / `destination_port` / `inland_origin` / `inland_destination` | *(none)* | CANONICAL_GAP | kept in raw payload; reported by the diagnostic |
| `payload.container_iso` | *(none — `Container.equipment_type` exists but is Container SCM's)* | PROVIDER_ONLY | kept in raw payload; deliberately not written to `Container` |

Everything marked *kept in raw payload* lands under a single `_vizion` key on the event's
`raw_data`, so a later canonical change can re-read stored payloads instead of needing every
container refetched.

## ETA — a milestone, not a field

Vizion has no ETA field. It publishes the POD arrival as a milestone marked
`planned: true` / `event_classifier: EST`, and **the same milestone becomes the actual
arrival** when the vessel berths.

Two consequences pull in opposite directions:

*The forecast needs no special handling to be usable.* It reaches `TrackingEvent` as an
ESTIMATED `VESSEL_ARRIVED` row through the ordinary mapper, and
`get_container_tracking_eta_event` finds it with no Vizion-specific code. This is the
opposite of Traqo, whose events were all actual and whose ETA existed only as a top-level
field.

*But an event is not a history.* When the forecast moves, the mapper produces a second event
rather than editing the first, and nothing records that *this provider* changed its mind. So
`eta.py` also reads the forecast into a `ProviderEtaObservation` and the existing
`record_provider_eta_observation` writes it to `ETAHistory`, per source, exactly as Traqo's
is.

**What the ETA is for is stated, not guessed.** Traqo's `data.eta` had to be recorded as
`provider_defined` because its value turned out to equal the last event in the list. Vizion
says which milestone its ETA is — a TRANSPORT/ARRI at `shipment_location.type_code == "POD"`
— so it is recorded as `vessel_arrival_pod`, a **specific** target. That matters downstream:
the benchmark refuses to subtract two ETAs unless both name the same target. Where Vizion
does not label the leg POD, the target degrades to `provider_defined` rather than being
assumed.

**ETD has no canonical home as an *observation*.** `ProviderEtaObservation` is arrival-only.
An estimated departure is perfectly representable as a forecast `VESSEL_DEPARTED`
`TrackingEvent`, and that is where it goes; there is simply no per-provider ETD history.
Documented, not fixed — inventing an ETD observation type would be speculative until
something needs one.

## Canonical gaps

### 1. A transshipment arrival marks the whole journey arrived — the headline finding

This is a **pre-existing defect in canonical code**, not a Vizion problem, and Vizion's
multi-leg data is simply the first payload in this installation rich enough to expose it.

```
normalize_dcsa_event_type(("TRANSPORT", "ARRI"))  →  VESSEL_ARRIVED     … for ANY port
ARRIVAL_ACTUAL_EVENT_TYPES = (VESSEL_ARRIVED, DISCHARGED)
has_journey_arrived()                  → .exists() on that, no time or place comparison
get_container_tracking_eta_event()     → .exists() on that, no time or place comparison
```

Both selectors' docstrings say the forecast is suppressed "once the carrier reports an actual
arrival **at or after it**". Neither implementation compares anything. So a box that has
genuinely arrived at Singapore with the POD five weeks away is treated as arrived:

* its POD ETA is **not displayed** — `get_container_tracking_eta_event` returns None;
* **no ETA observation is recorded** — `record_provider_eta_observation` declines because
  `_journey_is_over` is true;
* the same logic gates `_complete_if_terminal`, so a standalone container could stop being
  polled mid-journey.

Proven, not asserted:
`test_a_transshipment_arrival_suppresses_the_pod_eta_a_canonical_gap` and
`test_no_eta_observation_is_recorded_once_the_journey_counts_as_arrived`.

**Not fixed here.** Two candidate fixes exist and neither is "extremely small and obviously
provider-neutral":

1. add `event_datetime__gte=forecast.event_datetime` to the suppression check — small, but it
   changes production ETA behaviour for Maersk, CMA CGM and Traqo alike and their existing
   tests encode today's behaviour;
2. classify a non-POD `TRANSPORT/ARRI` as `TRANSSHIPMENT_ARRIVED` — needs the canonical
   classifier to read a leg label that only some providers send.

Both are Phase 2 decisions. Option 1 looks correct on the evidence and is the cheaper
experiment.

### 2. No per-event data source

`source` distinguishes `carrier`, `ais`, `terminal` and `rail`. An AIS-derived arrival is a
satellite inference; a carrier-derived one is the line's own record. Canonical
`TrackingEvent` has `confidence` (an integer, defaulted to 100 and never varied) but no
provenance-of-observation field. Filling `confidence` from `source` would be inventing a
number Vizion never stated, so it is preserved and not mapped.

### 3. No MMSI

`vessel_mmsi` accompanies every AIS-sourced milestone and is the identifier that joins to
live vessel position data. `TrackingEvent` has `vessel_name` and `vessel_imo` only.

### 4. Leg semantics are unrepresentable

`shipment_location.type_code` (PRE / POL / POD / PDE / RTP) is the only thing that says which
leg an event belongs to. Nothing canonical can hold it, which is the root of gap 1.

### 5. No structured top-level route

`origin_port`, `destination_port`, `inland_origin` and `inland_destination` are richer than
anything Container SCM models — each is a full location with UN/LOCODE and coordinates. There
is no route or itinerary model to put them in, and inventing one is out of POC scope. They are
preserved and reported by the diagnostic.

### 6. Location richness is lost

`city`, `state` and `country` are dropped from the canonical event. The Traqo work already
noted that Vizion-style location enrichment has nowhere to go; Vizion makes it concrete.

## Event identity and idempotency

**No event identity is claimed.** Milestones carry an `id`, it is preserved in `raw_data`,
and it is **not** used as `raw_event_id`.

The reasoning is forced by the ETA/ATA behaviour above. Vizion reuses one milestone for the
forecast and the actual arrival. If that flip **reuses** the id, an id-keyed fingerprint would
rewrite the forecast out of existence — the audit trail of what the provider predicted would
vanish. If it **does not**, an id-keyed fingerprint would duplicate the whole history on every
poll. Neither is knowable from the documentation, and a refetch of the same container is what
settles it.

The existing field-based fingerprint is safe under **both** hypotheses, so it is used:

| situation | behaviour | verdict |
| --- | --- | --- |
| same payload ingested twice | 10 DTOs → 9 rows; second run creates 0, updates 10 | correct |
| same milestone in two update envelopes | one row — identical fields, identical fingerprint | correct |
| ETA moves | a second ESTIMATED row; the newest is the one the selector returns | acceptable — an extra forecast row is recoverable, a rewritten history is not |
| estimated event becomes actual | two rows, EST and ACT — which is what DCSA models | correct; the canonical selector suppresses the forecast |
| voyage changes | a new row, because `voyage_number` is in the fingerprint | correct — a different voyage is a different event |
| location metadata becomes richer | **same** row, refreshed in place — UN/LOCODE and name are in the fingerprint, but coordinates are not | correct |
| a UN/LOCODE appears where there was none | a **new** row, because `location_unlocode or location_name` is in the fingerprint | known wart, shared with every provider |
| same milestone from another provider | a separate row — the fingerprint is scoped by provider | correct and intended; two sources leave two attributable trails |

Updates are mapped **oldest-first** so the newest version of a milestone is the one that
survives the upsert. Feeding them newest-first would let a stale envelope overwrite a fresh
one; `test_updates_are_ordered_oldest_first` holds this.

## Error semantics

Statuses are mapped by consequence onto the hierarchy the sync layer already classifies (see
`errors.py`):

| Status | Error | Sync outcome |
| --- | --- | --- |
| 400 | `CarrierInvalidResponseError` | FAILED / invalid_response |
| 401 "key lacks permissions" | `CarrierAuthenticationError` | FAILED / authentication |
| 403 "no valid key" | `CarrierAuthenticationError` | FAILED / authentication |
| 404 | `CarrierNoDataError` | SUCCESS with zero events |
| 422 | `CarrierUnsupportedReferenceError` | FAILED / unsupported_reference |
| 429 | `CarrierRateLimitError` | FAILED / rate_limit, transient, honours Retry-After |
| 5xx | `CarrierServerError` | FAILED / server_error, transient |

Vizion inverts the usual meanings of 401 and 403, and **401 is classified here rather than
left to the shared transport** — that transport treats a 401 as "the token may be stale,
refresh once and retry", and a Vizion key is static, so the retry could only fail the same
way and would double every rejected call.

429 and 5xx are left to the shared transport on purpose: its retry-then-classify logic is
what keeps a temporary Vizion outage from marking a container untrackable.

A Vizion failure never deletes or invalidates a stored event: the fetch happens before
anything is written, so a failed call leaves the container exactly as it was.

## Reference lifecycle, and what "not found" means

`last_update_status` is read into four Container SCM states, because three would have to merge
two that mean different things:

| Vizion status | state | meaning |
| --- | --- | --- |
| `auto_carrier_completed`, `data_received`, `duplicate_payload` | `IDENTIFIED` | a carrier is attached |
| `auto_carrier_not_found` | `NOT_FOUND` | no supported carrier had data — Vizion retries **daily for up to 7 days** |
| `auto_carrier_failed`, `invalid_container`, `extraction_failed` | `FAILED` | terminal; the reference is deactivated |
| absent, `no_data`, `incomplete_processing`, anything unrecognised | `PENDING` | Vizion has not answered yet |

`NOT_FOUND` is "not yet", not "no". Collapsing it into `FAILED` would discard a pending
answer — and a POC that waited sixty seconds has learned nothing about the seventh day.

## The architectural conflict, unchanged from Traqo

`tracking/sync.py` resolves its client and parser through the **carrier** registry, and
`fetch_tracking()` has no notion of a Vizion reference id. A Vizion subscription therefore
cannot be *fetched* by the scheduled poller. `tracking/sources.py` registers Vizion as a
non-carrier source, so:

- the scheduled poller never queues a Vizion subscription — a skip per cycle forever is
  correct and useless;
- a direct `sync_tracking_subscription()` call skips safely with `NOT_CARRIER_POLLED` and
  leaves `tracking_status` exactly as it was;
- a genuinely misconfigured *carrier* still reports `NOT_CONFIGURED` and still degrades.

Scheduled fetching needs somewhere to persist the Vizion reference id per subscription, and
`TrackingSubscription` has no metadata field. Re-run `vizion_test --reference <uuid> --track`
to refresh. The container refresh button still reports a Vizion source as "not configured":
correct about the *action*, misleading about the container, and a known gap shared with Traqo.

Correcting how a *stored* response is read needs no fetch at all — the existing re-parse
command works because `read_stored_payload` is registered in `tracking/sources.py`:

```bash
make manage ARGS='reparse_tracking_payloads --provider vizion --container BBCU3273070'
```

## Webhooks

Not built. Vizion can POST the **same update body** to a `callback_url`, and
`map_vizion_update()` reads that body directly — so a later phase adds an endpoint and feeds
the identical normalisation pipeline, with no second mapper. Polling is sufficient for Phase 1
and exposes nothing.

## What the tests prove, and what they do not

`fixtures/vizion/*.json` are **synthetic**, built from Vizion's published schema. No Vizion
credential existed in this installation when the POC was written. So the tests prove the
mapper reads the documented contract correctly and loses nothing — they do **not** prove a
live response matches that contract. The acceptance cases exist for that, and the fixtures
should be replaced with recorded responses as soon as a key is available.

93 offline tests, no live access required:

```bash
make test ARGS='apps.scm.integrations.tests.test_vizion_mapper'
make test ARGS='apps.scm.integrations.tests.test_vizion_client'
make test ARGS='apps.scm.integrations.tests.test_vizion_eta'
make test ARGS='apps.scm.tracking.tests.test_vizion_pipeline'
```
