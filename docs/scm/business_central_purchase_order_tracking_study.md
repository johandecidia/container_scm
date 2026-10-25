# ETAPP 3a — Förstudie: öppna inköpsorder och serienummer i Business Central

Datum: 2026-10-25 · Gren: `EPIC-3-FIXES` · Status: **förstudie, ingen implementation**

Underlaget kommer från en genomgång av Pegasus-koden och från read-only GET-anrop mot
`Sandbox_eligra_261002` (integration 5, team 4, företag MCR
`782d7133-5033-4c4b-9a29-83f7f44f062d`). Inga BC-write-anrop gjordes, inga migrationer
skapades och inga containrar, movements eller receives skapades.

Beteckningar som används i tabellerna:

- **Dokumenterat**: finns i `$metadata`.
- **Verifierat**: returneras faktiskt av sandboxen.
- **403**: objektet finns, men appen saknar behörighet. Ett 403 betyder inte att API:et saknas.
- **Saknas**: finns inte i någon publicerad resurs.
- **Ej verifierat**: kan inte avgöras med GET och befintliga data.

---

## A. Executive Summary

**Det som redan finns i Pegasus.** Inköpsorder och orderrader synkas från BC (header-watermark,
idempotent upsert, soft-delete via reconcile). Kopplingen *orderrad → individuella
containrar* finns redan som `ContainerAcquisition`: en rad kan ha många containrar och en
container har högst ett ursprung. Serienummer från **bokförda** inleveranser läses redan
(Analytics `itemLedgerEntries` → `BusinessCentralPurchaseReceipt` /
`BusinessCentralReceiptContainer`). Klassificering, ISO 6346-validering, kontrollerad
containerskapning och automatisk receive är på plats. Etapp 3 behöver ingen ny
inköpsordermodell.

**Det som saknas i BC:s standard-API.** Inget publicerat API, varken standard v2.0,
Analytics v1.0 eller någon av de 219 publicerade ODataV4-entitetsmängderna, exponerar
artikelspårning (serienummer) på **obokförda** inköpsorderrader. I BC ligger de i
tabell 337 *Reservation Entry*, som saknar standard-API. Svaret på huvudfrågan
*"kan vi läsa serienummer på en öppen inköpsorder innan inleveransen bokförts?"*
är därför **nej, inte i dag**. Det kräver en liten anpassning hos NAB: en read-only
API-sida eller query över Reservation Entry.

**Det som kan lösas utan utveckling hos NAB.** Det här kräver bara behörigheter:
variantkod, lagerställeskod, `Order_Line_No` på bokförda artikeltransaktioner och
utestående antal. De ligger alla bakom 403 och inte bakom saknade API:er (se C).

**Det som Pegasus själv behöver rätta.** `PurchaseOrderLine.received_qty` skrivs aldrig av
BC-synken (mappern lägger `receivedQuantity` i `raw_payload`). BC-order visar därför 0
mottaget i workspace och fulfillment. Det kan rättas utan migration.

**Rekommendation.**

1. Beställ **en** liten, read-only API-resurs från NAB, `purchaseLineTrackings`, plus
   fyra behörigheter.
2. Pegasus bygger etapp 3 med en staging-modell för spårningsrader (samma mönster som
   item ledger entries), full avstämning av öppen spårning vid varje körning, och
   befintliga `ContainerAcquisition` samt matchnings- och skapandeservices.

---

## B. Pegasus nuläge

### Modeller

| Modell | Fil | Roll |
|---|---|---|
| `PurchaseOrder` | `apps/scm/procurement/models.py` | BC-order. Unik på `(team, external_id)` = BC `id` (GUID). `status` = BC-dokumentstatus. `source_active`/`source_deleted_at` = finns/försvunnen i BC. `source_last_modified_at`, `raw_payload`, `sync_hash`. |
| `PurchaseOrderLine` | samma | Unik på `(purchase_order, external_id)` = BC line `id` (GUID). `line_no` = BC `sequence` (= Line No., t.ex. 10000). `item_no`, `ordered_qty`, `shipped_qty`, `received_qty`, `expected_receipt_date`. **Inga fält för variant eller lagerställe** (finns bara som GUID i `raw_payload`). |
| `ContainerAcquisition` | samma | "Containern *är* det som köptes på raden." FK till rad (1:N) och OneToOne till `Container` (ett ursprung per container). Manuell skrivning vägras för BC-order (`refuse_business_central`). Modulens docstring förutser en separat BC-sync-skrivare. |
| `ContainerLoad` | samma | Gods från raden lastat i containern (inte aktuellt för containerköp). |
| `SupplierDelivery(Line)` | `apps/scm/supplier_deliveries/models.py` | Daterad leveransbatch med valfri container per rad. Grunden för expected arrivals. |
| `BusinessCentralItemLedgerEntry` | `apps/scm/integrations/models.py` | Bokförda artikeltransaktioner med serie, ISO-status, `container`-FK (nullable) och `link_status`. |
| `BusinessCentralPurchaseReceipt` / `…ReceiptContainer` | samma | Bokförd inleverans (`order_no`, rader i `raw_payload`), plus en rad per inleverans och serie med upplösning och receive-status. |
| `BusinessCentralItemRule`, `…LocationMapping` | samma | Artikelklassificering (inkl. variant) och lagerställe → Pegasus-location. |

### Svar på frågorna i del A

1. **Representation.** En `PurchaseOrder` per BC-order och en `PurchaseOrderLine` per
   BC-rad, nycklade på BC:s GUID:er. BC är master och raderna är read-only i SCM.
2. **Radidentitet.** `external_id` = `purchaseOrderLine.id` (SystemId, stabil). `line_no` =
   `sequence`. Inleveransrader återanvänder samma `sequence`. Det är verifierat: varje
   inleverans listar alla orderrader med samma sequence, även med antal 0.
3. **Container → rad.** Via `ContainerAcquisition`. Det finns i dag ingen BC-skrivare, så
   alla acquisitions är manuella och bara på icke-BC-order.
4. **Flera containrar per rad.** Ja (FK). En container kan bara ha en rad (OneToOne).
5. **Rad utan containrar.** Ja. Workspace visar `unbooked_qty` = beställt − bokat på
   leveranser.
6. **Delvis mottagna order.** I BC är detta vanligt: 32 av 42 öppna order har mottagen kvantitet, och
   flera är halvvägs (100022: 150 beställda / 10 mottagna). I Pegasus syns det **inte**,
   eftersom `received_qty` aldrig mappas. Serienumren på bokförda delinleveranser finns redan
   via receipt resolution (`order_no` på inleveransen).
7. **Stängda/borttagna order.** BC tar bort ordern när den är helt mottagen *och*
   fakturerad. Den inkrementella synken ser aldrig borttagningen, men
   `bc_reconcile_purchase_orders` soft-deletar (`source_active=False`). Rader och
   kopplingar ligger kvar. `status=closed` ≠ `source_active=False` ≠ logistiskt
   `completed`. Ingen rad raderas hårt.
8. **Risk för dubbla datamodeller.** Hög om man bygger en "BC-orderrad med serier" vid
   sidan av `PurchaseOrderLine`, eller en egen koppling vid sidan av
   `ContainerAcquisition`. Det ska inte göras. Det som *behövs* är en speglingstabell för BC:s
   spårningsrader med nullable container. Den är ingen dubblett: den bär BC-fakta som
   saknar container, precis som `BusinessCentralItemLedgerEntry`.
9. **Att återanvända.**

| Service | Används till |
|---|---|
| `lifecycle.run_business_central_sync` | Körning, lås, source binding och markörer |
| `resources.py` | Ny resurs-typ |
| `client._iter_pages` | Paging, nextLink-skydd och loggning |
| `procurement.services.upsert_purchase_orders` | Order och rader |
| `reconcile.py` | Borttagna order |
| `containers.intake.normalise_container_number` / `parse_and_validate_container_number` | ISO-kontroll |
| `item_rules.ItemRuleSet` | Klassificering, inkl. variant |
| `container_links.match_item_ledger_entries` | Mönstret för matchning till befintliga containrar |
| `container_creation.create_approved_containers` | Lägena `match_only` / `review` / `create_approved` |
| `receipt_resolution` / `receipt_receive` | Mottagning (oförändrat) |
| `procurement.workspace.get_purchase_order_line_summaries` | Visning per rad |

---

## C. BC Standard API (verifierat 2026-10-25)

### Purchase Orders — `api/v2.0/…/purchaseOrders`

| Fält | Status | Kommentar |
|---|---|---|
| `id` | verifierat | GUID, stabil |
| `number` | verifierat | `100000`–`100046`. PO 106254 (exemplet) **finns inte** i sandboxen |
| `vendorNumber` / `vendorName` | verifierat | |
| `status` | verifierat | `Draft` (= BC *Öppen*) / `Open` (= *Släppt*); mappern hanterar det |
| `orderDate` | verifierat | |
| `requestedReceiptDate` | verifierat | Headern saknar expected receipt date i v2.0 |
| `fullyReceived` | verifierat | 21 av 42 öppna order är `true` men finns kvar (ofakturerade). Härled status från raderna |
| `lastModifiedDateTime` | verifierat | Uppdateras av inleveransbokföring (100022: 2026-09-21 = posting date för IN28043/44) |
| `$expand=purchaseOrderLines` | verifierat | Hela öppna orderstocken i **ett** anrop (42 order, 60 rader) |
| `purchaseOrderArchives(Lines)` | 403 | Page 30218/30219 |

### Purchase Order Lines — `purchaseOrders({id})/purchaseOrderLines`

| Fält | Status | Kommentar |
|---|---|---|
| `id` | verifierat | Stabilt rad-ID (SystemId) |
| `sequence` | verifierat | = Line No. (10000, 20000 …) |
| `lineObjectNumber` | verifierat | Artikelnummer |
| `itemVariantId` | verifierat (GUID) | **50 av 60 rader har variant.** Koden kräver `$expand=itemVariant` → 403 (Page 30052) |
| Variantkod | 403 | Se ovan |
| `locationId` | verifierat (GUID) | Koden kräver `$expand=location` → 403 (Page 30076) |
| Lagerställeskod | 403 | Se ovan |
| `quantity` | verifierat | Beställt |
| `receivedQuantity` | verifierat | Mottaget. **Mappas inte av Pegasus i dag** |
| `invoicedQuantity` | verifierat | |
| `receiveQuantity` | verifierat | = *Qty. to Receive*, inte utestående (100036: 20/8/0) |
| Utestående antal | saknas i v2.0 | Härleds som `quantity − receivedQuantity`. Finns som `outstandingQuantity` i Analytics `purchaseLines` (403, Query 37073) och ODataV4 `purchaseDocumentLines` (403, Codeunit 346) |
| `lastModifiedDateTime` | **saknas** | `$filter` ger 400 "Could not find a property". Finns i ODataV4 `BadgerPurchaseOrderLine` (tredjepartstillägg, 403) |
| `@odata.etag` | verifierat | Radversion. Kan upptäcka radändringar vid full jämförelse |
| Top-level `purchaseOrderLines` | 400 | Måste läsas via ordern |

### Artikelspårning

| Resurs | Serie | Order / rad | Status |
|---|---|---|---|
| Standard v2.0 `itemLedgerEntries` | nej | nej | Dokumenterat. Saknar serie helt |
| Analytics `itemLedgerEntries` (Query 36968) | ja | dokumentnr, **ingen** rad-/orderref | **verifierat**, används redan. Endast bokfört |
| ODataV4 `Artikeltransaktioner_Excel` (Page 38) | ja | `Document_Line_No`, `Order_No`, `Order_Line_No` | 403 (Codeunit 5859). Endast bokfört |
| ODataV4 `ItemLedgerEntries` (Query 265) | nej (bara lot) | | 403 |
| Analytics `warehouseActivityLines` / `warehouseEntries` | ja | nej | 403. Lagerplock/-entries, inte orderspårning |
| ODataV4 `purchaseDocumentLines` | nej (`reservedQuantity`) | rad | 403 |
| **Reservation Entry (tabell 337)** — där spårning på öppna orderrader ligger | — | — | **Saknas** i alla publicerade API:er och webbtjänster |

**Slutsats:** Serienummer före bokförd inleverans går inte att läsa i dag, varken med
nuvarande eller med utökade behörigheter. Inleverans och serie efter bokföring är
verifierat och redan implementerat.

---

## D. Serienummer före inleverans

**Tekniskt i BC.** När man registrerar artikelspårning på en inköpsorderrad skapar BC
rader i *Reservation Entry* med Source Type 39 (Purchase Line), Source Subtype 1
(Order), Source ID = ordernr, Source Ref. No. = radnr, Serial No., Quantity (Base) = 1
per serie och Reservation Status *Surplus* (eller *Reservation* om enheten är
reserverad mot en försäljning). Vid bokförd inleverans tas raderna bort och ersätts av
artikeltransaktioner. Det är standardbeteende i BC och behöver bekräftas av NAB för MCR:s
uppsättning.

**Det här följer av det:**

- En read-only API-sida eller query över tabell 337 är allt som behövs. Ingen ändring i
  inköpsflödet krävs.
- Purchase Line ändras inte när spårningen ändras, eftersom spårningen ligger i en annan
  tabell. Radens Last Modified räcker därför sannolikt **inte** (fråga D). Det behöver
  bekräftas av NAB.
- Ändrade spårningsrader kan raderas och återskapas, så Entry No. är inte en stabil
  identitet. Pegasus ska nyckla på (företag, order, rad, serie).
- När en spårad enhet tas emot försvinner den från det öppna API:et. Det är **inte** en
  borttagning, och Pegasus ser mottagningen via artikeltransaktionerna.

**Verifiering med verkliga data har inte varit möjlig.** Sandboxen innehåller inte PO
106254, och spårning på öppna rader kan inte läsas med någon behörighet. För att
verifiera behöver NAB registrera serier på en sandbox-order. Det är en write som NAB gör,
inte Pegasus.

**Det som redan är möjligt utan NAB.** Mottagna serier per order är verifierade: 66
inleveranser med serier, 33 order. Serien kan knytas till en rad när inleveransen bara har
en rad med samma artikel (62 av 66). I 4 inleveranser har flera rader samma artikel. Ett
exempel är IN28167 på 100029, med 11 enheter på rad 20000 och 22 enheter på rad 30000. Där
kan serien inte knytas till en rad via API:et. `Order_Line_No` på Page 38, som kräver en
behörighet, eller Pegasus egen spårningsbild från före bokföringen löser det.

**Serier mellan order (verifierat).** 8 serier förekommer på inleveranser till två olika
order. Ett exempel är PSLU2912950 på 100023 (IN27769) och 100022 (IN28043), efter en
KORR-korrigering. Serier kan alltså flyttas eller korrigeras mellan order och rader.

---

## E. Identifierade gap

| # | Gap | Typ | Åtgärd |
|---|---|---|---|
| E1 | Serie på öppen orderrad | BC saknar API | NAB: `purchaseLineTrackings` (G) |
| E2 | `received_qty` skrivs inte från BC | Pegasus-bugg | Mappa `receivedQuantity` (ingen migration) |
| E3 | Variantkod (50 av 60 rader har variant) | 403 Page 30052 | Behörighet |
| E4 | Lagerställeskod på orderrad | 403 Page 30076 | Behörighet (behövs för `BusinessCentralLocationMapping` och destination) |
| E5 | Bokförd serie → orderrad | 403 Codeunit 5859 (Page 38) | Behörighet |
| E6 | Radens Last Modified | Saknas i v2.0 | Behövs inte med full avstämning. `@odata.etag` räcker för radändringar |
| E7 | Utestående antal | Saknas i v2.0 | Härleds `quantity − receivedQuantity` |
| E8 | Historik efter stängd order | Arkiv 403. Order raderas i BC | Pegasus behåller själv (soft delete och staging-rader) |
| E9 | `ContainerAcquisition` är OneToOne och har ingen BC-skrivare | Pegasus | BC-skrivare som *flyttar* ursprunget när BC flyttar serien (se I) |
| E10 | Containers som saknas i Pegasus kan inte ha en acquisition | Pegasus | Staging-rad med nullable container |

---

## F. Rekommenderad synkstrategi

Volymen för MCR (verifierad) är 42 öppna order, 60 rader, 51 containerrader, 1 125 beställda
och 306 mottagna enheter. Det ger **högst ~820 öppna spårningsrader**.

| | 1. Last Modified | 2. Full avstämning | 3. Hybrid |
|---|---|---|---|
| Nya serier | bara om spårningsraden har egen tidsstämpel | ja | ja |
| Ändrade serier | dito | ja (som borttagning + ny) | ja |
| **Borttagna serier** | **nej**, borttagna rader syns inte | ja (mängddifferens) | ja, vid full körning |
| Ändrat antal | radens etag eller header | ja | ja |
| Delinleveranser | via artikeltransaktioner (befintligt) | dito | dito |
| Avslutade order | reconcile (befintligt) | ja | ja |
| API-anrop/körning | 1 | 1–2 sidor spårning + 1 för order med `$expand` | 1 + periodiskt 2 |
| Tillförlitlighet | låg (fråga D är obekräftad) | hög | hög |

**Rekommendation för MCR: alternativ 2.** Hämta hela öppna spårningsmängden, inte per
order, var 15–30:e minut och jämför per (order, rad, serie).

- Det som tillkommit skapas.
- Det som försvunnit klassas som:
  - **mottagen**, om serien finns på en bokförd inleverans till samma order
    (artikeltransaktioner; synka ILE före spårningen eller kör om klassningen efter ILE),
  - **flyttad**, om samma serie dyker upp på en annan rad eller order,
  - annars **borttagen**.
- Ingenting raderas hårt.
- Orderhuvuden och rader hämtas också i sin helhet med `$expand=purchaseOrderLines`
  (ett anrop). Det täcker radändringar som inte rör headerns tidsstämpel (obekräftat i
  dag, se `business_central_integration.md`).

**För multi-tenant SaaS: alternativ 3.** Inkrementellt på spårningsradens
`systemModifiedAt` ofta, och full avstämning varje timme eller natt för borttagningar.
Kontraktet nedan stöder båda, så valet kan göras per integration.

**Risker:**

- Om full avstämning misslyckas mitt i får den inte tolkas som borttagning. Följ samma
  regel som reconcile: bara en komplett körning från samma källa får deaktivera.
- Ordningen mellan ILE-synk och spårningssynk påverkar klassningen mottagen / borttagen.
  Räkna om klassningen när ILE kommer ikapp.

---

## G. Minimal NAB API Specification

**En** ny read-only API-resurs. Ingen kopia av inköpsordern, eftersom ordern och raderna redan finns i
standard-API:et.

```
GET /api/<publisher>/<group>/v1.0/companies({companyId})/purchaseLineTrackings
```

- Källa: tabell 337 Reservation Entry, filtrerad på
  `Source Type = 39`, `Source Subtype = 1 (Order)`, `Positive = true` och `Serial No. <> ''`.
- `Editable = false`, `DataAccessIntent = ReadOnly`, och ingen insert, modify eller delete.

| Fält | Källa | Krav |
|---|---|---|
| `id` | SystemId | stabil per post |
| `entryNo` | Entry No. | info |
| `documentNo` | Source ID | **krav** |
| `lineNo` | Source Ref. No. | **krav** (= `purchaseOrderLine.sequence`) |
| `purchaseOrderId` / `purchaseOrderLineId` | Purchase Header / Line SystemId (FlowField eller lookup) | önskvärt, så att raden matchar `PurchaseOrderLine.external_id` direkt |
| `itemNo`, `variantCode`, `locationCode` | | **krav** |
| `serialNo` | | **krav** |
| `lotNo` | | valfritt |
| `quantityBase` | Quantity (Base) | önskvärt, för validering (förväntat 1) |
| `reservationStatus` | Surplus / Reservation / Tracking | önskvärt |
| `expectedReceiptDate` | | valfritt |
| `systemCreatedAt`, `systemModifiedAt` | | **krav**, för det inkrementella läget |

**Beteenden:**

- Bara **öppna** spårningsrader. Efter bokförd inleverans gäller artikeltransaktionerna, som
  Pegasus redan läser.
- Ingen tombstone behövs, eftersom Pegasus upptäcker borttagningar med full avstämning.
- `$filter` på `systemModifiedAt`, `documentNo` och `itemNo`, plus `$orderby`, `$count`
  och server-side paging, krävs.
- Inga dubbletter av (documentNo, lineNo, serialNo) med samma status. Om BC delar en post
  ska summan av `quantityBase` per serie vara 1.

**Svar till Elin:**

- **A. Livscykel.** Bara medan raden är öppen. Historiken finns redan i
  artikeltransaktionerna, och Pegasus sparar själv det som har setts.
- **B. Antal.** Inget antal behövs, eftersom en serie alltid är 1. `quantityBase` är
  önskvärt som kontroll. Samma orderrad förekommer en gång per serie, vilket är rätt.
- **C. Ej tilldelade.** Ska **inte** finnas i API:et. Pegasus räknar
  `quantity − receivedQuantity − antal öppna serier` från standard-API:et.
- **D. Ändringstid.** Radens Last Modified räcker sannolikt inte, eftersom spårningen ligger
  i en annan tabell. Det behövs `systemModifiedAt` *på spårningsposten*, och borttagningar
  hanteras med full avstämning.

**Behörigheter (ingen utveckling)** till permission set `CONTAINERTRACKING`:

| Objekt | Ger | Prioritet |
|---|---|---|
| Page 30052 *APIV2 - Item Variants* | Variantkod | hög |
| Page 30076 *APIV2 - Locations* | Lagerställeskod | hög |
| Codeunit 5859 *Invt. Ledger Purchase Source* | Page 38 med `Order_No` och `Order_Line_No` för bokförda serier | medel |
| Page 30218/30219 *Purch. Ord. Archives* | Arkiv | låg |
| Query 37073 *Purchase Lines* (Power BI) | `outstandingQuantity` | låg |

---

## H. Frågor till Elin

**Blockerande:**

1. Registrerar MCR serienummer på inköpsorderrader **före** inleverans i praktiken? Hur
   ofta och hur långt i förväg? Om det nästan alltid sker vid inleverans ger etapp 3
   begränsat värde utöver det som redan finns.
2. Kan NAB bygga `purchaseLineTrackings` enligt G, som en read-only API-sida eller query
   över Reservation Entry? Vilken uppskattad insats? Alternativt: går standardsidan
   *Reservation Entries* (Page 497) att publicera som webbtjänst med `Source ID`,
   `Source Ref. No.` och `Serial No.`?
3. Bekräfta att Purchase Line inte ändras (SystemModifiedAt eller etag) när enbart
   artikelspårningen ändras.
4. Raderas och återskapas Reservation Entries vid ändring, så att Entry No. och SystemId
   inte är stabila per serie?
5. Kan NAB lägga upp en testorder i sandboxen (t.ex. 106254, CON22G1 × 5) med 2 serier,
   och senare ändra eller ta bort en, så att Pegasus kan verifiera hela livscykeln?

**Kan hanteras senare:**

6. Behörigheterna i G (variant, location och Codeunit 5859 är högst prioriterade).
7. Vad betyder varianterna på containerartiklarna (grad eller färg)? Påverkar de
   equipment type?
8. Används lagerinleveranser (Warehouse Receipt) på L005? Registreras serien då där?
9. Reserveras inköpta containrar mot försäljningsorder (status *Reservation*)?
10. Är arkivering av inköpsorder aktiverad?

---

## I. Föreslagen implementation av etapp 3

Principer: BC är master för ordern, Pegasus är master för containerhändelser, och identiteten
är enbart ISO 6346-serien. **Inga fiktiva containrar** skapas, så utestående kvantitet utan
serie är bara ett tal på raden.

| Steg | Innehåll | Beroende | Migration |
|---|---|---|---|
| **3b** | Mappa `receivedQuantity` → `received_qty` och visa utestående. Hämta rader med `$expand`. Visa **mottagna** serier per rad i workspace (från `BusinessCentralReceiptContainer` via `receipt.order_no` + `sequence` när entydigt). Begär behörigheter | inget | nej |
| **3c** | Staging-modell `BusinessCentralPurchaseLineTracking`: integration, company, `purchase_order_line` FK (nullable tills ordern synkats), order/rad-nr, artikel, variant, location, rå och normaliserad serie, ISO-status, `container` FK (nullable), `link_status`, `state` (open / received / moved / removed), `first_seen_at`, `last_seen_at`, `removed_at`, `raw_payload`, `sync_hash`. Unik på (integration, company, order, rad, serie). Ny resurs `purchase_line_trackings` i `resources.py` via `run_business_central_sync`, med full avstämning | NAB-API | ja |
| **3d** | Matchning: återanvänd `ItemRuleSet` och ISO-hjälparna (som `match_item_ledger_entries`). En BC-ägd skrivare för `ContainerAcquisition` (egen ingång, inte den manuella) som skapar kopplingen för öppna och mottagna serier och flyttar den när BC flyttar serien. Kopplingen tas bort vid `removed`, men staging-raden behåller historiken | 3c | nej |
| **3e** | Workspace per rad, t.ex. **"5 beställda – 2 identifierade – 3 utan serienummer"**, med identifierade = öppna och mottagna serier, utan serie = `max(beställt − mottaget − öppna serier, 0)`. Byggs i `get_purchase_order_line_summaries` | 3c | nej |
| **3f** | Kontrollerad containerskapning: samma lägen och regler som `container_creation` (giltig ISO, containerartikel med exakt en equipment type, admin-godkännande i `create_approved`). Aldrig från en kvantitet och aldrig automatiskt vid sync | 3d | nej |
| **3g** (senare) | Arrival och tracking: identifierad container → planned-container discovery (befintlig), `SupplierDelivery`/expected arrivals från radens `expected_receipt_date`, och destination via `BusinessCentralLocationMapping` för radens lagerställe (kräver E4). Mottagning sker fortsatt via befintlig receipt receive | 3d, E4 | — |

**Policy för containerskapning.** En container får skapas bara när:

- serien är giltig ISO 6346,
- artikeln och varianten är klassad som container med exakt en equipment type,
- serien är aktiv (`open`/`received`) på en aktiv BC-rad,
- ingen befintlig container har serien,
- och lägets krav är uppfyllt (admin-godkännande).

Containern får inte skapas med carrier eller location. Spårning startar inte förrän
transportinformation finns.

---

## J. Risker och beroenden

| Risk | Effekt | Hantering |
|---|---|---|
| MCR registrerar serier först vid inleverans | Etapp 3c–f ger lite. 3b räcker | Fråga H1 före beställning hos NAB |
| NAB-API försenas | Inga serier före inleverans | 3b och behörigheter är oberoende och kan göras nu |
| Last Modified på spårning är otillförlitlig | Missade ändringar | Full avstämning är grundläget |
| Mottagen tolkas som borttagen (ILE ligger efter) | Felaktig borttagning | Klassning via inleveranser och omräkning efter ILE-synk |
| Serie flyttas mellan order (8 verifierade fall) | Konflikt med OneToOne i `ContainerAcquisition` | BC-skrivaren flyttar ursprunget. Staging-raden visar historiken |
| Variantens betydelse okänd (50/60 rader) | Fel equipment type | Fråga H7. `BusinessCentralItemRule` stöder redan variant |
| Inga verkliga testdata för öppen spårning | Overifierad design | Fråga H5. Fixtures i dummy-klienten |
| Samma sandbox-GUID:er som produktion | Datablandning | Befintlig source binding och ett team per miljö gäller även den nya resursen |

**Beslut före implementation:**

1. Svar på H1–H4.
2. Beställ `purchaseLineTrackings` och behörigheterna.
3. Godkänn 3b som första, fristående steg.
4. Godkänn staging-modellen (3c), som är etappens enda migration.
