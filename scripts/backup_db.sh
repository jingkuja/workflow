#!/usr/bin/env bash
set -euo pipefail

backup_dir="${1:-backups}"
mkdir -p "${backup_dir}"
backup_file="${backup_dir}/workflow-$(date -u +%Y%m%dT%H%M%SZ).dump"

docker compose exec -T postgres sh -c \
  'pg_dump --format=custom --no-owner --no-acl --username="$POSTGRES_USER" "$POSTGRES_DB"' \
  >"${backup_file}"

echo "${backup_file}"
