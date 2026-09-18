# Copilot Instructions — QuickBooks to D365 SCM Migration

This project migrates QuickBooks Desktop data into D365 Supply Chain Management (D365 SCM),
via a Fabric Lakehouse/Warehouse pipeline.

## Pattern used for every entity: Led -> St -> Adm

- `trf.<Entity>LedX` (view) — maps/casts raw source columns to D365 field names and
  datatypes. No business logic here, structural mapping only.
- `trf.<Entity>LedToSt` (procedure) — materializes the LedX view into a `trf.<Entity>St`
  staging table (a stable snapshot).
- `trf.<Entity>StUp` (procedure) — business rules: cleansing, deduplication, derived
  fields, lookups/crosswalks, exception handling.
- `trf.<Entity>StToAdm` (procedure) — promotes the cleaned staging data into the final
  `adm.Extract<Entity>` table (load-ready).
- `trf.<Entity>MainRun` (procedure) — runs LedToSt -> StUp -> StToAdm in sequence.

## Source and target

- Source: QuickBooks Desktop data landed in the `bhs_quickbooks` schema, Fabric
  Lakehouse `LH_Silver`.
- Target: D365 SCM data entities, loaded via `adm.Extract<Entity>` tables in the Fabric
  Warehouse, ready for D365 data entity import.

## Key mapping rules learned so far

- Never copy QuickBooks `Balance` / `OpenBalance` fields directly into D365 — balances
  are derived from the transaction graph, not stored as ground truth. Reconstruct via
  an opening balance journal instead.
- Do not map legacy QuickBooks Customer address fields (`Address*`, `Addr*`,
  `BillAddress*`, `ShipAddress*`, `InvoiceAddress*`, or `DeliveryAddress*`) to generic
  customer `Address*`, `InvoiceAddress*`, or `DeliveryAddress*` targets. These are handled
  separately through `CustomerPostalAddressEntity` / `CustCustomerV3Entity` address flows.
- QuickBooks `CustomerShipToAddress` has multiple rows per customer (confirmed via
  `GROUP BY ListID HAVING COUNT(*) > 1`) — dedupe to 1 row per customer before loading
  Customers V3; load every row into `CustomerPostalAddressEntity`
  (staging table `CustomerPostalAddressStaging`) for shipping addresses, with
  `IsRoleDelivery = 1`.
- QuickBooks `Item` -> D365 **Released Product** (`EcoResProduct` family).
- Field names in D365 rarely match QuickBooks 1:1 — always confirm exact target field
  names against the actual staging table export before writing a `LedX` view (e.g.
  `AddressStreet`, not `DeliveryAddressStreet`, on `CustomerPostalAddressEntity`; role is
  set via separate `IsRoleX` boolean flags, not the field name).
- Credit card fields and other PCI-sensitive data should not be migrated as-is.
- Do not map QuickBooks `CompanyName` to D365 `Company` / legal-entity targets. A D365
  legal entity is not a QuickBooks company-name field.
- Do not map QuickBooks sales-tax code references (for example,
  `SalesTaxCodeRefFullName`) to D365 boolean or enum sales-tax inclusion fields (for
  example, `IsSalesTaxIncludedInPrice`). Tax-code references and Yes/No flags are distinct.
- When a target mapping workbook already contains a `Table` column, fill it with the legacy
  QuickBooks object that supplied the mapping (for example, `Customer`, `Vendor`, or
  `Invoice`). Do not add, rename, or otherwise modify the workbook's `Table`, `Mapping
  Source`, `Dependency Source`, or source-field columns beyond their existing mapping roles.
- Every generated `LedX` view (every entity/object, not just Customer) must carry the
  QuickBooks `ListID` from the base source table forward as a `LegacyListId` column.
- For the Customer entity only, filter to active customers: add `IsActive = 1` on the base
  Customer table and on every joined table in that entity's `LedX` view. Do not apply this
  filter to non-Customer entities.

## Conventions to follow when generating new scripts

- Name every object using the `<Entity>` pattern above — do not invent new naming.
- Always cast source columns explicitly to the target datatype in `LedX` views.
- Put business logic only in `StUp`, never in `LedX`.
- Before writing a new entity's mapping, ask for or check the actual D365 staging table
  field list — do not assume field names from general D365 knowledge alone.

## Rule maintenance

- When the user provides a confirmed QuickBooks-to-D365 mapping or SQL-generation rule,
  record or update it in this file and in `../D365MetadataMapperV3/.github/copilot-instructions.md`
  unless the user says the rule is local to only one project.
- Treat the confirmed rules in both instruction files as mandatory for future mapping,
  workbook, and SQL-generation work. Update an older instruction when a newer confirmed
  rule supersedes it.
