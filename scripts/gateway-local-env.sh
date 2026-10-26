# Load into your current terminal with: source scripts/gateway-local-env.sh

export PGHOST=127.0.0.1
export PGPORT=5433
export PGDATABASE=gateway
export PGUSER=gateway_app

# Local development uses only these three explicit forwarded MCP endpoints.
# The in-cluster gateway gets its separate service allowlist from Helm.
export WOO_MCP_URL="${WOO_MCP_URL:-http://127.0.0.1:8085/mcp}"
export ANALYTICS_MCP_URL="${ANALYTICS_MCP_URL:-http://127.0.0.1:8086/mcp}"
export NOTES_MCP_URL="${NOTES_MCP_URL:-http://127.0.0.1:8087/mcp}"
export UPSTREAM_INTERNAL_MCP_URLS="$(python3 -c 'import json, os; print(json.dumps([os.environ[name] for name in ("WOO_MCP_URL", "ANALYTICS_MCP_URL", "NOTES_MCP_URL")]))')"
export UPSTREAM_INTERNAL_MCP_CIDRS="127.0.0.1/32"

export PGPASSWORD="$(kubectl -n platform get secret gateway-db-credentials -o jsonpath='{.data.PASSWORD}' | base64 -d)"
export JWT_SECRET="$(kubectl -n platform get secret gateway-auth-secrets -o jsonpath='{.data.JWT_SECRET}' | base64 -d)"
export FERNET_KEY="$(kubectl -n platform get secret gateway-auth-secrets -o jsonpath='{.data.FERNET_KEY}' | base64 -d)"

: "${PGPASSWORD:?Gateway database password was not loaded}"
: "${JWT_SECRET:?JWT secret was not loaded}"
: "${FERNET_KEY:?Fernet key was not loaded}"

echo "Gateway local environment loaded."

# Groq chat configuration
export LLM_MODEL="${LLM_MODEL:-openai/gpt-oss-20b}"
export GROQ_API_KEY="$(kubectl -n platform get secret gateway-llm-secrets -o jsonpath='{.data.GROQ_API_KEY}' | base64 -d)"
: "${GROQ_API_KEY:?Groq API key was not loaded}"
echo "Groq environment loaded. Model: $LLM_MODEL"
