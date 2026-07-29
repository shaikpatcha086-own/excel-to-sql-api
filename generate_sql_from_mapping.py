import argparse
import json
import os
import re
from pathlib import Path

import pandas as pd
import requests


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
KEY_HINT_TOKENS = {"id", "no", "code", "key", "account", "acct", "number", "num", "client"}
NON_KEY_TOKENS = {
    "address",
    "city",
    "country",
    "region",
    "county",
    "name",
    "description",
    "email",
    "phone",
    "contact",
    "primary",
    "language",
    "priority",
    "payment",
    "terms",
    "method",
    "service",
    "group",
}
NON_JOIN_ID_TOKENS = {
    "tax",
    "vat",
    "salestax",
    "payment",
    "terms",
    "method",
    "service",
    "group",
    "priority",
    "language",
}


def _llm_enabled() -> bool:
    return os.getenv("ENABLE_LLM_JOIN_REASONING", "false").strip().lower() in {"1", "true", "yes", "on"}


def _llm_headers() -> dict[str, str] | None:
    api_key = os.getenv("LLM_API_KEY", "").strip()
    if not api_key:
        return None

    api_type = os.getenv("LLM_API_TYPE", "openai").strip().lower()
    if api_type == "azure":
        return {
            "Content-Type": "application/json",
            "api-key": api_key,
        }
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }


def _validate_join_condition(
    condition: str,
    base_alias: str,
    other_alias: str,
    base_fields: list[str],
    other_fields: list[str],
) -> str | None:
    cond = (condition or "").strip()
    if not cond or "=" not in cond:
        return None

    cond = re.sub(r"^\s*ON\s+", "", cond, flags=re.IGNORECASE).strip()
    pattern = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\.\[([^\]]+)\]\s*=\s*([A-Za-z_][A-Za-z0-9_]*)\.\[([^\]]+)\]\s*$")
    m = pattern.match(cond)
    if not m:
        return None

    left_alias, left_field, right_alias, right_field = m.groups()
    base_norm = {_norm(f): f for f in base_fields}
    other_norm = {_norm(f): f for f in other_fields}

    def ok_pair(a1: str, f1: str, a2: str, f2: str) -> bool:
        if a1 == other_alias and a2 == base_alias:
            return _norm(f1) in other_norm and _norm(f2) in base_norm
        if a1 == base_alias and a2 == other_alias:
            return _norm(f1) in base_norm and _norm(f2) in other_norm
        return False

    if not ok_pair(left_alias, left_field, right_alias, right_field):
        return None

    # Normalize order as other_alias = base_alias
    if left_alias == other_alias and right_alias == base_alias:
        return f"{other_alias}.{_bracket(left_field)} = {base_alias}.{_bracket(right_field)}"
    return f"{other_alias}.{_bracket(right_field)} = {base_alias}.{_bracket(left_field)}"


def _llm_suggest_join_condition(
    base_table: str,
    base_alias: str,
    other_table: str,
    other_alias: str,
    base_fields: list[str],
    other_fields: list[str],
) -> str | None:
    if not _llm_enabled():
        return None

    api_url = os.getenv("LLM_API_URL", "https://api.openai.com/v1/chat/completions").strip()
    model = os.getenv("LLM_MODEL", "gpt-4o-mini").strip()
    headers = _llm_headers()
    if not api_url or not model or headers is None:
        return None

    prompt = {
        "task": "Choose best SQL join condition between two tables.",
        "rules": [
            "Return exactly one condition using aliases and bracketed field names.",
            "Use format: <other_alias>.[field] = <base_alias>.[field]",
            "Prefer entity identity keys and avoid tax/payment/group/language/priority fields.",
            "If uncertain, return empty string.",
        ],
        "base_table": base_table,
        "base_alias": base_alias,
        "base_fields": base_fields,
        "other_table": other_table,
        "other_alias": other_alias,
        "other_fields": other_fields,
        "output": {"condition": "string"},
    }

    body = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": "You are a strict SQL join planner."},
            {"role": "user", "content": json.dumps(prompt)},
        ],
    }

    try:
        resp = requests.post(api_url, headers=headers, json=body, timeout=30)
        if resp.status_code >= 400:
            return None
        payload = resp.json()
        content = payload.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not content:
            return None
        parsed = json.loads(content)
        raw_condition = _clean(parsed.get("condition", ""))
        return _validate_join_condition(raw_condition, base_alias, other_alias, base_fields, other_fields)
    except Exception:
        return None
KEY_SUFFIXES = ("id", "code", "no", "number", "num", "key", "account", "acct", "ref", "reference")
GENERIC_KEY_FAMILIES = {"id", "code", "no", "number", "num", "key", "account", "acct", "ref", "reference"}
GENERIC_JOIN_TOKENS = {
    "id",
    "code",
    "no",
    "number",
    "num",
    "key",
    "account",
    "acct",
    "ref",
    "reference",
    "client",
    "customer",
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


def _norm_header_key(s: str) -> str:
    # Treat duplicate-suffixed headers like Field__2 as Field for matching.
    base = re.sub(r"__\d+$", "", str(s).strip())
    return _norm(base)


def _choose_col(
    df: pd.DataFrame,
    candidates: list[str],
    required: bool = False,
    allow_fuzzy: bool = True,
) -> str | None:
    norm_to_real: dict[str, str] = {}
    for c in df.columns:
        key = _norm_header_key(c)
        # Keep the first occurrence to preserve left-to-right precedence.
        if key not in norm_to_real:
            norm_to_real[key] = c
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
    norm_cols = [(_norm_header_key(c), c) for c in df.columns]
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


def _column_index_map(df: pd.DataFrame) -> dict[str, int]:
    return {str(col): idx for idx, col in enumerate(df.columns)}


def _prioritize_source_field_cols(
    df: pd.DataFrame,
    source_field_cols: list[str],
    source_table_cols: list[str],
    mapping_source_cols: list[str],
) -> list[str]:
    """
    Prefer explicit source-field headers first.
    For generic 'Field' duplicates, prioritize the one nearest source side
    columns (Table / Mapping Source).
    """
    if not source_field_cols:
        return source_field_cols

    explicit: list[str] = []
    generic: list[str] = []
    for col in source_field_cols:
        key = _norm_header_key(col)
        if key == "field":
            generic.append(col)
        else:
            explicit.append(col)

    if len(generic) <= 1:
        return explicit + generic

    idx_map = _column_index_map(df)
    anchor_cols = [c for c in (source_table_cols + mapping_source_cols) if c in idx_map]
    if not anchor_cols:
        return explicit + generic

    anchor_idx = [idx_map[c] for c in anchor_cols]

    def distance(col: str) -> tuple[int, int]:
        ci = idx_map.get(col, 10**9)
        d = min(abs(ci - ai) for ai in anchor_idx)
        return (d, ci)

    generic_sorted = sorted(generic, key=distance)
    return explicit + generic_sorted


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


def _mapping_source_entity(mapping_source: str, source_field: str = "") -> str:
    """Extract source entity/table name from Mapping Source text without hardcoded aliases."""
    value = _clean(mapping_source)
    if not value or _is_no_map(value):
        return ""
    raw = value.strip()
    # Pattern: Entity.Column or [Entity].Column
    token_match = re.match(r"^\s*(?:\[([^\]]+)\]|([A-Za-z_][A-Za-z0-9_$/\- ]*))\s*\.", raw)
    if token_match:
        token = (token_match.group(1) or token_match.group(2) or "").strip()
        if token and _norm(token) != _norm(source_field):
            return token

    # Pattern: standalone entity name in Mapping Source (e.g., Customer)
    standalone = raw[1:-1].strip() if raw.startswith("[") and raw.endswith("]") else raw
    if not standalone:
        return ""

    if _norm(standalone) == _norm(source_field):
        return ""

    # Skip obvious SQL expressions and function-like text.
    if re.search(r"\b(case|when|then|else|end|select|from|join|where|cast|convert)\b", standalone, re.IGNORECASE):
        return ""
    if re.search(r"[=+*/'()]", standalone):
        return ""

    # Accept text that looks like an entity token (letters/digits/_/$/- and spaces).
    if re.fullmatch(r"[A-Za-z0-9_$/\- ]+", standalone):
        return standalone.strip()

    return ""


def _mapping_source_logic_expr(mapping_source: str, source_field: str = "") -> str:
    """Treat Mapping Source as transformation logic when it looks like an expression."""
    value = _clean(mapping_source)
    if not value or _is_no_map(value):
        return ""

    raw = value.strip()
    # Pure entity token is for source-table inference, not expression use.
    if re.fullmatch(r"\[?[A-Za-z0-9_$/\- ]+\]?", raw):
        if _mapping_source_entity(raw, source_field):
            return ""

    # SQL logic markers (CASE statements, functions, predicates, operators, table.field refs).
    if re.search(r"\b(case|when|then|else|end|coalesce|isnull|cast|convert|iif)\b", raw, re.IGNORECASE):
        return raw
    if re.search(r"[=+*/'()]", raw):
        return raw
    if re.search(r"\b[A-Za-z_][A-Za-z0-9_]*\s*\.\s*\[?[A-Za-z0-9_ #$/\-]+\]?", raw):
        return raw

    return ""


def _effective_source_table(source_table: str, mapping_source: str, source_field: str = "") -> str:
    """Use Table as source table; if blank, infer from Mapping Source when not NoMap."""
    table_val = _clean(source_table)
    if table_val:
        return table_val
    return _mapping_source_entity(mapping_source, source_field)


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
    # Expand composite key tokens like clientid -> client + id.
    expanded = set(tokens)
    for tok in list(tokens):
        for suffix in KEY_SUFFIXES:
            if tok.endswith(suffix) and len(tok) > len(suffix):
                expanded.add(suffix)
                expanded.add(tok[: -len(suffix)])
    tokens = expanded
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
            source_field = _first_non_empty(row, source_field_cols)
            source_table = _effective_source_table(raw_source_table, mapping_source_value, source_field)
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
        score += 8
    if tokens & NON_KEY_TOKENS:
        score -= 3
    if len(tokens) <= 3:
        score += 1
    return score


def _is_likely_key_field(field_name: str) -> bool:
    tokens = _field_tokens(field_name)
    if not tokens:
        return False
    if tokens & KEY_HINT_TOKENS:
        return True
    merged = _norm(field_name)
    return any(hint in merged for hint in KEY_HINT_TOKENS)


def _is_strong_join_key(field_name: str) -> bool:
    """Stricter key signal used only for inferred join conditions."""
    tokens = _field_tokens(field_name)
    merged = _norm(field_name)
    if not _is_likely_key_field(field_name):
        return False
    if tokens & NON_KEY_TOKENS:
        return False
    # Reject business IDs that are clearly non-join dimensions like payment terms/group IDs.
    if any(nk in merged for nk in NON_KEY_TOKENS):
        return False
    # Explicit strong suffix/pattern markers.
    if any(merged.endswith(sfx) for sfx in KEY_SUFFIXES):
        return True
    return any(h in merged for h in {"id", "no", "code", "account", "key", "ref"})


def _canonical_join_key(field_name: str) -> str:
    """Build a comparable key signature for generic join-key matching."""
    merged = _norm(field_name)
    if not merged:
        return ""

    # Remove common key suffixes repeatedly (e.g., customerid, customercode).
    changed = True
    while changed:
        changed = False
        for suffix in KEY_SUFFIXES:
            if merged.endswith(suffix) and len(merged) > len(suffix):
                merged = merged[: -len(suffix)]
                changed = True

    # Remove repeated trailing digits if present in legacy column names.
    merged = re.sub(r"\d+$", "", merged)
    return merged or _norm(field_name)


def _join_semantic_tokens(field_name: str) -> set[str]:
    tokens = _field_tokens(field_name)
    return {t for t in tokens if t and t not in GENERIC_JOIN_TOKENS and t not in NON_KEY_TOKENS}


def _is_related_join_key_pair(left_field: str, right_field: str) -> bool:
    left_norm = _norm(left_field)
    right_norm = _norm(right_field)
    if left_norm == right_norm:
        return True

    left_key = _canonical_join_key(left_field)
    right_key = _canonical_join_key(right_field)
    if left_key and right_key and left_key == right_key:
        return True

    # Fallback: semantic overlap on non-generic tokens (e.g., customer/account).
    return bool(_join_semantic_tokens(left_field) & _join_semantic_tokens(right_field))


def _table_entity_family(table_name: str) -> str:
    norm_tbl = _norm(_clean_table_name(table_name))
    for family in ("customer", "vendor", "item", "account", "invoice", "order"):
        if family in norm_tbl:
            return family
    return ""


def _is_generic_key_field(field_name: str) -> bool:
    canon = _canonical_join_key(field_name)
    raw = _norm(field_name)
    return canon in GENERIC_KEY_FAMILIES or raw in GENERIC_KEY_FAMILIES


def _best_specific_key_field(fields: list[str], entity_family: str) -> str | None:
    candidates = [f for f in fields if _is_strong_join_key(f) and not _is_generic_key_field(f)]
    if not candidates:
        return None

    def score(field_name: str) -> int:
        s = _field_score(field_name, set())
        norm_field = _norm(field_name)
        if entity_family and entity_family in norm_field:
            s += 8
        if "id" in norm_field:
            s += 6
        return s

    return sorted(candidates, key=score, reverse=True)[0]


def _best_entity_id_key_field(fields: list[str], entity_family: str) -> str | None:
    """Prefer specific keys that explicitly look like entity IDs (e.g., ClientID)."""
    candidates = [f for f in fields if _is_strong_join_key(f) and not _is_generic_key_field(f)]
    if not candidates:
        return None

    scored: list[tuple[int, str]] = []
    for f in candidates:
        n = _norm(f)
        toks = _field_tokens(f)

        # Reject non-join business IDs such as SalesTaxId, PaymentTermsId, etc.
        if toks & NON_JOIN_ID_TOKENS:
            continue
        if any(tok in n for tok in NON_JOIN_ID_TOKENS):
            continue

        # Only allow fields that look tied to the entity identity.
        entity_tokens = {entity_family} if entity_family else set()
        entity_tokens |= {"client", "customer", "vendor", "item", "account"}
        has_entity_affinity = bool(toks & entity_tokens) or any(tok in n for tok in entity_tokens)

        if "id" not in n or not has_entity_affinity:
            continue

        s = _field_score(f, set())
        if "id" in n:
            s += 12
        if entity_family and entity_family in n:
            s += 10
        # De-prioritize account-like business keys when an entity ID exists.
        if "account" in n and "id" not in n:
            s -= 4
        scored.append((s, f))

    if not scored:
        return None

    scored.sort(reverse=True)
    top_score, top_field = scored[0]
    if top_score >= 10:
        return top_field
    return None


def _best_generic_key_field(fields: list[str]) -> str | None:
    candidates = [f for f in fields if _is_strong_join_key(f) and _is_generic_key_field(f)]
    if not candidates:
        return None

    # Prefer classic PK-like generic names first.
    priority = ["no_", "no", "id", "code", "key", "account"]

    def score(field_name: str) -> tuple[int, int]:
        n = _norm(field_name)
        pri = 100
        for idx, token in enumerate(priority):
            if token in n:
                pri = idx
                break
        return (pri, -_field_score(field_name, set()))

    return sorted(candidates, key=score)[0]


def _base_table_score(
    table_name: str,
    row_count: int,
    mapped_fields: list[str],
    mapping_source_hits: int,
) -> int:
    score = row_count * 5
    key_fields = sum(1 for f in mapped_fields if _is_likely_key_field(f))
    score += key_fields * 8
    score += mapping_source_hits * 3

    # Prefer stable business entities as anchors over lookup-like tables.
    norm_tbl = _norm(table_name)
    if any(x in norm_tbl for x in {"customer", "vendor", "item", "account"}):
        score += 5
    if any(x in norm_tbl for x in {"mapping", "lookup", "reference"}):
        score -= 3
    return score


def _choose_base_table(
    table_frequency: dict[str, int],
    table_fields: dict[str, list[str]],
    mapping_source_hits: dict[str, int],
    explicit_table_frequency: dict[str, int] | None = None,
) -> str:
    if not table_frequency:
        return ""

    # First priority: tables explicitly present in the source Table column.
    if explicit_table_frequency:
        explicit_candidates = {k: v for k, v in explicit_table_frequency.items() if k in table_frequency}
        if explicit_candidates:
            explicit_best = ""
            explicit_best_score = -10**9
            for tbl, freq in explicit_candidates.items():
                score = _base_table_score(
                    table_name=tbl,
                    row_count=freq,
                    mapped_fields=table_fields.get(tbl, []),
                    mapping_source_hits=mapping_source_hits.get(tbl, 0),
                )
                # Boost explicit-table candidates so they always outrank inferred-only tables.
                score += 10**6
                if score > explicit_best_score:
                    explicit_best_score = score
                    explicit_best = tbl
            if explicit_best:
                return explicit_best

    best_table = ""
    best_score = -10**9
    for tbl, freq in table_frequency.items():
        score = _base_table_score(
            table_name=tbl,
            row_count=freq,
            mapped_fields=table_fields.get(tbl, []),
            mapping_source_hits=mapping_source_hits.get(tbl, 0),
        )
        if score > best_score:
            best_score = score
            best_table = tbl
    return best_table


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
        src = _first_non_empty(row, source_cols)
        tbl = _effective_source_table(raw_tbl, mapping_source_value, src)
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


def _lookup_table_fields(table_fields: dict[str, list[str]], table_name: str) -> list[str]:
    """Lookup table fields with tolerant table-name matching (spaces/underscores/case)."""
    if table_name in table_fields:
        return table_fields[table_name]

    target_key = _norm(_clean_table_name(table_name))
    for known_name, fields in table_fields.items():
        if _norm(_clean_table_name(known_name)) == target_key:
            return fields
    return []


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

    source_field_cols = _prioritize_source_field_cols(df, source_field_cols, source_table_cols, mapping_source_cols)

    table_fields = _collect_table_fields(df, source_table_cols, source_field_cols, mapping_source_cols)
    base_fields = _lookup_table_fields(table_fields, base_table)
    other_fields = _lookup_table_fields(table_fields, other_table)

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

    # Deterministic override: if there is exactly one common strong key family,
    # force that join and avoid heuristic ambiguity.
    base_family_fields: dict[str, list[str]] = {}
    for bf in base_fields:
        if not _is_strong_join_key(bf):
            continue
        fam = _canonical_join_key(bf)
        if not fam:
            continue
        base_family_fields.setdefault(fam, []).append(bf)

    other_family_fields: dict[str, list[str]] = {}
    for of in other_fields:
        if not _is_strong_join_key(of):
            continue
        fam = _canonical_join_key(of)
        if not fam:
            continue
        other_family_fields.setdefault(fam, []).append(of)

    common_families = set(base_family_fields.keys()) & set(other_family_fields.keys())
    if len(common_families) == 1:
        family = next(iter(common_families))
        base_candidates = base_family_fields[family]
        other_candidates = other_family_fields[family]

        # Prefer exact normalized-name match within the common family.
        matched_pair: tuple[str, str] | None = None
        for bf in base_candidates:
            for of in other_candidates:
                if _norm(bf) == _norm(of):
                    matched_pair = (bf, of)
                    break
            if matched_pair:
                break

        if matched_pair is None:
            # Otherwise pick the strongest key-like columns in that family.
            bf = sorted(base_candidates, key=lambda x: _field_score(x, set()), reverse=True)[0]
            of = sorted(other_candidates, key=lambda x: _field_score(x, set()), reverse=True)[0]
            matched_pair = (bf, of)

        bf, of = matched_pair
        return f"{other_alias}.{_bracket(of)} = {base_alias}.{_bracket(bf)}"

    # Same-entity override: allow generic key on one side to match specific key on the other
    # when both tables represent the same business entity (e.g., Customer No <-> ClientID).
    if _table_entity_family(base_table) and _table_entity_family(base_table) == _table_entity_family(other_table):
        entity_family = _table_entity_family(base_table)
        base_generic = [bf for bf in base_fields if _is_strong_join_key(bf) and _is_generic_key_field(bf)]
        other_generic = [of for of in other_fields if _is_strong_join_key(of) and _is_generic_key_field(of)]
        base_specific = [bf for bf in base_fields if _is_strong_join_key(bf) and not _is_generic_key_field(bf)]
        other_specific = [of for of in other_fields if _is_strong_join_key(of) and not _is_generic_key_field(of)]

        best_pair: tuple[int, str, str] | None = None

        for bf in base_generic:
            for of in other_specific:
                score = _field_score(bf, set()) + _field_score(of, set()) + 22
                if "id" in _norm(of):
                    score += 6
                if best_pair is None or score > best_pair[0]:
                    best_pair = (score, bf, of)

        for bf in base_specific:
            for of in other_generic:
                score = _field_score(bf, set()) + _field_score(of, set()) + 22
                if "id" in _norm(bf):
                    score += 6
                if best_pair is None or score > best_pair[0]:
                    best_pair = (score, bf, of)

        if best_pair is not None:
            _, bf, of = best_pair
            return f"{other_alias}.{_bracket(of)} = {base_alias}.{_bracket(bf)}"

        # If mapped fields are incomplete, pair a specific key from one side with a
        # generic key from the other side (or default generic key label).
        base_best_specific = _best_specific_key_field(base_fields, entity_family)
        other_best_specific = _best_specific_key_field(other_fields, entity_family)
        base_best_generic = _best_generic_key_field(base_fields)
        other_best_generic = _best_generic_key_field(other_fields)

        if base_best_specific and (other_best_generic or not other_best_specific):
            bf = base_best_specific
            of = other_best_generic or "No_"
            return f"{other_alias}.{_bracket(of)} = {base_alias}.{_bracket(bf)}"

        if other_best_specific and (base_best_generic or not base_best_specific):
            bf = base_best_generic or "No_"
            of = other_best_specific
            return f"{other_alias}.{_bracket(of)} = {base_alias}.{_bracket(bf)}"

        # Final same-entity fallback: if no related pair was found, bind entity-ID
        # style key from one side to default generic key on the other side.
        base_entity_id = _best_entity_id_key_field(base_fields, entity_family)
        other_entity_id = _best_entity_id_key_field(other_fields, entity_family)

        if other_entity_id:
            return f"{other_alias}.{_bracket(other_entity_id)} = {base_alias}.[No_]"
        if base_entity_id:
            return f"{other_alias}.[No_] = {base_alias}.{_bracket(base_entity_id)}"

    if not base_fields or not other_fields:
        return None

    # Prefer exact/canonical same key names first (generic, not hardcoded to one field).
    exact_key_pairs: list[tuple[int, str, str]] = []
    for bf in base_fields:
        for of in other_fields:
            if not (_is_strong_join_key(bf) and _is_strong_join_key(of)):
                continue

            bf_norm = _norm(bf)
            of_norm = _norm(of)
            bf_key = _canonical_join_key(bf)
            of_key = _canonical_join_key(of)

            same_name = bf_norm == of_norm
            same_canonical = bool(bf_key) and bf_key == of_key
            if not (same_name or same_canonical):
                continue
            if not (_is_likely_key_field(bf) or _is_likely_key_field(of)):
                continue

            score = 0
            if same_name:
                score += 20
            if same_canonical:
                score += 14
            merged = bf_norm
            if "id" in merged:
                score += 20
            score += _field_score(bf, set()) + _field_score(of, set())
            exact_key_pairs.append((score, bf, of))

    if exact_key_pairs:
        exact_key_pairs.sort(reverse=True)
        _, bf, of = exact_key_pairs[0]
        return f"{other_alias}.{_bracket(of)} = {base_alias}.{_bracket(bf)}"

    # Second pass: related key-family matches (e.g., CustomerID <-> Customer No).
    related_key_pairs: list[tuple[int, str, str]] = []
    for bf in base_fields:
        for of in other_fields:
            if not (_is_strong_join_key(bf) and _is_strong_join_key(of)):
                continue
            if not _is_related_join_key_pair(bf, of):
                continue

            score = _field_score(bf, set()) + _field_score(of, set()) + 18
            if "id" in _norm(bf) and "id" in _norm(of):
                score += 10
            related_key_pairs.append((score, bf, of))

    if related_key_pairs:
        related_key_pairs.sort(reverse=True)
        _, bf, of = related_key_pairs[0]
        return f"{other_alias}.{_bracket(of)} = {base_alias}.{_bracket(bf)}"

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
        if not _is_strong_join_key(bf):
            continue
        base_score = _field_score(bf, other_tokens)
        for of in other_fields:
            if not _is_strong_join_key(of):
                continue
            other_score = _field_score(of, base_tokens)
            bf_is_key = _is_likely_key_field(bf)
            of_is_key = _is_likely_key_field(of)

            # Avoid joins that are key-like but clearly unrelated key families.
            if bf_is_key and of_is_key and not _is_related_join_key_pair(bf, of):
                continue

            score = base_score + other_score
            same_norm = _norm(bf) == _norm(of)
            same_canonical = _canonical_join_key(bf) and _canonical_join_key(bf) == _canonical_join_key(of)
            semantic_overlap = _join_semantic_tokens(bf) & _join_semantic_tokens(of)

            if _norm(bf) == _norm(of):
                score += 10
                if _is_likely_key_field(bf) and _is_likely_key_field(of):
                    score += 25
                if "id" in _norm(bf):
                    score += 20
            if bf.lower() == of.lower():
                score += 5
            if bf_is_key and of_is_key:
                score += 8
                # Penalize mismatched key families such as ClientID vs PaymentTermsId.
                if not same_norm and not same_canonical and not semantic_overlap:
                    score -= 30
            if (_field_tokens(bf) & NON_KEY_TOKENS) or (_field_tokens(of) & NON_KEY_TOKENS):
                score -= 6
            if any(tok in _field_tokens(bf) for tok in other_tokens):
                score += 4
            if any(tok in _field_tokens(of) for tok in base_tokens):
                score += 4
            if best is None or score > best[0]:
                best = (score, bf, of)

    if best and best[0] >= 10:
        _, bf, of = best
        return f"{other_alias}.{_bracket(of)} = {base_alias}.{_bracket(bf)}"

    # Optional LLM fallback for ambiguous join cases.
    llm_condition = _llm_suggest_join_condition(
        base_table=base_table,
        base_alias=base_alias,
        other_table=other_table,
        other_alias=other_alias,
        base_fields=base_fields,
        other_fields=other_fields,
    )
    if llm_condition:
        return llm_condition

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
    source_field_cols = _prioritize_source_field_cols(df, source_field_cols, source_table_cols, mapping_source_cols)
    static_col = _choose_col(df, ["staticvalue", "defaultvalue", "constantvalue", "literalvalue"], required=False)

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
    explicit_table_frequency: dict[str, int] = {}
    table_fields_seen: dict[str, list[str]] = {}
    mapping_source_hits: dict[str, int] = {}
    for _, row in df.iterrows():
        raw_tbl = _first_non_empty(row, source_table_cols)
        src = _first_non_empty(row, source_field_cols)
        tgt = _clean(row[target_field_col])
        mapping_source_value = _first_non_empty(row, mapping_source_cols)
        trn = _clean(row[transform_col]) if transform_col else ""
        mapping_expr = _mapping_source_logic_expr(mapping_source_value, src)
        effective_trn = trn or mapping_expr
        tbl = _effective_source_table(raw_tbl, mapping_source_value, src)
        keep_row = bool(tgt) and ((not _is_no_map(src)) or bool(effective_trn))
        if tbl and keep_row and not _is_static(tbl, src):
            clean_tbl = _clean_table_name(tbl)
            table_frequency[clean_tbl] = table_frequency.get(clean_tbl, 0) + 1

            explicit_tbl = _clean_table_name(raw_tbl)
            if explicit_tbl:
                explicit_table_frequency[explicit_tbl] = explicit_table_frequency.get(explicit_tbl, 0) + 1

            _, src_col = _split_table_col(src)
            if clean_tbl not in table_fields_seen:
                table_fields_seen[clean_tbl] = []
            if src_col and src_col not in table_fields_seen[clean_tbl]:
                table_fields_seen[clean_tbl].append(src_col)

            mapped_entity = _mapping_source_entity(mapping_source_value, src)
            if mapped_entity and _norm(_clean_table_name(mapped_entity)) == _norm(clean_tbl):
                mapping_source_hits[clean_tbl] = mapping_source_hits.get(clean_tbl, 0) + 1
    for tbl in table_frequency:
        add_table(tbl)

    base_table = _choose_base_table(
        table_frequency,
        table_fields_seen,
        mapping_source_hits,
        explicit_table_frequency=explicit_table_frequency,
    )

    for _, row in df.iterrows():
        raw_source_table = _first_non_empty(row, source_table_cols)
        mapping_source_value = _first_non_empty(row, mapping_source_cols)
        source_field = _first_non_empty(row, source_field_cols)
        source_table = _effective_source_table(raw_source_table, mapping_source_value, source_field)
        transform_logic = _clean(row[transform_col]) if transform_col else ""
        mapping_expr = _mapping_source_logic_expr(mapping_source_value, source_field)
        effective_transform = transform_logic or mapping_expr
        if source_table:
            add_table(source_table)
        for ref_alias in _transform_alias_refs(effective_transform):
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
        source_table = _effective_source_table(raw_source_table, mapping_source_value, source_field)
        target_field = _clean(row[target_field_col])
        transform_logic = _clean(row[transform_col]) if transform_col else ""
        mapping_expr = _mapping_source_logic_expr(mapping_source_value, source_field)
        effective_transform = transform_logic or mapping_expr
        static_value = _clean(row[static_col]) if static_col else ""

        if not target_field:
            continue

        # Keep rows only when source field is mapped (source Field <> NoMap).
        keep_row = (not _is_no_map(source_field)) or bool(effective_transform)
        if not keep_row:
            continue

        if source_table:
            add_table(source_table)

        ensure_table_aliases()

        expr = ""
        if effective_transform:
            expr = _normalize_transform(effective_transform, alias_map)
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
