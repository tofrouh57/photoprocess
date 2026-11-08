#!/usr/bin/env python3

import argparse
import hashlib
import json
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from main import database_settings, report_error


EXIFTOOL_TAGS = (
    "FileName",
    "FileType",
    "MIMEType",
    "FileSize",
    "FileModifyDate",
    "DateTimeOriginal",
    "MediaCreateDate",
    "Make",
    "Model",
    "ImageWidth",
    "ImageHeight",
)
BATCH_SIZE = 100
HASH_CHUNK_SIZE = 1024 * 1024

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pass1_folders (
    id BIGSERIAL PRIMARY KEY,
    root_path TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    absolute_path TEXT NOT NULL,
    scan_signature CHAR(64) NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'completed', 'completed_with_errors')),
    discovered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    error_message TEXT,
    UNIQUE (root_path, relative_path)
);

CREATE INDEX IF NOT EXISTS pass1_folders_status_idx
    ON pass1_folders (root_path, status);

CREATE TABLE IF NOT EXISTS pass1_files (
    id BIGSERIAL PRIMARY KEY,
    folder_id BIGINT NOT NULL
        REFERENCES pass1_folders(id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    absolute_path TEXT NOT NULL,
    media_type TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    file_signature CHAR(64),
    content_sha256 CHAR(64),
    content_size BIGINT,
    status TEXT NOT NULL CHECK (status IN ('processed', 'error')),
    error_message TEXT,
    indexed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (folder_id, relative_path)
);

CREATE INDEX IF NOT EXISTS pass1_files_status_idx
    ON pass1_files (folder_id, status);
"""


@dataclass(frozen=True)
class FileEntry:
    path: Path
    relative_path: str
    signature: tuple


@dataclass(frozen=True)
class FolderEntry:
    path: Path
    relative_path: str
    scan_signature: str


def file_signature(file_stat):
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
    )


def signature_for_files(files):
    digest = hashlib.sha256()
    for entry in files:
        name = os.fsencode(entry.path.name)
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        digest.update(",".join(map(str, entry.signature)).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def signature_for_file(signature):
    value = ",".join(map(str, signature)).encode("ascii")
    return hashlib.sha256(value).hexdigest()


def scan_directory(directory, root):
    files = []
    child_directories = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                try:
                    entry_stat = entry.stat(follow_symlinks=False)
                    if stat.S_ISREG(entry_stat.st_mode):
                        path = Path(entry.path)
                        files.append(
                            FileEntry(
                                path=path,
                                relative_path=path.relative_to(root).as_posix(),
                                signature=file_signature(entry_stat),
                            )
                        )
                    elif stat.S_ISDIR(entry_stat.st_mode):
                        child_directories.append(Path(entry.path))
                except OSError as exc:
                    raise OSError(f"Cannot inspect {entry.path}: {exc}") from exc
    except OSError as exc:
        raise OSError(f"Cannot scan directory {directory}: {exc}") from exc

    files.sort(key=lambda item: item.path.name)
    child_directories.sort(key=str)
    return files, signature_for_files(files), child_directories


def discover_folders(root):
    folders = []
    pending = [root]
    while pending:
        directory = pending.pop()
        _, scan_signature, child_directories = scan_directory(directory, root)
        relative_path = directory.relative_to(root).as_posix() or "."
        folders.append(FolderEntry(directory, relative_path, scan_signature))
        pending.extend(reversed(child_directories))

    return folders


def ensure_schema(conn):
    with conn.transaction():
        with conn.cursor() as cursor:
            for statement in SCHEMA_SQL.split(";"):
                if statement.strip():
                    cursor.execute(statement)


def acquire_root_lock(conn, root):
    lock_key = int.from_bytes(
        hashlib.sha256(str(root).encode("utf-8")).digest()[:8],
        byteorder="big",
        signed=True,
    )
    with conn.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(%s)", (lock_key,))


def register_folders(conn, root, folders):
    folder_states = {}
    with conn.transaction():
        with conn.cursor() as cursor:
            for folder in folders:
                cursor.execute(
                    """
                    INSERT INTO pass1_folders (
                        root_path, relative_path, absolute_path, scan_signature
                    )
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (root_path, relative_path) DO UPDATE
                    SET absolute_path = EXCLUDED.absolute_path,
                        status = CASE
                            WHEN pass1_folders.scan_signature
                                IS DISTINCT FROM EXCLUDED.scan_signature
                                OR pass1_folders.status = 'running'
                            THEN 'pending'
                            ELSE pass1_folders.status
                        END,
                        completed_at = CASE
                            WHEN pass1_folders.scan_signature
                                IS DISTINCT FROM EXCLUDED.scan_signature
                                OR pass1_folders.status = 'running'
                            THEN NULL
                            ELSE pass1_folders.completed_at
                        END,
                        error_message = CASE
                            WHEN pass1_folders.scan_signature
                                IS DISTINCT FROM EXCLUDED.scan_signature
                                OR pass1_folders.status = 'running'
                            THEN NULL
                            ELSE pass1_folders.error_message
                        END,
                        scan_signature = EXCLUDED.scan_signature
                    RETURNING id, status
                    """,
                    (
                        str(root),
                        folder.relative_path,
                        str(folder.path),
                        folder.scan_signature,
                    ),
                )
                folder_id, status = cursor.fetchone()
                folder_states[folder.relative_path] = (folder_id, status)
    return folder_states


def extract_metadata(files):
    option_args = [f"-{tag}" for tag in EXIFTOOL_TAGS]
    command = [
        "exiftool",
        "-json",
        *option_args,
        *(str(entry.path) for entry in files),
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
    except FileNotFoundError as exc:
        raise RuntimeError("ExifTool is not installed or is not available on PATH.") from exc
    except OSError as exc:
        raise RuntimeError(f"Could not run ExifTool: {exc}") from exc

    try:
        records = json.loads(result.stdout)
        if not isinstance(records, list):
            raise ValueError("ExifTool JSON output is not a list")
    except (json.JSONDecodeError, ValueError) as exc:
        diagnostic = result.stderr.strip()
        message = f"Invalid ExifTool JSON: {exc}"
        if diagnostic:
            message = f"{message}; {diagnostic}"
        return {entry.path: ({}, message) for entry in files}

    by_path = {}
    for record in records:
        if isinstance(record, dict) and record.get("SourceFile"):
            by_path[os.path.abspath(record["SourceFile"])] = record

    outcomes = {}
    diagnostic = result.stderr.strip()
    for entry in files:
        record = by_path.get(os.path.abspath(entry.path))
        if record is None:
            message = diagnostic or "ExifTool returned no metadata record."
            if result.returncode:
                message = f"ExifTool exited with status {result.returncode}: {message}"
            outcomes[entry.path] = ({}, message)
        elif record.get("Error"):
            outcomes[entry.path] = ({}, str(record["Error"]))
        else:
            outcomes[entry.path] = (record, None)
    return outcomes


def hash_file(entry):
    digest = hashlib.sha256()
    with entry.path.open("rb") as source:
        before = os.fstat(source.fileno())
        if file_signature(before) != entry.signature:
            raise OSError("File changed after directory discovery.")
        for chunk in iter(lambda: source.read(HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
        after = os.fstat(source.fileno())

    current = entry.path.stat()
    if not (
        file_signature(before)
        == file_signature(after)
        == file_signature(current)
        == entry.signature
    ):
        raise OSError("File changed while it was being hashed.")
    return digest.hexdigest(), before.st_size


def media_type(metadata):
    mime_type = metadata.get("MIMEType")
    if isinstance(mime_type, str) and "/" in mime_type:
        return mime_type.partition("/")[0].lower()
    return None


def existing_file_states(conn, folder_id, files):
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT relative_path, file_signature, status
            FROM pass1_files
            WHERE folder_id = %s AND relative_path = ANY(%s)
            """,
            (folder_id, [entry.relative_path for entry in files]),
        )
        return {
            relative_path: (file_signature, status)
            for relative_path, file_signature, status in cursor.fetchall()
        }


def store_batch(conn, folder_id, outcomes):
    errors = 0
    rows = []
    for entry, metadata, metadata_error in outcomes:
        sha256 = None
        content_size = None
        error_messages = []
        try:
            sha256, content_size = hash_file(entry)
        except OSError as exc:
            error_messages.append(f"SHA-256 failed: {exc}")

        if metadata_error:
            error_messages.append(f"ExifTool failed: {metadata_error}")
        error_message = "; ".join(error_messages) or None
        status = "error" if error_message else "processed"
        if error_message:
            errors += 1
        rows.append(
            (
                folder_id,
                entry.relative_path,
                str(entry.path),
                media_type(metadata),
                Jsonb(metadata),
                signature_for_file(entry.signature),
                sha256,
                content_size,
                status,
                error_message,
            )
        )

    with conn.transaction():
        with conn.cursor() as cursor:
            for row in rows:
                cursor.execute(
                    """
                    INSERT INTO pass1_files (
                        folder_id, relative_path, absolute_path, media_type,
                        metadata, file_signature, content_sha256, content_size,
                        status, error_message
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (folder_id, relative_path) DO UPDATE
                    SET absolute_path = EXCLUDED.absolute_path,
                        media_type = EXCLUDED.media_type,
                        metadata = EXCLUDED.metadata,
                        file_signature = EXCLUDED.file_signature,
                        content_sha256 = EXCLUDED.content_sha256,
                        content_size = EXCLUDED.content_size,
                        status = EXCLUDED.status,
                        error_message = EXCLUDED.error_message,
                        indexed_at = NOW()
                    """,
                    row,
                )
    return errors


def mark_folder(conn, folder_id, status, error_message=None):
    with conn.transaction():
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE pass1_folders
                SET status = %s,
                    completed_at = CASE
                        WHEN %s IN ('completed', 'completed_with_errors')
                        THEN NOW()
                        ELSE NULL
                    END,
                    error_message = %s
                WHERE id = %s
                """,
                (status, status, error_message, folder_id),
            )


def process_folder(conn, root, folder, folder_id):
    files, scan_signature, _ = scan_directory(folder.path, root)
    with conn.transaction():
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE pass1_folders
                SET status = 'running',
                    scan_signature = %s,
                    started_at = NOW(),
                    completed_at = NULL,
                    error_message = NULL
                WHERE id = %s
                """,
                (scan_signature, folder_id),
            )

    error_count = 0
    attempted = 0
    reused = 0
    for start in range(0, len(files), BATCH_SIZE):
        batch = files[start : start + BATCH_SIZE]
        known = existing_file_states(conn, folder_id, batch)
        todo = [
            entry
            for entry in batch
            if known.get(entry.relative_path)
            != (signature_for_file(entry.signature), "processed")
        ]
        reused += len(batch) - len(todo)
        if not todo:
            continue

        metadata_by_path = extract_metadata(todo)
        outcomes = [
            (entry, *metadata_by_path[entry.path])
            for entry in todo
        ]
        error_count += store_batch(conn, folder_id, outcomes)
        attempted += len(todo)
        print(
            f"  {min(start + len(batch), len(files))}/{len(files)} files "
            f"scanned; {reused} unchanged results reused."
        )

    final_status = "completed_with_errors" if error_count else "completed"
    summary = (
        f"{error_count} of {len(files)} files had errors."
        if error_count
        else None
    )
    mark_folder(conn, folder_id, final_status, summary)
    return attempted, reused, error_count


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Index each directory's files into resumable pass1_ PostgreSQL tables."
        )
    )
    parser.add_argument("root_folder", type=Path)
    args = parser.parse_args()

    root = args.root_folder.expanduser().resolve()
    if not root.is_dir():
        report_error(f"ERROR: Root folder does not exist or is not a directory: {root}")
        return 1

    try:
        folders = discover_folders(root)
        print(f"Discovered {len(folders)} directories under {root}")

        with psycopg.connect(**database_settings(), autocommit=True) as conn:
            ensure_schema(conn)
            acquire_root_lock(conn, root)
            folder_states = register_folders(conn, root, folders)

            completed = 0
            skipped = 0
            files_processed = 0
            files_reused = 0
            error_count = 0
            for index, folder in enumerate(folders, start=1):
                folder_id, status = folder_states[folder.relative_path]
                if status == "completed":
                    skipped += 1
                    continue

                print(
                    f"[{index}/{len(folders)}] Processing {folder.relative_path}"
                )
                try:
                    file_count, file_reused, folder_errors = process_folder(
                        conn, root, folder, folder_id
                    )
                except OSError as exc:
                    mark_folder(
                        conn,
                        folder_id,
                        "completed_with_errors",
                        str(exc),
                    )
                    report_error(
                        f"ERROR: Cannot process directory {folder.path}: {exc}"
                    )
                    error_count += 1
                    continue
                except RuntimeError as exc:
                    mark_folder(
                        conn,
                        folder_id,
                        "completed_with_errors",
                        str(exc),
                    )
                    raise

                completed += 1
                files_processed += file_count
                files_reused += file_reused
                error_count += folder_errors
                print(
                    f"  {file_count} files attempted, {file_reused} unchanged "
                    f"results reused; {folder_errors} errors."
                )

        print(
            "Pass 1 complete: "
            f"{completed} directories processed, {skipped} already complete, "
            f"{files_processed} files attempted, {files_reused} unchanged results "
            f"reused, {error_count} errors."
        )
        return 1 if error_count else 0
    except KeyboardInterrupt:
        report_error(
            "Interrupted. Completed directories are saved; rerun the same command "
            "to resume."
        )
        return 130
    except (psycopg.Error, RuntimeError, OSError, ValueError) as exc:
        report_error(f"ERROR: Pass 1 failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
