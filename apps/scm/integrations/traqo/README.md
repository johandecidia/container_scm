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

## ETA

An ETA is a Container SCM concept. A provider's ETA is an *observation* of it, and Traqo
is one observer among several:

```
Maersk Direct events ─┐
                      ├─→ ETA observation ─→ canonical ETA logic ─→ ETAHistory
Traqo data.eta       ─┘                       (tracking/eta_observations.py)
                                                      │
                                          Shipment.eta / current-ETA selector
                                                      │
                                              delay detection
```

`data.eta` is a **field, not an event**. Every one of the benchmark container's ten
`events_table` rows was `is_actual: 1`, so there was no forecast event to read and none
is invented: a synthesised `ARRI`/`EST` row would claim Traqo reported a movement it
never reported, and would be fingerprinted and shown alongside real ones. It is read by
`eta.py` into a `ProviderEtaObservation` instead.

**What the ETA is *for* is not assumed.** Traqo's `eta` on the benchmark container was
`2026-07-13 13:15:00` — the timestamp of the *last* event, the empty container's return
to the Gothenburg depot. The vessel had arrived at the POD on 1 July, eleven days
earlier. So `eta` is not "vessel arrival at POD", and it is recorded as
`provider_defined` rather than mapped onto a milestone we would be guessing at.

**Its timestamp goes through the same chain as an event's**: the destination's row in
`locations_table`, its published IANA zone, then UTC. The benchmark's destination is
BORAAS, which publishes no zone, so the value is kept as sent and says so — the same
honesty rule described under *Timezones*.

**What is preserved with the forecast**, on `ETAHistory.raw_payload`: the provider code,
`observed_at` (when Container SCM received it — not the forecast, and not Traqo's own
clock), `provider_updated_at` verbatim, `eta_target`, `eta_reliable`, and Traqo's own
`eta_warning`, `status` and `is_delayed`. The full response is not copied here; it is
already in `TrackingRawPayload`.

**A new history row only when the forecast actually moves.** Compared against *this*
provider's own last observation, so re-polling an unchanged ETA writes nothing and
Traqo's forecast is never measured as drift against Maersk's. Two providers watching one
journey leave two attributable trails and are not merged.

**Nothing is recorded once the journey is over** — an actual arrival, or a closed
shipment. A delivered box must not display a future arrival, whatever the provider still
says. This is why the completed benchmark container yields no ETA evidence: it is home,
so the correct behaviour is to record nothing.

**Traqo does not own the ETA.** Where the journey is on a shipment that has no forecast
at all, the observation goes through `update_shipment_eta` — the same writer carrier
events use, so `original_eta`, the shipment event and delay detection all follow with no
second code path. Where a shipment already has a forecast, the observation is recorded
against its own source and the cached value is left alone: choosing between two
providers is precedence, which Phase 2.1 does not decide.

`eta_history_table` stays raw and unmapped. It is Traqo's history of its own forecasts,
not Container SCM's: on the benchmark container it held a single row whose `logged_at`
was the moment we first fetched.

**Read-model gaps, documented rather than fixed here.** `ContainerWorkspace.current_eta`
reads the shipment's ETA or the container's forecast *event*, so an ETA observation for a
container with neither is recorded and attributable but not displayed; `eta_source` on
that read model answers `"shipment"`/`"tracking"` rather than naming the provider (the
provider code is on `Shipment.eta_source` and on every `ETAHistory.source`); and
`get_shipment_eta_history` has no container-scoped counterpart. Whether a journey is
finished is answerable — `has_journey_arrived`.

## Known gaps and the reasoning behind each

**No stable event ID.** A production event row offers three candidates and none of them
survives inspection:

| field | production value | what it is |
| --- | --- | --- |
| `idx` | 1…10 | position in the list — an insert mid-history shifts every one after it |
| `event_id` | 1…10 | equal to `idx` on every row, so the same ordinal by another name |
| `name` | 4122761…4122770 | a Frappe child-row primary key, contiguous across the ten rows |

`name` is the only real identifier of the three, and the evidence available argues against
trusting it: every row's `creation` **and** `modified` read `2026-08-19 19:55:37.097479`,
114 ms before the response's own `last_updated_at` and 12 s after its `last_synced_at`.
The rows were materialised *by the sync that answered this request* — `parent`,
`parenttype` and `parentfield` confirm they are a child table hanging off container doc
`CNT-82510`. A child table rebuilt per sync hands out new primary keys per sync, and
adopting one as `source_event_id` would then either duplicate the whole history or
overwrite one event with another.

Proving that would take a second fetch of the same container to compare `name` values,
which costs a shipment slot to confirm a negative. So identity stays **unresolved**: the
field is left empty, the existing field-based fingerprint identifies the event, and `idx`,
`event_id` and `name` are all kept in `raw_data` so the comparison can be made for free
the next time any container is fetched twice. Consequence of leaving it: if Traqo corrects
an event's **timestamp**, that is a new row rather than an update. An extra row is
recoverable; a silently rewritten history is not.

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

**Shipment-level ETA needs no new architecture, but it does need reading.** Phase 1 read
the sandbox, where `data.eta` duplicates an `is_actual: 0` `ARRI` event and the existing
event-derived ETA therefore covered it. Production contained no forecast event at all —
see *ETA* below.

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

### Re-run after the Phase 2.1 corrections

The stored CPWU2588297 payload was re-read with `reparse_tracking_payloads --provider
traqo --container CPWU2588297 --prune-superseded` and benchmarked again with
`--compare --no-fetch --live`, so no Traqo request was spent. Against the same 16 stored
Maersk events:

| | before | after |
| --- | --- | --- |
| matched events | 10 | 10 |
| matched within 1 h | 0 | 8 |
| Yantian Δ | +8 h | +0 min |
| Gothenburg Δ | +2 h | +0 min |
| BORAAS Δ | +2 h | +2 h |
| benchmark event coverage | 72.7% | 72.7% |

Eight of the ten now agree with Maersk to the minute, and nothing that matched before
stopped matching. The two unchanged rows are unchanged *necessarily*: an 8 h or 2 h offset
sits well inside the ±24 h match tolerance, so it never cost a match to begin with. Which
is the point — a tolerance wide enough to survive genuine provider disagreement is also
wide enough to hide a systematic timezone error. The per-event Δ is the figure that
catches it, and it is the figure that moved.

The two that remain apart are the BORAAS pair, and their residual is exactly the
unconverted offset: Maersk places them at 07:30Z, Traqo said `09:30` with `timezone:
null`, and 09:30 is stored as sent. So the cost of refusing to guess a zone is bounded,
attributable and visible in the one place it applies. Maersk's `SEBOS` reading is
evidence that a zone *could* be inferred, and deliberately not used for it.

## Phase 2.2 — repeat observation, and what one payload cannot say

Phase 2.2 set out to answer what Traqo's ETA means during an **active** journey and how
it evolves. It could not: on 2026-08-29 no container in the installation was in transit.
Every one of the nine Maersk-watched and four CMA-watched containers derives
`journey_state = ARRIVED`, with the newest observed movement on 2026-07-24 — five weeks
old. So no live Traqo request was spent, and the T0 experiment is deferred rather than
faked. `traqo_test --candidates` is the check, and it prints the reason per container:

```bash
make manage ARGS='traqo_test --candidates'
make manage ARGS='traqo_test --candidates --reference-provider cma_cgm'
```

The bar it applies is the canonical journey derivation, not the subscription status. All
four CMA subscriptions are `active / tracking` and synced within hours; all four are
finished journeys. A watch nobody cancelled says nothing about where the box is.

### Maersk Direct is currently returning 401

Worth recording because it bounds what any near-term benchmark can measure: every Maersk
subscription is `failed / error` with 9–11 consecutive failures and `authentication —
maersk rejected the credentials (HTTP 401)`. The last successful Maersk sync was
2026-08-26. Until the credential is restored, Maersk cannot act as a *live* reference —
only as a frozen one.

### What `data.eta` targets, from Traqo's own data

`benchmark/eta_target.py` classifies the value by testing it against every milestone
Traqo itself publishes. Maersk is never consulted: using the reference provider to supply
the candidate's missing semantics would credit the candidate with a statement it never
made. On the one production payload available (CPWU2588297, DELIVERED):

| Traqo's own milestone | value | vs `data.eta` |
| --- | --- | --- |
| `data.eta` | `2026-07-13 13:15:00` | — |
| `voyage_plan_table` `pod` @ Goteborg | `2026-07-01 15:09:00` | 11.9 days earlier |
| `voyage_plan_table` `postpod` @ BORAAS | `2026-07-13 09:30:12` | 3 h 45 m earlier |
| `destination` | `BORAAS` | a place, not a time |
| last `events_table` row — `GTIN`/`CER` @ Goteborg | `2026-07-13 13:15:00` | **exact match** |

The exact match is the *empty container's return to the Gothenburg depot*. Not the POD
arrival, and not the inland destination Traqo itself names. On a finished journey
`data.eta` has become a restatement of the final event, so it classifies as
**`PROVIDER_DEFINED`** — Traqo supplies a value and does not say what future milestone it
forecasts. What it targets on an *active* journey is unknown and cannot be inferred from
this payload; that is the question a live T0 exists to answer.

`compare_etas` therefore refuses to subtract two ETAs unless both name the same
*specific* target. Two `PROVIDER_DEFINED` values are not comparable to each other:
matching silence is not agreement. The report prints the verdict where a raw difference
used to sit.

### Structural findings, documented and not ingested

| Table | Rows | Finding |
| --- | --- | --- |
| `events_table` | 10 | every row `is_actual: 1`. **No forecast events at all**, so the top-level ETA is the only forward-looking value Traqo supplies. |
| `voyage_plan_table` | 4 | phases `prepol`, `pol`, `pod`, `postpod`; all `is_actual: 1`; every `predictive_eta` null. Carries a `location_id`, so it resolves to a place *and its timezone* — richer than Container SCM ingests. Whether it publishes forecast phases mid-journey is unknown. |
| `eta_history_table` | 1 | one row, `logged_at` equal to the fetch instant, one distinct `eta`. A **snapshot of the current ETA, not a history of how it moved**, over a 62-day journey. Not ingested; Container SCM stays the owner of its own ETA history. |
| `route_json` | 2 segments | sea + land, and the **only place Traqo publishes UN/LOCODEs** (`CNYTN`, `SEGOT`). `locations_table` has no locode field at all, which is why every Traqo event reaches `TrackingEvent` without one — and why `eta.py`'s `target_unlocode` is always empty. |
| `locations_table` | 3 | one row (`BORAAS`) with `timezone: null` — the Phase 2.1 residual. |

### Event identity — T0 baseline preserved, no conclusion drawn

`source_event_id` is still deliberately unresolved. The snapshot preserves each event's
`idx`, `event_id`, `name`, `creation` and `modified` so a refetch can decide it. What the
T0 payload already suggests:

* `event_id` runs 1–10 and equals `idx` — positional, so an event inserted mid-history
  would shift it;
* `name` runs 4122761–4122770, a global Frappe docname sequence;
* `creation` **equals** `modified` on all ten events, identical to the microsecond
  (`19:55:37.097479`), and sits between the payload's `last_synced_at` (`19:55:24`) and
  `last_updated_at` (`19:55:37.211575`).

That last line is the interesting one: the whole child table was written in a single
operation *during that sync*. It is consistent with Traqo rebuilding its event rows on
every fetch, which would make both `event_id` and `name` unstable. **Consistent with is
not evidence of.** A refetch is what settles it, and until one happens the field-based
fingerprint stays.

### Comparing two runs

```bash
make manage ARGS='traqo_test <container> --sealine MAEU --compare --live --output T0.json'
# later
make manage ARGS='traqo_test <container> --sealine MAEU --compare --live --previous T0.json --output T1.json'
```

No Celery task and no schedule. ETA drift is measured per provider against that
provider's own earlier value — never Traqo's new figure against Maersk's old one — and is
withheld entirely when a provider's ETA *target* changed between runs, because the
arrival did not move, the subject did.

### The read-side gap, unchanged and now precisely located

Phase 2.1 suspected it; Phase 2.2 confirms it. `ContainerWorkspace.current_eta`
(`containers/workspace.py`) reads `shipment.eta`, then falls back to `tracking_eta`,
which comes from `get_container_tracking_eta_event` — a **forecast `TrackingEvent`**.
`VisibilityObject.current_eta` (`visibility/read_models.py`) applies the same two-step
rule. Neither consults `ETAHistory`.

So a provider ETA recorded through the Phase 2.1 path as an observation only — no
shipment, no forecast event, which is exactly what Traqo's top-level `data.eta` produces
— is stored correctly, attributably, and **is not displayed anywhere**. Whether to route
those two properties through a canonical ETA selector is a Phase 2.3 question. Nothing
here changes them.

For the record, the pipeline is not merely untested against reality — `ETAHistory` is
empty across the whole installation. `record_provider_eta_observation` correctly declined
to write for CPWU2588297 because `_journey_is_over` is true, which is the intended
behaviour and also why a live in-transit container is the only thing that can validate
the path end to end.

### Delay detection

`check_shipment_delay` takes a `Shipment`. The installation has **zero** shipments, and
none of the tracked containers is on one, so canonical delay detection currently has
nothing to run against here — independent of any provider question. Even with a shipment,
a Traqo observation recorded straight to `ETAHistory` could not influence the verdict:
`evaluate_shipment_delay` compares `shipment.eta` against `shipment.original_eta`, and an
observation that does not become `shipment.eta` never reaches it. Documented, not
changed; a second delay engine is out of scope.

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
