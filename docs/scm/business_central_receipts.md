# Business Central — Posted Purchase Receipts and Automatic Receive (stage 2)

A posted purchase receipt in Business Central can record exactly one physical
receive (GATE_IN) per container in Pegasus — only when the BC location is
configured for it, the receipt is resolved unambiguously to serial numbers, it was
posted after automatic receive was switched on, and nothing physical contradicts it.
Business Central is never written to.

## Reused, not rebuilt

| Need | Existing part reused |
|---|---|
| Sync run, lock, source binding, watermark | `business_central/lifecycle.py` (`run_business_central_sync`) |
| Serial numbers per receipt | Item ledger entries (stage 1a, Analytics API) |
| Container identity / ISO 6346 | `containers.intake` (`get_team_containers_by_parts`, `create_or_get_container`) |
| Container item + equipment type | `BusinessCentralItemRule` / `ItemRuleSet` (stage 1b) |
| Physical location | `ContainerLocation` (no new location register) |
| Receive + idempotency | `containers.receive.receive_container` → `record_container_movement` |
| Tracking after receive | Team policy `stop_tracking_on_receive` + `stop_tracking_after_receive` (extracted from the pasted-report receive) |
| Permissions | `scm_team_admin_required` (Team Admin) |

There is **no location-specific auto-stop** in Pegasus today; the existing
team-level policy applies to BC receives exactly as to pasted depot reports.

## API (verified read-only against `Sandbox_eligra_261002`, 2026-10-25)

`GET …/api/v2.0/companies({id})/purchaseReceipts?$expand=purchaseReceiptLines($select=…)`

- Header: `id`, `number`, `postingDate`, `vendorNumber`, `vendorName`,
  `orderNumber`, `lastModifiedDateTime` (also invoice/due dates and addresses —
  not used). **No location code.** Old receipts have `lastModifiedDateTime =
  0001-01-01` (stored as unknown).
- Line: `id`, `documentId`, `sequence`, `lineType`, `lineObjectNumber`,
  `description(2)`, `unitOfMeasureCode`, `unitCost`, `quantity`, discount/tax,
  `expectedReceiptDate`. **No location, variant, serial number or order-line /
  ledger reference.**
- `$filter=lastModifiedDateTime gt …`, `$orderby`, `$expand` with nested
  `$select`, `$count` all work. 17 605 receipts in the sandbox.

**Receipt → serial link.** Posting a receipt with item tracking writes one item
ledger entry per serial: `entryType = Purchase`, `documentType = "Purchase
Receipt"`, `documentNo` = receipt number (unique per BC company), plus item,
location, posting date, quantity and `entryNo`. Pegasus links on (integration,
company, document type, document number) — never on item number or quantity.
Quantity is only a cross-check: per container item, the receipt lines' total must
equal the net quantity of the serial entries. All 66 sandbox receipts with
serials reconcile (0 mismatches). There is no line-level link, and none is needed:
everything a receive needs is on the ledger entry itself.

Undo Receipt writes a negative entry on the same document (net 0) and a negative
receipt line; both are handled (REVERSED).

## Configuration — Settings → Integrations → Business Central → Locations

`BusinessCentralLocationMapping` per integration and BC location code: type
(physical / commercial / at customer), Pegasus location (physical only, own team),
receive trigger (none / manual / posted purchase receipt), automatic receive,
create missing containers, active. Defaults: trigger none, everything off.
Unmapped codes seen in ledger entries are listed for configuration. Nothing is
created by a migration.

Enforced by form **and** database constraints: automatic receive only for an
active physical mapping with a Pegasus location and the posted-receipt trigger;
container creation only alongside automatic receive.

**Activation boundary.** Switching automatic receive on stores
`auto_receive_after_entry_no` = the highest item ledger `entryNo` synced, and is
refused unless the latest item ledger run completed with `has_more = false`. Only
entries above it are receivable. Posting dates are not used for this (BC allows
backdating; `entryNo` is posting order). Switching off clears it; switching on
again sets a new one.

MCR example: `L005` physical → Oceanterminalen, trigger posted receipt (auto
receive only after verification); `L005UTHYRT` at customer, no location, no
trigger; `L005PROD`, `L005SAMFAK`, `L001` commercial until verified.

## Resources and processing

- `purchase_receipts` resource (off by default):
  `config.resources.purchase_receipts.enabled = true`. Incremental by
  `lastModifiedDateTime` (same overlap / `initial_sync_days` as purchase orders).
  Lines are kept in `raw_payload` (no line model). The sync creates nothing else.
- `max_receipt_age_days` (default 30) in the same block.
- After a completed receipt or item ledger sync, and from the Receipts page
  ("Process now"), `process_business_central_purchase_receipts_task` runs
  `receipt_receive.process_purchase_receipts` under one processing lock per
  integration: re-resolve (`receipt_resolution`), then decide per serial row.

### Resolution (`BusinessCentralReceiptContainer`, one row per receipt + serial)

| Status | Meaning |
|---|---|
| RESOLVED | Receipt reconciles; serial valid, container item, net quantity 1 |
| AMBIGUOUS | Serial nets ≠ 0/1, or on two items/locations, or more serials than lines |
| MISSING_SERIAL | Fewer serial entries than container lines (also while the ledger lags) |
| INVALID_SERIAL | Not a valid ISO 6346 number |
| UNCLASSIFIED_ITEM | Item with serials but no rule |
| MISSING_EQUIPMENT_TYPE | No container in Pegasus and no type to create one |
| NOT_APPLICABLE | Not a container item / receipt without container items |
| REVERSED | Undone in BC (net 0) |

### Receive decision (first failing check wins)

unknown location → inactive → not physical → automatic receive off →
**historical** (entry ≤ boundary) → no posting date / **stale** (older than
`max_receipt_age_days`) → receipt unresolved → serial unresolved / invalid /
missing type → container missing (created only if the mapping allows and the item
has a type) → under the container row lock: arrival at the same location on/after
the posting date, or container already there → ALREADY_RECEIVED; any other
movement on/after the posting date → NEWER_MOVEMENT → `receive_container`
(GATE_IN, source API, 00:00 of the posting date in the location's timezone).

RECEIVED and ALREADY_RECEIVED are final (idempotency: unique receipt + serial,
plus `receive_container`'s own container/location/time check). A receipt undone in
BC after receive is never reversed automatically; it is counted for review.

## Monitoring — Settings → Integrations → Business Central → Receipts

Receipts and lines synced, serials, containers identified, received / already
received / waiting / not received, reasons, resolution per receipt and serial,
undone after receive, last successful sync, last automatic receive; rows filterable
by status, reason or resolution. Review only.

## Sandbox verification (2026-10-25, local dev DB, integration 5 / team 4)

Automatic receive was **off** throughout; team 4 has no Pegasus containers.

| Check | Result |
|---|---|
| Receipts imported | 17 605 (18 655 lines, 14 587 receipts with lines), 81 s, 0 failed |
| Re-sync | 1 fetched (watermark overlap), 1 unchanged, 0 created |
| Receipts with serial entries | 66 → 64 RESOLVED, 1 AMBIGUOUS (IN27769), 1 REVERSED (IN27576) |
| Other receipts | 17 539 NOT_APPLICABLE (container items only appear from 2026-08-03) |
| Serial rows | 303 (294 distinct serials): 276 RESOLVED, 14 MISSING_EQUIPMENT_TYPE (CON04G1, CON25G1, CON45G1DD), 6 INVALID_SERIAL, 6 AMBIGUOUS, 1 REVERSED |
| Locations | L005 279 rows, L005SAMFAK 24 |
| Receive outcome | 0 received; 279 AUTO_RECEIVE_DISABLED, 24 NOT_PHYSICAL; 0 movements, 0 containers |
| Re-process | Identical state |
| Dry run (rolled back): L005 auto receive + create ON | boundary 95108; 279 HISTORICAL, 24 NOT_PHYSICAL; 0 movements, 0 containers |

IN27769: 26 lines and 26 serial units, but six serials were posted twice — a
quantity-only check would have passed it; the per-serial check marks it
AMBIGUOUS. Nine serials appear on two receipts (e.g. PSLU2912950 on IN27769 and
IN28043, after a KORR correction): a second receive is ALREADY_RECEIVED while the
container is still at the location.
