# 启动

```bash
$env:PYNQ_PET_UPSTREAM_MODE='open_llm_ws'
$env:PYNQ_PET_OPEN_LLM_WS_URL='ws://127.0.0.1:12393/client-ws'
uv run uvicorn main:app --host 0.0.0.0 --port 8000
```
