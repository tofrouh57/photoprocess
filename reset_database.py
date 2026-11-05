#!/usr/bin/env python3

import os
import sys

import psycopg
from psycopg import sql


DB_HOST = "192.168.1.160"
DB_PORT = 5432
DB_NAME = "photoprocess_debian"
DB_USER = "postgres"

TABLES = (
    "file_content_hashes",
    "processing_errors",
    "file_metadata",
    "folders",
)


def database_settings():
#    password = os.environ.get("PHOTOPROCESS_DB_PASSWORD")
    password = 'tofer2578'
    if not password:
        raise RuntimeError(
            "Set PHOTOPROCESS_DB_PASSWORD before running this script."
        )

    return {
        "host": os.environ.get("PHOTOPROCESS_DB_HOST", DB_HOST),
        "port": int(os.environ.get("PHOTOPROCESS_DB_PORT", DB_PORT)),
        "dbname": os.environ.get("PHOTOPROCESS_DB_NAME", DB_NAME),
        "user": os.environ.get("PHOTOPROCESS_DB_USER", DB_USER),
        "password": password,
    }


def main():
    print(
        "This will drop the PhotoProcess tables in the public schema and "
        "permanently delete their data."
    )
    if input("Type RESET to continue: ").strip() != "RESET":
        print("Reset cancelled.")
        return

    try:
        with psycopg.connect(**database_settings()) as conn:
            with conn.cursor() as cursor:
                for table in TABLES:
                    cursor.execute(
                        sql.SQL("DROP TABLE IF EXISTS {}").format(
                            sql.Identifier("public", table)
                        )
                    )
    except (psycopg.Error, RuntimeError, ValueError) as exc:
        print(f"Database reset failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    print("PhotoProcess tables dropped.")


if __name__ == "__main__":
    main()
