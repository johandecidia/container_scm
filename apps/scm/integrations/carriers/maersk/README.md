# Maersk Track & Trace integration

The transport and parser are complete and tested against fixtures. **Live traffic is
off until the integration is configured** — the client has no hardcoded endpoint and
raises `CarrierConfigurationError` when a required value is missing, which the sync
layer records as a `SKIPPED` run (never as "synced, no events").

Maersk runs on the shared DCSA pipeline (`carriers/dcsa/client.py` and
`carriers/dcsa/carrier_parser.py`), which every DCSA carrier uses. This package holds
only Maersk's identity and capabilities; the configuration keys below are the shared
ones and apply to any DCSA carrier.

## What must be confirmed before going live

These values are deliberately configuration rather than constants, because they
differ per Maersk API product and per customer agreement. Take them from the API
documentation for the product the account is subscribed to — do not guess:

| Value | Where it comes from |
| --- | --- |
| `base_url` | Maersk API portal, for the subscribed product and environment |
| `tracking_path` | The Track & Trace events path of that product |
| `reference_params` | The query parameter name for each reference kind |
| `auth_style` | Which auth the product uses (`api_key_header` or `oauth2_client_credentials`) |
| `api_key_header_name` *or* `token_url` (+ `scope`) | The auth details for that style |
| `test_connection_reference` | A reference known to the account, used only to verify connectivity |

Also outstanding, and outside what code can settle:

- **Customer approval / account number.** The registry marks Maersk as
  `requires_customer_approval` and `requires_account_number`. Confirm the account is
  entitled to the Track & Trace product before enabling.
- **Rate limits.** Set `min_poll_interval_minutes` to the contractual limit; the
  polling policy treats it as a hard floor.
- **Whether 404 means "no data".** The default treats 404 as no data. If the product
  signals an unknown reference differently, set `no_data_statuses`.

## Configuration

Stored on the team's `Integration` (`provider_family=carrier`, `provider_code=maersk`).

```jsonc
{
  "base_url": "https://<from Maersk API portal>",
  "tracking_path": "<events path of the subscribed product>",
  "auth_style": "oauth2_client_credentials",   // or "api_key_header"

  // Query parameter Maersk expects for each reference kind. At least one is required;
  // a reference kind that is absent here is reported as unsupported rather than guessed.
  "reference_params": {
    "container_number": "<param name>",
    "bill_of_lading_number": "<param name>",
    "booking_number": "<param name>"
  },

  // auth_style = oauth2_client_credentials
  "token_url": "<token endpoint>",
  "scope": "<scope, if the product requires one>",

  // auth_style = api_key_header
  "api_key_header_name": "<header name>",

  // Connectivity check
  "test_connection_reference": "<a reference known to this account>",

  // Optional transport tuning
  "request_timeout_seconds": 30,
  "max_retries": 3,
  "retry_backoff_seconds": 0.5,
  "min_poll_interval_minutes": 60,
  "no_data_statuses": [404],
  "extra_headers": {}                          // non-secret headers only
}
```

## Credentials

Written through `apps.scm.integrations.credentials.set_integration_credentials`, which
encrypts them at rest. They are never in settings, logs, raw payload metadata or Git.

- `auth_style = oauth2_client_credentials` → `{"client_id": "...", "client_secret": "..."}`
- `auth_style = api_key_header` → `{"api_key": "..."}`

## Behaviour

- One reference per call, validated against the carrier's capabilities.
- Retries with exponential backoff on timeouts and 5xx; honours `Retry-After` on 429.
- 404 (configurable) → `CarrierNoDataError`, which is a successful sync with zero events.
- 401 triggers one token refresh, then fails as an authentication error.
- Every request is logged to `IntegrationRequestLog` with the path only — no query
  string, no headers, no body — so a credential cannot reach the log table.
- Responses are stored in `TrackingRawPayload` before parsing and only marked parsed
  once parsing succeeds.

## Tests

`apps/scm/integrations/tests/test_maersk_client.py` covers configuration validation,
both auth styles, timeout, 401/403, 404/no-data, 429 with `Retry-After`, 5xx, invalid
JSON, secret redaction in request logs, and DCSA normalisation of the fixture in
`tests/fixtures/carriers/maersk_tracking_response.json`. Every test uses an injected
fake session; the suite makes no live calls.
