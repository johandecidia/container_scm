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
| Tracking after receive | `ContainerLocation.auto_stop_tracking_on_receive` via `containers.receive.apply_receive_tracking_policy` → `tracking.lifecycle.stop_container_tracking` |
| Permissions | `scm_team_admin_required` (Team Admin) |

## Stop tracking on receive (stage 2g)

**Pegasus Location decides auto-stop; Business Central decides whether a posted
receipt may trigger a receive.** Stop-on-receive is
`ContainerLocation.auto_stop_tracking_on_receive` (default off), set by Team Admins
under Settings → Locations (and in the location form; members see it read-only).
It is the only policy: migration `scm_containers.0013` copied each team's former
`TeamTrackingSettings.stop_tracking_on_receive` onto all of that team's existing
locations, and `scm_tracking.0018` removed the team field. Never keyed on the BC
location code.

Every receive path calls `apply_receive_tracking_policy(team, movement)` after a
*new*, committed receive:

| Path | Where |
|---|---|
| Manual (movement modal, GATE_IN / RECEIVED) | `containers.views.container_record_movement` |
| Pasted depot report (bulk receive) | `containers.receive.bulk_receive` |
| Business Central | `receipt_receive.process_purchase_receipts` |

It stops tracking only when the destination's setting is on **and** the receive is
the container's current position (a superseded, backdated receive stops nothing),
through `stop_container_tracking` — the Stop button's function, so the existing
statuses (a stopped watch ends CANCELLED, or PAUSED when a provider release is
refused) are unchanged. Already-received rows never stop again; a container nothing
watches returns "not active". Not receives, and unchanged: CSV movement imports
(POSITION_UPDATE) and carrier-interpreted gate-ins (tracking evidence).

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
**historical** (entry ≤ boundary) → no posting date / posting date in the future
(manual review) → receipt unresolved → serial unresolved / invalid /
missing type → container missing (created only if the mapping allows and the item
has a type) → under the container row lock: arrival at the same location on/after
the posting date, or container already there → ALREADY_RECEIVED; any other
movement on/after the posting date → NEWER_MOVEMENT → `receive_container`
(GATE_IN, source API, at the time Pegasus registers it).

**Posting date vs received at (stage 2g).** The BC posting date is a bookkeeping
date, kept on the receipt row and in the movement notes; it is not an arrival time.
The movement's `occurred_at` and the row's `received_at` are when Pegasus recorded
the receive. History is compared with the start of the posting date (location
timezone), never with the receive time, so a late or backdated receipt cannot
override newer physical information. There is no age limit: the `entryNo`
activation boundary is what keeps historical receipts out.

RECEIVED and ALREADY_RECEIVED are final (idempotency: unique receipt + serial,
plus the physical-order check under the container row lock and the per-integration
processing lock). A receipt undone in
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

## Posted serials on purchase order lines (stage 3b.4)

`receipt_serial_lines.py` says which purchase order line each posted-receipt serial
belongs to. It reuses the receipt rows above (`BusinessCentralReceiptContainer`), so
there is no new model and no migration.

| Step | From → to | Field (verified on PO 100021) |
|---|---|---|
| 1 | Item ledger entry → posted receipt | ILE `documentType = "Purchase Receipt"`, `documentNo` = receipt `number` (stage 2) |
| 2 | Posted receipt → purchase order | `purchaseReceipts.orderNumber` = `PurchaseOrder.po_number` (same team, same company) |
| 3 | Receipt line → order line | `purchaseReceiptLines.sequence` = `PurchaseOrderLine.line_no` |

ILE `Order_No`/`Order_Line_No` are empty for purchases and are not used. The Analytics
API sends no variant, so the variant shown is the order line's.

**Counted.** A serial counts only when both of these hold:

- its receipt is RESOLVED,
- the serial is a single valid container unit (RESOLVED or MISSING_EQUIPMENT_TYPE, net
  quantity 1).

Reversed, ambiguous and undone postings never reach a line.

**Attributed to a line.** A serial is placed on a line only when the receipt has exactly
one received line for its item. Otherwise it is listed under "Posted serials not placed on
a line" with a reason, and never guessed. The reasons are:

- the same item on several receipt lines,
- no receipt line for the item,
- the order line not in Pegasus,
- the same serial posted on two lines of the order.

**States per serial:**

| State | Meaning |
|---|---|
| `linked` | The container's `ContainerAcquisition` is this line |
| `unlinked` | The container exists, no acquisition yet |
| `not_in_pegasus` | No container with this number in the team (nothing is created) |
| `conflict` | The container was acquired through another line (it is not moved) |
| `several_orders` | The serial is also posted on another order (not linked) |

**Linking.**

- `link_posted_receipt_serials(integration)` writes a `ContainerAcquisition` only for
  `unlinked` serials.
- It writes through `procurement.container_links.link_business_central_acquisition`.
  That is the BC-only counterpart of the manual writer, with the same container row lock
  and the same idempotency, and it accepts only BC-owned orders. The manual writer still
  refuses BC orders.
- It runs after every receipt processing pass, and after a purchase-order sync
  (`link_business_central_receipt_serials_task`).
- It is idempotent: a second pass finds everything `linked`.

**No physical effect.** Linking creates no container, movement or receive. It sets no
location and does not start or stop tracking. A posted receipt is commercial evidence,
not a physical receive.

**Shown on** the purchase order page, under each line ("Posted in Business Central"):
serial, receipt number and state. A serial links to its container when the team has
one. Serials registered on the open order but not yet received are not shown; they come
with NAB's `purchaseLineTrackings`.

**Sandbox, 2026-10-27** (PO sync run in a rolled-back transaction):

- PO 100021, line 10000 (CON22G1, RAL5013, L005, 49/3/46): BBCU3272941 and
  BBCU3273059 on IN28149, BBCU3273090 on IN28166, all `not_in_pegasus`.
- Across 32 orders, 225 serials are placed on a line.
- 51 are not placed: IN28167 (33), IN27580 (10) and IN27608 (8) have the same item on
  several received lines. They need the document line (Page 38 `Document_Line_No`, blocked
  by Codeunit 5860) or a pre-receipt tracking snapshot.
