# Hapag-Lloyd Track & Trace integration

Hapag-Lloyd runs on the shared DCSA pipeline
(`carriers/dcsa/client.py` + `carriers/dcsa/carrier_parser.py`), so this package adds
only the carrier's identity, capabilities and endpoint configuration. There is no
Hapag-Lloyd HTTP code, retry policy or parser to review separately — fixing a
transport bug once fixes it for every DCSA carrier.

## Authentication

Hapag-Lloyd's API sits behind an IBM API Connect gateway. The developer portal issues
a **client id** and **client secret** per application, and both go on every request as
headers:

```text
X-IBM-Client-Id:     <client id>
X-IBM-Client-Secret: <client secret>
```

There is no token endpoint and nothing to exchange, so this is neither a single-header
API key (Maersk) nor an OAuth grant. It is the shared
`carriers/oauth.py::ClientIdSecretHeaderAuth`, selected with
`auth_style = "client_id_secret_headers"`. Both header names are configurable, because
the prefix varies by gateway deployment.

A subscribed product that *does* use the OAuth2 client-credentials grant is supported
without any code change: set `HAPAG_TOKEN_URL` when running the setup command and the
integration switches to `auth_style = "oauth2_client_credentials"`, which the shared
`ClientCredentialsAuth` has always handled.

## Setup

Credentials are read from the environment at setup time and stored encrypted through
`apps.scm.integrations.credentials.set_integration_credentials`. They are never
written to `Integration.config`, never logged and never printed.

```bash
export HAPAG_CLIENT_ID='<client id>'
export HAPAG_CLIENT_SECRET='<client secret>'
python manage.py setup_hapag_lloyd_integration --team <team-slug> \
    --test-reference <a container the account can see>
```

Optional environment overrides, for a product whose gateway path differs from the
default:

| Variable | Effect |
| --- | --- |
| `HAPAG_API_BASE_URL` | Overrides `base_url` (default `https://api.hlag.com`) |
| `HAPAG_TRACKING_PATH` | Overrides `tracking_path` |
| `HAPAG_TOKEN_URL` | Switches to the OAuth2 client-credentials grant |
| `HAPAG_SCOPE` | OAuth scope, when the token endpoint requires one |

Verify with the carrier-neutral diagnostic (read-only, writes nothing but the ordinary
sanitised request log):

```bash
python manage.py test_carrier_tracking <container> --provider hapag_lloyd --team <team-slug>
```

## What must still be confirmed

**`tracking_path`.** Hapag-Lloyd versions its gateway paths per product, so the exact
Track & Trace events path comes from the OpenAPI spec of *your* subscribed product on
the portal. `TRACK_AND_TRACE_CONFIG` ships `/hlag/external/v2/events`; the setup
command prints the resolved URL and warns about this, because a wrong path answers 404
and the shared transport reads 404 as "the carrier has no data for this reference" —
indistinguishable from a container Hapag-Lloyd has never heard of. If you configure it
and the smoke test reports "no data" for a container you can see on the portal, suspect
the path before suspecting the container.

Also outstanding:

- **Rate limits.** Set `min_poll_interval_minutes` to the contractual limit.
- **Pagination.** Left off, so exactly one request is made. If the product advertises a
  next-page cursor, configure `pagination.cursor_param` and
  `pagination.next_page_header` from its documentation — half-configured is refused
  rather than silently fetching only the first page.
- **DCSA version.** The shared parser targets the DCSA Track & Trace event shape and
  reads both the flat and the nested (`eventLocation` / `transportCall`) spellings.
  Hapag-Lloyd sends the nested one. A genuine deviation belongs in `HapagLloydParser` —
  and only once a real response has shown it, not in anticipation.

## Tests

- `apps/scm/integrations/tests/test_hapag_lloyd_client.py` — identity, configuration
  validation, both auth styles, transport behaviour through the shared client, and
  normalisation of `tests/fixtures/carriers/hapag_lloyd_dcsa_events.json` (the real
  nested DCSA shape, including an unmapped event code).
- `apps/scm/tracking/tests/test_hapag_lloyd_pipeline.py` — the sync engine end to end:
  persistence, location resolution both ways, the map read for an unresolved place,
  carrier discovery, routing, and coexistence with Traqo.
- `apps/scm/integrations/tests/test_management_commands.py` —
  `HapagLloydSetupCommandTest`, covering both auth styles and the environment
  overrides.

No test makes a live call.
