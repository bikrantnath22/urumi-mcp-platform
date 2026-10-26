# gateway

Authenticated MCP gateway, dashboard API and store provisioner.

Start here: [project setup and usage](../../README.md). That guide covers Helm, credentials and the other services this app needs.

The container starts `app.main:app` with Uvicorn. For a local process after installing dependencies and loading the required environment:

```bash
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The provisioner runs separately with `python -m app.store_worker_main`. Database migrations are in `migrations/`; verification scripts are in `tests/`.
