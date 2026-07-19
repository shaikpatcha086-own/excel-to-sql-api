import re
import tempfile
from pathlib import Path

import streamlit as st

from generate_sql_from_mapping import generate_sql


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

        st.success("SQL generated successfully.")
        st.write(f"Saved file: {output_path}")
        st.code(sql_text, language="sql")
        st.download_button(
            label="Download SQL file",
            data=sql_text,
            file_name=safe_output_name,
            mime="text/sql",
        )


if __name__ == "__main__":
    main()
