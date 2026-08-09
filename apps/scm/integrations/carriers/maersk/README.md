# Maersk Track & Trace integration

Live against Maersk's **public Track & Trace events** endpoint, by container number:

```
GET https://api.maersk.com/track-and-trace/public-events?equipmentReference=<container>
consumer-key: <consumer key>
API-Version: 1
```

Maersk runs on the shared DCSA pipeline (`carriers/dcsa/client.py` and
`carriers/dcsa/carrier_parser.py`), which every DCSA carrier uses. This package holds
only Maersk's identity, capabilities and the verified endpoint settings; the
configuration keys below are the shared ones and apply to any DCSA carrier.

The client still has no hardcoded endpoint. Every endpoint value comes from the
team's `Integration.config`, and a missing one raises `CarrierConfigurationError`,
which the sync layer records as a `SKIPPED` run (never as "synced, no events").

## Enabling a team

The verified settings live in code as
`carriers.maersk.client.PUBLIC_TRACK_AND_TRACE_CONFIG` — configuration data, not a
secret. Apply them and store the consumer key with:

```bash
export MAERSK_CONSUMER_KEY='<consumer key>'
python manage.py setup_maersk_integration --team <team-slug>
```

The command reads the key from the environment, writes it through the credential
service (encrypted at rest) and never echoes it. Verify with a live read-only call:

```bash
python manage.py test_maersk_tracking TRDU9258963 --team <team-slug>
```

The public endpoint authenticates on the consumer key alone, so Maersk is **not**
marked `requires_account_number`. Contracted Maersk products that do need an account
number carry it in their own `Integration.config`.

Still worth confirming per account:

- **Rate limits.** Set `min_poll_interval_minutes` to the contractual limit; the
  polling policy treats it as a hard floor.
- **Bill of lading and booking.** The public config maps `container_number` only. Add
  the product's own parameter names to `reference_params` to enable those; a reference
  kind absent from `reference_params` is refused rather than guessed.

## Configuration

Stored on the team's `Integration` (`provider_family=carrier`, `provider_code=maersk`).

```jsonc
{
  "base_url": "https://api.maersk.com",
  "tracking_path": "/track-and-trace/public-events",
  "auth_style": "api_key_header",              // or "oauth2_client_credentials"

  // Query parameter Maersk expects for each reference kind. At least one is required;
  // a reference kind that is absent here is reported as unsupported rather than guessed.
  "reference_params": {
    "container_number": "equipmentReference"
  },

  // auth_style = api_key_header
  "api_key_header_name": "consumer-key",

  // auth_style = oauth2_client_credentials
  "token_url": "<token endpoint>",
  "scope": "<scope, if the product requires one>",

  // Non-secret headers sent with every request.
  "extra_headers": { "API-Version": "1", "Accept": "application/json" },

  // Connectivity check
  "test_connection_reference": "TRDU9258963",

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
encrypts them at rest. They are never in settings, logs, raw payload metadata or Git.

- `auth_style = api_key_header` → `{"api_key": "<consumer key>"}`
- `auth_style = oauth2_client_credentials` → `{"client_id": "...", "client_secret": "..."}`

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
both auth styles, the shipped public config (`equipmentReference`, `consumer-key`,
`API-Version: 1`), timeout, 401/403, 404/no-data, 429 with `Retry-After`, 5xx, invalid
JSON, secret redaction in request logs, cross-team credential isolation, and DCSA
normalisation of the fixture in `tests/fixtures/carriers/maersk_tracking_response.json`.

`apps/scm/tracking/tests/test_maersk_pipeline.py` drives the real sync engine end to
end, including idempotent re-import of the same payload.

Every test uses an injected fake session; the suite makes no live calls.
