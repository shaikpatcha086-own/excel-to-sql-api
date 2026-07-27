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


def _choose_col(
    df: pd.DataFrame,
    candidates: list[str],
    required: bool = False,
    allow_fuzzy: bool = True,
) -> str | None:
    norm_to_real = {_norm(c): c for c in df.columns}
    for cand in candidates:
        if cand in norm_to_real:
            return norm_to_real[cand]

    if allow_fuzzy:
        # Fuzzy fallback for slight header variations, e.g. extra suffix/prefix text.
        # Ignore very short candidate tokens to avoid false positives like "on".
        fuzzy_matches: list[tuple[int, str]] = []
        for real_norm, real_col in norm_to_real.items():
            for cand in candidates:
                if not cand or len(cand) < 4:
                    continue
                if cand in real_norm or real_norm in cand:
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


def _choose_cols(df: pd.DataFrame, candidates: list[str], allow_fuzzy: bool = True) -> list[str]:
    """Return all matching columns for candidate normalized names, in candidate priority order."""
    norm_cols = [(_norm(c), c) for c in df.columns]
    matches: list[str] = []
    seen: set[str] = set()

    for cand in candidates:
        for real_norm, real_col in norm_cols:
            if real_norm == cand and real_col not in seen:
                matches.append(real_col)
                seen.add(real_col)

    if matches or not allow_fuzzy:
        return matches

    fuzzy_matches: list[tuple[int, str]] = []
    for real_norm, real_col in norm_cols:
        for cand in candidates:
            if not cand or len(cand) < 4:
                continue
            if cand in real_norm or real_norm in cand:
                fuzzy_matches.append((len(cand), real_col))
                break

    for _, real_col in sorted(fuzzy_matches, reverse=True):
        if real_col not in seen:
            matches.append(real_col)
            seen.add(real_col)
    return matches


def _first_non_empty(row: pd.Series, cols: list[str]) -> str:
    for col in cols:
        val = _clean(row.get(col, ""))
        if val:
            return val
    return ""


def _is_blank(val) -> bool:
    return pd.isna(val) or str(val).strip() == ""


def _clean(val) -> str:
    # Pandas may return a Series when duplicate column names exist.
    if isinstance(val, pd.Series):
        for item in val.tolist():
            cleaned = _clean(item)
            if cleaned:
                return cleaned
        return ""
    if isinstance(val, (list, tuple)):
        for item in val:
            cleaned = _clean(item)
            if cleaned:
                return cleaned
        return ""

    if pd.isna(val):
        return ""
    return str(val).strip()


def _unique_headers(headers: list[str]) -> list[str]:
    """Make duplicate headers unique while preserving the first occurrence as-is."""
    counts: dict[str, int] = {}
    unique: list[str] = []
    for h in headers:
        key = h.strip() if isinstance(h, str) else str(h)
        counts[key] = counts.get(key, 0) + 1
        if counts[key] == 1:
            unique.append(key)
        else:
            unique.append(f"{key}__{counts[key]}")
    return unique


def _is_static(source_table: str, source_field: str) -> bool:
    st = _norm(source_table)
    sf = _norm(source_field)
    return st in STATIC_MARKERS or sf in STATIC_MARKERS


def _is_no_map(source_field: str) -> bool:
    return _norm(source_field) in NO_MAP_MARKERS


def _mapping_source_entity(mapping_source: str) -> str:
    """Extract source entity/table name from Mapping Source text."""
    value = _clean(mapping_source)
    if not value or _is_no_map(value):
        return ""

    alias_to_table = {
        "cust": "Company$Customer",
        "ship": "Company$Ship-to Address",
        "terms": "PaymentTerms_Mapping",
        "country": "Country/Region",
        "vend": "Vendor",
        "item": "Item",
        "ar": "AR",
    }

    raw = value.strip()
    token_match = re.match(r"^\s*(?:\[([^\]]+)\]|([A-Za-z_][A-Za-z0-9_$/-]*))(?:\s*\.|\s*$)", raw)
    if token_match:
        token = (token_match.group(1) or token_match.group(2) or "").strip()
        norm_token = _norm(token)
        if norm_token in alias_to_table:
            return alias_to_table[norm_token]
        return token

    if raw.lower() in alias_to_table:
        return alias_to_table[raw.lower()]

    return ""


def _effective_source_table(source_table: str, mapping_source: str) -> str:
    """Use Table as source table; if blank, infer from Mapping Source when not NoMap."""
    table_val = _clean(source_table)
    if table_val:
        return table_val
    return _mapping_source_entity(mapping_source)


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
    # First prefer the sheet that looks like a mapping sheet by header content.
    best_sheet: str | None = None
    best_score = -1
    best_header_idx = 10**9
    score_markers = {
        "sourcefield",
        "fieldmappingsourcefield",
        "sourcetable",
        "fieldmappingsourcetable",
        "mappingsource",
        "targetfield",
        "target",
        "field",
        "table",
    }

    for sheet_name in xls.sheet_names:
        try:
            raw_df = pd.read_excel(xls, sheet_name=sheet_name, header=None, nrows=500)
        except Exception:
            continue

        header_idx = _detect_header_row(raw_df)
        if header_idx is None:
            continue

        row_values = {_norm(v) for v in raw_df.iloc[header_idx].tolist() if not _is_blank(v)}
        score = len(row_values & score_markers)
        if score > best_score or (score == best_score and header_idx < best_header_idx):
            best_sheet = sheet_name
            best_score = score
            best_header_idx = header_idx

    if best_sheet:
        return best_sheet

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
    source_markers = {
        "sourcefield",
        "source",
        "sourcecolumn",
        "sourcefieldname",
        "fieldmappingsourcefield",
        "mappingsource",
        "table",
        "field",
    }
    source_system_triplet = {"table", "field", "mappingsource"}
    fallback_source_only_row: int | None = None

    # Scan deeper because many templates include title/banner rows before headers.
    scan_limit = min(500, len(raw_df))
    for i, row in raw_df.head(scan_limit).iterrows():
        values = {_norm(v) for v in row.tolist() if not _is_blank(v)}
        if any(marker in values for marker in source_markers) and any(marker in values for marker in target_markers):
            return int(i)
        # Fallback for SOURCE SYSTEM style sheets where only source headers
        # are present in the detected header row (e.g., Table/Field/Mapping Source).
        if fallback_source_only_row is None and source_system_triplet.issubset(values):
            fallback_source_only_row = int(i)
        elif fallback_source_only_row is None and {"table", "field"}.issubset(values):
            fallback_source_only_row = int(i)

    if fallback_source_only_row is not None:
        return fallback_source_only_row
    return None


def _as_typed_header(val, idx: int) -> str:
    if _is_blank(val):
        return f"Unnamed: {idx}"
    return str(val).strip()


def _read_mapping_df(xls: pd.ExcelFile, sheet_name: str) -> pd.DataFrame:
    raw_df = pd.read_excel(xls, sheet_name=sheet_name, header=None)
    header_idx = _detect_header_row(raw_df)
    if header_idx is None:
        data_df = pd.read_excel(xls, sheet_name=sheet_name)
        data_df.columns = _unique_headers([str(c) for c in data_df.columns])
        return data_df

    header_vals = [_as_typed_header(v, i) for i, v in enumerate(raw_df.iloc[header_idx].tolist())]
    data_df = raw_df.iloc[header_idx + 1 :].copy()
    data_df.columns = _unique_headers(header_vals)
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
        allow_fuzzy=False,
    )

    source_table_cols = _choose_cols(
        df,
        ["sourcetable", "src_table", "fieldmappingsourcetable", "table"],
        allow_fuzzy=True,
    )
    mapping_source_cols = _choose_cols(
        df,
        ["mappingsource", "fieldmappingsource", "mapping_source"],
        allow_fuzzy=True,
    )

    def is_join_condition(expr: str) -> bool:
        e = _clean(expr)
        if not e:
            return False
        u = e.upper()
        if u.startswith("CASE ") or " THEN " in u:
            return False
        if "=" not in e:
            return False

        refs = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\.", e)
        if len(set(refs)) < 2:
            return False
        return True

    if join_col:
        raw_joins: list[str] = []
        for _, row in df.iterrows():
            join_expr = _clean(row.get(join_col, ""))
            raw_source_table = _first_non_empty(row, source_table_cols)
            mapping_source_value = _first_non_empty(row, mapping_source_cols)
            source_table = _effective_source_table(raw_source_table, mapping_source_value)
            clean_source_table = _clean_table_name(source_table)

            if not join_expr or not clean_source_table:
                continue
            if _norm(clean_source_table) in STATIC_MARKERS or clean_source_table == base_table:
                continue
            if clean_source_table not in table_aliases:
                continue
            if not is_join_condition(join_expr):
                continue

            condition = _normalize_transform(join_expr, alias_map=table_aliases)
            condition = re.sub(r"^\s*ON\s+", "", condition, flags=re.IGNORECASE)
            join_line = f"LEFT JOIN {_bracket(clean_source_table)} {table_aliases[clean_source_table]} ON {condition}"
            raw_joins.append(join_line)

        if raw_joins:
            unique_joins: list[str] = []
            for j in raw_joins:
                if j not in unique_joins:
                    unique_joins.append(j)
            return unique_joins

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
        else:
            joins.append(
                f"LEFT JOIN {_bracket(clean_tbl)} {alias} ON 1=1 /* TODO: define join condition between {base_alias} and {alias} */"
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


def _collect_table_fields(
    df: pd.DataFrame,
    table_cols: list[str],
    source_cols: list[str],
    mapping_source_cols: list[str],
) -> dict[str, list[str]]:
    table_fields: dict[str, list[str]] = {}
    for _, row in df.iterrows():
        raw_tbl = _first_non_empty(row, table_cols)
        mapping_source_value = _first_non_empty(row, mapping_source_cols)
        tbl = _effective_source_table(raw_tbl, mapping_source_value)
        src = _first_non_empty(row, source_cols)
        if not src or _is_no_map(src) or _is_static(tbl, src):
            continue
        clean_tbl = _clean_table_name(tbl)
        if not clean_tbl:
            continue
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
    source_table_cols = _choose_cols(
        df,
        ["sourcetable", "src_table", "fieldmappingsourcetable", "table"],
        allow_fuzzy=True,
    )
    mapping_source_cols = _choose_cols(
        df,
        ["mappingsource", "fieldmappingsource", "mapping_source"],
        allow_fuzzy=True,
    )
    source_field_cols = _choose_cols(
        df,
        [
            "sourcefield",
            "source_column",
            "srccolumn",
            "source",
            "sourcefieldbelowd365",
            "d365sourcefield",
            "fieldmappingsourcefield",
            "field",
        ],
        allow_fuzzy=True,
    )
    if not source_field_cols:
        raise ValueError(
            "Missing required mapping column. "
            "Expected one of: ['sourcefield', 'source_column', 'srccolumn', 'source', "
            "'sourcefieldbelowd365', 'd365sourcefield', 'fieldmappingsourcefield', 'field']. "
            f"Available columns: {list(df.columns)}"
        )

    table_fields = _collect_table_fields(df, source_table_cols, source_field_cols, mapping_source_cols)
    base_fields = table_fields.get(base_table, [])
    other_fields = table_fields.get(other_table, [])

    other_norm = _norm(other_table)

    # Heuristics for common mapping tables when key columns are not explicitly mapped.
    if "ship" in other_norm or "shipto" in other_norm or "address" in other_norm:
        base_key = next(
            (
                bf
                for bf in base_fields
                if any(x in _field_tokens(bf) for x in {"no", "id", "customer", "account"})
            ),
            "No_",
        )
        return f"{other_alias}.[Customer No_] = {base_alias}.{_bracket(base_key)}"

    if "paymentterms" in other_norm or "terms" in other_norm:
        base_key = next(
            (
                bf
                for bf in base_fields
                if any(x in _field_tokens(bf) for x in {"payment", "term", "code"})
            ),
            "Payment Terms Code",
        )
        return f"{other_alias}.[NAV_Code] = {base_alias}.{_bracket(base_key)}"

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

    source_field_cols = _choose_cols(
        df,
        [
            "sourcefield",
            "source_column",
            "srccolumn",
            "source",
            "sourcefieldbelowd365",
            "d365sourcefield",
            "fieldmappingsourcefield",
            "field",
        ],
        allow_fuzzy=True,
    )
    if not source_field_cols:
        raise ValueError(
            "Missing required mapping column. "
            "Expected one of: ['sourcefield', 'source_column', 'srccolumn', 'source', "
            "'sourcefieldbelowd365', 'd365sourcefield', 'fieldmappingsourcefield', 'field']. "
            f"Available columns: {list(df.columns)}"
        )

    source_table_cols = _choose_cols(
        df,
        ["sourcetable", "src_table", "fieldmappingsourcetable", "table"],
        allow_fuzzy=True,
    )
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
    mapping_source_cols = _choose_cols(
        df,
        ["mappingsource", "fieldmappingsource", "mapping_source"],
        allow_fuzzy=True,
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
        raw_tbl = _first_non_empty(row, source_table_cols)
        src = _first_non_empty(row, source_field_cols)
        tgt = _clean(row[target_field_col])
        trn = _clean(row[transform_col]) if transform_col else ""
        mapping_source_value = _first_non_empty(row, mapping_source_cols)
        tbl = _effective_source_table(raw_tbl, mapping_source_value)
        keep_row = bool(tgt) and ((not _is_no_map(src)) or bool(trn) or _is_static(tbl, src))
        if tbl and keep_row and not _is_static(tbl, src):
            clean_tbl = _clean_table_name(tbl)
            table_frequency[clean_tbl] = table_frequency.get(clean_tbl, 0) + 1
    for tbl in table_frequency:
        add_table(tbl)

    base_table = max(table_frequency.items(), key=lambda item: item[1])[0] if table_frequency else ""

    for _, row in df.iterrows():
        raw_source_table = _first_non_empty(row, source_table_cols)
        mapping_source_value = _first_non_empty(row, mapping_source_cols)
        source_table = _effective_source_table(raw_source_table, mapping_source_value)
        source_field = _first_non_empty(row, source_field_cols)
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
        source_field = _first_non_empty(row, source_field_cols)
        raw_source_table = _first_non_empty(row, source_table_cols)
        mapping_source_value = _first_non_empty(row, mapping_source_cols)
        source_table = _effective_source_table(raw_source_table, mapping_source_value)
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

    if source_tables:
        used_aliases = set()
        for part in select_parts:
            used_aliases.update(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\.", part))

        filtered_tables: list[str] = []
        for tbl in source_tables:
            alias = alias_map.get(tbl, "")
            if tbl == base_table or (alias and alias in used_aliases):
                filtered_tables.append(tbl)

        if filtered_tables:
            source_tables = filtered_tables
            if base_table not in source_tables:
                base_table = source_tables[0]
            ensure_table_aliases()

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
