#!/usr/bin/env python3

import argparse
import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

import psycopg
from psycopg import sql

from main import FIELDS, database_settings, report_error


HASH_CHUNK_SIZE = 1024 * 1024
DISCOVERY_DIRECTORY_INTERVAL = 100
PROCESSING_FILE_INTERVAL = 25
ADDITIONAL_DATE_FIELDS = [
    "DateTimeDigitized",
    "DateTime",
    "SubSecTimeOriginal",
    "OffsetTimeOriginal",
    "DateCreated",
    "TimeCreated",
    "CreationDate",
    "ContentCreateDate",
    "DateEncoded",
    "EncodedDate",
    "TaggedDate",
]
INVENTORY_FIELDS = list(
    dict.fromkeys(
        field
        for field in [*FIELDS, *ADDITIONAL_DATE_FIELDS]
        if field not in {"FilePath", "Directory"}
    )
)


def ensure_schema(conn):
    tag_columns = sql.SQL(", ").join(
        sql.SQL("{} TEXT").format(sql.Identifier(field))
        for field in INVENTORY_FIELDS
    )
    with conn.cursor() as cursor:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS public.one_by_one_folders (
                folder_path TEXT PRIMARY KEY,
                status TEXT CHECK (status IS NULL OR status = 'completed'),
                scandate TIMESTAMPTZ,
                CHECK (
                    (status IS NULL AND scandate IS NULL)
                    OR (status = 'completed' AND scandate IS NOT NULL)
                )
            )
            """
        )
        cursor.execute(
            sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS public.one_by_one_inventory (
                    source_folder_path TEXT NOT NULL,
                    source_file_path TEXT NOT NULL,
                    {},
                    sha256 CHAR(64),
                    exif_return_code INTEGER,
                    exif_error TEXT,
                    hash_error TEXT,
                    indexed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (source_file_path)
                )
                """
            ).format(tag_columns)
        )


def discover_folders(root):
    folders = []
    errors = 0
    directories_scanned = 0

    def onerror(error):
        nonlocal errors
        errors += 1
        report_error(f"ERROR: Cannot scan directory {error.filename}: {error}")

    for current, dirs, _ in os.walk(
        root, topdown=True, onerror=onerror, followlinks=False
    ):
        directories_scanned += 1
        dirs[:] = sorted(
            name
            for name in dirs
            if name != "#recycle" and not (Path(current) / name).is_symlink()
        )
        folders.append(Path(current))
        if directories_scanned % DISCOVERY_DIRECTORY_INTERVAL == 0:
            print(
                f"Scanning: {directories_scanned} directories visited, "
                f"folders found; current folder: {current}",
                flush=True,
            )
    print(
        f"Discovery complete: {directories_scanned} directories visited, "
        f"{len(folders)} folders found.",
        flush=True,
    )
    return folders, errors


def direct_files(folder):
    files = []
    with os.scandir(folder) as entries:
        for entry in entries:
            entry_stat = entry.stat(follow_symlinks=False)
            if stat.S_ISREG(entry_stat.st_mode):
                files.append(Path(entry.path))
    return sorted(files, key=lambda path: path.name)


def acquire_root_lock(conn, root):
    lock_key = int.from_bytes(
        hashlib.sha256(str(root).encode("utf-8")).digest()[:8],
        byteorder="big",
        signed=True,
    )
    with conn.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(%s)", (lock_key,))


def acquire_inventory_lock(conn):
    lock_key = int.from_bytes(
        hashlib.sha256(b"one_by_one_inventory").digest()[:8],
        byteorder="big",
        signed=True,
    )
    with conn.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(%s)", (lock_key,))


def list_folders(conn, root):
    folders, scan_errors = discover_folders(root)
    with conn.transaction():
        with conn.cursor() as cursor:
            for index, folder in enumerate(folders, start=1):
                cursor.execute(
                    """
                    INSERT INTO public.one_by_one_folders (
                        folder_path, status, scandate
                    )
                    VALUES (%s, NULL, NULL)
                    ON CONFLICT (folder_path) DO NOTHING
                    """,
                    (str(folder),),
                )
                if index % DISCOVERY_DIRECTORY_INTERVAL == 0:
                    print(
                        f"Registered {index}/{len(folders)} folders.",
                        flush=True,
                    )
    print(
        f"Folder listing complete: {len(folders)} folders registered under {root}."
    )
    return 1 if scan_errors else 0


def read_metadata(path):
    command = [
        "exiftool",
        "-json",
        *(f"-{field}" for field in INVENTORY_FIELDS),
        str(path),
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        return {}, None, f"Could not run ExifTool: {exc}"

    error_message = result.stderr.strip() or None
    try:
        records = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        message = f"Invalid ExifTool JSON: {exc}"
        if error_message:
            message = f"{message}; {error_message}"
        return {}, result.returncode, message

    if not isinstance(records, list) or not records or not isinstance(records[0], dict):
        message = "ExifTool returned no metadata record."
        if error_message:
            message = f"{message} {error_message}"
        return {}, result.returncode, message

    if result.returncode:
        message = f"ExifTool exited with status {result.returncode}."
        if error_message:
            message = f"{message} {error_message}"
        error_message = message

    return records[0], result.returncode, error_message


def hash_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        before = os.fstat(source.fileno())
        before_signature = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        for chunk in iter(lambda: source.read(HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
        after = os.fstat(source.fileno())

    current = path.stat()
    after_signature = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    current_signature = (
        current.st_dev,
        current.st_ino,
        current.st_size,
        current.st_mtime_ns,
    )
    if not before_signature == after_signature == current_signature:
        raise OSError("File changed while it was being hashed.")
    return digest.hexdigest()


def database_value(value):
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return str(value)
    return str(value)


def save_file(conn, path, metadata, exif_return_code, exif_error, sha256, hash_error):
    fields = [
        "source_folder_path",
        "source_file_path",
        *INVENTORY_FIELDS,
        "sha256",
        "exif_return_code",
        "exif_error",
        "hash_error",
    ]
    values = [
        str(path.parent),
        str(path),
        *(database_value(metadata.get(field)) for field in INVENTORY_FIELDS),
        sha256,
        exif_return_code,
        exif_error,
        hash_error,
    ]
    columns = sql.SQL(", ").join(sql.Identifier(field) for field in fields)
    placeholders = sql.SQL(", ").join(sql.Placeholder() for _ in values)
    updates = sql.SQL(", ").join(
        sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(field), sql.Identifier(field))
        for field in fields[1:]
    )
    with conn.transaction():
        with conn.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                    INSERT INTO public.one_by_one_inventory ({})
                    VALUES ({})
                    ON CONFLICT (source_file_path)
                    DO UPDATE SET {}, indexed_at = NOW()
                    """
                ).format(columns, placeholders, updates),
                values,
            )


def process_file(conn, path):
    metadata, exif_return_code, exif_error = read_metadata(path)
    if exif_error:
        report_error(f"ERROR: ExifTool for {path}: {exif_error}")

    mime_type = metadata.get("MIMEType")
    if not isinstance(mime_type, str) or not mime_type.lower().startswith(
        ("image/", "video/")
    ):
        return int(bool(exif_error)), True

    sha256 = None
    hash_error = None
    try:
        sha256 = hash_file(path)
    except OSError as exc:
        hash_error = str(exc)
        report_error(f"ERROR: Cannot hash {path}: {exc}")

    save_file(
        conn,
        path,
        metadata,
        exif_return_code,
        exif_error,
        sha256,
        hash_error,
    )
    return int(bool(exif_error or hash_error)), False


def process_pending_folders(conn):
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT folder_path
            FROM public.one_by_one_folders
            WHERE status IS NULL
            ORDER BY folder_path
            """
        )
        folders = cursor.fetchall()

    print(f"Found {len(folders)} pending folders in one_by_one_folders.")
    file_count = 0
    skipped_count = 0
    file_errors = 0
    folder_errors = 0

    for folder_index, (folder_path,) in enumerate(folders, start=1):
        folder = Path(folder_path)
        print(
            f"[Folder {folder_index}/{len(folders)}] {folder.name} ({folder})",
            flush=True,
        )
        try:
            files = direct_files(folder)
        except OSError as exc:
            folder_errors += 1
            report_error(
                f"ERROR: Cannot scan folder {folder}: {exc}. "
                "Folder remains pending."
            )
            continue

        for file_index, path in enumerate(files, start=1):
            file_error_count, skipped = process_file(conn, path)
            file_errors += file_error_count
            file_count += 1
            skipped_count += int(skipped)
            if (
                file_index % PROCESSING_FILE_INTERVAL == 0
                or file_index == len(files)
            ):
                status = " (skipped: not an image/video)" if skipped else ""
                print(
                    f"  [File {file_index}/{len(files)}] {path.name}{status}",
                    flush=True,
                )

        with conn.transaction():
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE public.one_by_one_folders
                    SET status = 'completed', scandate = NOW()
                    WHERE folder_path = %s
                      AND status IS NULL
                    """,
                    (str(folder),),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        f"Folder status was not updated for {folder}."
                    )
        print(
            f"  Folder completed: {folder} ({len(files)} file(s)).",
            flush=True,
        )

    print(
        f"Inventory complete: {len(folders)} folder(s) checked, {file_count} "
        f"file(s) scanned ({file_count - skipped_count} indexed, {skipped_count} "
        f"skipped as non-image/video), {file_errors} file-level error(s), "
        f"{folder_errors} folder scan error(s)."
    )
    return 1 if folder_errors or file_errors else 0


def main():
    parser = argparse.ArgumentParser(
        description="Register source folders and inventory their files one by one."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    list_parser = commands.add_parser(
        "list-folders", help="Register the root and all descendant folders."
    )
    list_parser.add_argument("source_folder", type=Path)
    commands.add_parser(
        "inventory", help="Inventory all folders whose status is NULL."
    )
    args = parser.parse_args()

    root = None
    if args.command == "list-folders":
        root = args.source_folder.expanduser().resolve()
        if not root.is_dir():
            report_error(
                f"ERROR: Source folder does not exist or is not a directory: {root}"
            )
            return 1

    try:
        with psycopg.connect(**database_settings(), autocommit=True) as conn:
            ensure_schema(conn)
            if args.command == "list-folders":
                acquire_root_lock(conn, root)
                return list_folders(conn, root)
            acquire_inventory_lock(conn)
            return process_pending_folders(conn)
    except KeyboardInterrupt:
        report_error(
            "Interrupted. Saved file records are retained; pending folders "
            "can be resumed by rerunning inventory."
        )
        return 130
    except (psycopg.Error, RuntimeError, OSError, ValueError) as exc:
        report_error(f"ERROR: one-by-one inventory failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())