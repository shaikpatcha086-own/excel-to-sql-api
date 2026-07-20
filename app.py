import re
import tempfile
from pathlib import Path

import streamlit as st

from generate_sql_from_mapping import generate_sql, normalize_sql, review_sql


OUTPUT_DIR = Path("generated_sql")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _sanitize_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._")
    return cleaned or "generated_query"


def _infer_extension(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in {".xlsx", ".xls"}:
        return suffix
    return ".xlsx"


def main() -> None:
    st.set_page_config(page_title="Excel Mapping SQL Agent", page_icon="SQL", layout="wide")
    st.title("Excel Mapping to SQL")
    st.write("Upload your mapping Excel file and generate a ready-to-run SQL query.")

    uploaded_file = st.file_uploader("Upload mapping file", type=["xlsx", "xls"])
    sheet_name_input = st.text_input("Sheet name (optional)", value="")
    output_name_input = st.text_input("Output SQL name", value="generated_query.sql")

    generate_clicked = st.button("Generate SQL", type="primary")

    if generate_clicked:
        if uploaded_file is None:
            st.error("Please upload a mapping Excel file first.")
            return

        extension = _infer_extension(uploaded_file.name)
        safe_output_stem = _sanitize_filename(output_name_input or "generated_query")
        if safe_output_stem.lower().endswith(".sql"):
            safe_output_name = safe_output_stem
        else:
            safe_output_name = f"{safe_output_stem}.sql"

        output_path = OUTPUT_DIR / safe_output_name
        sheet_name = sheet_name_input.strip() or None

        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / f"mapping_input{extension}"
            input_path.write_bytes(uploaded_file.getbuffer())

            try:
                sql_text = generate_sql(input_path, output_path, sheet_name)
            except Exception as exc:
                st.error(f"Failed to build SQL from mapping: {exc}")
                return

        st.session_state["generated_sql"] = sql_text
        st.session_state["reviewed_sql"] = sql_text
        st.session_state["reviewed_sql_editor"] = sql_text
        st.session_state["output_path"] = str(output_path)
        st.session_state["output_name"] = safe_output_name

        st.success("SQL generated successfully.")

    if "generated_sql" in st.session_state:
        sql_text = st.session_state.get("generated_sql", "")
        reviewed_sql = st.session_state.get("reviewed_sql", sql_text)
        output_name = st.session_state.get("output_name", "generated_query.sql")
        output_path = st.session_state.get("output_path", str(OUTPUT_DIR / output_name))

        st.write(f"Saved file: {output_path}")

        with st.expander("SQL Review Layer", expanded=True):
            issues = review_sql(reviewed_sql)
            if issues and issues != ["No obvious syntax issues detected."]:
                for issue in issues:
                    st.warning(issue)
            else:
                st.success("No obvious syntax issues detected.")

            if st.button("Normalize SQL formatting"):
                normalized_sql = normalize_sql(reviewed_sql)
                st.session_state["reviewed_sql"] = normalized_sql
                st.session_state["reviewed_sql_editor"] = normalized_sql
                st.rerun()

            edited_sql = st.text_area(
                "Review and edit SQL before download",
                value=reviewed_sql,
                height=420,
                key="reviewed_sql_editor",
            )

            st.session_state["reviewed_sql"] = edited_sql

        Path(output_path).write_text(st.session_state["reviewed_sql"], encoding="utf-8")

        st.download_button(
            label="Download reviewed SQL file",
            data=st.session_state["reviewed_sql"],
            file_name=output_name,
            mime="text/sql",
        )


if __name__ == "__main__":
    main()
