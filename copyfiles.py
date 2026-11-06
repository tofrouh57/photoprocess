#!/usr/bin/env python3

import argparse
import os
import shutil
import sys
from pathlib import Path, PureWindowsPath

import psycopg
from psycopg import sql

from main import database_settings, report_error


MANIFEST_TABLE = "image_copy_manifest"
MANIFEST_COLUMNS = {
    "work_id",
    "source_path",
    "destination_folder",
    "destination_filename",
    "copied",
    "copied_at",
    "copy_error",
}


def validate_manifest(conn):
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            """,
            (MANIFEST_TABLE,),
        )
        columns = {row[0] for row in cursor.fetchall()}

    missing_columns = MANIFEST_COLUMNS - columns
    if missing_columns:
        missing = ", ".join(sorted(missing_columns))
        raise RuntimeError(
            f"public.{MANIFEST_TABLE} is missing required columns: {missing}"
        )


def destination_path(destination_root, destination_folder, filename):
    if not destination_folder or not filename:
        raise ValueError("Manifest destination folder or filename is empty")
    if (
        Path(destination_folder).is_absolute()
        or PureWindowsPath(destination_folder).is_absolute()
        or PureWindowsPath(destination_folder).drive
        or "\\" in destination_folder
    ):
        raise ValueError("Manifest destination folder must be a relative path")
    if (
        Path(filename).name != filename
        or filename in {".", ".."}
        or "\\" in filename
    ):
        raise ValueError("Manifest destination filename must be a single filename")

    target = destination_root / destination_folder / filename
    try:
        target.parent.resolve().relative_to(destination_root)
    except ValueError as exc:
        raise ValueError("Manifest destination resolves outside the destination folder") from exc
    return target


def copy_file(source, target, mode):
    source = source.resolve(strict=True)
    if not source.is_file():
        raise FileNotFoundError(f"Source is not a file: {source}")

    target.parent.mkdir(parents=True, exist_ok=True)
    if mode == "physical":
        if target.is_symlink():
            raise FileExistsError(f"Refusing to overwrite destination symlink: {target}")
        shutil.copy2(source, target)
    elif mode == "symlink":
        if target.is_symlink() and os.readlink(target) == str(source):
            return
        target.symlink_to(source)
    else:
        raise ValueError(f"Unsupported copy mode: {mode}")


def copy_manifest(mode, destination):
    destination_root = destination.expanduser().resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    if not destination_root.is_dir():
        raise NotADirectoryError(
            f"Destination is not a directory: {destination_root}"
        )

    with psycopg.connect(**database_settings(), autocommit=True) as conn:
        validate_manifest(conn)
        manifest_table = sql.Identifier("public", MANIFEST_TABLE)
        with conn.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                    SELECT work_id, source_path, destination_folder,
                           destination_filename
                    FROM {}
                    WHERE copied IS FALSE
                    ORDER BY work_id
                    """
                ).format(manifest_table)
            )
            pending = cursor.fetchall()

        remaining = len(pending)
        failures = 0
        print(f"{remaining} files remaining to copy")

        for index, (work_id, source_value, relative_folder, filename) in enumerate(
            pending, start=1
        ):
            try:
                if not source_value:
                    raise ValueError("Manifest source path is empty")
                source = Path(source_value)
                target = destination_path(
                    destination_root, relative_folder, filename
                )
                copy_file(source, target, mode)
            except (OSError, TypeError, ValueError) as exc:
                failures += 1
                message = f"ERROR: Cannot {mode} {source_value!r}: {exc}"
                with conn.cursor() as cursor:
                    cursor.execute(
                        sql.SQL(
                            """
                            UPDATE {}
                            SET copied = FALSE, copied_at = NULL, copy_error = %s
                            WHERE work_id = %s
                            """
                        ).format(manifest_table),
                        (str(exc), work_id),
                    )
                report_error(message)
            else:
                with conn.cursor() as cursor:
                    cursor.execute(
                        sql.SQL(
                            """
                            UPDATE {}
                            SET copied = TRUE, copied_at = NOW(), copy_error = NULL
                            WHERE work_id = %s
                            """
                        ).format(manifest_table),
                        (work_id,),
                    )
                remaining -= 1

            print(
                f"[{index}/{len(pending)}] {filename}: "
                f"{remaining} files remaining to copy",
                flush=True,
            )

    if failures:
        print(f"Copy finished with {failures} failure(s).", file=sys.stderr)
        return 1
    print("Copy complete.")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Copy pending entries from public.image_copy_manifest "
            "to a destination folder."
        )
    )
    parser.add_argument(
        "mode",
        choices=("symlink", "physical"),
        help="Create symbolic links or physical file copies.",
    )
    parser.add_argument(
        "destination_folder",
        type=Path,
        help="Root folder under which manifest destination paths are created.",
    )
    args = parser.parse_args()

    try:
        return copy_manifest(args.mode, args.destination_folder)
    except (psycopg.Error, RuntimeError, OSError, TypeError, ValueError) as exc:
        report_error(f"ERROR: File copy process failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())