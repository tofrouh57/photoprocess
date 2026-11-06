BEGIN;

-- Prevent concurrent manifest inserts while choosing collision-free names.
LOCK TABLE public.image_copy_manifest IN EXCLUSIVE MODE;

CREATE TEMP TABLE remaining_manifest_inserted_ids (
    work_id BIGINT PRIMARY KEY
) ON COMMIT DROP;

WITH files_to_add AS (
    SELECT
        'remaining_duplicate_master'::TEXT AS source_type,
        "id" AS source_id,
        "FilePath" AS source_path,
        "FileName" AS original_filename,
        "FileModifyDate" AS original_date
    FROM public.remaining_duplicates
    WHERE copy_of IS NULL

    UNION ALL

    SELECT
        'remaining_no_duplicate'::TEXT AS source_type,
        "id" AS source_id,
        "FilePath" AS source_path,
        "FileName" AS original_filename,
        "FileModifyDate" AS original_date
    FROM public.remaining_no_duplicates
),
new_rows AS (
    INSERT INTO public.image_copy_manifest (
        source_type,
        source_id,
        source_path,
        original_filename,
        original_date,
        destination_folder,
        destination_filename,
        copied,
        copied_at,
        copy_error
    )
    SELECT
        files.source_type,
        files.source_id,
        files.source_path,
        files.original_filename,
        files.original_date,
        'image/check_dates/' || TO_CHAR(files.original_date, 'YYYY/MM'),
        files.original_filename,
        FALSE,
        NULL,
        NULL
    FROM files_to_add AS files
    WHERE NOT EXISTS (
        SELECT 1
        FROM public.image_copy_manifest AS existing
        WHERE existing.source_type = files.source_type
          AND existing.source_id = files.source_id
    )
    RETURNING work_id
)
INSERT INTO remaining_manifest_inserted_ids (work_id)
SELECT work_id
FROM new_rows;

-- Give newly added rows a _dupN filename where the destination name
-- already exists for the same folder and original filename.
WITH ranked AS (
    SELECT
        manifest.work_id,
        ROW_NUMBER() OVER (
            PARTITION BY
                manifest.destination_folder,
                LOWER(manifest.original_filename)
            ORDER BY
                CASE WHEN inserted.work_id IS NULL THEN 0 ELSE 1 END,
                manifest.source_id,
                manifest.work_id
        ) AS rn
    FROM public.image_copy_manifest AS manifest
    LEFT JOIN remaining_manifest_inserted_ids AS inserted
        ON inserted.work_id = manifest.work_id
)
UPDATE public.image_copy_manifest AS manifest
SET destination_filename =
    CASE
        WHEN ranked.rn = 1 THEN manifest.original_filename
        WHEN manifest.original_filename ~ '\.[^.]+$' THEN
            REGEXP_REPLACE(
                manifest.original_filename,
                '(\.[^.]+)$',
                '_dup' || (ranked.rn - 1)::TEXT || '\1'
            )
        ELSE
            manifest.original_filename || '_dup' || (ranked.rn - 1)::TEXT
    END
FROM ranked
WHERE ranked.work_id = manifest.work_id
  AND ranked.rn > 1
  AND EXISTS (
      SELECT 1
      FROM remaining_manifest_inserted_ids AS inserted
      WHERE inserted.work_id = manifest.work_id
  );

SELECT COUNT(*) AS rows_added
FROM remaining_manifest_inserted_ids;

-- Check for any destination-name collisions.
SELECT
    destination_folder,
    destination_filename,
    COUNT(*) AS occurrences,
    ARRAY_AGG(work_id ORDER BY work_id) AS work_ids
FROM public.image_copy_manifest
GROUP BY destination_folder, destination_filename
HAVING COUNT(*) > 1
ORDER BY occurrences DESC;

COMMIT;