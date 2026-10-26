#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

NS=store
SECRET=store-secrets

if [ -f .env ]; then set -a; . ./.env; set +a; fi

kubectl get ns "$NS" >/dev/null 2>&1 || kubectl create ns "$NS"

if kubectl -n "$NS" get secret "$SECRET" >/dev/null 2>&1; then
  echo "Secret $SECRET already exists in namespace $NS - leaving it unchanged."
  exit 0
fi

rand() { openssl rand -hex "$1"; }

kubectl -n "$NS" create secret generic "$SECRET" \
  --from-literal=DB_ROOT_PASSWORD="$(rand 16)" \
  --from-literal=DB_PASSWORD="$(rand 16)" \
  --from-literal=WP_ADMIN_USER="${WP_ADMIN_USER:-admin}" \
  --from-literal=WP_ADMIN_EMAIL="${WP_ADMIN_EMAIL:-admin@example.com}" \
  --from-literal=WP_ADMIN_PASSWORD="${WP_ADMIN_PASSWORD:-$(rand 12)}" \
  --from-literal=WC_RW_KEY="ck_$(rand 20)" \
  --from-literal=WC_RW_SECRET="cs_$(rand 20)" \
  --from-literal=WC_RO_KEY="ck_$(rand 20)" \
  --from-literal=WC_RO_SECRET="cs_$(rand 20)" \
  --from-literal=WP_MEDIA_USER="mediabot" \
  --from-literal=WP_MEDIA_APP_PASSWORD="$(rand 12)"

echo "Created secret $SECRET in namespace $NS."
