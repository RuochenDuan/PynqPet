# Pynq Pet Gateway

FastAPI wrapper service for the Pynq pet client protocol.

## Run

```powershell
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

## Test

```powershell
$env:UV_NO_CACHE='1'
$env:UV_CACHE_DIR='.uv-cache'
uv run python -m pytest -p no:cacheprovider
uv run ruff check .
```
