
#!/usr/bin/env python3

import json
import os
import subprocess
import sys
from pathlib import Path

import psycopg
from psycopg import sql

# ---------- Configuration ----------
#git


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
                status TEXT CHECK (status IS NULL OR status IN ('processed', 'processed_with_errors')),
                processed_date TIMESTAMPTZ,
                CHECK (
                    (status IS NULL AND processed_date IS NULL)
                    OR (status IN ('processed', 'processed_with_errors') AND processed_date IS NOT NULL)
                )
            )
            """
        )
        # Migrate constraints created by earlier versions of this script.
        cursor.execute(
            """
            ALTER TABLE folders
                DROP CONSTRAINT IF EXISTS folders_status_check,
                DROP CONSTRAINT IF EXISTS folders_check,
                DROP CONSTRAINT IF EXISTS folders_status_allowed_check,
                DROP CONSTRAINT IF EXISTS folders_processed_date_check
            """
        )
        cursor.execute(
            """
            ALTER TABLE folders
                ADD CONSTRAINT folders_status_allowed_check
                    CHECK (status IS NULL OR status IN ('processed', 'processed_with_errors')),
                ADD CONSTRAINT folders_processed_date_check
                    CHECK (
                        (status IS NULL AND processed_date IS NULL)
                        OR (status IN ('processed', 'processed_with_errors')
                            AND processed_date IS NOT NULL)
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
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS corrupted_files (
                id BIGSERIAL PRIMARY KEY,
                folder_id BIGINT NOT NULL
                    REFERENCES folders(id) ON DELETE CASCADE,
                occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                file_path TEXT NOT NULL,
                error_message TEXT NOT NULL,
                exit_code INTEGER
            )
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS corrupted_files_folder_time_idx
            ON corrupted_files (folder_id, occurred_at)
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


def recover_error_folders(conn):
    """Check every file individually in folders recorded in processing_errors."""
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT f.id, f.path
            FROM folders AS f
            WHERE EXISTS (
                SELECT 1
                FROM processing_errors AS pe
                WHERE pe.folder_id = f.id
            )
            ORDER BY f.path
            """
        )
        pending = cursor.fetchall()

    if not pending:
        print("No folders with processing errors were found.")
        return

    print(f"Checking {len(pending)} folders file by file")

    metadata_columns = sql.SQL(", ").join(
        sql.Identifier(field) for field in FIELDS
    )
    metadata_placeholders = sql.SQL(", ").join(
        [sql.Placeholder()] * len(FIELDS)
    )
    metadata_updates = sql.SQL(", ").join(
        sql.SQL("{} = EXCLUDED.{}").format(
            sql.Identifier(field), sql.Identifier(field)
        )
        for field in FIELDS
    )
    insert_metadata = sql.SQL(
        """
        INSERT INTO file_metadata (folder_id, {})
        VALUES ({})
        ON CONFLICT (folder_id, "FilePath") DO UPDATE SET {}
        """
    ).format(
        metadata_columns,
        sql.SQL(", ").join(
            [sql.Placeholder(), metadata_placeholders]
        ),
        metadata_updates,
    )

    for folder_index, (folder_id, folder) in enumerate(pending, start=1):
        print(f"[{folder_index}/{len(pending)}] {folder}", flush=True)
        corrupted_rows = []
        metadata_rows = []

        try:
            with os.scandir(folder) as entries:
                files = sorted(
                    (entry.path for entry in entries if entry.is_file()),
                    key=str.casefold,
                )
        except OSError as exc:
            # The folder itself cannot be inspected. Preserve the diagnostic
            # in corrupted_files using the folder path as the affected path.
            corrupted_rows.append(
                (folder_id, folder, f"Cannot enumerate folder: {exc}", None)
            )
            files = []

        for file_index, file_path in enumerate(files, start=1):
            print(
                f"    [{file_index}/{len(files)}] {Path(file_path).name}",
                flush=True,
            )
            command = [
                "exiftool",
                "-json",
                "-charset",
                "filename=UTF8",
                *[f"-{field}" for field in FIELDS],
                file_path,
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
                records = json.loads(result.stdout or "[]")
                if not isinstance(records, list) or not all(
                    isinstance(record, dict) for record in records
                ):
                    raise ValueError("ExifTool returned an unexpected JSON structure")

                record_error = next(
                    (record.get("Error") for record in records if record.get("Error")),
                    None,
                )
                if result.returncode != 0:
                    error_message = result.stderr.strip() or record_error or (
                        f"ExifTool exited with status {result.returncode}"
                    )
                    corrupted_rows.append(
                        (folder_id, file_path, error_message, result.returncode)
                    )
                elif record_error:
                    corrupted_rows.append(
                        (folder_id, file_path, str(record_error), result.returncode)
                    )
                elif not records:
                    corrupted_rows.append(
                        (folder_id, file_path, "ExifTool returned no file record", result.returncode)
                    )
                else:
                    metadata_rows.extend(
                        (folder_id, *metadata_values(record))
                        for record in records
                    )

            except OSError as exc:
                corrupted_rows.append(
                    (folder_id, file_path, f"Cannot run ExifTool: {exc}", None)
                )
            except (json.JSONDecodeError, ValueError) as exc:
                details = result.stderr.strip() if "result" in locals() else ""
                error_message = f"Invalid ExifTool response: {exc}"
                if details:
                    error_message += f"; stderr: {details}"
                corrupted_rows.append(
                    (folder_id, file_path, error_message, getattr(locals().get("result"), "returncode", None))
                )

        # Replace this folder's previous corruption snapshot, clear its
        # processing_errors, and mark it complete in one database transaction.
        with conn.transaction():
            with conn.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM corrupted_files WHERE folder_id = %s",
                    (folder_id,),
                )
                if metadata_rows:
                    cursor.executemany(insert_metadata, metadata_rows)
                if corrupted_rows:
                    cursor.executemany(
                        """
                        INSERT INTO corrupted_files
                            (folder_id, file_path, error_message, exit_code)
                        VALUES (%s, %s, %s, %s)
                        """,
                        corrupted_rows,
                    )
                cursor.execute(
                    "DELETE FROM processing_errors WHERE folder_id = %s",
                    (folder_id,),
                )
                cursor.execute(
                    """
                    UPDATE folders
                    SET status = 'processed_with_errors', processed_date = NOW()
                    WHERE id = %s
                    """,
                    (folder_id,),
                )

        print(
            f"    Finished: {len(files)} files checked, "
            f"{len(corrupted_rows)} corrupted/unreadable entries recorded"
        )

    print("Error recovery complete.")


def export(mode="export"):
    """Export pending folders, or recover folders recorded in processing_errors."""
    with psycopg.connect(**database_settings(), autocommit=True) as conn:
        ensure_schema(conn)

        if mode == "errors":
            recover_error_folders(conn)
            return

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

        print(f"Processing {len(pending)} folders in export mode")

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
            sql.SQL(", ").join([sql.Placeholder(), placeholders]),
        )

        for index, (folder_id, folder) in enumerate(pending, start=1):
            print(f"[{index}/{len(pending)}] {folder}", flush=True)
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
                report_error(f"ERROR: Cannot run ExifTool for {folder}: {exc}")
                save_processing_error(conn, folder_id, "exiftool_launch", str(exc))
                continue

            if result.returncode != 0:
                details = result.stderr.strip() or (
                    "ExifTool returned a nonzero exit code without writing to stderr."
                )
                report_error(f"EXIFTOOL FAILED (exit {result.returncode}) for {folder}:\n{details}")
                save_processing_error(
                    conn, folder_id, "exiftool_command", details, result.returncode
                )

            try:
                records = json.loads(result.stdout or "[]")
                if not isinstance(records, list) or not all(
                    isinstance(record, dict) for record in records
                ):
                    raise ValueError("Expected a JSON list of objects")
            except (json.JSONDecodeError, ValueError) as exc:
                message = f"Invalid ExifTool JSON for {folder}: {exc}"
                if result.stderr.strip():
                    message += f"\nExifTool diagnostic output:\n{result.stderr.strip()}"
                report_error(message)
                save_processing_error(conn, folder_id, "exiftool_json", message, result.returncode)
                continue

            successful_records = [record for record in records if not record.get("Error")]
            corrupted_rows = []
            for record in records:
                error = record.get("Error")
                if error:
                    file_path = record.get("SourceFile") or record.get("FilePath") or folder
                    corrupted_rows.append(
                        (folder_id, str(file_path), str(error), result.returncode)
                    )
                    save_processing_error(
                        conn, folder_id, "exiftool_file",
                        f"EXIFTOOL FILE ERROR for {file_path}: {error}",
                        result.returncode,
                    )

            with conn.transaction():
                with conn.cursor() as cursor:
                    cursor.execute("DELETE FROM file_metadata WHERE folder_id = %s", (folder_id,))
                    if result.returncode == 0 or corrupted_rows:
                        cursor.execute(
                            "DELETE FROM corrupted_files WHERE folder_id = %s",
                            (folder_id,),
                        )
                    if result.returncode == 0 and successful_records:
                        cursor.executemany(
                            insert_metadata,
                            [(folder_id, *metadata_values(record)) for record in successful_records],
                        )
                    if corrupted_rows:
                        cursor.executemany(
                            """
                            INSERT INTO corrupted_files
                                (folder_id, file_path, error_message, exit_code)
                            VALUES (%s, %s, %s, %s)
                            """,
                            corrupted_rows,
                        )
                    if result.returncode == 0 or corrupted_rows:
                        cursor.execute(
                            """
                            UPDATE folders
                            SET status = 'processed', processed_date = NOW()
                            WHERE id = %s
                            """,
                            (folder_id,),
                        )
                    else:
                        print(
                            "Folder remains pending: ExifTool returned a nonzero "
                            "exit code but identified no individual failed files."
                        )

    print("Export complete.")

def main():
    if len(sys.argv) < 2:
        sys.exit(
            "Usage:\n"
            "  python3 main.py inventory /path/to/drive\n"
            "  python3 main.py export\n"
            "  python3 main.py errors"
        )

    try:
        if sys.argv[1] == "inventory":
            if len(sys.argv) != 3:
                sys.exit("Specify the root folder to inventory.")
            inventory(sys.argv[2])

        elif sys.argv[1] == "export":
            if len(sys.argv) != 2:
                sys.exit("The export command takes no extra arguments.")
            export(mode="export")

        elif sys.argv[1] == "errors":
            if len(sys.argv) != 2:
                sys.exit("The errors command takes no extra arguments.")
            export(mode="errors")

        else:
            sys.exit("Unknown command. Use inventory, export or errors.")
    except (psycopg.Error, RuntimeError, ValueError) as exc:
        message = f"ERROR: PostgreSQL operation failed: {exc}"
        report_error(message)
        sys.exit(1)


if __name__ == "__main__":
    main()
