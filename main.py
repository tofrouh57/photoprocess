
#!/usr/bin/env python3

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
import unicodedata

import psycopg
from psycopg import sql

# ---------- Configuration ----------

BASE = Path(__file__).resolve().parent
MANIFEST = BASE / "metadata_folders.json"
ERROR_LOG = BASE / "metadata_errors.log"

DB_HOST = "192.168.1.160"
DB_PORT = 5432
DB_NAME = "photoprocess"
DB_USER = "postgres"

FOLDER_BATCH_SIZE = 500
EXIFTOOL_FILE_BATCH_SIZE = 100

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


def log_error(message):
    with ERROR_LOG.open("a", encoding="utf-8") as f:
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        f.write(f"{timestamp}\t{message.rstrip()}\n")


def report_error(message):
    print(message, file=sys.stderr)
    log_error(message)


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
                status TEXT NOT NULL DEFAULT 'pending',
                processed_date TIMESTAMPTZ,
                CONSTRAINT folders_status_valid_check
                    CHECK (status IN ('pending', 'processed')),
                CONSTRAINT folders_processing_dates_check
                    CHECK (
                        (status = 'pending' AND processed_date IS NULL)
                        OR (status = 'processed' AND processed_date IS NOT NULL)
                    )
            )
            """
        )
        cursor.execute(
            """
            ALTER TABLE folders
            ALTER COLUMN status SET DEFAULT 'pending'
            """
        )
        cursor.execute(
            """
            ALTER TABLE folders
            DROP CONSTRAINT IF EXISTS folders_status_check,
            DROP CONSTRAINT IF EXISTS folders_check
            """
        )
        cursor.execute(
            "UPDATE folders SET status = 'pending' WHERE status IS NULL"
        )
        cursor.execute(
            "ALTER TABLE folders ALTER COLUMN status SET NOT NULL"
        )
        cursor.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conrelid = 'folders'::regclass
                      AND conname = 'folders_status_valid_check'
                ) THEN
                    ALTER TABLE folders
                    ADD CONSTRAINT folders_status_valid_check
                    CHECK (status IN ('pending', 'processed'));
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conrelid = 'folders'::regclass
                      AND conname = 'folders_processing_dates_check'
                ) THEN
                    ALTER TABLE folders
                    ADD CONSTRAINT folders_processing_dates_check
                    CHECK (
                        (status = 'pending' AND processed_date IS NULL)
                        OR (status = 'processed' AND processed_date IS NOT NULL)
                    );
                END IF;
            END
            $$;
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


def filesystem_path_text(path_bytes):
    """Decode a filesystem path without changing its Unicode normalization."""
    return path_bytes.decode("utf-8")


def iter_directories(root_bytes, report_scan_error):
    """Yield byte paths exactly as returned by the filesystem."""
    pending = [root_bytes]
    while pending:
        current = pending.pop()
        yield current

        child_directories = []
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            child_directories.append(entry.path)
                    except OSError as exc:
                        report_scan_error(entry.path, exc)
        except OSError as exc:
            report_scan_error(current, exc)
            continue

        pending.extend(sorted(child_directories, reverse=True))


def iter_manifest_paths(path):
    """Read the existing JSON array format without loading it all at once."""
    decoder = json.JSONDecoder()
    with path.open("r", encoding="utf-8") as manifest:
        buffer = ""
        position = 0
        at_end = False

        def read_more():
            nonlocal buffer, position, at_end
            buffer = buffer[position:]
            position = 0
            chunk = manifest.read(65536)
            if chunk:
                buffer += chunk
            else:
                at_end = True

        def skip_whitespace():
            nonlocal position
            while True:
                while position < len(buffer) and buffer[position].isspace():
                    position += 1
                if position < len(buffer) or at_end:
                    return
                read_more()

        def finish_array():
            nonlocal position
            position += 1
            skip_whitespace()
            while position == len(buffer) and not at_end:
                read_more()
                skip_whitespace()
            if position != len(buffer):
                raise ValueError("Unexpected content after inventory.")

        read_more()
        skip_whitespace()
        if position >= len(buffer) or buffer[position] != "[":
            raise ValueError("Inventory must be a JSON array.")
        position += 1
        first = True

        while True:
            skip_whitespace()
            if position >= len(buffer):
                raise ValueError("Inventory JSON array is incomplete.")

            if first and buffer[position] == "]":
                finish_array()
                return

            if not first:
                if buffer[position] == "]":
                    finish_array()
                    return
                if buffer[position] != ",":
                    raise ValueError("Expected a comma between inventory paths.")
                position += 1
                skip_whitespace()
                if position >= len(buffer):
                    raise ValueError("Inventory JSON array is incomplete.")
                if buffer[position] == "]":
                    raise ValueError("Inventory has a trailing comma.")

            while True:
                try:
                    value, end = decoder.raw_decode(buffer, position)
                    break
                except json.JSONDecodeError as exc:
                    if at_end:
                        raise ValueError(
                            f"Invalid inventory JSON: {exc}"
                        ) from exc
                    read_more()

            if not isinstance(value, str):
                raise ValueError("Inventory entries must be folder paths.")
            yield value
            position = end
            first = False


def folder_rows_for_paths(conn, paths):
    """Match exact stored paths first, with NFC used only as a fallback key."""
    lookup_paths = set(paths)
    for path in paths:
        lookup_paths.add(unicodedata.normalize("NFC", path))
        lookup_paths.add(unicodedata.normalize("NFD", path))

    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, path, status
            FROM folders
            WHERE path = ANY(%s)
            """,
            (list(lookup_paths),),
        )
        rows = cursor.fetchall()

    exact_rows = {
        path: (folder_id, stored_path, status)
        for folder_id, stored_path, status in rows
        for path in (stored_path,)
    }
    normalized_rows = {}
    for folder_id, stored_path, status in rows:
        normalized_rows.setdefault(
            unicodedata.normalize("NFC", stored_path), []
        ).append((folder_id, stored_path, status))

    matches = {}
    for path in paths:
        if path in exact_rows:
            matches[path] = exact_rows[path]
            continue

        candidates = normalized_rows.get(unicodedata.normalize("NFC", path), [])
        if len(candidates) == 1:
            matches[path] = candidates[0]
    return matches


def iter_folder_file_batches(folder, conn, folder_id, scan_errors):
    """Yield bounded groups of direct files using names from scandir."""
    file_paths = []
    try:
        with os.scandir(os.fsencode(folder)) as entries:
            for entry in entries:
                try:
                    if entry.is_file(follow_symlinks=False):
                        filesystem_path_text(entry.path)
                        file_paths.append(entry.path)
                        if len(file_paths) >= EXIFTOOL_FILE_BATCH_SIZE:
                            yield file_paths
                            file_paths = []
                except UnicodeDecodeError as exc:
                    scan_errors[0] = True
                    message = (
                        f"ERROR: Filename is not valid UTF-8 and cannot be "
                        f"passed to PostgreSQL/ExifTool: {entry.path!r}: {exc}"
                    )
                    report_error(message)
                    save_processing_error(
                        conn, folder_id, "filesystem_encoding", message
                    )
                except OSError as exc:
                    scan_errors[0] = True
                    message = (
                        f"ERROR: Cannot inspect file entry "
                        f"{os.fsdecode(entry.path)} in {folder}: {exc}"
                    )
                    report_error(message)
                    save_processing_error(
                        conn, folder_id, "filesystem_file", message
                    )
    except OSError as exc:
        scan_errors[0] = True
        message = f"ERROR: Cannot scan folder {folder}: {exc}"
        report_error(message)
        save_processing_error(conn, folder_id, "filesystem_folder", message)
        return

    if file_paths:
        yield file_paths


def exiftool_records(conn, folder, folder_id, file_batches):
    """Read file metadata in bounded ExifTool batches; return None on errors."""
    records = []
    had_errors = False
    total_files = 0

    for batch_index, batch_paths in enumerate(file_batches, start=1):
        total_files += len(batch_paths)
        print(f"  ExifTool batch {batch_index}: {len(batch_paths)} files", flush=True)
        command = [
            "exiftool",
            "-json",
            "-charset",
            "filename=UTF8",
            *[f"-{field}" for field in FIELDS],
            *(filesystem_path_text(path) for path in batch_paths),
        ]

        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                shell=False,
            )
        except OSError as exc:
            had_errors = True
            message = f"ERROR: Cannot run ExifTool for files in {folder}: {exc}"
            report_error(message)
            save_processing_error(conn, folder_id, "exiftool_launch", message)
            continue

        try:
            batch_records = json.loads(result.stdout or "[]")
            if not isinstance(batch_records, list):
                raise ValueError("Expected a JSON list")
            if not all(isinstance(record, dict) for record in batch_records):
                raise ValueError("Expected each JSON record to be an object")
        except (json.JSONDecodeError, ValueError) as exc:
            had_errors = True
            message = (
                f"ERROR: Invalid ExifTool JSON for {folder} "
                f"(exit {result.returncode}): {exc}"
            )
            if result.stderr.strip():
                message += f"\nExifTool diagnostic output:\n{result.stderr.strip()}"
            report_error(message)
            save_processing_error(
                conn, folder_id, "exiftool_json", message, result.returncode
            )
            continue

        batch_errors = []
        for record in batch_records:
            error = record.get("Error")
            if error:
                file_path = (
                    record.get("FilePath")
                    or record.get("SourceFile")
                    or record.get("FileName")
                    or folder
                )
                message = (
                    f"EXIFTOOL FILE ERROR for {file_path} "
                    f"(folder {folder}): {error}"
                )
                report_error(message)
                save_processing_error(
                    conn,
                    folder_id,
                    "exiftool_file",
                    message,
                    result.returncode,
                )
                batch_errors.append(message)

        if result.returncode != 0:
            had_errors = True
            details = result.stderr.strip() or (
                "ExifTool returned a nonzero exit code without "
                "writing a message to stderr."
            )
            message = (
                f"EXIFTOOL FAILED (exit {result.returncode}) for {folder}.\n"
                "Folder remains pending; export will continue.\n"
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

        records.extend(
            record for record in batch_records if not record.get("Error")
        )
        if batch_errors:
            had_errors = True
        print(
            f"  ExifTool returned {len(batch_records)} records; "
            f"{len(batch_errors)} file errors",
            flush=True,
        )

    return (None if had_errors else records), total_files


def inventory(root):
    """Inventory folders using byte paths returned by the filesystem."""
    root_bytes = os.path.realpath(
        os.fsencode(os.path.expanduser(os.fspath(root)))
    )
    if not os.path.isdir(root_bytes):
        sys.exit(f"Error: folder does not exist: {os.fsdecode(root_bytes)}")

    ERROR_LOG.write_text("", encoding="utf-8")
    manifest_temp = MANIFEST.with_suffix(MANIFEST.suffix + ".tmp")
    folder_count = 0
    scan_error_count = 0
    batch = []

    def report_scan_error(path_bytes, error):
        nonlocal scan_error_count
        scan_error_count += 1
        message = (
            f"INVENTORY WARNING: {os.fsdecode(path_bytes)}: {error}"
        )
        report_error(message)

    def insert_batch(conn, paths):
        if not paths:
            return
        with conn.transaction():
            with conn.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO folders (path, status)
                    VALUES (%s, 'pending')
                    ON CONFLICT (path) DO NOTHING
                    """,
                    [(path,) for path in paths],
                )

    try:
        with manifest_temp.open("w", encoding="utf-8") as manifest:
            manifest.write("[\n")
            with psycopg.connect(
                **database_settings(), autocommit=True
            ) as conn:
                ensure_schema(conn)
                for path_bytes in iter_directories(root_bytes, report_scan_error):
                    try:
                        folder = filesystem_path_text(path_bytes)
                    except UnicodeDecodeError as exc:
                        report_scan_error(path_bytes, exc)
                        continue

                    if folder_count:
                        manifest.write(",\n")
                    json.dump(folder, manifest, ensure_ascii=False)
                    folder_count += 1
                    batch.append(folder)

                    if len(batch) >= FOLDER_BATCH_SIZE:
                        insert_batch(conn, batch)
                        batch.clear()

                insert_batch(conn, batch)
            manifest.write("\n]\n")
            manifest.flush()
            os.fsync(manifest.fileno())

        os.replace(manifest_temp, MANIFEST)
    except BaseException:
        manifest_temp.unlink(missing_ok=True)
        raise

    print(
        f"Inventory complete: {folder_count} folders, "
        f"{scan_error_count} scan errors"
    )
    print(f"Folder list: {MANIFEST}")
    print("Next: run the export command.")


def export():
    """Read metadata for pending folders, resuming from database status."""
    if not MANIFEST.exists():
        sys.exit("Error: no inventory. Run inventory first.")

    try:
        folders = iter_manifest_paths(MANIFEST)
        first_path = next(folders, None)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        sys.exit(f"Cannot read inventory: {exc}")

    with psycopg.connect(**database_settings(), autocommit=True) as conn:
        ensure_schema(conn)
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

        if first_path is None:
            print("Inventory is empty; nothing to export.")
            return

        print(f"Errors: {ERROR_LOG}")
        folder_batch = [first_path]
        folder_index = 0
        processed_count = 0
        pending_count = 0
        failed_count = 0

        def process_folder_batch(paths):
            nonlocal folder_index, processed_count, pending_count, failed_count
            folder_rows = folder_rows_for_paths(conn, paths)
            for folder in paths:
                folder_index += 1
                row = folder_rows.get(folder)
                if row is None:
                    message = (
                        "ERROR: Inventory folder is missing from PostgreSQL "
                        f"or has an ambiguous Unicode-normalized path: {folder}"
                    )
                    report_error(message)
                    failed_count += 1
                    continue

                folder_id, _, status = row
                if status == "processed":
                    print(
                        f"[{folder_index}] Already processed: {folder}",
                        flush=True,
                    )
                    continue

                pending_count += 1
                print(
                    f"[{folder_index}] Scanning folder: {folder}",
                    flush=True,
                )
                scan_errors = [False]
                file_batches = iter_folder_file_batches(
                    folder, conn, folder_id, scan_errors
                )
                records, file_count = exiftool_records(
                    conn, folder, folder_id, file_batches
                )
                print(f"  Scanned {file_count} direct files", flush=True)
                if records is None or scan_errors[0]:
                    failed_count += 1
                    print(f"  Folder remains pending: {folder}", flush=True)
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
                processed_count += 1
                print(
                    f"  Stored metadata for {len(records)} files: {folder}",
                    flush=True,
                )

        try:
            for path in folders:
                folder_batch.append(path)
                if len(folder_batch) >= FOLDER_BATCH_SIZE:
                    process_folder_batch(folder_batch)
                    folder_batch = []
            if folder_batch:
                process_folder_batch(folder_batch)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            message = f"ERROR: Cannot continue reading inventory: {exc}"
            report_error(message)
            failed_count += 1

    if pending_count == 0 and failed_count == 0:
        print("Export is already complete.")
    else:
        print(
            f"Export complete: {processed_count} folders processed, "
            f"{failed_count} folders with errors."
        )
    print(f"Error log: {ERROR_LOG}")


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
    except KeyboardInterrupt:
        print(
            "\nInterrupted. Completed folders remain saved; rerun the command "
            "to continue.",
            file=sys.stderr,
        )
        sys.exit(130)
    except (psycopg.Error, RuntimeError, ValueError, OSError) as exc:
        message = f"ERROR: Operation failed: {exc}"
        report_error(message)
        sys.exit(1)


if __name__ == "__main__":
    main()
