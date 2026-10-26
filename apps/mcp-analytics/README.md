# mcp-analytics

Read-only WooCommerce sales and order analytics over MCP.

Start here: [project setup and usage](../../README.md). That guide covers Helm, credentials and the other services this app needs.

The container starts `main.py`. For a local process after installing dependencies and loading the required environment:

```bash
uv run python main.py
```

Use a read-only WooCommerce key. Analytics reads the store REST API; it does not query the WordPress database.
