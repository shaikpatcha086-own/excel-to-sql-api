import argparse
import re
from pathlib import Path

import pandas as pd


STATIC_MARKERS = {"static", "const", "constant", "literal", "hardcoded", "hard-coded"}
NO_MAP_MARKERS = {"nomap", "no_map", "no map", "na", "n/a", ""}
COMMON_ALIAS_HINTS = {
    "cust": {"customer", "cust", "companycustomer"},
    "ship": {"ship", "shipto", "shiptoaddress", "shiptoaddr", "address"},
    "terms": {"paymentterms", "paymentterm", "terms", "payterms"},
    "vend": {"vendor", "vend"},
    "item": {"item"},
    "ar": {"ar", "accountsreceivable", "accountreceivable"},
    "country": {"country", "countryregion"},
    "terms2": {"mapping"},
}


def review_sql(sql_text: str) -> list[str]:
    issues: list[str] = []
    text = sql_text or ""
    if not text.strip():
        return ["SQL text is empty."]

    stripped = text.strip()
    upper = stripped.upper()

    if not upper.startswith("SELECT"):
        issues.append("SQL does not start with SELECT.")
    if " FROM " not in upper:
        issues.append("SQL is missing a FROM clause.")
    if stripped.count("'") % 2 != 0:
        issues.append("Unbalanced single quotes detected.")
    if stripped.count("[") != stripped.count("]"):
        issues.append("Unbalanced square brackets detected.")
    if "TODO: define join condition" in upper:
        issues.append("At least one join condition is still a placeholder.")
    if re.search(r"\bNULL\b\s+AS\s+\[[^\]]+\]", upper):
        issues.append("One or more mapped fields are producing NULL expressions.")

    if not issues:
        issues.append("No obvious syntax issues detected.")
    return issues


def normalize_sql(sql_text: str) -> str:
    text = (sql_text or "").strip()
    if not text:
        return ""

    text = re.sub(r";{2,}", ";", text)
    text = re.sub(r"\s+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if not text.endswith(";"):
        text += ";"
    return text + "\n"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).strip().lower())


def _choose_col(df: pd.DataFrame, candidates: list[str], required: bool = False) -> str | None:
    norm_to_real = {_norm(c): c for c in df.columns}
    for cand in candidates:
        if cand in norm_to_real:
            return norm_to_real[cand]

    # Fuzzy fallback for slight header variations, e.g. extra suffix/prefix text.
    fuzzy_matches: list[tuple[int, str]] = []
    for real_norm, real_col in norm_to_real.items():
        for cand in candidates:
            if cand and (cand in real_norm or real_norm in cand):
                fuzzy_matches.append((len(cand), real_col))
                break
    if fuzzy_matches:
        fuzzy_matches.sort(reverse=True)
        return fuzzy_matches[0][1]

    if required:
        raise ValueError(
            "Missing required mapping column. "
            f"Expected one of: {candidates}. "
            f"Available columns: {list(df.columns)}"
        )
    return None


def _is_blank(val) -> bool:
    return pd.isna(val) or str(val).strip() == ""


def _clean(val) -> str:
    if pd.isna(val):
        return ""
    return str(val).strip()


def _is_static(source_table: str, source_field: str) -> bool:
    st = _norm(source_table)
    sf = _norm(source_field)
    return st in STATIC_MARKERS or sf in STATIC_MARKERS


def _is_no_map(source_field: str) -> bool:
    return _norm(source_field) in NO_MAP_MARKERS


def _split_table_col(source_field: str) -> tuple[str | None, str]:
    sf = _clean(source_field)
    if "." in sf:
        left, right = sf.rsplit(".", 1)
        if left and right:
            return left.strip(), right.strip()
    return None, sf


def _sql_literal(raw: str) -> str:
    val = _clean(raw)
    if val == "":
        return "NULL"
    up = val.upper()
    if up == "NULL":
        return "NULL"
    if re.fullmatch(r"-?\d+(\.\d+)?", val):
        return val
    if up in {"TRUE", "FALSE"}:
        return up
    return "'" + val.replace("'", "''") + "'"


def _bracket(name: str) -> str:
    cleaned = _clean(name)
    if cleaned.startswith("[") and cleaned.endswith("]"):
        return cleaned
    return f"[{cleaned}]"


def _clean_table_name(table_name: str) -> str:
    tbl = _clean(table_name)
    if tbl.startswith("[") and tbl.endswith("]"):
        return tbl[1:-1].strip()
    return tbl


def _field_tokens(text: str) -> set[str]:
    cleaned = _clean(text).lower()
    tokens = set(re.findall(r"[a-z0-9]+", cleaned))
    tokens |= {t.replace("_", "") for t in tokens}
    return {t for t in tokens if t}


def _table_alias_hint(table_name: str) -> str | None:
    tbl = _clean_table_name(table_name)
    norm = _norm(tbl)
    for alias, keywords in COMMON_ALIAS_HINTS.items():
        if any(keyword in norm for keyword in keywords):
            if alias == "terms2":
                continue
            return alias
    return None


def _table_alias(table_name: str) -> str:
    tbl = _clean_table_name(table_name)
    norm = _norm(tbl)
    if not tbl:
        return "src"
    hinted = _table_alias_hint(tbl)
    if hinted:
        return hinted
    parts = re.findall(r"[A-Za-z]+", tbl.lower())
    if not parts:
        return "src"
    if len(parts) == 1:
        if parts[0] in {"ar", "gl", "ap", "pm"}:
            return parts[0]
        return parts[0][:8]
    return "".join(p[0] for p in parts[:2])[:8]


def _source_ref(alias: str, column: str) -> str:
    return f"{alias}.{_bracket(column)}"


def _transform_alias_refs(expr: str) -> set[str]:
    if not expr:
        return set()
    return {m.group(1).strip() for m in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\.", expr)}


def _table_reference_hints(text: str) -> set[str]:
    refs = _transform_alias_refs(text)
    refs |= {_norm(t) for t in refs}
    refs |= {tok for tok in _field_tokens(text) if tok in {"cust", "ship", "terms", "vend", "item", "ar", "country"}}
    return refs


def _normalize_transform(expr: str, alias_map: dict[str, str]) -> str:
    if not expr:
        return expr
    updated = expr
    for table_name, alias in alias_map.items():
        cleaned = _clean_table_name(table_name)
        if cleaned:
            updated = re.sub(rf"\b{re.escape(cleaned)}\.", f"{alias}.", updated)
    return updated


def _preferred_sheet_name(xls: pd.ExcelFile) -> str:
    preferred = ["mapping", "map", "field_mapping", "column_mapping"]
    lower_names = {name.lower(): name for name in xls.sheet_names}
    for p in preferred:
        if p in lower_names:
            return lower_names[p]
    return xls.sheet_names[0]


def _detect_header_row(raw_df: pd.DataFrame) -> int | None:
    target_markers = {
        "field",
        "targetfield",
        "target",
        "alias",
        "d365field",
        "fieldbelowd365",
        "targetcolumn",
        "fieldname",
        "name",
    }
    source_markers = {"sourcefield", "source", "sourcecolumn", "sourcefieldname"}
    for i, row in raw_df.head(60).iterrows():
        values = {_norm(v) for v in row.tolist() if not _is_blank(v)}
        if any(marker in values for marker in source_markers) and any(marker in values for marker in target_markers):
            return int(i)
    return None


def _as_typed_header(val, idx: int) -> str:
    if _is_blank(val):
        return f"Unnamed: {idx}"
    return str(val).strip()


def _read_mapping_df(xls: pd.ExcelFile, sheet_name: str) -> pd.DataFrame:
    raw_df = pd.read_excel(xls, sheet_name=sheet_name, header=None)
    header_idx = _detect_header_row(raw_df)
    if header_idx is None:
        return pd.read_excel(xls, sheet_name=sheet_name)

    header_vals = [_as_typed_header(v, i) for i, v in enumerate(raw_df.iloc[header_idx].tolist())]
    data_df = raw_df.iloc[header_idx + 1 :].copy()
    data_df.columns = header_vals
    data_df = data_df.dropna(how="all").reset_index(drop=True)
    return data_df


def _build_join_clauses(df: pd.DataFrame, table_aliases: dict[str, str], base_table: str) -> list[str]:
    joins: list[str] = []
    base_alias = table_aliases[base_table]
    base_clean = _clean_table_name(base_table)

    join_col = _choose_col(
        df,
        [
            "joincondition",
            "join_condition",
            "joinlogic",
            "join_logic",
            "joinclause",
            "join_clause",
            "on",
            "relationship",
            "relation",
            "linklogic",
        ],
        required=False,
    )

    if join_col:
        raw_joins: list[str] = []
        for _, row in df.iterrows():
            join_expr = _clean(row.get(join_col, ""))
            if join_expr:
                raw_joins.append(_normalize_transform(join_expr, alias_map=table_aliases))
        if raw_joins:
            unique_joins: list[str] = []
            for j in raw_joins:
                if j not in unique_joins:
                    unique_joins.append(j)
            return [f"LEFT JOIN /* explicit join */ {j}" if not j.strip().upper().startswith("LEFT JOIN") else j for j in unique_joins]

    for tbl, alias in table_aliases.items():
        if tbl == base_table:
            continue

        clean_tbl = _clean_table_name(tbl)
        norm_tbl = _norm(clean_tbl)

        inferred = _infer_join_condition(
            base_table=base_table,
            base_alias=base_alias,
            other_table=clean_tbl,
            other_alias=alias,
            df=df,
        )
        if inferred:
            joins.append(f"LEFT JOIN {_bracket(clean_tbl)} {alias} ON {inferred}")

    return joins


def _add_source_table(source_tables: list[str], table_name: str) -> None:
    tbl = _clean(table_name)
    if not tbl:
        return
    if _norm(tbl) in STATIC_MARKERS:
        return
    if tbl not in source_tables:
        source_tables.append(tbl)


def _field_score(field_name: str, table_tokens: set[str]) -> int:
    tokens = _field_tokens(field_name)
    score = 0
    key_tokens = {"no", "code", "id", "num", "number", "key", "account", "acct", "reference", "ref"}
    if tokens & key_tokens:
        score += 4
    if any(tok in tokens for tok in table_tokens):
        score += 6
    if "no" in tokens:
        score += 3
    if "code" in tokens:
        score += 3
    if "id" in tokens:
        score += 2
    if len(tokens) <= 3:
        score += 1
    return score


def _collect_table_fields(df: pd.DataFrame, table_col: str | None, source_col: str) -> dict[str, list[str]]:
    table_fields: dict[str, list[str]] = {}
    for _, row in df.iterrows():
        tbl = _clean(row[table_col]) if table_col else ""
        src = _clean(row[source_col])
        if not src or _is_no_map(src) or _is_static(tbl, src):
            continue
        clean_tbl = _clean_table_name(tbl)
        if clean_tbl not in table_fields:
            table_fields[clean_tbl] = []
        tbl_from_src, src_col = _split_table_col(src)
        field_name = src_col if tbl_from_src else src
        if field_name not in table_fields[clean_tbl]:
            table_fields[clean_tbl].append(field_name)
    return table_fields


def _best_table_alias(table_name: str, used_aliases: set[str]) -> str:
    alias = _table_alias(table_name)
    if alias not in used_aliases:
        return alias
    idx = 2
    while f"{alias}{idx}" in used_aliases:
        idx += 1
    return f"{alias}{idx}"


def _infer_join_condition(
    base_table: str,
    base_alias: str,
    other_table: str,
    other_alias: str,
    df: pd.DataFrame,
) -> str | None:
    source_table_col = _choose_col(df, ["sourcetable", "src_table", "table"], required=False)
    source_field_col = _choose_col(
        df,
        ["sourcefield", "source_column", "srccolumn", "source", "sourcefieldbelowd365", "d365sourcefield"],
        required=True,
    )

    table_fields = _collect_table_fields(df, source_table_col, source_field_col)
    base_fields = table_fields.get(base_table, [])
    other_fields = table_fields.get(other_table, [])

    if not base_fields or not other_fields:
        return None

    base_tokens = _field_tokens(base_table)
    other_tokens = _field_tokens(other_table)

    special_pairs: list[tuple[str, str]] = []
    if "ship" in _norm(other_table) or "address" in _norm(other_table):
        for bf in base_fields:
            if any(x in _field_tokens(bf) for x in {"no", "id", "account", "customer"}):
                for of in other_fields:
                    if any(x in _field_tokens(of) for x in {"customer", "no", "id", "account"}):
                        special_pairs.append((bf, of))
    if "terms" in _norm(other_table) or "payment" in _norm(other_table):
        for bf in base_fields:
            if any(x in _field_tokens(bf) for x in {"payment", "terms", "code"}):
                for of in other_fields:
                    if any(x in _field_tokens(of) for x in {"nav", "code", "mapping", "term"}):
                        special_pairs.append((bf, of))
    if "country" in _norm(other_table):
        for bf in base_fields:
            if any(x in _field_tokens(bf) for x in {"country", "region", "code"}):
                for of in other_fields:
                    if any(x in _field_tokens(of) for x in {"code", "id", "country"}):
                        special_pairs.append((bf, of))

    if special_pairs:
        bf, of = special_pairs[0]
        return f"{other_alias}.{_bracket(of)} = {base_alias}.{_bracket(bf)}"

    best: tuple[int, str, str] | None = None
    for bf in base_fields:
        base_score = _field_score(bf, other_tokens)
        for of in other_fields:
            other_score = _field_score(of, base_tokens)
            score = base_score + other_score
            if _norm(bf) == _norm(of):
                score += 10
            if bf.lower() == of.lower():
                score += 5
            if any(tok in _field_tokens(bf) for tok in other_tokens):
                score += 4
            if any(tok in _field_tokens(of) for tok in base_tokens):
                score += 4
            if best is None or score > best[0]:
                best = (score, bf, of)

    if best and best[0] >= 6:
        _, bf, of = best
        return f"{other_alias}.{_bracket(of)} = {base_alias}.{_bracket(bf)}"

    return None


def generate_sql(mapping_file: Path, output_file: Path, sheet_name: str | None = None) -> str:
    with pd.ExcelFile(mapping_file) as xls:
        sheet = sheet_name or _preferred_sheet_name(xls)
        df = _read_mapping_df(xls, sheet)
    if df.empty:
        raise ValueError("Mapping sheet is empty.")

    source_field_col = _choose_col(
        df,
        [
            "sourcefield",
            "source_column",
            "srccolumn",
            "source",
            "sourcefieldbelowd365",
            "d365sourcefield",
        ],
        required=True,
    )
    source_table_col = _choose_col(df, ["sourcetable", "src_table", "table"], required=False)
    target_field_col = _choose_col(
        df,
        [
            "targetfield",
            "target_column",
            "tgtcolumn",
            "target",
            "alias",
            "targetalias",
            "d365field",
            "fieldbelowd365",
        ],
        required=True,
    )
    transform_col = _choose_col(
        df,
        ["transformationlogic", "transformlogic", "logic", "transformation", "rule", "expression"],
        required=False,
    )
    static_col = _choose_col(df, ["staticvalue", "defaultvalue", "constantvalue", "literalvalue"], required=False)
    example_col = _choose_col(df, ["example", "sample", "default", "value"], required=False)

    select_parts: list[str] = []
    source_tables: list[str] = []
    alias_map: dict[str, str] = {}

    def add_table(table_name: str) -> None:
        tbl = _clean_table_name(table_name)
        if not tbl:
            return
        if _norm(tbl) in STATIC_MARKERS:
            return
        if tbl not in source_tables:
            source_tables.append(tbl)

    def ensure_table_aliases() -> None:
        alias_counts: dict[str, int] = {}
        alias_map.clear()
        for tbl in source_tables:
            base_alias = _table_alias(tbl)
            alias_counts[base_alias] = alias_counts.get(base_alias, 0) + 1
            alias = base_alias if alias_counts[base_alias] == 1 else f"{base_alias}{alias_counts[base_alias]}"
            alias_map[tbl] = alias

    table_frequency: dict[str, int] = {}
    for _, row in df.iterrows():
        tbl = _clean(row[source_table_col]) if source_table_col else ""
        if tbl and not _is_static(tbl, tbl):
            clean_tbl = _clean_table_name(tbl)
            table_frequency[clean_tbl] = table_frequency.get(clean_tbl, 0) + 1
    for tbl in table_frequency:
        add_table(tbl)

    base_table = max(table_frequency.items(), key=lambda item: item[1])[0] if table_frequency else ""

    for _, row in df.iterrows():
        source_table = _clean(row[source_table_col]) if source_table_col else ""
        source_field = _clean(row[source_field_col])
        transform_logic = _clean(row[transform_col]) if transform_col else ""
        if source_table:
            add_table(source_table)
        for ref_alias in _transform_alias_refs(transform_logic):
            if ref_alias == "terms":
                add_table("PaymentTerms_Mapping")
            elif ref_alias == "ship":
                add_table("[Company$Ship-to Address]")
            elif ref_alias == "cust":
                add_table("[Company$Customer]")
            elif ref_alias == "country":
                add_table("[Country/Region]")
            elif ref_alias == "vend":
                add_table("Vendor")
            elif ref_alias == "item":
                add_table("Item")
            elif ref_alias == "ar":
                add_table("AR")

    ensure_table_aliases()
    if not base_table and source_tables:
        base_table = source_tables[0]

    for _, row in df.iterrows():
        source_field = _clean(row[source_field_col])
        source_table = _clean(row[source_table_col]) if source_table_col else ""
        target_field = _clean(row[target_field_col])
        transform_logic = _clean(row[transform_col]) if transform_col else ""
        static_value = _clean(row[static_col]) if static_col else ""
        example_value = _clean(row[example_col]) if example_col else ""

        if not target_field:
            continue

        # Keep rows where source_field is mapped OR explicit static/transformation row.
        keep_row = (not _is_no_map(source_field)) or bool(transform_logic) or _is_static(source_table, source_field)
        if not keep_row:
            continue

        if source_table:
            add_table(source_table)

        ensure_table_aliases()

        expr = ""
        if transform_logic:
            expr = _normalize_transform(transform_logic, alias_map)
        elif example_value and _norm(source_field) == _norm(target_field):
            expr = _sql_literal(example_value)
        elif _is_static(source_table, source_field):
            static_raw = static_value if static_value else source_field
            expr = _sql_literal(static_raw)
        else:
            tbl_from_field, col = _split_table_col(source_field)
            effective_table = _clean_table_name(tbl_from_field or source_table or base_table)
            if not effective_table:
                # If table is missing and expression is direct column, keep it as-is.
                expr = col
            else:
                if effective_table not in alias_map:
                    add_table(effective_table)
                    ensure_table_aliases()
                expr = _source_ref(alias_map.get(effective_table, _table_alias(effective_table)), col)

        if expr:
            select_parts.append(f"    {expr} AS {_bracket(target_field)}")

    if not select_parts:
        raise ValueError("No mapped rows found after filtering rules were applied.")

    if not source_tables:
        from_clause = "FROM /* TODO: add source table */"
        join_clauses: list[str] = []
    else:
        # Keep stable order as first appearance in mapping.
        base_alias = alias_map[base_table]
        from_clause = f"FROM {_bracket(base_table)} {base_alias}"

        join_clauses = _build_join_clauses(df, alias_map, base_table)

    sql_lines = ["SELECT", ",\n".join(select_parts), from_clause]
    if join_clauses:
        sql_lines.extend(join_clauses)
    sql_text = "\n".join(sql_lines) + ";\n"

    output_file.write_text(sql_text, encoding="utf-8")
    return sql_text


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate SQL SELECT query from Excel mapping sheet.")
    parser.add_argument("mapping_file", type=Path, help="Path to mapping Excel file (.xlsx/.xls)")
    parser.add_argument("-o", "--output", type=Path, default=Path("generated_query.sql"), help="Output SQL file path")
    parser.add_argument("-s", "--sheet", type=str, default=None, help="Sheet name (default: auto-detect)")
    args = parser.parse_args()

    if not args.mapping_file.exists():
        raise FileNotFoundError(f"Mapping file not found: {args.mapping_file}")

    generate_sql(args.mapping_file, args.output, args.sheet)
    print(f"SQL generated: {args.output}")


if __name__ == "__main__":
    main()
