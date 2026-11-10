#!/usr/bin/env python3

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import psycopg
from psycopg import sql
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
CREATE TABLE IF NOT EXISTS pass1_libraries (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    root_path TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS pass1_folders (
    id BIGSERIAL PRIMARY KEY,
    library_id BIGINT NOT NULL
        REFERENCES pass1_libraries(id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    absolute_path TEXT NOT NULL,
    scan_signature CHAR(64) NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'completed', 'completed_with_errors')),
    discovered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    error_message TEXT,
    UNIQUE (library_id, relative_path)
);

CREATE INDEX IF NOT EXISTS pass1_folders_status_idx
    ON pass1_folders (library_id, status);

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

SOURCE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pass1_source_folders (
    id BIGSERIAL PRIMARY KEY,
    library_id BIGINT NOT NULL
        REFERENCES pass1_libraries(id) ON DELETE CASCADE,
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
    UNIQUE (library_id, root_path, relative_path)
);

CREATE INDEX IF NOT EXISTS pass1_source_folders_status_idx
    ON pass1_source_folders (library_id, root_path, status);

CREATE TABLE IF NOT EXISTS pass1_source_files (
    id BIGSERIAL PRIMARY KEY,
    folder_id BIGINT NOT NULL
        REFERENCES pass1_source_folders(id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    absolute_path TEXT NOT NULL,
    media_type TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    file_signature CHAR(64),
    content_sha256 CHAR(64),
    content_size BIGINT,
    status TEXT NOT NULL CHECK (status IN ('processed', 'error')),
    reference_file_id BIGINT
        REFERENCES pass1_files(id) ON DELETE SET NULL,
    error_message TEXT,
    indexed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (folder_id, relative_path)
);

CREATE INDEX IF NOT EXISTS pass1_source_files_status_idx
    ON pass1_source_files (folder_id, status);

CREATE INDEX IF NOT EXISTS pass1_source_files_reference_idx
    ON pass1_source_files (reference_file_id);
"""

CANDIDATE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pass1_new_candidates (
    id BIGSERIAL PRIMARY KEY,
    reference_root TEXT NOT NULL,
    source_root TEXT NOT NULL,
    source_relative_path TEXT NOT NULL,
    source_path TEXT NOT NULL,
    media_type TEXT,
    original_filename TEXT NOT NULL,
    original_date DATE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    content_sha256 CHAR(64),
    content_size BIGINT,
    match_status TEXT NOT NULL CHECK (
        match_status IN ('new', 'duplicate', 'error')
    ),
    duplicate_reference_file_id BIGINT,
    duplicate_reference_path TEXT,
    destination_folder TEXT,
    destination_filename TEXT,
    destination_error TEXT,
    copied BOOLEAN NOT NULL DEFAULT FALSE,
    copied_at TIMESTAMPTZ,
    copy_error TEXT,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (reference_root, source_root, source_relative_path)
);

ALTER TABLE pass1_new_candidates
    ADD COLUMN IF NOT EXISTS destination_error TEXT;

CREATE INDEX IF NOT EXISTS pass1_new_candidates_copy_idx
    ON pass1_new_candidates (reference_root, match_status, copied);
"""

BINARY_DATE_PATTERN = re.compile(r"^(\d{4}):(\d{2}):(\d{2})")


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
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'pass1_folders'
                      AND column_name = 'root_path'
                )
                """
            )
            if cursor.fetchone()[0]:
                raise RuntimeError(
                    "The database has the previous Pass 1 schema. This version "
                    "does not migrate old data; run `pass1.py reset` to start "
                    "with an empty Pass 1 database."
                )
            for statement in SCHEMA_SQL.split(";"):
                if statement.strip():
                    cursor.execute(statement)


def ensure_source_schema(conn):
    with conn.transaction():
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'pass1_source_folders'
                      AND column_name = 'root_path'
                ) AND NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'pass1_source_folders'
                      AND column_name = 'library_id'
                )
                """
            )
            if cursor.fetchone()[0]:
                raise RuntimeError(
                    "The database has the previous Pass 1 source schema. Run "
                    "`pass1.py reset` to start with an empty Pass 1 database."
                )
            for statement in SOURCE_SCHEMA_SQL.split(";"):
                if statement.strip():
                    cursor.execute(statement)


def ensure_candidate_schema(conn):
    with conn.transaction():
        with conn.cursor() as cursor:
            for statement in CANDIDATE_SCHEMA_SQL.split(";"):
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


def get_library(conn, name):
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT id, root_path FROM pass1_libraries WHERE name = %s",
            (name,),
        )
        library = cursor.fetchone()
    if library is None:
        raise ValueError(f"Pass 1 library does not exist: {name}")
    return library[0], Path(library[1])


def register_folders(conn, library_id, root, folders):
    folder_states = {}
    with conn.transaction():
        with conn.cursor() as cursor:
            for folder in folders:
                cursor.execute(
                    """
                    INSERT INTO pass1_folders (
                        library_id, relative_path, absolute_path, scan_signature
                    )
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (library_id, relative_path) DO UPDATE
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
                        library_id,
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


def parse_exif_date(value):
    if not isinstance(value, str):
        return None
    match = BINARY_DATE_PATTERN.match(value.strip())
    if not match:
        return None
    try:
        return datetime.strptime("-".join(match.groups()), "%Y-%m-%d").date()
    except ValueError:
        return None


def candidate_destination(metadata, file_media_type):
    primary_tag = (
        "DateTimeOriginal" if file_media_type == "image" else "MediaCreateDate"
    )
    primary_date = parse_exif_date(metadata.get(primary_tag))
    if primary_date:
        return (
            primary_date,
            f"{file_media_type}/{primary_date:%Y/%m/%d}",
        )

    fallback_date = parse_exif_date(metadata.get("FileModifyDate"))
    if fallback_date:
        return (
            fallback_date,
            f"{file_media_type}/check_dates/{fallback_date:%Y/%m/%d}",
        )
    return None, None


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


def prepare_reference_hash_lookup(conn, library_id):
    with conn.transaction():
        with conn.cursor() as cursor:
            cursor.execute(
                """
                CREATE TEMP TABLE pass1_reference_hash_lookup
                ON COMMIT PRESERVE ROWS AS
                SELECT files.id, files.absolute_path, files.content_sha256,
                       files.content_size
                FROM pass1_files AS files
                JOIN pass1_folders AS folders ON folders.id = files.folder_id
                WHERE folders.library_id = %s
                  AND files.content_sha256 IS NOT NULL
                """,
                (library_id,),
            )
            cursor.execute(
                """
                CREATE INDEX pass1_reference_hash_lookup_hash_idx
                ON pass1_reference_hash_lookup (content_sha256, content_size)
                """
            )


def prepare_candidate_seen_paths(conn):
    with conn.transaction():
        with conn.cursor() as cursor:
            cursor.execute(
                """
                CREATE TEMP TABLE pass1_candidate_seen_paths (
                    source_relative_path TEXT PRIMARY KEY
                ) ON COMMIT PRESERVE ROWS
                """
            )


def find_reference_matches(conn, hashes):
    if not hashes:
        return {}
    content_hashes, content_sizes = zip(*hashes)
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT ON (
                       incoming.content_sha256, incoming.content_size
                   )
                   incoming.content_sha256,
                   incoming.content_size,
                   reference.id,
                   reference.absolute_path
            FROM unnest(%s::CHAR(64)[], %s::BIGINT[])
                AS incoming(content_sha256, content_size)
            JOIN pass1_reference_hash_lookup AS reference
              ON reference.content_sha256 = incoming.content_sha256
             AND reference.content_size = incoming.content_size
            ORDER BY incoming.content_sha256, incoming.content_size, reference.id
            """,
            (list(content_hashes), list(content_sizes)),
        )
        return {
            (content_sha256.strip(), content_size): (file_id, path)
            for content_sha256, content_size, file_id, path in cursor.fetchall()
        }


def candidate_row(
    reference_root,
    source_root,
    entry,
    metadata,
    file_media_type,
    content_sha256,
    content_size,
    match_status,
    reference_match,
    error_message,
):
    original_filename = metadata.get("FileName")
    if not isinstance(original_filename, str) or not original_filename:
        original_filename = entry.path.name
    original_date, destination_folder = candidate_destination(
        metadata, file_media_type
    )
    reference_file_id, reference_path = reference_match or (None, None)

    return (
        str(reference_root),
        str(source_root),
        entry.relative_path,
        str(entry.path),
        file_media_type,
        original_filename,
        original_date,
        Jsonb(metadata),
        content_sha256,
        content_size,
        match_status,
        reference_file_id,
        reference_path,
        destination_folder,
        original_filename,
        (
            "No usable primary or FileModifyDate for destination folder."
            if file_media_type in {"image", "video"}
            and destination_folder is None
            else None
        ),
        error_message,
    )


def store_candidates(conn, rows, seen_paths):
    if not rows and not seen_paths:
        return
    with conn.transaction():
        with conn.cursor() as cursor:
            if rows:
                cursor.executemany(
                    """
                    INSERT INTO pass1_new_candidates (
                        reference_root, source_root, source_relative_path,
                        source_path, media_type, original_filename, original_date,
                        metadata, content_sha256, content_size, match_status,
                        duplicate_reference_file_id, duplicate_reference_path,
                        destination_folder, destination_filename, destination_error,
                        error_message
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s
                    )
                    ON CONFLICT (
                        reference_root, source_root, source_relative_path
                    )
                    DO UPDATE SET
                        source_path = EXCLUDED.source_path,
                        media_type = EXCLUDED.media_type,
                        original_filename = EXCLUDED.original_filename,
                        original_date = EXCLUDED.original_date,
                        metadata = EXCLUDED.metadata,
                        content_sha256 = EXCLUDED.content_sha256,
                        content_size = EXCLUDED.content_size,
                        match_status = EXCLUDED.match_status,
                        duplicate_reference_file_id =
                            EXCLUDED.duplicate_reference_file_id,
                        duplicate_reference_path =
                            EXCLUDED.duplicate_reference_path,
                        destination_folder = EXCLUDED.destination_folder,
                        destination_filename = EXCLUDED.destination_filename,
                        destination_error = EXCLUDED.destination_error,
                        copied = CASE
                            WHEN pass1_new_candidates.content_sha256
                                = EXCLUDED.content_sha256
                            THEN pass1_new_candidates.copied
                            ELSE FALSE
                        END,
                        copied_at = CASE
                            WHEN pass1_new_candidates.content_sha256
                                = EXCLUDED.content_sha256
                            THEN pass1_new_candidates.copied_at
                            ELSE NULL
                        END,
                        copy_error = CASE
                            WHEN pass1_new_candidates.content_sha256
                                = EXCLUDED.content_sha256
                            THEN pass1_new_candidates.copy_error
                            ELSE NULL
                        END,
                        error_message = EXCLUDED.error_message,
                        updated_at = NOW()
                    """,
                    rows,
                )
            if seen_paths:
                cursor.executemany(
                    """
                    INSERT INTO pass1_candidate_seen_paths (source_relative_path)
                    VALUES (%s)
                    ON CONFLICT (source_relative_path) DO NOTHING
                    """,
                    [(path,) for path in seen_paths],
                )


def remove_stale_candidates(conn, reference_root, source_root):
    with conn.transaction():
        with conn.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM pass1_new_candidates AS candidate
                WHERE candidate.reference_root = %s
                  AND candidate.source_root = %s
                  AND NOT candidate.copied
                  AND NOT EXISTS (
                      SELECT 1
                      FROM pass1_candidate_seen_paths AS seen
                      WHERE seen.source_relative_path =
                            candidate.source_relative_path
                  )
                """,
                (str(reference_root), str(source_root)),
            )


def assign_candidate_filenames(conn, reference_root):
    with conn.transaction():
        with conn.cursor() as cursor:
            cursor.execute(
                """
                WITH ranked AS (
                    SELECT
                        id,
                        ROW_NUMBER() OVER (
                            PARTITION BY destination_folder,
                                         LOWER(original_filename)
                            ORDER BY source_root, source_relative_path, id
                        ) AS row_number
                    FROM pass1_new_candidates
                    WHERE reference_root = %s
                      AND (match_status = 'new' OR copied)
                      AND destination_folder IS NOT NULL
                )
                UPDATE pass1_new_candidates AS candidate
                SET destination_filename =
                    CASE
                        WHEN ranked.row_number = 1
                            THEN candidate.original_filename
                        WHEN candidate.original_filename ~ '\\.[^.]+$'
                            THEN REGEXP_REPLACE(
                                candidate.original_filename,
                                '(\\.[^.]+)$',
                                '_dup' || (ranked.row_number - 1)::TEXT || '\\1'
                            )
                        ELSE candidate.original_filename || '_dup'
                             || (ranked.row_number - 1)::TEXT
                    END,
                    updated_at = NOW()
                FROM ranked
                WHERE ranked.id = candidate.id
                  AND NOT candidate.copied
                  AND candidate.match_status = 'new'
                """,
                (str(reference_root),),
            )


def compare_folder(conn, reference_root, source_root, folder):
    files, _, _ = scan_directory(folder.path, source_root)
    counters = {
        "new": 0,
        "duplicate": 0,
        "error": 0,
        "skipped": 0,
        "needs_destination": 0,
    }
    for start in range(0, len(files), BATCH_SIZE):
        batch = files[start : start + BATCH_SIZE]
        metadata_by_path = extract_metadata(batch)
        processed = []
        input_hashes = []
        seen_paths = []
        for entry in batch:
            metadata, metadata_error = metadata_by_path[entry.path]
            file_type = media_type(metadata)
            if file_type not in {"image", "video"} and not metadata_error:
                counters["skipped"] += 1
                continue
            seen_paths.append(entry.relative_path)

            content_sha256 = None
            content_size = None
            errors = []
            try:
                content_sha256, content_size = hash_file(entry)
            except OSError as exc:
                errors.append(f"SHA-256 failed: {exc}")
            if metadata_error:
                errors.append(f"ExifTool failed: {metadata_error}")
            if file_type not in {"image", "video"}:
                errors.append("ExifTool did not identify this file as an image or video.")
            if content_sha256:
                input_hashes.append((content_sha256, content_size))
            processed.append(
                (entry, metadata, file_type, content_sha256, content_size, errors)
            )

        reference_matches = find_reference_matches(conn, input_hashes)
        candidate_rows = []
        for entry, metadata, file_type, content_sha256, content_size, errors in processed:
            reference_match = (
                reference_matches.get((content_sha256, content_size))
                if content_sha256
                else None
            )
            if reference_match:
                match_status = "duplicate"
            elif errors:
                match_status = "error"
            else:
                match_status = "new"

            supported_type = file_type if file_type in {"image", "video"} else None
            _, destination_folder = candidate_destination(metadata, supported_type)
            if supported_type and destination_folder is None:
                counters["needs_destination"] += 1

            candidate_rows.append(
                candidate_row(
                    reference_root,
                    source_root,
                    entry,
                    metadata,
                    supported_type,
                    content_sha256,
                    content_size,
                    match_status,
                    reference_match,
                    "; ".join(errors) or None,
                )
            )
            counters[match_status] += 1
            if errors and match_status != "error":
                counters["error"] += 1

        store_candidates(conn, candidate_rows, seen_paths)

        print(
            f"  {min(start + len(batch), len(files))}/{len(files)} files scanned "
            f"in {folder.relative_path}"
        )
    return counters


def compare_root(reference_library, source_root):
    source_root = source_root.expanduser().resolve()
    if not source_root.is_dir():
        raise NotADirectoryError(
            f"Incoming folder does not exist or is not a directory: {source_root}"
        )

    folders = discover_folders(source_root)
    print(
        f"Discovered {len(folders)} incoming directories under {source_root}; "
        "recording candidates only (no file copies)."
    )

    totals = {
        "new": 0,
        "duplicate": 0,
        "error": 0,
        "skipped": 0,
        "needs_destination": 0,
    }
    with psycopg.connect(**database_settings(), autocommit=True) as conn:
        ensure_schema(conn)
        library_id, reference_root = get_library(conn, reference_library)
        if not reference_root.is_dir():
            raise NotADirectoryError(
                "Library root does not exist or is not a directory: "
                f"{reference_root}"
            )
        acquire_root_lock(conn, reference_root)
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1
                FROM pass1_folders
                WHERE library_id = %s
                LIMIT 1
                """,
                (library_id,),
            )
            if cursor.fetchone() is None:
                raise ValueError(
                    f"Library has not been indexed by Pass 1: {reference_library}"
                )

        ensure_candidate_schema(conn)
        prepare_reference_hash_lookup(conn, library_id)
        prepare_candidate_seen_paths(conn)

        for index, folder in enumerate(folders, start=1):
            print(f"[{index}/{len(folders)}] Comparing {folder.relative_path}")
            folder_counts = compare_folder(
                conn, reference_root, source_root, folder
            )
            for key, count in folder_counts.items():
                totals[key] += count

        remove_stale_candidates(conn, reference_root, source_root)
        assign_candidate_filenames(conn, reference_root)

    print(
        "Comparison complete: "
        f"{totals['new']} new, {totals['duplicate']} duplicates, "
        f"{totals['error']} errors, {totals['needs_destination']} need manual "
        f"destination dates, {totals['skipped']} unsupported files skipped. "
        "Candidate rows are stored in pass1_new_candidates; no files were copied "
        "and pass1_folders/pass1_files were not changed."
    )
    return 1 if totals["error"] else 0


def register_source_folders(conn, library_id, root, folders):
    states = {}
    with conn.transaction():
        with conn.cursor() as cursor:
            for folder in folders:
                cursor.execute(
                    """
                    INSERT INTO pass1_source_folders (
                        library_id, root_path, relative_path, absolute_path,
                        scan_signature
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (library_id, root_path, relative_path) DO UPDATE
                    SET absolute_path = EXCLUDED.absolute_path,
                        status = CASE
                            WHEN pass1_source_folders.scan_signature
                                IS DISTINCT FROM EXCLUDED.scan_signature
                                OR pass1_source_folders.status = 'running'
                            THEN 'pending'
                            ELSE pass1_source_folders.status
                        END,
                        completed_at = CASE
                            WHEN pass1_source_folders.scan_signature
                                IS DISTINCT FROM EXCLUDED.scan_signature
                                OR pass1_source_folders.status = 'running'
                            THEN NULL
                            ELSE pass1_source_folders.completed_at
                        END,
                        error_message = CASE
                            WHEN pass1_source_folders.scan_signature
                                IS DISTINCT FROM EXCLUDED.scan_signature
                                OR pass1_source_folders.status = 'running'
                            THEN NULL
                            ELSE pass1_source_folders.error_message
                        END,
                        scan_signature = EXCLUDED.scan_signature
                    RETURNING id, status
                    """,
                    (
                        library_id,
                        str(root),
                        folder.relative_path,
                        str(folder.path),
                        folder.scan_signature,
                    ),
                )
                states[folder.relative_path] = cursor.fetchone()
    return states


def mark_source_folder(conn, folder_id, status, error_message=None):
    with conn.transaction():
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE pass1_source_folders
                SET status = %s,
                    started_at = CASE
                        WHEN %s = 'running' THEN NOW()
                        ELSE started_at
                    END,
                    completed_at = CASE
                        WHEN %s IN ('completed', 'completed_with_errors')
                        THEN NOW()
                        ELSE NULL
                    END,
                    error_message = %s
                WHERE id = %s
                """,
                (status, status, status, error_message, folder_id),
            )


def save_source_file(
    conn,
    folder_id,
    entry,
    media,
    metadata,
    sha256,
    content_size,
    status,
    reference_file_id=None,
    error_message=None,
):
    with conn.transaction():
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO pass1_source_files (
                    folder_id, relative_path, absolute_path, media_type, metadata,
                    file_signature, content_sha256, content_size, status,
                    reference_file_id, error_message
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (folder_id, relative_path) DO UPDATE
                SET absolute_path = EXCLUDED.absolute_path,
                    media_type = EXCLUDED.media_type,
                    metadata = EXCLUDED.metadata,
                    file_signature = EXCLUDED.file_signature,
                    content_sha256 = EXCLUDED.content_sha256,
                    content_size = EXCLUDED.content_size,
                    status = EXCLUDED.status,
                    reference_file_id = EXCLUDED.reference_file_id,
                    error_message = EXCLUDED.error_message,
                    indexed_at = NOW()
                """,
                (
                    folder_id,
                    entry.relative_path,
                    str(entry.path),
                    media,
                    Jsonb(metadata),
                    signature_for_file(entry.signature),
                    sha256,
                    content_size,
                    status,
                    reference_file_id,
                    error_message,
                ),
            )


def reference_file_for_hash(conn, sha256):
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT id
            FROM pass1_reference_hash_lookup
            WHERE content_sha256 = %s
            ORDER BY id
            LIMIT 1
            """,
            (sha256,),
        )
        row = cursor.fetchone()
        return row[0] if row else None


def remember_reference_file(conn, file_id, path, sha256, content_size):
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO pass1_reference_hash_lookup (
                id, absolute_path, content_sha256, content_size
            )
            VALUES (%s, %s, %s, %s)
            """,
            (file_id, str(path), sha256, content_size),
        )


def ensure_destination_folders(conn, library_id, root, destination_folder):
    relative_paths = ["."]
    parent = Path(destination_folder)
    if str(parent) != ".":
        relative_paths.extend(
            Path(*parent.parts[:index]).as_posix()
            for index in range(1, len(parent.parts) + 1)
        )

    folder_ids = {}
    with conn.transaction():
        with conn.cursor() as cursor:
            for relative_path in relative_paths:
                directory = root if relative_path == "." else root / relative_path
                _, scan_signature, _ = scan_directory(directory, root)
                cursor.execute(
                    """
                    INSERT INTO pass1_folders (
                        library_id, relative_path, absolute_path, scan_signature,
                        status, completed_at
                    )
                    VALUES (%s, %s, %s, %s, 'completed', NOW())
                    ON CONFLICT (library_id, relative_path) DO UPDATE
                    SET absolute_path = EXCLUDED.absolute_path,
                        scan_signature = EXCLUDED.scan_signature
                    RETURNING id
                    """,
                    (
                        library_id,
                        relative_path,
                        str(directory),
                        scan_signature,
                    ),
                )
                folder_ids[relative_path] = cursor.fetchone()[0]
    return folder_ids[relative_paths[-1]]


def candidate_filename(filename, suffix):
    if suffix == 0:
        return filename
    path = Path(filename)
    if path.suffix:
        return f"{path.stem}_dup{suffix}{path.suffix}"
    return f"{filename}_dup{suffix}"


def ensure_destination_directory(root, relative_directory):
    directory = root
    for component in Path(relative_directory).parts:
        directory = directory / component
        directory.mkdir(exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            raise OSError(
                f"Destination path contains a non-directory or symlink: {directory}"
            )
    return directory


def indexed_destination_exists(conn, library_id, path):
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT 1
            FROM pass1_files AS files
            JOIN pass1_folders AS folders ON folders.id = files.folder_id
            WHERE folders.library_id = %s AND files.absolute_path = %s
            LIMIT 1
            """,
            (library_id, str(path)),
        )
        return cursor.fetchone() is not None


def copy_without_overwrite(
    source, destination_directory, filename, library_id, conn
):
    suffix = 0
    while True:
        destination = destination_directory / candidate_filename(filename, suffix)
        if destination.exists() or os.path.lexists(destination) or indexed_destination_exists(
            conn, library_id, destination
        ):
            suffix += 1
            continue

        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=destination_directory, prefix=".pass1-copy-", delete=False
            ) as temporary:
                temporary_path = Path(temporary.name)
            shutil.copy2(source, temporary_path)
            try:
                os.link(temporary_path, destination)
            except FileExistsError:
                suffix += 1
                continue
            return destination
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def save_copied_file(
    conn,
    library_id,
    source_folder_id,
    source_entry,
    source_metadata,
    sha256,
    content_size,
    destination_root,
    destination,
    media,
):
    destination_stat = destination.stat()
    destination_entry = FileEntry(
        destination,
        destination.relative_to(destination_root).as_posix(),
        file_signature(destination_stat),
    )
    copied_sha256, copied_size = hash_file(destination_entry)
    if copied_sha256 != sha256 or copied_size != content_size:
        if file_signature(destination.stat()) == destination_entry.signature:
            destination.unlink()
        raise OSError("Copied file did not match the source SHA-256 and size.")

    destination_folder = destination.parent.relative_to(destination_root).as_posix()
    destination_folder_id = ensure_destination_folders(
        conn, library_id, destination_root, destination_folder
    )
    metadata = dict(source_metadata)
    metadata["SourceFile"] = str(destination)
    metadata["FileName"] = destination.name
    with conn.transaction():
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO pass1_files (
                    folder_id, relative_path, absolute_path, media_type, metadata,
                    file_signature, content_sha256, content_size, status
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'processed')
                ON CONFLICT (folder_id, relative_path) DO UPDATE
                SET absolute_path = EXCLUDED.absolute_path,
                    media_type = EXCLUDED.media_type,
                    metadata = EXCLUDED.metadata,
                    file_signature = EXCLUDED.file_signature,
                    content_sha256 = EXCLUDED.content_sha256,
                    content_size = EXCLUDED.content_size,
                    status = 'processed',
                    error_message = NULL,
                    indexed_at = NOW()
                RETURNING id
                """,
                (
                    destination_folder_id,
                    destination.name,
                    str(destination),
                    media,
                    Jsonb(metadata),
                    signature_for_file(destination_entry.signature),
                    copied_sha256,
                    copied_size,
                ),
            )
            reference_file_id = cursor.fetchone()[0]
            cursor.execute(
                """
                INSERT INTO pass1_source_files (
                    folder_id, relative_path, absolute_path, media_type, metadata,
                    file_signature, content_sha256, content_size, status,
                    reference_file_id
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'processed', %s)
                ON CONFLICT (folder_id, relative_path) DO UPDATE
                SET absolute_path = EXCLUDED.absolute_path,
                    media_type = EXCLUDED.media_type,
                    metadata = EXCLUDED.metadata,
                    file_signature = EXCLUDED.file_signature,
                    content_sha256 = EXCLUDED.content_sha256,
                    content_size = EXCLUDED.content_size,
                    status = 'processed',
                    reference_file_id = EXCLUDED.reference_file_id,
                    error_message = NULL,
                    indexed_at = NOW()
                """,
                (
                    source_folder_id,
                    source_entry.relative_path,
                    str(source_entry.path),
                    media,
                    Jsonb(source_metadata),
                    signature_for_file(source_entry.signature),
                    sha256,
                    content_size,
                    reference_file_id,
                ),
            )
    return reference_file_id, copied_sha256, copied_size


def copy_source_file(
    conn, library_id, destination_root, source_folder_id, entry, metadata
):
    media = media_type(metadata)
    if media not in {"image", "video"}:
        with conn.transaction():
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    DELETE FROM pass1_source_files
                    WHERE folder_id = %s AND relative_path = %s
                    """,
                    (source_folder_id, entry.relative_path),
                )
        return "skipped"

    sha256, content_size = hash_file(entry)
    reference_file_id = reference_file_for_hash(conn, sha256)
    if reference_file_id is not None:
        save_source_file(
            conn,
            source_folder_id,
            entry,
            media,
            metadata,
            sha256,
            content_size,
            "processed",
            reference_file_id,
        )
        return "duplicate"

    _, destination_folder = candidate_destination(metadata, media)
    if destination_folder is None:
        raise ValueError(
            "No usable primary media date or FileModifyDate for destination folder."
        )
    destination_directory = ensure_destination_directory(
        destination_root, destination_folder
    )
    original_filename = metadata.get("FileName")
    if not isinstance(original_filename, str) or not original_filename:
        original_filename = entry.path.name
    filename = Path(original_filename.replace("\\", "/")).name
    if not filename:
        filename = entry.path.name

    destination = copy_without_overwrite(
        entry.path, destination_directory, filename, library_id, conn
    )
    reference_file_id, copied_sha256, copied_size = save_copied_file(
        conn,
        library_id,
        source_folder_id,
        entry,
        metadata,
        sha256,
        content_size,
        destination_root,
        destination,
        media,
    )
    remember_reference_file(
        conn, reference_file_id, destination, copied_sha256, copied_size
    )
    return f"copied to {destination}"


def process_source_folder(
    conn,
    library_id,
    source_root,
    destination_root,
    folder,
    folder_id,
    folder_index,
    folder_total,
):
    files, _, _ = scan_directory(folder.path, source_root)
    processed = copied = duplicates = skipped = errors = 0
    print(f"  Found {len(files)} file(s) in {folder.relative_path}.", flush=True)
    for start in range(0, len(files), BATCH_SIZE):
        batch = files[start : start + BATCH_SIZE]
        batch_end = min(start + len(batch), len(files))
        print(
            f"  Extracting metadata for files {start + 1}-{batch_end}/{len(files)}.",
            flush=True,
        )
        try:
            metadata_by_path = extract_metadata(batch)
        except RuntimeError as exc:
            metadata_by_path = {entry.path: ({}, str(exc)) for entry in batch}
        for batch_index, entry in enumerate(batch, start=1):
            file_index = start + batch_index
            print(
                f"  [Folder {folder_index}/{folder_total}] "
                f"[File {file_index}/{len(files)}] Processing {entry.path.name}.",
                flush=True,
            )
            metadata, metadata_error = metadata_by_path[entry.path]
            if metadata_error:
                try:
                    sha256, content_size = hash_file(entry)
                except OSError as exc:
                    sha256, content_size = None, None
                    message = f"ExifTool failed: {metadata_error}; SHA-256 failed: {exc}"
                else:
                    message = f"ExifTool failed: {metadata_error}"
                save_source_file(
                    conn,
                    folder_id,
                    entry,
                    media_type(metadata),
                    metadata,
                    sha256,
                    content_size,
                    "error",
                    error_message=message,
                )
                report_error(f"ERROR: {entry.path}: {message}")
                print(
                    f"  [Folder {folder_index}/{folder_total}] "
                    f"[File {file_index}/{len(files)}] {entry.path.name}: ERROR",
                    flush=True,
                )
                errors += 1
                processed += 1
                continue

            if media_type(metadata) not in {"image", "video"}:
                copy_source_file(
                    conn, library_id, destination_root, folder_id, entry, metadata
                )
                skipped += 1
                print(
                    f"  [Folder {folder_index}/{folder_total}] "
                    f"[File {file_index}/{len(files)}] {entry.path.name}: "
                    "skipped (not image/video)",
                    flush=True,
                )
                continue

            try:
                result = copy_source_file(
                    conn, library_id, destination_root, folder_id, entry, metadata
                )
            except (OSError, ValueError) as exc:
                try:
                    sha256, content_size = hash_file(entry)
                except OSError:
                    sha256, content_size = None, None
                message = str(exc)
                save_source_file(
                    conn,
                    folder_id,
                    entry,
                    media_type(metadata),
                    metadata,
                    sha256,
                    content_size,
                    "error",
                    error_message=message,
                )
                report_error(f"ERROR: {entry.path}: {message}")
                print(
                    f"  [Folder {folder_index}/{folder_total}] "
                    f"[File {file_index}/{len(files)}] {entry.path.name}: ERROR",
                    flush=True,
                )
                errors += 1
            else:
                if result == "duplicate":
                    duplicates += 1
                    file_status = "already in library"
                elif result.startswith("copied to "):
                    copied += 1
                    file_status = result
                else:
                    file_status = result
                print(
                    f"  [Folder {folder_index}/{folder_total}] "
                    f"[File {file_index}/{len(files)}] {entry.path.name}: "
                    f"{file_status}",
                    flush=True,
                )
            processed += 1
    final_status = "completed_with_errors" if errors else "completed"
    mark_source_folder(
        conn,
        folder_id,
        final_status,
        f"{errors} file(s) had errors." if errors else None,
    )
    return processed, copied, duplicates, skipped, errors


def copy_source_root(source_root, library_name):
    source_root = source_root.expanduser().resolve()
    if not source_root.is_dir():
        raise NotADirectoryError(
            f"Source folder does not exist or is not a directory: {source_root}"
        )

    folders = discover_folders(source_root)
    print(f"Discovered {len(folders)} source directories under {source_root}.")
    totals = {
        "files": 0,
        "copied": 0,
        "duplicates": 0,
        "skipped": 0,
        "errors": 0,
        "folders_skipped": 0,
    }
    with psycopg.connect(**database_settings(), autocommit=True) as conn:
        ensure_schema(conn)
        ensure_source_schema(conn)
        library_id, destination_root = get_library(conn, library_name)
        if not destination_root.is_dir():
            raise NotADirectoryError(
                f"Library root does not exist or is not a directory: {destination_root}"
            )
        if (
            destination_root == source_root
            or source_root in destination_root.parents
            or destination_root in source_root.parents
        ):
            raise ValueError("Source and destination library roots must not overlap.")
        for root in sorted({source_root, destination_root}, key=str):
            acquire_root_lock(conn, root)
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1 FROM pass1_folders
                WHERE library_id = %s
                LIMIT 1
                """,
                (library_id,),
            )
            if cursor.fetchone() is None:
                raise ValueError(
                    f"Library has not been indexed by Pass 1: {library_name}"
                )
        prepare_reference_hash_lookup(conn, library_id)
        source_folder_ids = register_source_folders(
            conn, library_id, source_root, folders
        )

        for index, folder in enumerate(folders, start=1):
            folder_id, status = source_folder_ids[folder.relative_path]
            if status == "completed":
                print(
                    f"[Folder {index}/{len(folders)}] {folder.relative_path}: "
                    "already processed; skipping.",
                    flush=True,
                )
                totals["folders_skipped"] += 1
                continue
            print(
                f"[Folder {index}/{len(folders)}] Processing "
                f"{folder.relative_path}.",
                flush=True,
            )
            mark_source_folder(conn, folder_id, "running")
            try:
                counts = process_source_folder(
                    conn,
                    library_id,
                    source_root,
                    destination_root,
                    folder,
                    folder_id,
                    index,
                    len(folders),
                )
            except OSError as exc:
                mark_source_folder(conn, folder_id, "completed_with_errors", str(exc))
                report_error(f"ERROR: Cannot process source folder {folder.path}: {exc}")
                totals["errors"] += 1
                continue
            for key, count in zip(
                ("files", "copied", "duplicates", "skipped", "errors"), counts
            ):
                totals[key] += count

    print(
        "Source copy complete: "
        f"{totals['files']} media file(s) processed, {totals['copied']} copied, "
        f"{totals['duplicates']} linked to existing destination files, "
        f"{totals['skipped']} unsupported file(s) skipped, "
        f"{totals['errors']} error(s), "
        f"{totals['folders_skipped']} already-processed folder(s) skipped."
    )
    return 1 if totals["errors"] else 0


def process_folder(conn, root, folder, folder_id, folder_index, folder_total):
    files, scan_signature, _ = scan_directory(folder.path, root)
    print(
        f"  Found {len(files)} file(s) in {folder.relative_path}.",
        flush=True,
    )
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
            for file_index, entry in enumerate(batch, start=start + 1):
                print(
                    f"  [Folder {folder_index}/{folder_total}] "
                    f"[File {file_index}/{len(files)}] {entry.path.name}: reused",
                    flush=True,
                )
            continue

        batch_end = min(start + len(batch), len(files))
        print(
            f"  Extracting metadata for {len(todo)} pending file(s) "
            f"in batch {start + 1}-{batch_end}/{len(files)}.",
            flush=True,
        )
        metadata_by_path = extract_metadata(todo)
        outcomes = [
            (entry, *metadata_by_path[entry.path])
            for entry in todo
        ]
        error_count += store_batch(conn, folder_id, outcomes)
        for file_index, entry in enumerate(batch, start=start + 1):
            outcome = "reused" if entry not in todo else "recorded"
            print(
                f"  [Folder {folder_index}/{folder_total}] "
                f"[File {file_index}/{len(files)}] {entry.path.name}: {outcome}",
                flush=True,
            )
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


def compare_command(argv):
    parser = argparse.ArgumentParser(
        description=(
            "Compare an incoming folder with a Pass 1 reference and store a "
            "copy-manifest-style candidate plan."
        )
    )
    parser.add_argument("--reference-library", required=True)
    parser.add_argument("incoming_folder", type=Path)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        required=True,
        help=(
            "Store candidate rows only; do not copy files or change the "
            "existing pass1_folders/pass1_files tables."
        ),
    )
    args = parser.parse_args(argv)
    try:
        return compare_root(args.reference_library, args.incoming_folder)
    except KeyboardInterrupt:
        report_error("Comparison interrupted. Candidate rows already stored are retained.")
        return 130
    except (psycopg.Error, RuntimeError, OSError, ValueError) as exc:
        report_error(f"ERROR: Pass 1 comparison failed: {exc}")
        return 1


def copy_source_command(argv):
    parser = argparse.ArgumentParser(
        description=(
            "Copy new image and video files from a source tree into an indexed "
            "Pass 1 destination."
        )
    )
    parser.add_argument("library_name")
    parser.add_argument("source_folder", type=Path)
    args = parser.parse_args(argv)
    try:
        return copy_source_root(args.source_folder, args.library_name)
    except KeyboardInterrupt:
        report_error(
            "Copy interrupted. Completed file records are retained; rerun the "
            "same command to resume."
        )
        return 130
    except (psycopg.Error, RuntimeError, OSError, ValueError) as exc:
        report_error(f"ERROR: Pass 1 source copy failed: {exc}")
        return 1


def index_command(argv):
    parser = argparse.ArgumentParser(
        description=(
            "Index each directory's files into resumable pass1_ PostgreSQL tables."
        )
    )
    parser.add_argument("library_name")
    args = parser.parse_args(argv)

    try:
        with psycopg.connect(**database_settings(), autocommit=True) as conn:
            ensure_schema(conn)
            library_id, root = get_library(conn, args.library_name)
            if not root.is_dir():
                raise NotADirectoryError(
                    f"Library root does not exist or is not a directory: {root}"
                )
            folders = discover_folders(root)
            print(f"Discovered {len(folders)} directories under {root}")
            acquire_root_lock(conn, root)
            folder_states = register_folders(conn, library_id, root, folders)

            completed = 0
            skipped = 0
            files_processed = 0
            files_reused = 0
            error_count = 0
            for index, folder in enumerate(folders, start=1):
                folder_id, status = folder_states[folder.relative_path]
                if status == "completed":
                    skipped += 1
                    print(
                        f"[Folder {index}/{len(folders)}] {folder.relative_path}: "
                        "already processed; skipping.",
                        flush=True,
                    )
                    continue

                print(
                    f"[Folder {index}/{len(folders)}] "
                    f"Processing {folder.relative_path}.",
                    flush=True,
                )
                try:
                    file_count, file_reused, folder_errors = process_folder(
                        conn, root, folder, folder_id, index, len(folders)
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


def library_command(argv):
    parser = argparse.ArgumentParser(description="Manage Pass 1 photo libraries.")
    commands = parser.add_subparsers(dest="action", required=True)
    create_parser = commands.add_parser(
        "create", help="Register a library name and its root directory."
    )
    create_parser.add_argument("name")
    create_parser.add_argument("root", type=Path)
    commands.add_parser("list", help="List registered libraries.")
    args = parser.parse_args(argv)

    try:
        with psycopg.connect(**database_settings(), autocommit=True) as conn:
            ensure_schema(conn)
            if args.action == "create":
                if not args.name.strip():
                    raise ValueError("Library name must not be empty.")
                root = args.root.expanduser().resolve()
                if not root.is_dir():
                    raise NotADirectoryError(
                        f"Library root does not exist or is not a directory: {root}"
                    )
                with conn.transaction():
                    with conn.cursor() as cursor:
                        cursor.execute(
                            """
                            INSERT INTO pass1_libraries (name, root_path)
                            VALUES (%s, %s)
                            ON CONFLICT DO NOTHING
                            """,
                            (args.name, str(root)),
                        )
                        cursor.execute(
                            """
                            SELECT root_path FROM pass1_libraries WHERE name = %s
                            """,
                            (args.name,),
                        )
                        existing = cursor.fetchone()
                        if existing is None or existing[0] != str(root):
                            cursor.execute(
                                """
                                SELECT name FROM pass1_libraries WHERE root_path = %s
                                """,
                                (str(root),),
                            )
                            root_owner = cursor.fetchone()
                            if existing is not None:
                                raise ValueError(
                                    f"Library name {args.name!r} is already "
                                    f"registered for {existing[0]}."
                                )
                            if root_owner is not None:
                                raise ValueError(
                                    f"Library root {root} is already registered "
                                    f"as {root_owner[0]!r}."
                                )
                            raise RuntimeError("Library registration did not persist.")
                if existing:
                    print(f"Library {args.name} is already registered: {root}")
                else:
                    print(f"Created library {args.name}: {root}")
            else:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT name, root_path FROM pass1_libraries ORDER BY name"
                    )
                    libraries = cursor.fetchall()
                if not libraries:
                    print("No Pass 1 libraries registered.")
                for name, root in libraries:
                    print(f"{name}\t{root}")
        return 0
    except (psycopg.Error, RuntimeError, OSError, ValueError) as exc:
        report_error(f"ERROR: Pass 1 library command failed: {exc}")
        return 1


def reset_command(argv):
    parser = argparse.ArgumentParser(
        description="Delete all Pass 1 database tables and their data."
    )
    parser.parse_args(argv)
    print(
        "This will permanently delete all Pass 1 libraries, indexes, source "
        "records, and comparison candidates. It will not delete media files."
    )
    if input("Type RESET PASS1 to continue: ").strip() != "RESET PASS1":
        print("Pass 1 reset cancelled.")
        return 0
    try:
        with psycopg.connect(**database_settings()) as conn:
            with conn.cursor() as cursor:
                for table in (
                    "pass1_new_candidates",
                    "pass1_source_files",
                    "pass1_source_folders",
                    "pass1_files",
                    "pass1_folders",
                    "pass1_libraries",
                ):
                    cursor.execute(
                        sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(
                            sql.Identifier("public", table)
                        )
                    )
        print("Pass 1 database tables dropped.")
        return 0
    except (psycopg.Error, RuntimeError, ValueError) as exc:
        report_error(f"ERROR: Pass 1 reset failed: {exc}")
        return 1


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if arguments and arguments[0] == "library":
        return library_command(arguments[1:])
    if arguments and arguments[0] == "reset":
        return reset_command(arguments[1:])
    if arguments and arguments[0] == "compare":
        return compare_command(arguments[1:])
    if arguments and arguments[0] == "copy-source":
        return copy_source_command(arguments[1:])
    if arguments and arguments[0] == "index":
        return index_command(arguments[1:])
    parser = argparse.ArgumentParser(
        description="Index and organize image/video libraries with Pass 1."
    )
    parser.add_argument(
        "command",
        choices=("library", "index", "copy-source", "compare", "reset"),
    )
    parser.parse_args(arguments)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
