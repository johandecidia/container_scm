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

`resolve_carrier_for_container()` tries four steps and **short-circuits at the first
answer**:

```
1. trusted knowledge already held      free
2. Traqo free carrier lookup           free
3. direct carrier API discovery        the team's own rate limits
4. Vizion ACI                          a paid reference, every call
```

The order is not a preference. Vizion creates a billable reference on *every* call, so it
must never run for a container an earlier step has already explained; a container whose
shipment names its carrier must reach no provider at all. `test_carrier_resolution.py`
asserts on **what was not called** for exactly this reason.

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
carrier_source      vizion_aci
tracking_reference  BBCU3273070
provider_reference  ONEY
```

Three things about this shape:

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

`provider_reference` also closes the gap both provider READMEs recorded as blocking
scheduled refresh: there is now somewhere to persist the SCAC and the reference id.

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

## The BBCU3273070 flow, end to end

Taken from a real run, and covered by `tracking/tests/test_carrier_routing_acceptance.py`.

```
BBCU3273070, nothing tracking it

  trusted knowledge      → nothing recorded
  Traqo carrier lookup   → NOT_FOUND        (free; Traqo does not have the container)
  direct API discovery   → no verified carrier
  Vizion ACI             → ONEY             (a reference is created and billed)

  carrier resolution:
      carrier        = one
      carrier source = VIZION_ACI
      verified       = False

  provider routing:
      ONE direct        unavailable / not connected
      Traqo             configured, publishes sealine ONEY
      → provider = traqo, provider_reference = ONEY

  activation:
      GET /container/BBCU3273070?sealine=ONEY
      → existing Traqo mapper
      → existing store_verified_carrier_result / persist_normalised_events
      → TrackingEvent rows against provider traqo

  → Journey / ETA / Timeline / Visibility, unchanged
```

Pressing Refresh a second time resolves at step 1 — the subscription now records
`carrier_code = one` — so it buys no further Vizion reference and creates no second watch.

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

Direct carriers stay per-team through `Integration` + `IntegrationCredential`. Secrets are
never logged, never placed in a raw payload, and never rendered.

See `integrations/traqo/README.md` and `integrations/vizion/README.md` for each provider's
payload shape, canonical gaps and what its fixtures do and do not prove.
