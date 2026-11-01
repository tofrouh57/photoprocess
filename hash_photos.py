#!/usr/bin/env python3

import hashlib
import os
from pathlib import Path

import psycopg

from main import database_settings, ensure_schema, report_error


def file_signature(file_stat):
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
    )


def hash_file(path):
    """Return a file's SHA-256 and size, rejecting files changed while read."""
    digest = hashlib.sha256()

    with path.open("rb") as source:
        before = os.fstat(source.fileno())
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
        after = os.fstat(source.fileno())

    current = path.stat()
    if not (
        file_signature(before)
        == file_signature(after)
        == file_signature(current)
    ):
        raise OSError("File changed while it was being hashed.")

    return digest.hexdigest(), before.st_size


def ensure_hash_schema(conn):
    with conn.cursor() as cursor:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS file_content_hashes (
                id BIGSERIAL PRIMARY KEY,
                folder_id BIGINT NOT NULL
                    REFERENCES folders(id) ON DELETE CASCADE,
                file_path TEXT NOT NULL,
                sha256 CHAR(64) NOT NULL,
                file_size BIGINT NOT NULL,
                hashed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (folder_id, file_path)
            )
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS file_content_hashes_sha256_idx
            ON file_content_hashes (sha256)
            """
        )


def hash_photos():
    with psycopg.connect(**database_settings(), autocommit=True) as conn:
        ensure_schema(conn)
        ensure_hash_schema(conn)

        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT m.folder_id, m."FilePath"
                FROM file_metadata AS m
                WHERE m."FilePath" IS NOT NULL
                  AND m."FilePath" <> ''
                  AND m."MIMEType" ILIKE 'image/%'
                ORDER BY m.folder_id, m."FilePath"
                """
            )
            files = cursor.fetchall()

        succeeded = 0
        failed = 0
        total = len(files)
        print(f"Hashing {total} image files")

        for index, (folder_id, file_path) in enumerate(files, start=1):
            path = Path(file_path)
            print(f"[{index}/{total}] {path}", flush=True)

            try:
                digest, file_size = hash_file(path)
            except OSError as exc:
                failed += 1
                message = f"ERROR: Cannot hash image file {path}: {exc}"
                report_error(message)
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        DELETE FROM file_content_hashes
                        WHERE folder_id = %s AND file_path = %s
                        """,
                        (folder_id, file_path),
                    )
                    cursor.execute(
                        """
                        INSERT INTO processing_errors
                            (folder_id, error_type, error_message)
                        VALUES (%s, 'hash_file', %s)
                        """,
                        (folder_id, message),
                    )
                continue

            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO file_content_hashes
                        (folder_id, file_path, sha256, file_size)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (folder_id, file_path) DO UPDATE
                    SET sha256 = EXCLUDED.sha256,
                        file_size = EXCLUDED.file_size,
                        hashed_at = NOW()
                    """,
                    (folder_id, file_path, digest, file_size),
                )
            succeeded += 1

        with conn.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM file_content_hashes AS h
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM file_metadata AS m
                    WHERE m.folder_id = h.folder_id
                      AND m."FilePath" = h.file_path
                      AND m."MIMEType" ILIKE 'image/%'
                )
                """
            )

        print(
            f"Hashing complete: {succeeded} succeeded, {failed} failed."
        )


def main():
    try:
        hash_photos()
    except (psycopg.Error, RuntimeError, ValueError) as exc:
        message = f"ERROR: Photo hashing failed: {exc}"
        report_error(message)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
