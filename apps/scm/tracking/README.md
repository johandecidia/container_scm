# Carrier, and tracking provider

Two questions this app deliberately answers separately:

```
Who is moving this container?      the carrier      ONE
How do we know?                    the evidence     Vizion ACI
Who supplies the tracking data?    the provider     Traqo
```

For most containers the first and third have the same answer — Maersk carries the box
and Maersk's API tells us where it is — and for a long time one code served for both. It
stops working the moment an aggregator is involved: `carrier = ONE` with
`tracking provider = Traqo` is a normal, correct state, and a system with one field
cannot express it. It would call Traqo the carrier.

So there are two fields and two decisions.

| | decides | module | writes? |
|---|---|---|---|
| Carrier resolution | who is carrying the box | `integrations/carriers/carrier_resolution.py` | no |
| Provider routing | who to ask about it | `tracking/provider_routing.py` | no |
| Activation | does the asking and storing | `tracking/activation.py` | yes |

Keeping the first two free of writes is what makes them safe to reason about: neither can
spend a call or create a subscription, so a return value is the whole of what happened.

## Discovery order, and why it is a cost policy

`resolve_carrier_for_container()` tries five steps and **short-circuits at the first
answer**:

```
1. trusted knowledge already held      free
2. Traqo free carrier lookup           free
3. Traqo candidate probing             a few shipment calls, capped at 5
4. direct carrier API discovery        the team's own rate limits
5. Vizion ACI                          a paid reference, every call
```

The order is not a preference. Vizion creates a billable reference on *every* call, so it
must never run for a container an earlier step has already explained; a container whose
shipment names its carrier must reach no provider at all. `test_carrier_resolution.py`
asserts on **what was not called** for exactly this reason.

### Traqo lookup and Traqo probe are one provider and two steps

Steps 2 and 3 are both Traqo. They are separate because they spend different budgets and
produce different *kinds* of answer.

| | endpoint | cost | answer |
|---|---|---|---|
| `traqo_lookup` | `carriers/lookup` | free, own quota, creates nothing | a carrier **named** |
| `traqo_candidate_probe` | `container/<no>?sealine=X` | may consume a shipment slot | a carrier **proved** |

The lookup asks "which carrier is likely to know this number". The probe asks Traqo's real
container endpoint about a short list of likely carriers and sees whether one returns this
container's tracking data. So the lookup stays first — it is free and writes nothing at
Traqo either — and probing sits behind it.

BBCU3273070 is why step 3 exists. Traqo's lookup did not recognise the number; Vizion was
paid to identify ONE; Traqo then tracked the box perfectly once told `sealine=ONEY`. Traqo
had the shipment the whole time and could not answer "who moves this" without being told
who to ask about. Probing asks.

**Why there is a cap.** `MAX_TRAQO_PROBE_ATTEMPTS = 5`, against a
`DEFAULT_TRAQO_DISCOVERY_ORDER` of nine carriers. The order is a guess — evidence-first
where there is any, global volume where there is none — so five is where the marginal
chance of a hit stops justifying another shipment call. A container none of the five can
explain is better handed to step 4 than swept across every carrier Traqo covers. Both
constants live in `integrations/traqo/carrier_probe.py` and are a *discovery priority*,
not a second carrier registry: identity, display name and SCAC still come from the
registry and `traqo/sealines.py`.

**What makes a probe an answer.** Sending `sealine=ONEY` is the question, so HTTP 200 is
not evidence. A probe is FOUND only when Traqo returns a shipment for the container that
was asked about, the mapper produces events from it, and the SCAC — read from the
*response* where Traqo states one, not from the request — belongs to a carrier that can be
routed to. That is the same standard a direct probe meets, which is why `TRAQO_PROBE` is
recorded as `verified` and `TRAQO_LOOKUP` is not.

**Only for containers with no source.** Probing is part of carrier discovery, which the
manual refresh reaches only when a container has no verified subscription. A container that
is already tracked is refreshed through the sources it has; it is never re-probed.

### What counts as trusted knowledge

In order of what the evidence is worth:

1. a carrier already recorded on a verified subscription for this container
2. a carrier recorded on the container's `PlannedContainer` — a person chose it
3. the carrier on the container's shipment — a booking fact, and the weakest of the three

The ISO 6346 owner prefix is **not** here. It names who owns the box; a leased container
travels under whoever booked it. Direct discovery may use it to order a sweep, never to
stand in for knowing.

The manual refresh path deliberately passes `use_trusted_knowledge=False` and feeds those
same signals in as `preferred_carrier_codes` instead: it only runs for a container with no
verified source, so accepting a hint as the answer would route straight to an unconfirmed
carrier and skip the sweep that would have found the real one.

### Carrier resolution never writes the shipment's carrier

`Shipment.carrier` is what was booked. A provider saying ONE is moving the box is a
different fact, and reconciling the two is not resolution's decision to make.

## Provider routing order

`resolve_tracking_route()` picks, for a known carrier:

```
1. the carrier's own API   registered, answers by container number, connected for this team
2. Traqo                   configured, and publishing a sealine for this carrier
3. nothing                 a clean NOT_CONFIGURED, never a guess
```

Direct is first for reasons that outlast any one carrier: the carrier's own API is the
primary record rather than a copy of it, it carries detail aggregators normalise away, and
it spends no third-party quota.

**Vizion is not a tracking route.** It can track, and its ACI has already created the
reference that would make tracking nearly free — but a reference is the billable unit, and
routing to it would turn every unresolvable container into a purchase. It is a *discovery*
provider. The day that changes, a fourth branch is added to `provider_routing.py` and
nothing else moves.

Routing is centralised so that `if carrier == "one"` appears nowhere else. When ONE's
direct adapter starts working, this module's answer changes and every caller follows.

## The data model

`TrackingProvider.code` is the **technical** provider. `TrackingSubscription` carries the
carrier separately:

| field | meaning |
|---|---|
| `provider` | whose API supplies this watch's data |
| `carrier_code` | the registry code of the carrier moving the box, when known |
| `carrier_name` | its display name |
| `carrier_source` | how the carrier was established (`CarrierSource`) |
| `provider_reference` | the provider's own handle — Traqo's sealine, Vizion's reference id |

So the acceptance case stores:

```
container           BBCU3273070
provider.code       traqo
carrier_code        one
carrier_name        ONE (Ocean Network Express)
carrier_source      traqo_probe    (vizion_aci where probing could not find it either)
tracking_reference  BBCU3273070
provider_reference  ONEY
```

Only `carrier_source` differs between the two routes to that state, and it is the field
whose whole job is to say so: probing returned this box's events, Vizion named a carrier.
Four things about this shape:

**Blank `carrier_code` is honest.** An aggregator watch whose carrier was never
established genuinely does not know one, and nothing infers a carrier from the provider to
fill the gap. It reads as "Carrier unknown" in the UI, because that is true and "Traqo" is
not.

**A *direct* watch needs no recorded carrier.** Maersk supplying the data and Maersk
carrying the box are the same fact, so
`selectors.TrackingProvenance.carrier_code` reads the provider code as a carrier code
where — and only where — the provider is itself a registered carrier. That single rule
lives in one place; `manual_refresh.describe_subscription_carrier` and
`ContainerWorkspace.tracking_carrier_name` both call it.

**Carrier identity is not part of the natural key.** The key is still
(team, provider, container, reference type), so a provider that later learns the carrier
enriches the existing watch rather than creating a second one beside it. And
`record_subscription_carrier()` only ever *adds* — a later lookup disagreeing with a
carrier that already proved itself is a thing for a person to settle, not for the system
to overwrite quietly.

**The `provider_reference` stored is the one that answered.** For a probed container that
is Traqo's own sealine from the response, not the one routing would have chosen. They
agree today; if they ever disagree, the question that returned this box's data is the one a
scheduled refresh must ask again. It also closes the gap both provider READMEs recorded as
blocking scheduled refresh: there is now somewhere to persist the SCAC and the reference id.

## Failure semantics

Every step reports one of five things, and the distinctions are load-bearing:

| | means |
|---|---|
| `FOUND` | a carrier was named |
| `NOT_FOUND` | the provider answered and named nobody — a real answer about the box |
| `NOT_CONFIGURED` | no credential; we never asked |
| `ERROR` | we asked and the call failed |
| `SKIPPED` | the step was turned off |

A Traqo timeout is **not** "this container has no carrier". A Vizion 401 is **not**
"Vizion has never heard of it". Collapsing either into `NOT_FOUND` would make the next,
paid step run on the strength of our own outage. A direct `NOT_FOUND` means only that that
provider could not verify this container, and never withdraws an existing verified source.

The chain continues past an `ERROR` exactly as it continues past a `NOT_FOUND`, and both
are visible in `CarrierResolution.steps`.

The same five values apply *within* the probe, per candidate, and the distinction earns its
keep there twice over. A candidate that answers `NOT_FOUND` is simply wrong about this box
and the next is asked. A candidate that fails technically is too — unless the failure was
never about the candidate: a rejected key, an account problem or a container number Traqo
will not accept ends the probe, because four more calls would be told the same thing.
Every attempt is on `CarrierResolution.traqo_probe.attempts` either way.

## The BBCU3273070 flow, end to end

Taken from a real run, and covered by `tracking/tests/test_traqo_probe_acceptance.py`.

```
BBCU3273070, nothing tracking it

  trusted knowledge      → nothing recorded
  Traqo carrier lookup   → NOT_FOUND        (free; Traqo's lookup cannot see the number)
  Traqo candidate probe  → ONEY             (one shipment call: ONE leads the order)

  carrier resolution:
      carrier              = one
      carrier source       = TRAQO_PROBE
      verified             = True           (Traqo returned this box's own events)
      tracking provider    = traqo
      provider reference   = ONEY
      events + raw payload = already in hand

  provider routing:
      ONE direct        unavailable / not connected
      Traqo             configured, publishes sealine ONEY
      → provider = traqo, provider_reference = ONEY

  activation:
      no second request — the probe's payload is stored
      → existing store_traqo_container_result
      → existing store_verified_carrier_result / persist_normalised_events
      → TrackingEvent rows against provider traqo, plus the ETA observation

  → Journey / ETA / Timeline / Visibility, unchanged
```

One Traqo container call for the whole refresh: the probe's. Direct discovery and Vizion
are never reached, which is the saving the probe step was added for — the same container
previously cost a Vizion reference to resolve.

**The fallback still works.** Where not even the probe can find the box,
`tracking/tests/test_carrier_routing_acceptance.py` covers the original path: the chain
continues to the direct sweep and then to Vizion ACI, which identifies ONE and routes to
Traqo as before, recording `carrier_source = VIZION_ACI`.

Pressing Refresh a second time resolves at step 1 — the subscription now records
`carrier_code = one` — so it re-probes nothing, buys no Vizion reference, and creates no
second watch.

### Re-using the payload that proved the carrier

Two steps can prove a carrier, and both do it by *fetching* its events. Activating that
carrier must not immediately ask the same question again, so `CarrierResolution` carries
the answer out provider-neutrally:

| field | direct hit | Traqo probe hit |
|---|---|---|
| `events` / `raw_payload` | the carrier's | Traqo's |
| `tracking_provider_code` | the carrier's own code | `traqo` |
| `provider_reference` | `""` (nothing more is needed) | the sealine that answered |
| `discovery` | the sweep's `CarrierDiscoveryOutcome` | `None` |
| `traqo_probe` | the probe result if one ran | the probe result |

`has_tracking_payload` is the test for "storable without another call"
(`has_direct_payload` remains as its former name). `activation.py` branches on which
provider produced it, and each branch stores through that provider's ordinary write path —
`store_discovered_carrier_source` for a sweep, `store_traqo_container_result` for Traqo.
Neither is a second ingestion path: they are the same functions the fetching paths use,
reached with the payload already in hand.

## Multi-source and continuation

A container can have several verified sources at once, covering different legs, and this
change does not narrow that. What it fixes is an exclusion set that was quietly in the
wrong vocabulary: `continuation.get_recently_checked_carrier_codes()` feeds
`exclude_carrier_codes`, which is in **carrier** space. A Traqo watch carrying ONE now
contributes `one`; contributing `traqo` excluded nothing, and the sweep would have probed
ONE directly moments after Traqo answered about it. An aggregator watch with no carrier
recorded contributes nothing, which is right — we do not know which carrier it covered, so
we cannot skip one.

## Adding another aggregator later

1. Build the client and mapper in `integrations/<provider>/`, following Traqo's shape.
   Map to `NormalisedTrackingEvent`; do not add an event model.
2. Register it in `tracking/sources.py` so the carrier poller steps aside rather than
   recording a fault.
3. If it can identify carriers, add a `discovery.py` returning the five-valued outcome,
   and a step in `carrier_resolution.py` at the right point in the cost order.
4. If it can track, add a branch to `_traqo_route`'s neighbours in `provider_routing.py`
   and an activation branch in `activation.py`.
5. Add SCACs to the registry's `scac_codes` if it names carriers this system does not yet
   know. Do **not** add a second mapping table —
   `registry.resolve_carrier_code_from_scac()` is the one.

## Configuration

Both aggregators are installation-wide rather than per-team: one account for the
installation, not a customer agreement each team holds with a carrier.

```
TRAQO_ENABLED=false           gates live calls only; the sandbox needs no credential
TRAQO_BASE_URL="https://traqocontainer.com/api/v1"
TRAQO_API_KEY=""

VIZION_ENABLED=false          gates both environments; Vizion's demo is metered
VIZION_BASE_URL="https://prod.vizionapi.com"
VIZION_DEMO_BASE_URL="https://demo.vizionapi.com"
VIZION_API_KEY=""
```

Routing reads these through `is_traqo_configured()` / `is_vizion_configured()` rather than
by attempting a call, so an unconfigured provider is ruled out without spending a request.
Candidate probing checks the same way, once, before any candidate: "Traqo is not set up
here" is not a statement about the container, and discovering it five times over would
report five failures where there is one configuration gap.

Both aggregators are switched **off under `manage.py test`** (see `settings.py`). They sit
inside the resolution chain, and one of them spends shipment calls, so on a machine with
working credentials a test that exercised the chain without injecting its provider calls
would have reached them for real. Tests that need one configured say so with
`@override_settings` and inject the call or a fake session.

Direct carriers stay per-team through `Integration` + `IntegrationCredential`. Secrets are
never logged, never placed in a raw payload, and never rendered.

See `integrations/traqo/README.md` and `integrations/vizion/README.md` for each provider's
payload shape, canonical gaps and what its fixtures do and do not prove.
