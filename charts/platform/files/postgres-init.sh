#!/usr/bin/env bash
set -euo pipefail

psql --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" \
  --set=ON_ERROR_STOP=1 \
  --set=notes_password="$NOTES_DB_PASSWORD" \
  --set=gateway_password="$GATEWAY_DB_PASSWORD" <<'SQL'
CREATE ROLE notes_app LOGIN PASSWORD :'notes_password';
CREATE ROLE gateway_app LOGIN PASSWORD :'gateway_password';

CREATE DATABASE notes OWNER notes_app;
CREATE DATABASE gateway OWNER gateway_app;

REVOKE ALL ON DATABASE notes FROM PUBLIC;
REVOKE ALL ON DATABASE gateway FROM PUBLIC;

GRANT CONNECT ON DATABASE notes TO notes_app;
GRANT CONNECT ON DATABASE gateway TO gateway_app;
SQL

echo "[postgres-init] notes and gateway databases ready"
