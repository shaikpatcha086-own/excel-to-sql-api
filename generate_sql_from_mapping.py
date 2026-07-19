import argparse
import re
from pathlib import Path

import pandas as pd


STATIC_MARKERS = {"static", "const", "constant", "literal", "hardcoded", "hard-coded"}
NO_MAP_MARKERS = {"nomap", "no_map", "no map", "na", "n/a", ""}


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


def _preferred_sheet_name(xls: pd.ExcelFile) -> str:
    preferred = ["mapping", "map", "field_mapping", "column_mapping"]
    lower_names = {name.lower(): name for name in xls.sheet_names}
    for p in preferred:
        if p in lower_names:
            return lower_names[p]
    return xls.sheet_names[0]


def _detect_header_row(raw_df: pd.DataFrame) -> int | None:
    target_markers = {"field", "targetfield", "target", "alias", "d365field", "fieldbelowd365"}
    for i, row in raw_df.head(60).iterrows():
        values = {_norm(v) for v in row.tolist() if not _is_blank(v)}
        if "sourcefield" in values and any(marker in values for marker in target_markers):
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
    join_col = _choose_col(
        df,
        [
            "joincondition",
            "joinlogic",
            "joinclause",
            "join",
            "relationship",
            "tablejoin",
            "join_key",
            "joinkey",
        ],
        required=False,
    )

    joins: list[str] = []
    used = set()
    base_alias = table_aliases[base_table]

    if join_col:
        for _, row in df.iterrows():
            jc = _clean(row.get(join_col, ""))
            if not jc:
                continue

            src_table = _clean(row.get(_choose_col(df, ["sourcetable", "src_table", "table"], required=False) or "", ""))
            if not src_table or _norm(src_table) in STATIC_MARKERS:
                continue

            if src_table == base_table:
                continue

            ta = table_aliases[src_table]
            keyed = (src_table, jc)
            if keyed in used:
                continue

            condition = jc
            for tbl, alias in table_aliases.items():
                condition = re.sub(rf"\b{re.escape(tbl)}\.", f"{alias}.", condition)
            if not re.search(r"\b(on|where)\b", condition, re.IGNORECASE):
                joins.append(f"LEFT JOIN {src_table} {ta} ON {condition}")
            else:
                condition = re.sub(r"^\s*on\s+", "", condition, flags=re.IGNORECASE)
                joins.append(f"LEFT JOIN {src_table} {ta} ON {condition}")
            used.add(keyed)

    # Fallback: if no explicit join logic, attach all extra tables with placeholder comments.
    if not joins:
        for tbl, alias in table_aliases.items():
            if tbl == base_table:
                continue
            joins.append(
                f"LEFT JOIN {tbl} {alias} ON /* TODO: define join condition between {base_alias} and {alias} */ 1=1"
            )

    return joins


def _add_source_table(source_tables: list[str], table_name: str) -> None:
    tbl = _clean(table_name)
    if not tbl:
        return
    if _norm(tbl) in STATIC_MARKERS:
        return
    if tbl not in source_tables:
        source_tables.append(tbl)


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

    select_parts: list[str] = []
    source_tables: list[str] = []

    for _, row in df.iterrows():
        source_field = _clean(row[source_field_col])
        source_table = _clean(row[source_table_col]) if source_table_col else ""
        target_field = _clean(row[target_field_col])
        transform_logic = _clean(row[transform_col]) if transform_col else ""
        static_value = _clean(row[static_col]) if static_col else ""

        if not target_field:
            continue

        # Keep rows where source_field is mapped OR explicit static/transformation row.
        keep_row = (not _is_no_map(source_field)) or bool(transform_logic) or _is_static(source_table, source_field)
        if not keep_row:
            continue

        # Always track non-static source tables provided in mapping metadata.
        _add_source_table(source_tables, source_table)

        expr = ""
        if transform_logic:
            expr = transform_logic
        elif _is_static(source_table, source_field):
            static_raw = static_value if static_value else source_field
            expr = _sql_literal(static_raw)
        else:
            tbl_from_field, col = _split_table_col(source_field)
            effective_table = tbl_from_field or source_table
            if not effective_table:
                # If table is missing and expression is direct column, keep it as-is.
                expr = col
            else:
                expr = f"{effective_table}.{col}"
                _add_source_table(source_tables, effective_table)

        if expr:
            select_parts.append(f"    {expr} AS {target_field}")

    if not select_parts:
        raise ValueError("No mapped rows found after filtering rules were applied.")

    if not source_tables:
        from_clause = "FROM /* TODO: add source table */"
        join_clauses: list[str] = []
    else:
        # Keep stable order as first appearance in mapping.
        table_aliases = {tbl: f"t{i+1}" for i, tbl in enumerate(source_tables)}
        base_table = source_tables[0]
        from_clause = f"FROM {base_table} {table_aliases[base_table]}"

        # Replace table names in select expressions with aliases for readability.
        aliased_select_parts = []
        for item in select_parts:
            converted = item
            for tbl, alias in table_aliases.items():
                converted = re.sub(rf"\b{re.escape(tbl)}\.", f"{alias}.", converted)
            aliased_select_parts.append(converted)
        select_parts = aliased_select_parts

        join_clauses = _build_join_clauses(df, table_aliases, base_table)

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
