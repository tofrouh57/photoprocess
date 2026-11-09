#!/usr/bin/env python3

import argparse
import hashlib
from pathlib import Path

import psycopg

from main import database_settings


DUPLICATES_SQL = """
WITH ranked AS (
    SELECT
        files.id,
        files.folder_id,
        files.relative_path,
        files.absolute_path,
        files.content_sha256,
        ROW_NUMBER() OVER (
            PARTITION BY files.content_sha256
            ORDER BY
                CASE
                    WHEN POSITION(
                        '/check_dates/' IN
                        ('/' || files.relative_path || '/')
                    ) > 0 THEN 1
                    ELSE 0
                END,
                CHAR_LENGTH(REGEXP_REPLACE(files.relative_path, '^.*/', '')),
                REGEXP_REPLACE(files.relative_path, '^.*/', '') COLLATE "C",
                files.relative_path COLLATE "C",
                files.id
        ) AS duplicate_rank
    FROM public.pass1_files AS files
    JOIN public.pass1_folders AS folders ON folders.id = files.folder_id
    WHERE folders.root_path = %s
      AND files.content_sha256 IS NOT NULL
),
keepers AS (
    SELECT content_sha256, relative_path AS keep_path
    FROM ranked
    WHERE duplicate_rank = 1
)
SELECT
    ranked.id,
    ranked.folder_id,
    ranked.relative_path,
    ranked.absolute_path,
    ranked.content_sha256,
    keepers.keep_path
FROM ranked
JOIN keepers USING (content_sha256)
WHERE ranked.duplicate_rank > 1
ORDER BY ranked.content_sha256, ranked.relative_path;
"""


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def checked_path(root, stored_path, expected_hash):
    path = Path(stored_path)

    if path.is_symlink():
        raise ValueError("refusing to delete a symlink")

    resolved = path.resolve(strict=True)
    resolved.relative_to(root)

    if not resolved.is_file():
        raise ValueError("path is not a regular file")

    if sha256_file(resolved) != expected_hash.strip():
        raise ValueError("file content no longer matches the database hash")

    return resolved


def main():
    parser = argparse.ArgumentParser(
        description="Preview or delete duplicate Pass 1 files and their DB rows."
    )
    parser.add_argument(
        "reference_root",
        type=Path,
        help="Exact root path used when indexing this reference with Pass 1.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Delete the listed files and their pass1_files rows.",
    )
    args = parser.parse_args()

    try:
        root = args.reference_root.expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError(f"not a directory: {root}")

        with psycopg.connect(**database_settings()) as conn:
            with conn.cursor() as cursor:
                cursor.execute(DUPLICATES_SQL, (str(root),))
                candidates = cursor.fetchall()
            conn.commit()

            if not candidates:
                print("No duplicate files found.")
                return 0

            for _, _, relative_path, _, _, keep_path in candidates:
                print(f"DELETE: {relative_path}")
                print(f"  KEEP: {keep_path}")

            print(f"\n{len(candidates)} file(s) selected.")
            if not args.apply:
                print("Preview only; nothing was changed. Add --apply to delete.")
                return 0

            confirmation = input(
                f"Type DELETE {len(candidates)} to remove these files and DB rows: "
            )
            if confirmation != f"DELETE {len(candidates)}":
                print("Cancelled; nothing was changed.")
                return 1

            deleted = 0
            errors = 0

            for file_id, folder_id, relative_path, absolute_path, file_hash, _ in candidates:
                try:
                    path = checked_path(root, absolute_path, file_hash)

                    with conn.transaction():
                        with conn.cursor() as cursor:
                            cursor.execute(
                                """
                                DELETE FROM public.pass1_files
                                WHERE id = %s AND folder_id = %s
                                RETURNING id
                                """,
                                (file_id, folder_id),
                            )
                            if cursor.fetchone() is None:
                                raise RuntimeError("database row no longer exists")

                        # The database deletion rolls back if unlinking fails.
                        path.unlink()

                    deleted += 1
                    print(f"Deleted: {relative_path}")
                except (OSError, ValueError, RuntimeError, psycopg.Error) as exc:
                    errors += 1
                    print(f"ERROR: Kept {relative_path}: {exc}")

        print(f"Finished: {deleted} deleted, {errors} error(s).")
        return 1 if errors else 0

    except (OSError, ValueError, psycopg.Error, RuntimeError) as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())