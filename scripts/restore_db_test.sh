#!/usr/bin/env bash
set -euo pipefail

backup_file="${1:?usage: scripts/restore_db_test.sh BACKUP_FILE}"
test_db="${2:-workflow_restore_test}"

docker compose exec -T postgres sh -c \
  'dropdb --if-exists --username="$POSTGRES_USER" "$1"; createdb --username="$POSTGRES_USER" "$1"' \
  sh "${test_db}"
docker compose exec -T postgres sh -c \
  'pg_restore --exit-on-error --no-owner --no-acl --username="$POSTGRES_USER" --dbname="$1"' \
  sh "${test_db}" <"${backup_file}"
docker compose exec -T postgres sh -c \
  'psql --username="$POSTGRES_USER" --dbname="$1" --tuples-only --command="SELECT count(*) FROM alembic_version"' \
  sh "${test_db}"
