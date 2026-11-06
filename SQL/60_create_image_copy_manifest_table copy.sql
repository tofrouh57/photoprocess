CREATE EXTENSION IF NOT EXISTS unaccent;


BEGIN;

DROP TABLE IF EXISTS public.image_copy_manifest;

CREATE TABLE public.image_copy_manifest AS

WITH files_to_copy AS (

    -- Master files from duplicate groups
    SELECT
        'duplicate_master'::TEXT AS source_type,
        "id" AS source_id,
        "FilePath" AS source_path,
        "FileName" AS original_filename,
        "DateTimeOriginal" AS original_date
    FROM public.image_duplicates
    WHERE copy_of IS NULL

    UNION ALL

    -- Files without metadata duplicates
    SELECT
        'no_duplicate'::TEXT AS source_type,
        "id" AS source_id,
        "FilePath" AS source_path,
        "FileName" AS original_filename,
        "DateTimeOriginal" AS original_date
    FROM public.image_no_duplicates
)

SELECT
    source_type,
    source_id,
    source_path,
    original_filename,
    original_date,

    -- Destination folder: image/YYYY/MM
    'image/' || TO_CHAR(original_date, 'YYYY/MM') AS destination_folder,

    -- Uppercase filename without accents
    --UPPER(UNACCENT(original_filename)) AS destination_filename,
	original_filename AS destination_filename,

    FALSE AS copied,

    NULL::TIMESTAMP AS copied_at,
    NULL::TEXT AS copy_error

FROM files_to_copy;

-- Add a unique identifier for each copy task
ALTER TABLE public.image_copy_manifest
ADD COLUMN work_id BIGINT GENERATED ALWAYS AS IDENTITY;

ALTER TABLE public.image_copy_manifest
ADD PRIMARY KEY (work_id);

COMMIT;


-- check for duplicates


SELECT
    destination_folder,
    destination_filename,
    COUNT(*) AS occurrences,
    ARRAY_AGG(work_id ORDER BY work_id) AS work_ids
FROM public.image_copy_manifest
GROUP BY
    destination_folder,
    destination_filename
HAVING COUNT(*) > 1
ORDER BY occurrences DESC;


-- rename existing duplicate file names

WITH ranked AS (
    SELECT
        work_id,
        ROW_NUMBER() OVER (
            PARTITION BY
                destination_folder,
                LOWER(original_filename)
            ORDER BY source_id, work_id
        ) AS rn
    FROM public.image_copy_manifest
)
UPDATE public.image_copy_manifest AS m
SET destination_filename =
    CASE
        WHEN r.rn = 1 THEN m.original_filename

        WHEN m.original_filename ~ '\.[^.]+$' THEN
            REGEXP_REPLACE(
                m.original_filename,
                '(\.[^.]+)$',
                '_dup' || (r.rn - 1)::TEXT || '\1'
            )

        ELSE
            m.original_filename || '_dup' || (r.rn - 1)::TEXT
    END
FROM ranked AS r
WHERE m.work_id = r.work_id;


-- check for duplicates


SELECT
    destination_folder,
    destination_filename,
    COUNT(*) AS occurrences,
    ARRAY_AGG(work_id ORDER BY work_id) AS work_ids
FROM public.image_copy_manifest
GROUP BY
    destination_folder,
    destination_filename
HAVING COUNT(*) > 1
ORDER BY occurrences DESC;





