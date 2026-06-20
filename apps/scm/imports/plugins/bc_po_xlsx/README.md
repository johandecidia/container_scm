# Business Central Purchase Order XLSX Importer

An isolated, removable importer plugin for Business Central-style Purchase Order XLSX files.

## Why is this isolated?

BC PO XLSX files are document-layout exports, not clean spreadsheet tables.  The parsing logic
is complex and specific to this one file format.  Keeping it isolated means:

* The plugin can be disabled or deleted without touching any procurement models or the core
  import pipeline.
* Changes to BC's XLSX layout only require updating this module.
* The rest of the codebase treats this exactly like any other `PURCHASE_ORDERS` import once
  the flat rows are produced.

## How to disable it

Set the feature flag in your `.env` file or environment:

```
SCM_ENABLE_BC_PO_XLSX_IMPORT=False
```

When disabled:
* The import type is hidden from the upload form.
* Direct form submissions with `import_type=bc_po_xlsx` are rejected with a validation error.
* All existing PurchaseOrder / PurchaseOrderLine data is unaffected.

## How to remove it permanently

1. Delete this plugin directory:
   ```
   rm -rf apps/scm/imports/plugins/bc_po_xlsx/
   ```

2. Remove the import type from `apps/scm/imports/models.py`:
   ```python
   # Delete this line:
   BC_PO_XLSX = "bc_po_xlsx", _("Business Central Purchase Order XLSX")
   ```

3. Remove the one-line registrations in each of these files (search for `BC_PO_XLSX`):
   * `apps/scm/imports/parsers.py`   — `_parse_bc_po_xlsx` function + dispatch branch
   * `apps/scm/imports/mappings.py`  — `_DEFAULT_MAPPINGS` entry
   * `apps/scm/imports/schemas.py`   — `_SCHEMA_REGISTRY` entry
   * `apps/scm/imports/validators.py` — `_VALIDATORS` entry
   * `apps/scm/imports/importers.py` — `_IMPORTERS` entry
   * `apps/scm/imports/forms.py`     — feature-flag branch in `_get_import_type_choices` + `clean()`

4. Remove the feature flag from `container_scm/settings.py`:
   ```python
   # Delete this line:
   SCM_ENABLE_BC_PO_XLSX_IMPORT = env.bool("SCM_ENABLE_BC_PO_XLSX_IMPORT", default=False)
   ```

5. Remove the migration that added `BC_PO_XLSX` to `ImportType`:
   ```
   rm apps/scm/imports/migrations/0004_add_bc_po_xlsx_import_type.py
   ```
   Then create a new squashed or reverting migration, or just delete the choice and let Django
   create a new `AlterField` migration.

No procurement core models (PurchaseOrder, PurchaseOrderLine, Supplier, etc.) need to be
changed or deleted.  All previously imported POs remain intact.

## Supported file structure

The parser handles XLSX files exported from Business Central using the **PEB Purchase Order**
printed report layout (report id 12047977), available in both English and Swedish locales.

### Document layout

```
Row 1-N:   Header section — label/value pairs for PO metadata
Row N+1:   Item table header row (e.g. "No. | Description | Quantity | ...")
Row N+2..: Item data rows, one per PO line
Last row:  Totals/footer row (parser stops here)
```

### Header labels recognised

| English label            | Swedish label        | Field          |
|--------------------------|----------------------|----------------|
| No.                      | Inköpsordernr.       | PO Number      |
| Vendor No.               | Leverantörsnr.       | Vendor No.     |
| Buy-from Vendor Name     | Leverantör           | Vendor Name    |
| Order Date               | Orderdatum           | Order Date     |
| Payment Terms            | Betalningsvillkor    | Payment Terms  |
| Purchaser                | Inköpare             | Purchaser      |
| Currency Code            | Valutakod            | Currency       |

### Item table columns recognised

`No.` / `Nr.`, `Description` / `Beskrivning`, `Quantity` / `Antal`,
`Unit of Measure` / `Enhet`, `Direct Unit Cost` / `À-pris`, `Amount` / `Belopp`

## Parser assumptions

* The PO number is in the **header section** (rows before the item table), not in the table.
* Label/value pairs: the label is in one cell, the value is in the next non-empty cell to
  the right in the same row.
* Lines without an item number (text/comment lines) are silently skipped.
* The same item number can appear multiple times — each occurrence becomes a separate
  PurchaseOrderLine with a unique `line_no` (10000, 20000, …).
* Amounts use Swedish decimal notation in the Swedish locale (comma as decimal separator,
  space as thousands separator) and standard dot notation in the English locale.
* The parser stops at the first row whose first cell starts with a "stop" keyword
  (`Total`, `Summa`, `VAT`, `Moms`, etc.).

## Known limitations

* Only the **first worksheet** of the workbook is parsed.
* Multi-PO files (multiple POs in one XLSX) are **not** supported; the parser returns a
  single PO.  For multiple POs, use separate uploads.
* Password-protected or macro-enabled XLSX files are not supported.
* `expected_receipt_date` is not extracted (not present in the standard BC PO XLSX layout).
* Unit-price and amount columns are stored as metadata (`_unit_price`, `_amount`) but are
  not written to any model field — the current PurchaseOrderLine model does not have these
  fields.
