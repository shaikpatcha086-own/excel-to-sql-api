# Copilot Instructions — QuickBooks to D365 SCM Migration

This project migrates QuickBooks Desktop data into D365 Supply Chain Management (D365 SCM),
via a Fabric Lakehouse/Warehouse pipeline.

## Architecture pattern (every entity): Led -> St -> Adm

- `trf.<Entity>LedX` (view) — maps/casts raw source columns to D365 field names and
  datatypes. No business logic here, structural mapping only.
- `trf.<Entity>LedToSt` (procedure) — materializes the LedX view into a `trf.<Entity>St`
  staging table (a stable snapshot).
- `trf.<Entity>StUp` (procedure) — business rules: cleansing, deduplication, derived
  fields, lookups/crosswalks, exception handling. This is CLIENT-OWNED: it is only ever
  `CREATE`d the first time (`IF NOT EXISTS ... EXEC('CREATE PROCEDURE ...')`), never
  `CREATE OR ALTER` - the client adds their own custom logic directly into this
  procedure in the database after go-live, and every later regeneration of the script
  must leave it untouched so that logic survives and keeps running via `MainRun`.
- `trf.<Entity>StToAdm` (procedure) — promotes the cleaned staging data into the final
  `adm.Extract<Entity>` table (load-ready).
- `trf.<Entity>MainRun` (procedure) — runs LedToSt -> StUp -> StToAdm in sequence.

## Source and target

- Source: QuickBooks Desktop data landed in the `bhs_quickbooks` schema, Fabric
  Lakehouse `LH_Silver`.
- Target: D365 SCM data entities, loaded via `adm.Extract<Entity>` tables in the Fabric
  Warehouse, ready for D365 data entity import.

## Global rules (apply to every entity)

- Every generated `LedX` view must carry the QuickBooks `ListID` from the base source
  table forward as a `LegacyListId` column.
- Field names in D365 rarely match QuickBooks 1:1 — always confirm exact target field
  names against the actual staging table export before writing a `LedX` view (e.g.
  `AddressStreet`, not `DeliveryAddressStreet`, on `CustomerPostalAddressEntity`; role is
  set via separate `IsRoleX` boolean flags, not the field name). Do not assume field
  names from general D365 knowledge alone.
- Credit card fields and other PCI-sensitive data should not be migrated as-is. Enforced in
  the matcher: any source field containing both `credit` and `card` tokens (e.g.
  `CreditCardInfoExpirationMonth`, `CreditCardInfoCreditCardNumber`) is blocked from
  matching any target field at all, regardless of how plausible the target name looks
  (e.g. `CreditCardInfoExpirationMonth` must never land on a target like `Expiration`).
- Never copy QuickBooks `Balance` / `OpenBalance` fields directly into D365 — balances
  are derived from the transaction graph, not stored as ground truth. Reconstruct via
  an opening balance journal instead.
- QuickBooks stores country as a plain name (e.g. `United States`), while D365
  `*CountryRegionId` targets expect an ISO code (e.g. `USA`). Never convert this directly
  in `LedX` (structural mapping only). Instead, `LedX` casts the raw name through
  unchanged, and the generated `StUp` procedure gets a commented-out crosswalk-join
  placeholder (`trf.CountryRegionCrosswalk`) for any target field ending in
  `CountryRegionId`/`CountryRegion` - confirm the real crosswalk table/column names,
  then enable it.

## Mapping workbook rules (mapper app / Excel output)

- When a target mapping workbook already contains a `Table` column, fill it with the
  legacy QuickBooks object that supplied the mapping (for example, `Customer`, `Vendor`,
  or `Invoice`). Do not add, rename, or otherwise modify the workbook's `Table`,
  `Mapping Source`, `Dependency Source`, or source-field columns beyond their existing
  mapping roles.
- If a target row's Source Field is already filled in with a real value (not blank, not
  `NoMap`) when the mapping run starts, it is preserved as-is and never re-matched or
  overwritten - this is how a client's manual correction of a prior `NoMap` row (e.g.
  filling in the legacy column for `VendorKnownAsName`) survives re-running the mapper
  on that same workbook. The matched source field is also reserved (not reused for a
  different still-unmapped target). See `mapping_helpers.partition_already_mapped_targets`.

---

## Entity: Customer

### Address fields

- Do not map legacy QuickBooks Customer address fields (`Address*`, `Addr*`,
  `BillAddress*`, `ShipAddress*`, `InvoiceAddress*`, or `DeliveryAddress*`) to generic
  customer `Address*`, `InvoiceAddress*`, or `DeliveryAddress*` targets. These are handled
  separately through `CustomerPostalAddressEntity` / `CustCustomerV3Entity` address flows.

### Active-customer filter

- For the Customer entity only, filter to active customers: add `IsActive = 1` on the base
  Customer table ONLY - not on any joined reference/dimension table (Currency, Terms,
  PaymentMethod, SalesTaxCode, etc.). Those are `LEFT JOIN`ed lookups: they either have no
  `IsActive` column at all, or a `NULL` from an unmatched `LEFT JOIN` would make the `AND`'d
  condition drop an otherwise-valid active customer row from `adm.ExtractCustomers`
  entirely. Do not apply this filter to non-Customer entities (including
  `CustomerShipToAddress`, whose rows are not filtered by `IsActive`).
- Because `IsActive` is already consumed directly in that `WHERE` clause, the Fabric mapper
  UI (app.py) excludes it from the "Leftover Fabric Columns" / unmapped-columns report for
  the same entity scope (base Customer table, not `CustomerShipToAddress`/
  `CustomerContacts`) - it is not truly unmapped/ignored, so flagging it for customer
  review would be misleading.

### Sub-entity: CustomerShipToAddress -> CustomerPostalAddress (special case)

- QuickBooks `CustomerShipToAddress` has multiple rows per customer (confirmed via
  `GROUP BY ListID HAVING COUNT(*) > 1`) and each row carries both `BillAddress*` and
  `ShipAddress*` columns together. It loads into `CustomerPostalAddressEntity` (staging
  table `CustomerPostalAddressStaging`), never into `CustCustomerV3Entity`/Customers V3
  directly.
- When a mapping maps both prefixes to the same target Address* field, generate the
  `LedX` view as a `UNION ALL` of a billing branch and a shipping branch instead of a
  normal 1:1 SELECT:
  - Billing branch: deduplicate to one row per customer
    (`ROW_NUMBER() OVER (PARTITION BY ListID ORDER BY EditSequence DESC) = 1`, keeping the
    most recently changed row - QuickBooks' `EditSequence` is its list change-version
    column).
  - Shipping branch: keep every row, unfiltered.
  - Each branch excludes rows where its own address-line column (Street/Addr1/etc.) is
    blank (`IS NOT NULL`).
  - If the target template defines `IsRoleInvoice`/`IsRoleDelivery` fields, set them to
    literal `1`/`0` per branch (billing = Invoice, shipping = Delivery).
  - If it defines `AddressDescription`, set it to literal `'Bill-To'`/`'Ship-To'`.
  - If it defines `IsPrimary`, set billing to `1`. The shipping branch defaults to literal
    `0` - the `ShipToAddressDefaultShipTo` "default ship-to" flag is not reliable enough to
    drive it, so its derivation (`CASE WHEN <flag> = 1 THEN 1 ELSE 0 END`) is kept only as a
    commented-out reference line above the active `0` literal, never executed.
  - If it defines `AddressLocationId`, it has no real QuickBooks source - generate a unique
    sequential value with `ROW_NUMBER() OVER (ORDER BY [combined].[LegacyListId])` in the
    outer SELECT (same ordering as `[ID]`), instead of leaving it `NULL`/NoMap.
  - `IsRoleInvoice`/`IsRoleDelivery`/`IsPrimary`/`AddressDescription` are the assumed D365
    field names for this pattern - confirm the exact names against the actual
    `CustomerPostalAddressStaging` export before relying on them.
  - The matcher must never let a real source field match a synthetic role/description
    target (`IsRole*`, `AddressDescription`, `IsPrivate*`, `IsPostalAddress`,
    `IsLocationOwner`, `IsPrimaryTaxRegistration`, `AddressDefaultRoles`,
    `AddressLocationRoles`) - these are always populated by the SQL generator's
    deterministic literals. `IsPrimary` is the sole exception: it may match a real
    "default address" flag (e.g. `ShipToAddressDefaultShipTo`, matched via a dedicated
    context-alias rule), but nothing else.
  - `AddressPostBox` is a distinct address component from `AddressState`/`AddressCity` -
    a `State`-typed source field must never match a `PostBox`-typed target, or vice versa.
  - Confirmed client-specific data quirk: `BillAddressCounty`/`ShipAddressCounty` actually
    carry the country ISO code in this QuickBooks export, not a US county name. They must
    map to `AddressCountryRegionISOCode` (not `AddressCountyId`) - a dedicated context-alias
    rule routes them there and blocks them from ever matching `AddressCountyId`. Note:
    `violates_business_rule()` normally flags "county" vs "country" as a component mismatch,
    so this override only fires when `preassign_context_aliases()` has run first (its
    candidate search bypasses that generic rule, same as other context-alias overrides).
  - `IsRoleInvoice`/`IsRoleDelivery`/`IsPrimary`/`AddressDescription`/`AddressLocationId`
    have no real QuickBooks source field, so the matcher always leaves them `NoMap`. The
    SQL generator must still promote them into the active `UNION` SELECT (not the
    commented-out NoMap placeholder list) so the per-branch literal/sequence override
    still populates them.
  - QuickBooks has a THIRD address block on this table, `ShipToAddress*` (additional named
    ship-to addresses, e.g. `ShipToAddressCountry`, `ShipToAddressDefaultShipTo`) - distinct
    from the `BillAddress*`/`ShipAddress*` pair and with no `BillToAddress*` sibling at all.
    Never derive a `BillToAddress*` counterpart from it. It must also never outcompete
    `BillAddress*`/`ShipAddress*` for a generic `Address*` target (e.g. `ShipToAddressCountry`
    must not beat `BillAddressCountry` for `AddressCountryRegionId`) - the matcher blocks any
    `ShipToAddress*` field from matching a generic `Address*` target entirely.
  - Every column referenced against `bhs_quickbooks.CustomerShipToAddress` - including ones
    derived by the Bill/Ship prefix swap - must be validated against the confirmed real
    column list before the script is finalized. Generation must fail loudly (raise an
    error) on an unrecognized column instead of letting it reach the generated SQL and only
    fail when executed.
  - The outer `combined` SELECT must explicitly `CAST` every column to its intended type
    (`,CAST([combined].[Field] AS <type>) AS [Field]`), not a bare `[combined].[Field]`
    pass-through. Fabric Warehouse's `UNION ALL` type inference can otherwise widen a
    literal `CAST(... AS varchar(n))` column (e.g. `AddressDescription`'s `'Bill-To'`/
    `'Ship-To'`) to an unsupported `nvarchar(n)` on `SELECT INTO`.
  - The type-extraction used for that outer re-CAST must tolerate a space before the size
    (e.g. a real ADM template Data Type value like `nvarchar (60)`) - a strict
    no-space-allowed pattern fails to match entirely and must never silently fall back to
    `nvarchar(60)` (unsupported); the safe fallback is `varchar(60)`.

### Field mapping guards

- Do not map QuickBooks `CompanyName` to D365 `Company` / legal-entity targets. A D365
  legal entity is not a QuickBooks company-name field.
- Do not map QuickBooks sales-tax code references (for example,
  `SalesTaxCodeRefFullName`) to D365 boolean or enum sales-tax inclusion fields (for
  example, `IsSalesTaxIncludedInPrice`). Tax-code references and Yes/No flags are distinct.

### Customers V3 static-only fields

- The following `CustCustomerV3Entity` target fields are always populated as static/
  constant values, never mapped from real QuickBooks source data. The matcher auto-resolves
  them directly to a `Static Value` result (`source_entity = "Static"`, `source_field` = the
  literal) instead of searching for or being assigned a real source column:
  - `PartyType` -> static value `Organization` (status `Auto Accept`).
  - `LanguageId` -> static value `en-us` (status `Auto Accept`).
  - `COMPANY` -> resolves to a placeholder literal `<CONFIRM_LEGAL_ENTITY_CODE>` with status
    `Review` - it depends on the target D365 legal entity and is not a fixed literal across
    environments, so it must be manually confirmed/replaced with the real code before
    executing the generated SQL, never left as the placeholder.
  - `CustomerAccountNumber` -> resolves to a placeholder literal `<D365_CUSTOMER_ACCONTNUMBER>`
    with status `Review` - D365 generates this account ID itself via its own number
    sequence at record creation; it is a different identifier from the legacy QuickBooks
    `AccountNumber` and must never be populated from it.
  - This relies on the existing SQL-generation static-value convention: `Mapping Source` /
    `Table` = `Static`, `Source Field` = the literal - `generate_sql()` emits it as
    `CAST('<literal>' AS <type>)` rather than a column reference.

### Sub-entity: CustomerContacts -> smmContactPersonV2Entity

- QuickBooks `CustomerContacts` has multiple rows per customer (one per legacy contact
  person). `smmContactPersonV2Entity` needs exactly one contact per customer, so the
  `LedX` view wraps the base table in a dedup subquery instead of a normal 1:1 SELECT:
  - Dedupe via `ROW_NUMBER() OVER (PARTITION BY [ListID] ORDER BY [TimeModified] DESC)`,
    keeping the most recently changed row, filtered to `WHERE [ContactRowNum] = 1`.
  - Only include rows that actually have a contact record:
    `WHERE [ContactsRetListID] IS NOT NULL` inside the dedup subquery.
  - Legacy contact fields on this table use the `ContactsRet*` prefix (e.g.
    `ContactsRetFirstName`, `ContactsRetMiddleName`, `ContactsRetLastName`).
  - This Fabric table also exposes bare, same-named columns (`FullName`, `Name`,
    `FirstName`, `MiddleName`, `LastName`, `Email`, `Fax`, `Phone`, `JobTitle`, etc.) that
    belong to the customer record, not the contact person. The matcher blocks **any** bare
    (non-`ContactsRet*`) column on this entity from matching **any** target - only
    `ContactsRet*` fields are ever eligible sources here, regardless of target name
    (covers `SearchName`, `KnownAsName`, `Phonetic*Name`, `Primary*Number`/`Address`,
    `EmploymentJobTitle`, and any future target on this entity).
  - The `IsActive` Customer-entity filter does not apply to `CustomerContacts`.
  - The `[ID]` column orders by `[ListID]`, same as other QuickBooks list/master tables.
  - No UNION split is needed here (unlike `CustomerShipToAddress`) - there is only one
    contact role per customer, not a billing/shipping pair.

## Entity: Vendor

### Sub-entity: VendVendorV2Entity field mapping guards

- QuickBooks `Tax1099BoxId` (a reference to a 1099 box category, e.g. `"3"` - not a
  boolean) is the deterministic source for the D365 1099-eligibility flag (the real
  target field is `IsReportingTax1099`; `IsVendorEligibleFor1099` is also recognized in
  case a template names it that way): a vendor with a `Tax1099BoxId` assigned IS
  1099-eligible, regardless of which box number it is.
  - The matcher matches this pair deterministically (a plain token-overlap match fails:
    digit boundaries prevent `tokenize()` from splitting "1099" out on its own, e.g.
    `Tax1099BoxId` -> `["tax1099box","id"]`). The target-side check keys off the tokens
    "reporting" or "eligible" combined with "1099" in the normalized field name.
  - Never cast `Tax1099BoxId` straight to `bit` - it is an ID/reference value, not 0/1. The
    `LedX` view must derive it: `CASE WHEN Tax1099BoxId IS NOT NULL THEN 1 ELSE 0 END`.
  - The vendor template ALSO has its own literal `Tax1099BoxId` target field (the raw
    value, mapped directly) alongside the derived eligibility flag - both legitimately
    share the same single QuickBooks `Tax1099BoxId` source column. The matcher's
    contested-field resolution (`preassign_context_aliases`) must not mark that source
    field as "used" when it resolves the derived-flag target, or the literal
    `Tax1099BoxId` target would be starved and left NoMap.
  - If the Vendor Fabric table has its OWN legacy `IsReportingTax1099` column (a real,
    exact-name field, separate from `Tax1099BoxId`), that direct exact-name match always
    wins over deriving the flag from `Tax1099BoxId` - `preassign_context_aliases` skips
    the derived-heuristic candidate for a target whenever an unused exact-name source
    column exists for it.
  - Confirmed client-specific case: this Fabric Vendor export also has its own computed
    `IsVendorEligibleFor1099` boolean column (not exactly named like the target, but the
    same concept) - it maps directly to `IsReportingTax1099`/`IsVendorEligibleFor1099`
    with a plain `CAST`, no derivation (unlike `Tax1099BoxId`, since the source name has
    no "box" substring). This pair is exempted from the generic "two different `Is*`
    booleans never match" guard in `_has_incompatible_field_roles`.
  - Confirmed client-specific case: this same `IsVendorEligibleFor1099` column ALSO feeds
    the literal `Tax1099BoxId` target in this template - both a plain `CAST`, both
    resolving from the same single source column without one starving the other
    (see `_is_shareable_derived_alias`).

## Entity: Item

- QuickBooks `Item` -> D365 **Released Product** (`EcoResProduct` family).

---

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
- Add new entities under their own `## Entity: <Name>` heading, with `###` sub-headers for
  that entity's address handling, sub-entities/special cases, filters, and field mapping
  guards. If a rule applies to every entity, put it under `## Global rules` instead of
  duplicating it per entity.
