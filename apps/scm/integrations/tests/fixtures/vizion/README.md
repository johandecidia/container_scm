# Vizion fixtures

**These are synthetic, not recorded.** No Vizion credential existed in this installation
when the Phase 1 POC was written, so nothing here was captured from a live response.

Each file is built strictly from Vizion's published schema — the `reference_create`,
`reference_get` and `reference_updates_list` endpoint definitions, the reference-status
vocabulary, and the documented `journey_event` / location / milestone shapes. Where the
documentation left a field's exact value open, the fixture uses the value from Vizion's
own worked example rather than an invented one.

That distinction matters for reading the tests. They prove that **the mapper reads
Vizion's documented contract correctly and loses nothing**. They do not prove that a live
Vizion response matches that contract — only a live run can, and the acceptance cases
(`BBCU3273070`, `BBCU3273090`) exist for exactly that. Replace these files with recorded
responses as soon as a key is available, and the same assertions become evidence about
reality rather than about the specification.

| File | What it is |
| --- | --- |
| `reference_create_aci_pending.json` | `POST /references` with a container number and no carrier — the ACI request. Vizion has not searched yet. |
| `reference_aci_completed_oney.json` | `GET /references/{id}` after ACI succeeded and attached ONE. |
| `reference_aci_not_found.json` | `GET /references/{id}` where no supported carrier had data. Vizion keeps retrying. |
| `reference_aci_failed.json` | `GET /references/{id}` where identification errored and the reference was deactivated. |
| `updates_transshipment.json` | Two `GET /references/{id}/updates` envelopes for one journey with a transshipment, an ETA that moves between them, and full location/vessel detail. |
