# ETAPP 3a.1 — BC API-diagnostik, behörigheter och artikelspårning

Datum: 2026-10-25 · Fortsättning på
[`business_central_purchase_order_tracking_study.md`](business_central_purchase_order_tracking_study.md)

**Avgränsning:**

- Endast HTTP GET mot `Sandbox_eligra_261002`, integration 5, företag **MCR**
  (`782d7133-5033-4c4b-9a29-83f7f44f062d`).
- Anropen gjordes med `requests.get` och token från befintlig `BusinessCentralAuth`.
  Credentials lästes via `get_integration_credentials`, som bara läser.
- Ingen `IntegrationRequestLog` och inga andra databasskrivningar.
- Inga ändringar i BC eller Pegasus, inga tokens eller secrets loggade.

> **Slutläge (3a.4, 2026-10-25).** Rapporten är skriven i etapper. Avsnitt A–H beskriver läget i
> 3a.1, och de senare avsnitten beskriver förändringarna. Det som gäller nu:
>
> | Område | Status |
> |---|---|
> | Variantkod (Page 30052 + Table 5401 Execute) | **Fungerar** — 100021/10000 `RAL5013` |
> | Lagerställe (Page 30076 + Table 14 Execute) | **Fungerar** — 100021/10000 `L005` |
> | Page 38 (bokförda serier per dokumentrad) | 403 Codeunit 5860. Inte blockerande |
> | Page 497 `ContainertrackingReservationEntries` | **Underkänd.** Den kräver behörighet över hela reservationsmotorn (senast Codeunit 925) |
> | Serienummer på öppen order | **NAB:s `purchaseLineTrackings`** (read-only API över tabell 337, utan beräknade fält). Specifikationen finns i 3a.4 |
> | Bokförd serie → orderrad | ILE `documentNo` → `purchaseReceipts.number` → `orderNumber`, och rad via Page 38 `Document_Line_No` = `purchaseReceiptLines.sequence` = `purchaseOrderLines.sequence`. **Inte** via ILE `Order_No`/`Order_Line_No` |

---

## A. Sammanfattning

- **Alla 403 har samma orsak.** Integrationsappen saknar **Execute** på namngivna
  BC-objekt (API-sidor, codeunits och queries). Sökväg, company, environment,
  ODataV4-publicering och token är verifierat korrekta (se D). Det går att rätta med
  behörigheter, utan utveckling.
- **Med enbart behörigheter kan vi få:**
  - variantkod och lagerställeskod på orderraderna,
  - utestående antal,
  - bokförda serier med inleverans och dokumentrad (Page 38), som via inleveransen ger orderrad,
  - artikelregistret.
- **Serienummer på öppen order är verifierat oläsbart i dag.** `BBCU3273070` är
  registrerat på 100021 i BC:s UI men finns inte i någon läsbar resurs. Det finns inte i
  artikeltransaktionerna, varken i Analytics eller i Page 38. Tabell 337 *Reservation Entry*
  är inte publicerad.
- **Nästa steg kan göras av oss själva:** publicera standardsidan **Page 497 Reservation
  Entries** som webbtjänst och ge appen läsrätt. Det är en konfigurationsändring i BC och
  ingen utveckling. Om sidan saknar källfälten (Source ID / Source Ref. No.) eller inte går att
  läsa stabilt behövs NAB (alternativ C).

---

## B. Inköpsorder 100021 (verifierat, standard API v2.0)

| Uppgift | Värde |
|---|---|
| Purchase Order GUID | `acb56aaf-a799-f111-8072-6045bdaa5148` |
| Ordernummer | `100021` |
| Leverantör | `L00040` — Bullbox / CYNSA S.A BKBKESMM |
| Status | `Draft` (= BC *Öppen*), `fullyReceived=false` |
| Orderdatum / senaste bokföring | 2026-08-16 / 2026-09-30 |
| `lastModifiedDateTime` | 2026-10-02T11:39:07.397Z |
| Antal orderrader | 1 |
| **Purchase Order Line GUID** | **`19b968df-a799-f111-8072-6045bdaa5148`** |
| **Line No. (`sequence`)** | **10000** |
| Artikel | `CON22G1` (`itemId` `69587f85-4784-f111-8070-70a8a5520caf`), `description2` "Colboltblå" |
| Variant | `itemVariantId` `44a9efd3-4784-f111-8070-70a8a5520caf`. **Koden (RAL5013) är inte läsbar** (403, Page 30052) |
| Lagerställe | `locationId` `b7033553-40e0-ee11-904c-6045bde0976d`. Koden är 403 (Page 30076). Troligen L005: radens två inleveranser bokfördes på L005 (härlett, inte verifierat) |
| Beställt / mottaget / att ta emot | 49 / 3 / 46 (`quantity` / `receivedQuantity` / `receiveQuantity`) |
| Utestående | 46 (härlett, `quantity − receivedQuantity`) |
| Förväntad inleverans | 2026-12-14 |
| Inleveranser | IN28149 (2 st) och IN28166 (1 st), 2026-09-30 |

Om UI-uppgifterna stämmer är fördelningen 3 mottagna + 26 spårade men inte mottagna + 20 utan
serie = 49. Ett fungerande spårnings-API ska alltså ge **26 poster** för
(100021, 10000), däribland `BBCU3273070`. Det är acceptanskriteriet för lösningen.

PO 100021 är inte synkad till Pegasus. Sandbox-teamet 4 har ingen PO-synk körd.

---

## C. API-resultat (2026-10-25)

`root` = `https://api.businesscentral.dynamics.com/v2.0/{tenant}/Sandbox_eligra_261002`

- STD = `/api/v2.0/companies({MCR})`
- ANA = `/api/microsoft/analytics/v1.0/companies({MCR})`
- OD4 = `/ODataV4/Company('MCR')`

Samtliga 403 har felkoden `Internal_ServerError` och meddelandet *"Sorry, the current
permissions prevented the action. (<objekt> Execute: <app>)"*.

| API | Endpoint | HTTP | BC-objekt (från felet) | Tabell | Alternativ |
|---|---|---|---|---|---|
| STD | `purchaseOrders?$filter=number eq '100021'&$expand=purchaseOrderLines` | **200** | — | 38/39 | — |
| STD | `purchaseOrders({id})/purchaseOrderLines({lineId})` (en rad via nyckel) | 403 | Codeunit 5528 *Graph Mgt - Purch. Inv. Lines* | 39 | Samlingen via ordern (200) |
| STD | `…/purchaseOrderLines?$expand=itemVariant`, `itemVariants`, `itemVariants({id})` | 403 | Page 30052 *APIV2 - Item Variants* | 5401 | OD4 `purchaseDocumentLines.variantCode` |
| STD | `…/purchaseOrderLines?$expand=location`, `locations`, `locations({id})` | 403 | Page 30076 *APIV2 - Locations* | 14 | OD4 `purchaseDocumentLines.locationCode` |
| STD | `…/purchaseOrderLines?$expand=item`, `items({id})`, `items({id})/itemVariants` | 403 | Page 30008 *APIV2 - Items* | 27 | — |
| STD | `purchaseOrderArchives` | 403 | Page 30218 *APIV2 - Purch. Ord. Archives* (rader: 30219) | 5109/5110 | — |
| STD | `itemLedgerEntries` | 403 | Page 30069 *APIV2 - Item Ledger Entries* | 32 | ANA `itemLedgerEntries` (200) |
| STD | `vendors` | 403 | Page 30010 *APIV2 - Vendors* | 23 | Namn finns på ordern |
| STD | `purchaseReceipts?$filter=orderNumber eq '100021'&$expand=purchaseReceiptLines` | **200** | — | 120/121 | — |
| ANA | `itemLedgerEntries?$filter=serialNo eq 'BBCU3273070'` | **200, 0 poster** | Query 36968 | 32 | — |
| ANA | `itemLedgerEntries?$filter=startswith(serialNo,'BBCU')` | **200, 4 poster** | Query 36968 | 32 | — |
| ANA | `items` | 403 | Query 36953 *Items - PBI API* | 27 | — |
| ANA | `locations` | 403 | Page 36957 *Locations* | 14 | — |
| ANA | `purchaseLines` | 403 | Query 37073 *Purchase Lines - PBI API* | 39 | OD4 `purchaseDocumentLines` |
| ANA | `outstandingPurchaseLines` / `itemOutstandingPurchaseLines` | 403 | Query 36973 / 36998 | 39 | dito |
| ANA | `warehouseActivityLines` / `warehouseEntries` / `transferLines` | 403 | Query 36978 / 36979 / 36977 | 5767 / 7312 / 5741 | — |
| OD4 | `Artikeltransaktioner_Excel?$filter=Serial_No eq 'BBCU3273070'` | **200, 0 poster** | Page 38 | 32 | — |
| OD4 | `Artikeltransaktioner_Excel?$top=1` | 403 | Codeunit 5859 *Invt. Ledger Purchase Source* | 32 | — |
| OD4 | `ItemLedgerEntries` | 403 | Query 265 | 32 | ANA |
| OD4 | `BadgerPurchaseOrderLine`, `purchaseDocumentLines`, `workflowPurchaseDocumentLines` | 403 | Codeunit 346 *Purch. Line CaptionClass Mgmt* | 39 | — |
| OD4 | `Artikelkort` / `workflowItems` / `BadgerItem` | 403 | Page 30 / Page 6409 / Page 12077500 (Badger) | 27 | — |
| OD4 | `Power_BI_Item_Purchase_List` | 403 | Query 52 | 39 | — |
| AUT | `/api/microsoft/automation/v2.0/companies({MCR})/users`, `permissionSets` | 403 | Page 30004 / 30005 | — | BC-UI (G) |

Appen i felmeddelandena: `_Exclude_APIV2_` är Microsofts API v2.0-app. *Power BI Report
embeddings…* är Analytics-appen. *Base Application* respektive *Badger* (tredjepart) står på
övriga.

**Sökning efter artikelspårning** i `$metadata` (standard: 150 entitetstyper, Analytics: 108,
ODataV4: 219 entitetsmängder, sökt även på svenska namn):

- Ingen resurs bygger på *Reservation Entry* (337) eller *Tracking Specification* (336).
- Inga navigation properties från `purchaseOrderLine` till spårning.
- Fältet Serial No finns bara på artikeltransaktioner (Analytics ILE, Page 38), på
  artikeljournaler (oftast tomma) och på lager- och projektrader.

---

## D. Behörighetsdiagnos

### Autentisering (verifierad)

- OAuth 2.0 client credentials (`BusinessCentralAuth`) mot Entra ID, med scope
  `https://api.businesscentral.dynamics.com/.default`.
- Token-claims: `idtyp=app`, `roles=["API.ReadWrite.All"]` och
  `aud=https://api.businesscentral.dynamics.com`. `tid` stämmer med konfigurationen.
- Client ID: **`b05e2f47…7755`** (maskerat, fullständigt värde i Entra / BC-UI).
- `tenant_id`, `environment` och `company_id` ligger i `Integration.config`. `client_id` och
  `client_secret` ligger krypterade i `IntegrationCredential` och visas aldrig.

### Vad som är verifierat

| Hypotes | Resultat |
|---|---|
| Fel company-ID | **Nej.** `companies` visar 7 bolag, och MCR har konfigurerat ID |
| Fel sökväg eller endpoint | **Nej.** Samma rot ger 200 för purchaseOrders, purchaseReceipts och Analytics ILE. Felen namnger ett objekt, vilket en felaktig sökväg inte gör (den ger 400/404) |
| Felaktig ODataV4-publicering | **Nej.** `Artikeltransaktioner_Excel` med ett filter som ger 0 rader svarar 200. Tjänsten är publicerad och sidan körs, och 403 uppstår först när en rad hämtas och sidan anropar Codeunit 5859 |
| Entra-appen saknar åtkomst eller är inaktiv | **Nej.** Token accepteras och flera resurser returnerar data |
| API-behörighet i Entra (`API.ReadWrite.All`) | Räcker. Begränsningen ligger i BC |
| **Saknad objektbehörighet (Execute)** | **Verifierat** som felets typ. Varje fel säger *"<objekt> Execute"* |
| Saknad tabellbehörighet (Read) | **Hypotes.** BC rapporterar första saknade behörighet. Efter Execute kan *TableData … Read* visa sig (t.ex. 5401, 14, 337). Ett tabellfel har en annan formulering ("You do not have the following permissions on TableData …") |

### Minsta behörighet

Lägg till följande i appens egna permission set (troligen `CONTAINERTRACKING`). Alla rader är
Execute på objektet plus Read på tabellen och inget annat, så Insert, Modify och Delete på
tabellerna behövs inte.

| Prio | Objekt | Tabell (Read) | Ger |
|---|---|---|---|
| 1 | Page 30052 *APIV2 - Item Variants* | 5401 Item Variant | Variantkod (RAL5013) |
| 1 | Page 30076 *APIV2 - Locations* | 14 Location | Lagerställeskod |
| 1 | Codeunit 5859 *Invt. Ledger Purchase Source* | (32, redan läsbar) | Page 38: bokförda serier med `Document_No` + `Document_Line_No` (`Order_No` är tom för inköp, se 3a.2) |
| 2 | Codeunit 346 *Purch. Line CaptionClass Mgmt* | (39, redan läsbar) | OD4 `purchaseDocumentLines` med `variantCode`, `locationCode` och `outstandingQuantity` i **ett** anrop, som alternativ till prio 1 rad 1–2 |
| 2 | Page 30008 *APIV2 - Items* | 27 Item | Artikelregister och beskrivningar |
| 3 | Codeunit 5528 *Graph Mgt - Purch. Inv. Lines* | — | GET av enskild rad via nyckel (behövs inte, samlingen fungerar) |
| 3 | Page 30218/30219 *Purch. Ord. Archives* | 5109/5110 | Arkiv (om arkivering används) |
| — | Page 497 *Reservation Entries* (efter publicering, se F) | **337 Reservation Entry** | Öppen artikelspårning |

**Separat beslut som kräver ditt godkännande:** ett bredare standard-set (t.ex.
*D365 READ*) tillfälligt i sandboxen för diagnostik. Det ger appen läsrätt på i princip all
affärsdata i bolaget. Det rekommenderas inte och behövs inte, eftersom felen redan namnger
exakta objekt. `SUPER` ska inte användas.

---

## E. Serienummer BBCU3273070

ISO 6346: giltigt (BBC · U · 327307 · 0).

| Källa | Finns? | Koppling | Bokförd? | Orderrad? | Räcker före ankomst? |
|---|---|---|---|---|---|
| 1. Öppen PO 100021 (BC-UI) | Ja, enligt UI och skärmbild | 100021 / 10000 | Obokförd | Ja (i UI) | Ja, om det kan läsas |
| 1. Öppen PO 100021 (API) | **Nej**, ingen resurs exponerar det | — | — | — | — |
| 2. Spårningsresurser (337/336) | Inte publicerade | — | — | — | — |
| 3. Item Ledger Entries (ANA, live) | **Nej** (0 poster) | — | — | — | — |
| 3. Item Ledger Entries (Pegasus-kopia, cursor 95108 = live max 95110) | Nej | — | — | — | — |
| 3. Page 38 `Artikeltransaktioner_Excel` | Nej (200, 0 poster) | — | — | — | — |
| 4. Posted Purchase Receipts (IN28149, IN28166) och `BusinessCentralReceiptContainer` | **Nej** | — | — | — | — |
| 5. Pegasus containerregister | **Ja**, men i team `decidia` (id 86, AVAILABLE, 22 tracking-events, 1 subscription, manuellt skapad 2026-08-30) och `decidia-2` (id 78). **Inte** i BC-sandboxens team 4 | Ingen BC-koppling | — | — | Containern spåras redan, men i ett annat team |
| 6. BC-containerkopplingar (`ContainerAcquisition`, ILE `container`, receipt containers) | Nej | — | — | — | — |

**Kontroll av distinktionen.** Av de nio exempelserierna är tre **bokförda** och syns i
Analytics ILE och i Pegasus receipt resolution (resolved, `container_not_found`, ej mottagna
eftersom auto receive är av):

- BBCU3272941 och BBCU3273059 på IN28149,
- BBCU3273090 på IN28166.

De sex övriga, däribland `BBCU3273070`, finns bara som obokförd spårning. Det bekräftar
att serier i ILE inte bevisar läsbarhet före inleverans, och att det är de obokförda vi
saknar.

---

## F. Möjliga lösningar

### A — Befintligt API fungerar: **nej (verifierat)**

Ingen publicerad resurs innehåller obokförd spårning, och inga behörigheter ändrar det.

### B — Publicera Page 497 *Reservation Entries*: **realistiskt nästa steg, kräver verifiering**

| Fråga | Status |
|---|---|
| Finns sidan i applikationen? | Ja, standard Base Application (list page över tabell 337) |
| Publicerad som webbtjänst? | **Nej** (inte bland de 219 ODataV4-mängderna, verifierat) |
| Kan den publiceras? | Tekniskt ja, via *Web Services* → Ny → Page 497. Det är en konfigurationsändring i BC som du gör, inte Pegasus |
| Exponerar den rätt fält? | **Hypotes.** Enligt standardkoden innehåller sidan Entry No., Reservation Status, Item No., Variant Code, Location Code, Serial No., Lot No., Quantity (Base), Expected Receipt Date och Source Type / Subtype / ID / Ref. No. Flera av dem är dolda (`Visible=false`), men dolda fält exponeras ändå i OData. Kontrollera med *Page Inspection* (Ctrl+Alt+F1) innan publicering och i `$metadata` efter |
| Filter per order? | Om `Source_ID` och `Source_Ref_No` finns som fält: `$filter=Source_Type eq 39 and Source_ID eq '100021'`. Beräknade textfält (Reserved From/For) kan inte filtreras |
| Säkerhet | Ge **endast Read** på TableData 337 och Execute på Page 497. Då kan ingen skrivning ske trots att Entra-rollen heter `API.ReadWrite.All`. Publiceringen gäller tjänsten men inte data, eftersom behörigheten styr |
| Begränsningar | Ingen `SystemModifiedAt` och inget rad-GUID på sidan (troligt). Sidans triggers kan kräva fler Execute-behörigheter (iterera). Det är en UI-sida, inte ett API-kontrakt, så uppgraderingar kan ändra fältnamn. Spårning som redan hanterats på öppen rad ligger i tabell 336 och syns inte, men täcks av ILE |

**Acceptanstest:** 26 poster för `Source_ID='100021'`, `Source_Ref_No=10000`, med
`BBCU3273070` bland dem, ingen av de tre mottagna och `Quantity_Base = 1` per post.

### C — Anpassat API från NAB (om B inte räcker eller ska bli permanent)

Det är en read-only API-sida över tabell 337. Kontraktet i studien gäller (avsnitt G), nu med verifierade
identiteter:

| Fält | Exempel (100021) |
|---|---|
| `documentNo` | `100021` |
| `lineNo` | `10000` |
| `purchaseOrderLineId` (önskvärt) | `19b968df-a799-f111-8072-6045bdaa5148` |
| `itemNo` / `variantCode` / `locationCode` | `CON22G1` / `RAL5013` / (L005?) |
| `serialNo` | `BBCU3273070` |
| `quantityBase`, `reservationStatus` | 1 / Surplus |
| `systemModifiedAt` | — |

En post per serie, så att samma orderrad förekommer 26 gånger. Ingen logik för receive, tracking eller
container ska finnas i BC.

**Rekommendation:**

1. Behörigheterna prio 1 (snabb vinst, ingen utveckling).
2. Prova B i sandboxen mot acceptanstestet.
3. Beställ C först om B fallerar eller om man vill ha ett stabilt kontrakt med
   `systemModifiedAt` för produktion. B kan räcka för MCR. C är bättre för SaaS.

---

## G. Åtgärder i BC Sandbox (för dig)

Svenska menynamn kan skilja sig något. Använd sökfunktionen (Alt+Q).

### Kan göras direkt (endast kontroll, inga ändringar)

1. **Hitta appen.** Sök *Microsoft Entra Applications* (sv. *Microsoft Entra-program*).
   Öppna raden vars **Client ID** börjar på `b05e2f47` och slutar på `7755`. Kontrollera att
   **State = Enabled**.
2. **Se dess permission sets.** I kortets del *User Permission Sets*, notera varje set och
   dess **Company**. Tomt betyder alla bolag, annars ska det vara *MCR*.
3. **Effektiva behörigheter.** Sök *Users*, välj användaren med appens namn och öppna
   *Effective Permissions*. Filtrera på Object Type/ID 30052, 30076, 5859, 346 och 337.
   Execute eller Read ska saknas, vilket bekräftar diagnosen utan att något ändras.
4. **Granska setet.** Sök *Permission Sets*, öppna `CONTAINERTRACKING` (eller det set du
   hittade) och sedan *Permissions*. Notera vilka objekt som finns.

### Kräver ditt beslut och en begränsad ändring (sandbox)

5. I `CONTAINERTRACKING` → *Permissions*, lägg till prio 1-raderna i D:
   - Page 30052: Execute; TableData 5401: Read.
   - Page 30076: Execute; TableData 14: Read.
   - Codeunit 5859: Execute.

   Ange inga Insert, Modify eller Delete.
6. **Verifiera.** Be mig köra om följande GET-anrop. Ett nytt 403 som namnger *ett annat*
   objekt betyder att nästa behörighet saknas, och då itererar vi en i taget.
   - `purchaseOrders(acb56aaf-…)/purchaseOrderLines?$expand=itemVariant,location`.
     Förväntat: variant `RAL5013` och en lagerställeskod.
   - `ODataV4/Company('MCR')/Artikeltransaktioner_Excel?$filter=Document_No eq 'IN28149'`.
     Förväntat: 2 poster med `Document_Line_No=10000` (rättat i 3a.2: `Order_No` är tom för inköp).

### Kräver ytterligare verifiering

7. **Page 497.** Öppna *Reservation Entries* från spårningen på 100021 (eller sök sidan),
   kör *Page Inspection* (Ctrl+Alt+F1) och kontrollera att fälten Source ID, Source Ref. No.,
   Serial No., Variant Code och Location Code finns.
8. **Om de finns:** sök *Web Services* och lägg till Object Type *Page*, Object ID *497*, ett
   Service Name (t.ex. `PegasusReservationEntries`) och kryssa *Published*. Lägg sedan
   Page 497: Execute och TableData 337: **Read** i setet. Be mig köra
   `ODataV4/Company('MCR')/PegasusReservationEntries?$filter=Source_ID eq '100021'` mot
   acceptanstestet i F.
9. **Fråga om Analytics-köerna** (prio 3, behövs inte nu). De kräver Execute på objekt i
   Power BI-appen.

### Kräver sannolikt NAB

10. Om steg 7–8 visar att fälten saknas, att sidan inte går att läsa via OData eller att den
    behöver fler behörigheter än rimligt: beställ API-sidan i H.

---

## H. Uppdaterat underlag till NAB

Endast det här kräver NAB, och bara om alternativ B inte håller:

> En read-only API-sida (`DataAccessIntent = ReadOnly`, `Editable = false`) över tabell
> 337 *Reservation Entry*, filtrerad på Source Type 39 (Purchase Line), Source Subtype 1
> (Order), Positive = true, Serial No. ≠ ''. Fält: `id` (SystemId), `documentNo`
> (Source ID), `lineNo` (Source Ref. No.), `purchaseOrderLineId` (Purchase Line SystemId),
> `itemNo`, `variantCode`, `locationCode`, `serialNo`, `quantityBase`,
> `reservationStatus`, `systemModifiedAt`. En post per serie. Filter på `documentNo` och
> `systemModifiedAt`, med server-side paging.
>
> Testfall: PO 100021 rad 10000 (`19b968df-a799-f111-8072-6045bdaa5148`) ska ge 26
> poster, inklusive BBCU3273070, och inga av de redan mottagna BBCU3272941, BBCU3273059 och
> BBCU3273090.

Behörigheterna i D och publiceringen av Page 497 kan vi göra själva.

---

# ETAPP 3a.2 — Verifiering efter behörighetsändring (2026-10-25)

Samma metod som ovan: GET med `requests.get`, inga databasskrivningar och nytt token.

**Behörigheterna gäller för Pegasus app.** Varje tidigare fel har flyttats ett steg i
BC:s behörighetskedja och namnger nu ett *annat* objekt. Det visar att ändringarna i
`CONTAINERTRACKING` slagit igenom för appen `b05e2f47…7755`.

## Resultat

| Resurs | Tidigare | Nu | Exakt BC-fel nu | Slutsats |
|---|---|---|---|---|
| STD `…/purchaseOrderLines?$expand=itemVariant`, `itemVariants`, `itemVariants({id})` | 403 Page 30052 Execute | 403 | `(Table 5401 Item Variant Execute: _Exclude_APIV2_)` | Page OK. Saknar **Execute på objekttypen Table 5401**, vilket är något annat än *Table Data* Read |
| STD `…/purchaseOrderLines?$expand=location`, `locations`, `locations({id})` | 403 Page 30076 Execute | 403 | `(Table 14 Location Execute: _Exclude_APIV2_)` | Saknar **Execute på Table 14** |
| OD4 `Artikeltransaktioner_Excel` (Page 38), rader som finns | 403 Codeunit 5859 Execute | 403 | `(TableData 23 Vendor Read: Base Application)` | Codeunit OK. Saknar **Read på TableData 23 Vendor** (sidan visar källans namn) |
| OD4 Page 38, filter utan träff | 200 | 200 | — | Oförändrat |
| OD4 `purchaseDocumentLines` | 403 Codeunit 346 | 403 | `(Page 6405 Purchase Document Line Entity Execute: Base Application)` | Ej begärt. Behövs inte (A4) |
| OD4 `BadgerPurchaseOrderLine` | 403 Codeunit 346 | 403 | `(Page 12077539 QWEBI Purchase Order Line … Execute: Badger)` | Behövs inte |
| STD `items({id})`, rad via nyckel, `purchaseOrderArchives` | 403 | 403 | Page 30008 / Codeunit 5528 / Page 30218 (oförändrat) | Ej begärt, behövs inte nu |
| **ODataV4 Page 497** | ej publicerad | **ej publicerad** | — | 219 tjänster, ingen för Reservation Entries. Se B1 |

## A3 — Bokförda serier (verifierat indirekt)

Page 38 returnerar 403 när minst en rad matchar filtret, eftersom raden måste läsas och
då behövs Vendor, och 200 med tom lista när ingen rad matchar. Det ger säkra ja/nej-svar
utan att data läses.

| Filter (`Artikeltransaktioner_Excel`) | Svar | Betydelse |
|---|---|---|
| `Document_No eq 'IN28149' and Serial_No eq 'BBCU3272941'` | 403 | finns |
| `Document_No eq 'IN28149' and Serial_No eq 'BBCU3273059'` | 403 | finns |
| `Document_No eq 'IN28166' and Serial_No eq 'BBCU3273090'` | 403 | finns |
| `Serial_No eq 'BBCU3273070'` | 200 tom | **inte bokförd** |
| `Document_No eq 'IN28149' and Document_Line_No eq 10000` / `… 20000` | 403 / 200 tom | rad 10000 |
| `Document_No eq 'IN28149' and Item_No eq 'CON22G1'` | 403 | CON22G1 |
| `Document_No eq 'IN28149' and Variant_Code eq 'RAL5013'` / `… eq ''` | 403 / 200 tom | variant RAL5013 |
| `Document_No eq 'IN28149' and Location_Code eq 'L005'` | 403 | L005 |
| `Document_No eq 'IN28149' and Order_No ne ''` / `Order_Line_No ne 0` | 200 tom | **`Order_No` och `Order_Line_No` är tomma** |

**Rättelse av 3a och 3a.1.** `Order_No` och `Order_Line_No` på artikeltransaktionen gäller
produktions-, överförings- och monteringsorder, inte inköp. Kopplingen bokförd serie →
inköpsorderrad går i stället så här:

- `Document_No` (inleveransnr) → `purchaseReceipts.orderNumber` (= 100021),
- `Document_Line_No` (10000) = inleveransradens `sequence` = orderradens `sequence`
  (verifierat i 3a).

Page 38 löser därmed också de 4 inleveranser där flera rader har samma artikel. Avsnitt A, C, D,
E5 och G i `business_central_purchase_order_tracking_study.md` är rättade i 3a.3 (ej
committat).

## A4 — Codeunit 346

Behövs inte. Variant och lagerställe fås via Page 30052 och 30076 när Table 5401 och 14 har
Execute. Utestående antal härleds som `quantity − receivedQuantity` (100021: 49 − 3 = 46).
`purchaseDocumentLines` skulle bara ge samma uppgifter en gång till, och den kräver
dessutom nu Page 6405.

## B1 — Page 497 är inte publicerad (stoppunkt)

Behörighet och publicering är separata. Execute på Page 497 och Read på 337 är tillagda,
men sidan finns inte bland de 219 ODataV4-tjänsterna. B2–B4, C och D har inte kunnat
testas.

**Publicera så här (du gör det, inte Pegasus):**

1. Sök (Alt+Q) *Web Services* (sv. *Webbtjänster*) i Sandbox_eligra_261002.
2. Välj **Ny** och fyll i:
   - Object Type = **Page**
   - Object ID = **497** (Object Name visar *Reservation Entries*)
   - Service Name = **`PegasusReservationEntries`**
   - kryssa **Published**
3. Kolumnen *OData V4 URL* ska visa
   `…/ODataV4/Company('MCR')/PegasusReservationEntries`.
4. Webbtjänster gäller hela miljön (alla bolag), men åtkomst till data styrs av appens
   permission sets.

**Förbered (hypotes utifrån mönstret ovan):** efter publicering kan BC begära
**Execute på Table 337**, eftersom Page 30052 och 30076 krävde Execute på sina
tabellobjekt. Sidans beräknade fält Reserved From/For kan också kräva läsrätt på
källtabellerna (t.ex. Purchase Line 39, som redan är läsbar). Vi redovisar exakt fel och
itererar en behörighet i taget.

## Kvarstående behörigheter (konkret verifierade)

| Objekttyp | ID | Behörighet | Behövs för |
|---|---:|---|---|
| Table | 5401 | Execute | Variantkod (A1) |
| Table | 14 | Execute | Lagerställeskod (A2) |
| Table Data | 23 | Read | Page 38, bokförda serier per rad (A3). Exponerar leverantörsnamn, se nedan |

Page 38 visar även källans namn och belopp (kost, försäljning). Det är mer än Pegasus
behöver, men samma data finns redan via Analytics ILE. Behörigheten kan senare begränsas
via ett eget API (se H).

## Nästa steg

1. Publicera Page 497 (B1).
2. Lägg Table 5401 Execute och Table 14 Execute (variant och lagerställe).
3. Lägg TableData 23 Read (Page 38).
4. Meddela mig, så kör jag om A1–A3 och B2–B4 med acceptanstestet: 26 poster för
   100021/10000 inklusive BBCU3273070, inga av de tre mottagna.

---

# ETAPP 3a.3 — Slutverifiering (2026-10-25)

Samma metod som tidigare: endast GET, nytt token och inga databasskrivningar.

**Status: Page 497 är publicerad men ännu inte läsbar.** Felet är en behörighet, inte
publicering och inte en API-begränsning. Enligt stoppregeln har B3–B4 och C–D inte
körts mot data.

## Resultat

| API | HTTP | Resultat |
|---|---|---|
| STD `purchaseOrders(acb56aaf-…)/purchaseOrderLines?$expand=itemVariant,location` | **200** | **Löst.** Rad 10000: `itemVariant.code = RAL5013`, `location.code = L005` ("Lager MCR05 - Lager Oceantermialen i JohnEvans"). Verifierat direkt på orderraden |
| STD `itemVariants({id})`, `itemVariants?$filter=itemNumber eq 'CON22G1'` | **200** | **Löst.** GUID → kod. CON22G1 har bl.a. CAMO, RAL0, RAL1014 … RAL5013 … RAL7013. `lastModifiedDateTime` finns |
| STD `locations` | **200** | **Löst.** L001, L005, L005PROD, L005SAMFAK, L005UTHYRT med GUID |
| OD4 `Artikeltransaktioner_Excel` (Page 38) | 403 | **Nytt steg i kedjan:** `(CodeUnit 5860 Invt. Ledger Sales Source Execute: Base Application)`. TableData 23 är löst. Sidan kör källkodeunits för både inköp och försäljning, även när filtret bara träffar inleveranser |
| OD4 `$metadata` / tjänstelistan | 200 | 220 tjänster. Den nya heter **`ContainertrackingReservationEntries`**, inte `PegasusReservationEntries` |
| OD4 `ContainertrackingReservationEntries` | 403 | `(Table 337 Reservation Entry Execute: Base Application)`. Saknar **Execute på objekttypen Table 337** (TableData 337 Read finns) |

## B2 — Fält i `ContainertrackingReservationEntries` (från `$metadata`)

Nyckel: **`Entry_No` (Int32) + `Positive` (Boolean)**.

| Behov | Fält | Typ | Finns |
|---|---|---|---|
| Source Type | `Source_Type` | Int32 (39 = Purchase Line) | ja |
| Source Subtype | `Source_Subtype` | String (enum, t.ex. `"1"`) | ja |
| Source ID | `Source_ID` | String | ja |
| Source Ref. No. | `Source_Ref_No` | Int32 | ja |
| Item No. | `Item_No` | String | ja |
| Variant Code | `Variant_Code` | String | ja |
| Location Code | `Location_Code` | String | ja |
| Serial No. | `Serial_No` | String | ja |
| Quantity (Base) | `Quantity_Base` | Decimal | ja |
| Quantity | — | — | **nej** (behövs inte, serier har bas-enhet ST) |
| Entry No. | `Entry_No` | Int32 | ja (del av nyckeln) |
| Övrigt | `Reservation_Status`, `Lot_No`, `Package_No`, `Expected_Receipt_Date`, `Shipment_Date`, `Creation_Date`, `Transferred_from_Entry_No`, `Description`, `ReservedFrom`, `ReservEngineMgt_CreateForText_Rec`, `Source_Batch_Name` | | ja |
| SystemId / SystemModifiedAt / Purchase Line GUID | — | — | **nej** |

Alla fält som behövs för kopplingen till orderraden finns. `Source_ID` och
`Source_Ref_No` motsvarar `purchaseOrders.number` och `purchaseOrderLines.sequence`.
Detta är verifierat i metadata, men inte mot data ännu.

## Preliminär produktionsbedömning (från metadata, ej verifierad mot data)

| Krav | Bedömning |
|---|---|
| C1 Identitet | `Entry_No`+`Positive` är unik men **inte stabil**, eftersom BC kan ta bort och skapa om poster när spårning ändras (bekräftas med data). Pegasus bör nyckla på (`Source_ID`, `Source_Ref_No`, `Serial_No`) |
| C2 Orderrad | `Source_ID` + `Source_Ref_No` → `purchaseOrderLines.sequence` → rad-GUID. Entydigt även när flera rader har samma artikel. Kräver filter `Source_Type eq 39` och `Source_Subtype eq '1'` (Order) |
| C3 Filtrering | Fälten finns i metadata. Server-side filter ska verifieras när GET fungerar |
| C4 Avstämning | Inga ändringsstämplar. Ändringar upptäcks genom att hela mängden hämtas och jämförs, vilket är den strategi studien redan rekommenderar för MCR. Nya, borttagna, ändrade och flyttade serier syns som mängddifferens på (order, rad, serie) |
| C5 Säkerhet | Med Read på 337 exponerar tjänsten **alla** reservationer och all spårning i bolaget: försäljning, överföring, produktion, beskrivningar och datum. Det är bredare än Pegasus behöver, men innehåller inga belopp eller personuppgifter (förutom ev. text i `Description`/`ReservedFrom`). Pegasus filtrerar själv. Tjänsten kan inte begränsas utan utveckling |
| C6 Prestanda | Bedöms när GET fungerar (`$count`, paging) |

## Kvarstående behörigheter (konkret verifierade)

| Objekttyp | ID | Behörighet | För |
|---|---:|---|---|
| **Table** | **337** | **Execute** | `ContainertrackingReservationEntries`, **blockerande för etapp 3** |
| Codeunit | 5860 | Execute | Page 38 (bokförda serier per dokumentrad). Inte blockerande: relationen finns redan via Analytics ILE + inleveranser när inleveransen har en rad per artikel |

Efter Table 337 kan BC kräva fler behörigheter för sidans beräknade textfält
(`ReservedFrom`, `ReservEngineMgt_CreateForText_Rec`). Det är en hypotes.

## Nästa steg

1. Lägg **Table 337: Execute** i `CONTAINERTRACKING`.
2. (Valfritt) Codeunit 5860: Execute för Page 38.
3. Meddela mig. Jag kör då B3–B4 mot
   `ODataV4/Company('MCR')/ContainertrackingReservationEntries?$filter=Source_Type eq 39 and Source_ID eq '100021'`
   med acceptanstestet (26 poster för rad 10000, BBCU3273070 med, inga av de tre mottagna)
   samt C3/C6 (filter, `$count`, paging).

---

# ETAPP 3a.4 — Acceptanstest Reservation Entries (2026-10-25)

Efter att Execute på Table 337 lagts till:

| API | HTTP | Exakt BC-fel |
|---|---|---|
| OD4 `ODataV4/Company('MCR')/ContainertrackingReservationEntries?$count=true&$top=2` | 403 | `(CodeUnit 99000831 Reservation Engine Mgt. Execute: Base Application)` |

**Table 337 är löst.** Nästa steg i kedjan är codeunit 99000831 *Reservation Engine Mgt.*,
som sidan anropar för att bygga de beräknade textfälten `ReservEngineMgt_CreateForText_Rec`
och `ReservedFrom`. Pegasus behöver inte de fälten, men sidan beräknar dem för varje rad.
Det är alltså en behörighet som saknas, inte publicering och inte en begränsning i API:et.

**Acceptanstestet är inte genomfört** (stoppregel). Ingen data har lästs från tjänsten.

**Nästa steg:** Codeunit 99000831: Execute i `CONTAINERTRACKING`. Därefter körs
acceptanstestet (26 poster för 100021/10000, BBCU3273070 med, inga av BBCU3272941,
BBCU3273059 och BBCU3273090). Om BC därefter begär fler objekt för textfälten
(t.ex. källtabeller eller codeunits för andra källtyper än inköp) kan en egen API-sida
från NAB, utan beräknade fält, bli enklare än att fortsätta lägga till behörigheter.

## 3a.4 (forts.) — Efter Codeunit 99000831 (2026-10-25)

| API | HTTP | Exakt BC-fel |
|---|---|---|
| OD4 `ContainertrackingReservationEntries?$count=true&$top=2` | 403 | `(CodeUnit 925 Assembly Header-Reserve Execute: Base Application)` |

**Stoppat enligt beslut.** Inga fler behörigheter föreslås för Page 497.

**Varför kedjan inte tar slut.** Codeunit 99000831 är löst. Den anropar nu källspecifika
reserve-codeunits för att bygga textfälten `ReservedFrom` /
`ReservEngineMgt_CreateForText_Rec`. Den första som nekades gäller **montering**, en källtyp
Pegasus inte använder. Det visar att sidan kräver behörighet för *alla* källtyper och inte
bara inköp. Troliga nästa objekt (hypotes, ej verifierat):

- 926 Assembly Line-Reserve
- 99000832 Sales Line-Reserve
- 99000833 Req. Line-Reserve
- 99000834 Purch. Line-Reserve
- 99000835 Item Jnl. Line-Reserve
- 99000836 Transfer Line-Reserve
- 99000837 Prod. Order Line-Reserve
- 99000838 Prod. Order Comp.-Reserve
- 99000840 Plng. Component-Reserve
- 99000842 Service Line-Reserve
- 1032 Job Planning Line-Reserve

Kanske följer också tabelläsning på deras källtabeller (Sales Line, Assembly Header …).

**Slutsats för Page 497 som integrationskälla: underkänd.**

- **Behörigheter:** sidan kräver Execute och sannolikt Read över hela reservationsmotorn,
  inklusive källtyper utan koppling till Pegasus. Det motsäger principen om minsta
  behörighet.
- **Exponering:** alla reservationer och all spårning i bolaget, inte bara inköp.
- **Stabilitet:** UI-sida. Fältnamn och triggers kan ändras vid BC-uppgraderingar.
- **Identitet och ändringar:** ingen SystemId och ingen SystemModifiedAt. Nyckeln är
  `Entry_No` + `Positive`.
- **Verifierat värde:** metadata visar att tabell 337 innehåller exakt de fält vi behöver
  (`Source_Type`, `Source_Subtype`, `Source_ID`, `Source_Ref_No`, `Item_No`,
  `Variant_Code`, `Location_Code`, `Serial_No`, `Quantity_Base`). En NAB-sida över
  samma tabell *utan* beräknade fält kräver bara Read på 337 och Execute på sin egen sida.

**Rekommendation:**

- Återkalla de behörigheter som bara lades till för Page 497: Page 497, Table 337
  Execute, Codeunit 99000831.
- Avpublicera `ContainertrackingReservationEntries` när NAB-API:et finns.
- Behåll TableData 337 Read för NAB-sidan.
- Variant (Page 30052 + Table 5401) och lagerställe (Page 30076 + Table 14) behålls, eftersom
  de fungerar och behövs.

### Begränsat API från NAB (slutlig specifikation)

```
GET /api/<publisher>/pegasus/v1.0/companies({companyId})/purchaseLineTrackings
```

- AL `PageType = API`, `SourceTable = "Reservation Entry"`, `Editable = false`,
  `DataAccessIntent = ReadOnly`, `ODataKeyFields = SystemId`.
- `SourceTableView` fast filtrerad på Source Type 39 (Purchase Line), Source Subtype 1
  (Order), Positive = true och Serial No. ≠ ''.
- **Inga beräknade textfält** och inga anrop till Reservation Engine.
- Kräver endast: Execute på den nya sidan och Read på TableData 337 (+ 39 för rad-GUID).

| API-fält | Källa (tabell 337) | Krav |
|---|---|---|
| `id` | SystemId | krav |
| `entryNo` | Entry No. | info |
| `documentNo` | Source ID | krav |
| `lineNo` | Source Ref. No. | krav |
| `purchaseOrderLineId` | Purchase Line.SystemId (lookup på 38/39 via Source ID + Ref. No.) | önskvärt |
| `itemNo`, `variantCode`, `locationCode` | Item No., Variant Code, Location Code | krav |
| `serialNo` | Serial No. | krav |
| `quantityBase` | Quantity (Base) | krav (kontroll = 1) |
| `reservationStatus` | Reservation Status | önskvärt |
| `expectedReceiptDate` | Expected Receipt Date | valfritt |
| `systemModifiedAt` | SystemModifiedAt | önskvärt |

**Beteenden:**

- En post per serie.
- `$filter` på `documentNo`, `lineNo`, `serialNo` och `systemModifiedAt`, plus `$count`
  och server-side paging.
- Ingen logik för receive, tracking eller container.

**Acceptanstest (sandbox):** `documentNo eq '100021' and lineNo eq 10000` ska ge **26**
poster, varav `BBCU3273070` en (CON22G1 / RAL5013 / L005), och inga av BBCU3272941,
BBCU3273059 eller BBCU3273090. `purchaseOrderLineId` ska vara
`19b968df-a799-f111-8072-6045bdaa5148`.
