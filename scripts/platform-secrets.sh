#!/usr/bin/env bash
set -euo pipefail

kubectl create namespace platform --dry-run=client -o yaml \
  | kubectl apply -f -

create_password_secret() {
  local name="$1"
  local password

  if kubectl -n platform get secret "$name" >/dev/null 2>&1; then
    echo "Keeping existing secret: $name"
  else
    password="$(openssl rand -hex 24)"
    kubectl -n platform create secret generic "$name" \
      --from-literal=PASSWORD="$password"
  fi
}

create_password_secret postgres-admin
create_password_secret notes-db-credentials
create_password_secret gateway-db-credentials
