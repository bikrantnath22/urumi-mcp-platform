# mcp-notes

Shared team notes stored in PostgreSQL and exposed over MCP.

Start here: [project setup and usage](../../README.md). That guide covers Helm, credentials and the other services this app needs.

The container starts `main.py`. For a local process after installing dependencies and loading the required environment:

```bash
uv run python main.py
```

Notes are shared by the team. Use the dedicated Notes database credentials, not the gateway database account.
