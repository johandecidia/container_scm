# Hapag-Lloyd Track & Trace integration

Hapag-Lloyd runs on the shared DCSA pipeline
(`carriers/dcsa/client.py` + `carriers/dcsa/carrier_parser.py`), so this package adds
only the carrier's identity and capabilities. There is no Hapag-Lloyd HTTP code,
retry policy, auth flow or parser to review separately — fixing a transport bug once
fixes it for every DCSA carrier.

**Live traffic is off until the integration is configured.** As with Maersk, no
endpoint is hardcoded: a missing value raises `CarrierConfigurationError` and the
sync layer records a `SKIPPED` run rather than a misleading "no events".

## What must be confirmed before going live

| Value | Where it comes from |
| --- | --- |
| `base_url` | Hapag-Lloyd API portal, for the subscribed product and environment |
| `tracking_path` | The Track & Trace events path of that product |
| `reference_params` | The query parameter name for each reference kind |
| `auth_style` | `api_key_header` or `oauth2_client_credentials` |
| `api_key_header_name` *or* `token_url` (+ `scope`) | The auth details for that style |
| `test_connection_reference` | A reference known to the account |

Also outstanding:

- **Customer approval / account number.** The registry marks Hapag-Lloyd as
  `requires_customer_approval` and `requires_account_number`.
- **Rate limits.** Set `min_poll_interval_minutes` to the contractual limit.
- **Whether 404 means "no data".** Default is yes; override with `no_data_statuses`.
- **DCSA version.** The shared parser targets the DCSA Track & Trace event shape. If
  the subscribed product deviates, the deviation belongs in `HapagLloydParser` — and
  only once a real response has shown it, not in anticipation.

## Configuration and credentials

Identical in shape to Maersk; see `carriers/maersk/README.md` for the annotated
example. Credentials go through
`apps.scm.integrations.credentials.set_integration_credentials`, which encrypts them
at rest:

- `auth_style = oauth2_client_credentials` → `{"client_id": "...", "client_secret": "..."}`
- `auth_style = api_key_header` → `{"api_key": "..."}`

## Tests

`apps/scm/integrations/tests/test_hapag_lloyd_client.py` covers configuration
validation, transport behaviour through the shared client, and normalisation of
`tests/fixtures/carriers/hapag_lloyd_tracking_response.json`, including that the
third event's estimated arrival is not recorded as an actual one. No test makes a
live call.
