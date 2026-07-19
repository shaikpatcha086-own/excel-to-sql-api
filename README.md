# Excel to SQL API

Standalone project to generate SQL from Excel mapping metadata.

## What is included

- `sql_api.py`: FastAPI web API (main deployment entrypoint)
- `app.py`: Streamlit UI (optional local/manual usage)
- `generate_sql_from_mapping.py`: Core SQL generation engine

## Run locally

1. Install dependencies

   pip install -r requirements.txt

2. Start API

   python -m uvicorn sql_api:app --host 0.0.0.0 --port 8000 --reload

3. Open docs

   http://localhost:8000/docs

## API endpoints

- `GET /health`
- `POST /generate/upload` (multipart: mapping_file + optional sheet_name/output_name)
- `POST /generate/url` (params: file_url + optional sheet_name/output_name)
- `GET /download/{filename}`

## Render deployment

- Build Command: `pip install -r requirements.txt`
- Start Command: `python -m uvicorn sql_api:app --host 0.0.0.0 --port $PORT`

## Notes

- This repo is independent from D365 metadata mapper and can be deployed on a separate URL.
- SQL output files are generated at runtime under `generated_sql/` and are ignored by git.
