#!/usr/bin/env bash
set -euo pipefail

URUMI_PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if kubectl -n platform get secret gateway-auth-secrets >/dev/null 2>&1; then
  echo "Keeping existing gateway-auth-secrets"
else
  JWT_VALUE="$(openssl rand -hex 32)"
  ADMIN_PASSWORD_VALUE="$(openssl rand -hex 24)"
  FERNET_VALUE="$(uv run --project "$URUMI_PROJECT_DIR/apps/gateway" python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"

  kubectl -n platform create secret generic gateway-auth-secrets \
    --from-literal=JWT_SECRET="$JWT_VALUE" \
    --from-literal=FERNET_KEY="$FERNET_VALUE" \
    --from-literal=ADMIN_EMAIL="admin@urumi.local" \
    --from-literal=ADMIN_PASSWORD="$ADMIN_PASSWORD_VALUE"
fi
