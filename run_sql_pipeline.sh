#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SQL_DIR="$PROJECT_DIR/SQL"

if [[ $# -gt 1 || ( $# -eq 1 && "$1" != "--yes" ) ]]; then
    printf 'Usage: %s [--yes]\n' "$0" >&2
    exit 2
fi

export PGHOST="${PGHOST:-${PHOTOPROCESS_DB_HOST:-192.168.1.160}}"
export PGPORT="${PGPORT:-${PHOTOPROCESS_DB_PORT:-5432}}"
export PGDATABASE="${PGDATABASE:-${PHOTOPROCESS_DB_NAME:-photoprocess_debian}}"
export PGUSER="${PGUSER:-${PHOTOPROCESS_DB_USER:-postgres}}"

if [[ "${1:-}" != "--yes" ]]; then
    printf 'This pipeline drops and recreates bronze_file_metadata and image_copy_manifest.\n'
    printf 'Database: %s on %s:%s as %s\n' "$PGDATABASE" "$PGHOST" "$PGPORT" "$PGUSER"
    read -r -p 'Continue? [y/N] ' confirmation
    if [[ ! "$confirmation" =~ ^[Yy]$ ]]; then
        printf 'Cancelled.\n'
        exit 1
    fi
fi

if [[ -n "${PGPASSWORD:-${PHOTOPROCESS_DB_PASSWORD:-}}" ]]; then
    export PGPASSWORD="${PGPASSWORD:-$PHOTOPROCESS_DB_PASSWORD}"
elif [[ -t 0 ]]; then
    read -r -s -p 'PostgreSQL password: ' PGPASSWORD
    printf '\n'
    if [[ -z "$PGPASSWORD" ]]; then
        printf 'A non-empty PostgreSQL password is required.\n' >&2
        exit 1
    fi
    export PGPASSWORD
fi

scripts=(
    "10_create_bronze_file_metadata_table.sql"
    "20_create_image_intermediate_tables.sql"
    "30_create_image_remaining_cluster.sql"
    "40_create_video_intermediate_tables.sql"
    "50_create_video_remaining_cluster.sql"
    "60_create_image_copy_manifest_table.sql"
    "70_insert_image_remaining_copy_manifest_table.sql"
    "80_insert_video_copy_manifest.sql"
    "90_insert_video_remaining_copy_manifest.sql"
)

for script in "${scripts[@]}"; do
    script_path="$SQL_DIR/$script"
    if [[ ! -f "$script_path" ]]; then
        printf 'SQL script not found: %s\n' "$script_path" >&2
        exit 1
    fi

    printf '\n==> Running %s\n' "$script"
    psql --no-psqlrc --set ON_ERROR_STOP=1 --file "$script_path"
done

printf '\nSQL pipeline completed successfully.\n'
