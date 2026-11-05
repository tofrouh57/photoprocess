
#!/usr/bin/env python3

import json
import os
import subprocess
import sys
from pathlib import Path

import psycopg
from psycopg import sql

# ---------- Configuration ----------
#export PHOTOPROCESS_DB_PASSWORD='tofer2578'


DB_HOST = "192.168.1.160"
DB_PORT = 5432
DB_NAME = "photoprocess_debian"
DB_USER = "postgres"

FIELDS = [
    "FilePath",
    "FileName",
    "Directory",
    "FileType",
    "MIMEType",
    "FileSize",

    # macOS filesystem dates
    "FileCreateDate",
    "FileModifyDate",
    "FileAccessDate",
    "FileInodeChangeDate",

    # Embedded photo/document metadata dates
    "DateTimeOriginal",
    "CreateDate",
    "ModifyDate",
    "MetadataDate",
    "DateAcquired",

    # Video/audio dates
    "MediaCreateDate",
    "MediaModifyDate",
    "TrackCreateDate",
    "TrackModifyDate",

    # Camera/image metadata
    "Make",
    "Model",
    "ImageWidth",
    "ImageHeight",
]

# -----------------------------------


def report_error(message):
    print(message, file=sys.stderr)


def database_settings():
    password = os.environ.get("PHOTOPROCESS_DB_PASSWORD")
    if not password:
        raise RuntimeError(
            "Set the PHOTOPROCESS_DB_PASSWORD environment variable "
            "before running this command."
        )

    return {
        "host": os.environ.get("PHOTOPROCESS_DB_HOST", DB_HOST),
        "port": int(os.environ.get("PHOTOPROCESS_DB_PORT", DB_PORT)),
        "dbname": os.environ.get("PHOTOPROCESS_DB_NAME", DB_NAME),
        "user": os.environ.get("PHOTOPROCESS_DB_USER", DB_USER),
        "password": password,
    }


def ensure_schema(conn):
    metadata_columns = sql.SQL(", ").join(
        sql.SQL("{} TEXT").format(sql.Identifier(field))
        for field in FIELDS
    )

    with conn.cursor() as cursor:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS folders (
                id BIGSERIAL PRIMARY KEY,
                path TEXT NOT NULL UNIQUE,
                discovered_date TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                status TEXT CHECK (status IS NULL OR status = 'processed'),
                processed_date TIMESTAMPTZ,
                CHECK (
                    (status IS NULL AND processed_date IS NULL)
                    OR (status = 'processed' AND processed_date IS NOT NULL)
                )
            )
            """
        )
        cursor.execute(
            sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS file_metadata (
                    id BIGSERIAL PRIMARY KEY,
                    folder_id BIGINT NOT NULL
                        REFERENCES folders(id) ON DELETE CASCADE,
                    {},
                    UNIQUE (folder_id, "FilePath")
                )
                """
            ).format(metadata_columns)
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS processing_errors (
                id BIGSERIAL PRIMARY KEY,
                folder_id BIGINT NOT NULL
                    REFERENCES folders(id) ON DELETE CASCADE,
                occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                error_type TEXT NOT NULL,
                exit_code INTEGER,
                error_message TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS processing_errors_folder_time_idx
            ON processing_errors (folder_id, occurred_at)
            """
        )


def save_processing_error(conn, folder_id, error_type, message, exit_code=None):
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO processing_errors
                (folder_id, error_type, exit_code, error_message)
            VALUES (%s, %s, %s, %s)
            """,
            (folder_id, error_type, exit_code, message.rstrip()),
        )


def metadata_values(record):
    values = []
    for field in FIELDS:
        value = record.get(field)
        if isinstance(value, (list, dict)):
            value = json.dumps(value, ensure_ascii=False)
        elif value is not None:
            value = str(value)
        values.append(value)
    return values


def inventory(root):
    """List folders without following symlinked directories."""
    root = Path(root).expanduser().resolve()

    if not root.is_dir():
        sys.exit(f"Error: folder does not exist: {root}")

    folders = []

    def onerror(error):
        message = f"INVENTORY WARNING: {error}"
        report_error(message)

    for current, dirs, _ in os.walk(
        root, topdown=True, onerror=onerror, followlinks=False
    ):
        dirs[:] = sorted(
            d for d in dirs
            if not (Path(current) / d).is_symlink()
        )
        folders.append(current)

    folders.sort()

    with psycopg.connect(**database_settings(), autocommit=True) as conn:
        ensure_schema(conn)
        with conn.transaction():
            with conn.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO folders (path)
                    VALUES (%s)
                    ON CONFLICT (path) DO NOTHING
                    """,
                    [(folder,) for folder in folders],
                )

    print(f"Inventory complete: {len(folders)} folders")
    print("Next: run the export command.")


def export():
    """Read metadata for unprocessed inventory folders into PostgreSQL."""
    with psycopg.connect(**database_settings(), autocommit=True) as conn:
        ensure_schema(conn)
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, path
                FROM folders
                WHERE status IS NULL
                ORDER BY path
                """
            )
            pending = cursor.fetchall()

        if not pending:
            with conn.cursor() as cursor:
                cursor.execute("SELECT EXISTS (SELECT 1 FROM folders)")
                has_inventory = cursor.fetchone()[0]
            if not has_inventory:
                sys.exit("Error: no inventory. Run inventory first.")
            print("Export is already complete.")
            return

        print(f"Processing {len(pending)} folders")

        columns = sql.SQL(", ").join(
            sql.Identifier(field) for field in FIELDS
        )
        placeholders = sql.SQL(", ").join(
            [sql.Placeholder()] * len(FIELDS)
        )
        insert_metadata = sql.SQL(
            "INSERT INTO file_metadata (folder_id, {}) VALUES ({})"
        ).format(
            columns,
            sql.SQL(", ").join(
                [sql.Placeholder(), placeholders]
            ),
        )

        for index, (folder_id, folder) in enumerate(pending, start=1):
            print(
                f"[{index}/{len(pending)}] {folder}",
                flush=True
            )

            command = [
                "exiftool",
                "-json",
                "-charset",
                "filename=UTF8",
                *[f"-{field}" for field in FIELDS],
                folder,
            ]

            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
            except OSError as exc:
                message = f"ERROR: Cannot run ExifTool for {folder}: {exc}"
                report_error(message)
                save_processing_error(
                    conn, folder_id, "exiftool_launch", str(exc)
                )
                continue

            if result.returncode != 0:
                details = result.stderr.strip() or (
                    "ExifTool returned a nonzero exit code without "
                    "writing a message to stderr."
                )
                message = (
                    f"EXIFTOOL FAILED (exit {result.returncode}) for {folder}.\n"
                    "Folder left pending: metadata was not saved and the "
                    "export will continue with the next folder.\n"
                    f"ExifTool diagnostic output:\n{details}"
                )
                report_error(message)
                save_processing_error(
                    conn,
                    folder_id,
                    "exiftool_command",
                    details,
                    result.returncode,
                )

            try:
                records = json.loads(result.stdout or "[]")
                if not isinstance(records, list):
                    raise ValueError("Expected a JSON list")
                if not all(isinstance(record, dict) for record in records):
                    raise ValueError("Expected each JSON record to be an object")
            except (json.JSONDecodeError, ValueError) as exc:
                message = (
                    f"ERROR: Invalid ExifTool JSON for {folder} "
                    f"(exit {result.returncode}): {exc}"
                )
                if result.stderr.strip():
                    message += f"\nExifTool diagnostic output:\n{result.stderr.strip()}"
                report_error(message)
                save_processing_error(
                    conn,
                    folder_id,
                    "exiftool_json",
                    message,
                    result.returncode,
                )
                print("Folder remains pending; rerun export to retry.")
                return

            file_errors = []
            for record in records:
                error = record.get("Error")
                if error:
                    file_path = (
                        record.get("FilePath")
                        or record.get("SourceFile")
                        or record.get("FileName")
                        or folder
                    )
                    file_errors.append(f"{file_path}: {error}")
                    file_error_message = (
                        f"EXIFTOOL FILE ERROR for {file_path} "
                        f"(folder {folder}): {error}"
                    )
                    report_error(file_error_message)
                    save_processing_error(
                        conn,
                        folder_id,
                        "exiftool_file",
                        file_error_message,
                        result.returncode,
                    )

            if result.returncode != 0 or file_errors:
                continue

            with conn.transaction():
                with conn.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM file_metadata WHERE folder_id = %s",
                        (folder_id,),
                    )
                    if records:
                        cursor.executemany(
                            insert_metadata,
                            [
                                (folder_id, *metadata_values(record))
                                for record in records
                            ],
                        )
                    cursor.execute(
                        """
                        UPDATE folders
                        SET status = 'processed', processed_date = NOW()
                        WHERE id = %s
                        """,
                        (folder_id,),
                    )
    print("Export complete.")
    print("Export complete.")


def main():
    if len(sys.argv) < 2:
        sys.exit(
            "Usage:\n"
            "  python3 main.py inventory /path/to/drive\n"
            "  python3 main.py export"
        )

    try:
        if sys.argv[1] == "inventory":
            if len(sys.argv) != 3:
                sys.exit("Specify the root folder to inventory.")
            inventory(sys.argv[2])

        elif sys.argv[1] == "export":
            if len(sys.argv) != 2:
                sys.exit("The export command takes no extra arguments.")
            export()

        else:
            sys.exit("Unknown command. Use inventory or export.")
    except (psycopg.Error, RuntimeError, ValueError) as exc:
        message = f"ERROR: PostgreSQL operation failed: {exc}"
        report_error(message)
        sys.exit(1)


if __name__ == "__main__":
    main()
