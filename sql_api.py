import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import requests
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from generate_sql_from_mapping import generate_sql


app = FastAPI(
    title="SQL Generator API",
    description="Generate SQL from Excel mapping files.",
    version="1.0.0",
)

OUTPUT_DIR = Path("generated_sql")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
 D365_LINK = os.getenv("D365_METADATA_URL", "https://d365-mapper-demo.onrender.com")


def _sanitize_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._")
    return cleaned or "generated_query"


def _final_output_name(raw_name: str | None) -> str:
    safe_output_stem = _sanitize_filename(raw_name or "generated_query")
    if safe_output_stem.lower().endswith(".sql"):
        return safe_output_stem
    return f"{safe_output_stem}.sql"


def _infer_extension(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in {".xlsx", ".xls"}:
        return suffix
    return ".xlsx"


def _download_excel(file_url: str, destination: Path) -> None:
    parsed = urlparse(file_url)
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="file_url must be an http/https URL")

    try:
        with requests.get(file_url, stream=True, timeout=60) as response:
            response.raise_for_status()
            with destination.open("wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
    except requests.RequestException as exc:
        raise HTTPException(status_code=400, detail=f"Failed to download file: {exc}") from exc


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/generate/upload")
async def generate_from_upload(
    mapping_file: UploadFile = File(...),
    sheet_name: str | None = Form(default=None),
    output_name: str | None = Form(default=None),
) -> dict[str, str]:
    if not mapping_file.filename:
        raise HTTPException(status_code=400, detail="Uploaded file has no filename")

    extension = _infer_extension(mapping_file.filename)
    safe_output_name = _final_output_name(output_name)
    output_path = OUTPUT_DIR / safe_output_name

    with tempfile.TemporaryDirectory() as temp_dir:
        input_path = Path(temp_dir) / f"mapping_input{extension}"
        content = await mapping_file.read()
        input_path.write_bytes(content)

        try:
            sql_text = generate_sql(input_path, output_path, sheet_name)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Failed to build SQL from mapping: {exc}") from exc

    return {
        "message": "SQL generated successfully",
        "output_file": str(output_path),
        "download_url": f"/download/{safe_output_name}",
        "sql": sql_text,
    }


@app.post("/generate/url")
def generate_from_url(
    file_url: str,
    sheet_name: str | None = None,
    output_name: str | None = None,
) -> dict[str, str]:
    extension = _infer_extension(file_url)
    safe_output_name = _final_output_name(output_name)
    output_path = OUTPUT_DIR / safe_output_name

    with tempfile.TemporaryDirectory() as temp_dir:
        input_path = Path(temp_dir) / f"mapping_input{extension}"
        _download_excel(file_url, input_path)

        try:
            sql_text = generate_sql(input_path, output_path, sheet_name)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Failed to build SQL from mapping: {exc}") from exc

    return {
        "message": "SQL generated successfully",
        "output_file": str(output_path),
        "download_url": f"/download/{safe_output_name}",
        "sql": sql_text,
    }


@app.get("/download/{filename}")
def download_sql(filename: str) -> FileResponse:
    safe_name = _sanitize_filename(filename)
    if not safe_name.lower().endswith(".sql"):
        safe_name = f"{safe_name}.sql"

    file_path = OUTPUT_DIR / safe_name
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="SQL file not found")

    return FileResponse(path=file_path, filename=safe_name, media_type="text/sql")


@app.get("/excel-to-sql", response_class=HTMLResponse)
def excel_to_sql_page(request: Request) -> HTMLResponse:
    base_url = str(request.base_url).rstrip("/")
    html = f"""
    <!doctype html>
    <html>
        <head>
            <meta charset="utf-8" />
            <meta name="viewport" content="width=device-width, initial-scale=1" />
            <title>Excel to SQL</title>
            <style>
                body {{ font-family: Segoe UI, sans-serif; margin: 0; background: #f4f7fb; color: #1f2d3d; }}
                .wrap {{ max-width: 900px; margin: 40px auto; padding: 0 20px 40px; }}
                .top {{ display: flex; justify-content: space-between; gap: 12px; align-items: center; margin-bottom: 20px; }}
                .home {{ text-decoration: none; color: #0b5fff; font-weight: 600; }}
                .panel {{ background: #fff; border-radius: 14px; padding: 22px; box-shadow: 0 2px 10px rgba(0,0,0,.06); }}
                .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 14px; }}
                label {{ display: block; font-weight: 600; margin-bottom: 6px; }}
                input {{ width: 100%; padding: 10px 12px; border: 1px solid #cfd8e3; border-radius: 8px; box-sizing: border-box; }}
                button {{ margin-top: 14px; background: #0b5fff; color: white; border: 0; padding: 11px 16px; border-radius: 8px; font-weight: 700; cursor: pointer; }}
                .muted {{ color: #5b6b7b; font-size: 14px; }}
                .result {{ margin-top: 18px; white-space: pre-wrap; background: #0b1220; color: #d7e7ff; padding: 14px; border-radius: 10px; min-height: 120px; }}
                .download {{ display: inline-block; margin-top: 10px; text-decoration: none; background: #14a44d; color: #fff; padding: 10px 14px; border-radius: 8px; font-weight: 600; }}
            </style>
        </head>
        <body>
            <div class="wrap">
                <div class="top">
                    <div>
                        <h1>Excel to SQL</h1>
                        <div class="muted">Upload your mapping Excel and generate SQL instantly.</div>
                    </div>
                    <a class="home" href="{base_url}/">Back to hub</a>
                </div>

                <div class="panel">
                    <form id="sqlForm">
                        <div class="grid">
                            <div>
                                <label>Excel file</label>
                                <input type="file" id="mapping_file" name="mapping_file" accept=".xlsx,.xls" required />
                            </div>
                            <div>
                                <label>Sheet name (optional)</label>
                                <input type="text" id="sheet_name" name="sheet_name" placeholder="Sheet2" />
                            </div>
                            <div>
                                <label>Output SQL file name</label>
                                <input type="text" id="output_name" name="output_name" value="generated_query.sql" />
                            </div>
                        </div>
                        <button type="submit">Generate SQL</button>
                    </form>

                    <div style="margin-top:16px;">
                        <div class="muted">Result</div>
                        <div id="result" class="result">Upload a file and click Generate SQL.</div>
                        <a id="downloadLink" class="download" href="#" style="display:none;">Download SQL</a>
                    </div>
                </div>
            </div>

            <script>
                const form = document.getElementById('sqlForm');
                const result = document.getElementById('result');
                const downloadLink = document.getElementById('downloadLink');

                form.addEventListener('submit', async (event) => {{
                    event.preventDefault();
                    result.textContent = 'Generating SQL...';
                    downloadLink.style.display = 'none';

                    const fileInput = document.getElementById('mapping_file');
                    const sheetName = document.getElementById('sheet_name').value;
                    const outputName = document.getElementById('output_name').value;

                    const formData = new FormData();
                    formData.append('mapping_file', fileInput.files[0]);
                    formData.append('sheet_name', sheetName);
                    formData.append('output_name', outputName);

                    try {{
                        const response = await fetch('{base_url}/generate/upload', {{
                            method: 'POST',
                            body: formData
                        }});
                        const data = await response.json();

                        if (!response.ok) {{
                            throw new Error(data.detail || 'Failed to generate SQL');
                        }}

                        result.textContent = data.sql || 'SQL generated successfully.';
                        downloadLink.href = data.download_url;
                        downloadLink.textContent = 'Download SQL';
                        downloadLink.style.display = 'inline-block';
                    }} catch (error) {{
                        result.textContent = 'Error: ' + error.message;
                    }}
                }});
            </script>
        </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    excel_sql_link = str(request.base_url).rstrip("/") + "/excel-to-sql"
    d365_link = D365_LINK.strip()

    d365_card = (
        f'<a class="btn" href="{d365_link}" target="_blank" rel="noopener">Open D365 API</a>'
        if d365_link
        else '<div class="small">Set D365_METADATA_URL in Render to link D365 docs.</div>'
    )

    html = f"""
    <!doctype html>
    <html>
        <head>
            <meta charset="utf-8" />
            <meta name="viewport" content="width=device-width, initial-scale=1" />
            <title>Data Integration Hub</title>
            <style>
                body {{ font-family: Segoe UI, sans-serif; margin: 0; background: #f4f7fb; }}
                .wrap {{ max-width: 840px; margin: 48px auto; padding: 0 20px; }}
                h1 {{ margin: 0 0 10px 0; }}
                p {{ color: #445; margin: 0 0 26px 0; }}
                .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 16px; }}
                .card {{ background: #fff; border-radius: 12px; padding: 18px; box-shadow: 0 2px 10px rgba(0,0,0,.06); }}
                .btn {{ display: inline-block; margin-top: 10px; text-decoration: none; background: #0b5fff; color: #fff; padding: 10px 14px; border-radius: 8px; font-weight: 600; }}
                .small {{ font-size: 13px; color: #566; }}
            </style>
        </head>
        <body>
            <div class="wrap">
                <h1>Data Integration Hub</h1>
                <p>Use one URL and choose the project you need.</p>
                <div class="grid">
                    <div class="card">
                        <h3>D365 Metadata Mapping API</h3>
                        <div class="small">Open D365 metadata service documentation.</div>
                        {d365_card}
                    </div>
                    <div class="card">
                        <h3>Excel to SQL Generator API</h3>
                        <div class="small">Upload mapping file and generate SQL.</div>
                        <a class="btn" href="{excel_sql_link}" target="_blank" rel="noopener">Open Excel-SQL API</a>
                    </div>
                </div>
            </div>
        </body>
    </html>
    """
    return HTMLResponse(content=html)
