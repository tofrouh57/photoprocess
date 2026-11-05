#!/usr/bin/env python3

import sys

import psycopg

from main import database_settings, ensure_schema, report_error


def find_duplicate_candidates():
    """Report image files with matching filename and selected EXIF metadata."""
    query = """
        SELECT
            m."FileName",
            m."DateTimeOriginal",
            m."Make",
            m."Model",
            m."ImageWidth",
            m."ImageHeight",
            COUNT(*) AS copies,
            COUNT(DISTINCT m.folder_id) AS folder_count,
            ARRAY_AGG(
                f.path || ' / ' || m."FilePath"
                ORDER BY f.path, m."FilePath"
            ) AS file_locations
        FROM file_metadata AS m
        JOIN folders AS f ON f.id = m.folder_id
        WHERE m."MIMEType" ILIKE 'image/%'
          AND m."FileName" IS NOT NULL
          AND m."FileName" <> ''
          AND m."DateTimeOriginal" IS NOT NULL
          AND m."DateTimeOriginal" <> ''
          AND m."ImageWidth" IS NOT NULL
          AND m."ImageHeight" IS NOT NULL
        GROUP BY
            m."FileName",
            m."DateTimeOriginal",
            m."Make",
            m."Model",
            m."ImageWidth",
            m."ImageHeight"
        HAVING COUNT(DISTINCT m.folder_id) > 1
        ORDER BY copies DESC, m."FileName"
    """

    with psycopg.connect(**database_settings(), autocommit=True) as conn:
        ensure_schema(conn)
        with conn.cursor() as cursor:
            cursor.execute(query)
            candidates = cursor.fetchall()

    if not candidates:
        print("No metadata duplicate candidates found.")
        return

    print(f"Found {len(candidates)} metadata duplicate candidate groups.")
    for index, candidate in enumerate(candidates, start=1):
        (
            filename,
            date_time_original,
            make,
            model,
            image_width,
            image_height,
            copies,
            folder_count,
            file_locations,
        ) = candidate
        print(
            f"\n[{index}] {filename} "
            f"({copies} files in {folder_count} folders)"
        )
        print(
            f"    DateTimeOriginal: {date_time_original}; "
            f"Camera: {make or 'unknown'} {model or 'unknown'}; "
            f"Dimensions: {image_width}x{image_height}"
        )
        for location in file_locations:
            print(f"    {location}")


def main():
    if len(sys.argv) != 1:
        sys.exit("This command takes no arguments.")

    try:
        find_duplicate_candidates()
    except (psycopg.Error, RuntimeError, ValueError) as exc:
        message = f"ERROR: Duplicate candidate search failed: {exc}"
        report_error(message)
        sys.exit(1)


if __name__ == "__main__":
    main()
