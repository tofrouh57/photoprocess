BEGIN;

DROP TABLE IF EXISTS public.remaining_duplicates;
DROP TABLE IF EXISTS public.remaining_no_duplicates;

CREATE TEMP TABLE remaining_duplicate_ranking
ON COMMIT DROP AS
SELECT
    "id",
    FIRST_VALUE("id") OVER (
        PARTITION BY
            "Make",
            "Model",
            UPPER("FileName"),
            "FileModifyDate",
            "FileSize",
            "ImageWidth",
            "ImageHeight"
        ORDER BY "id"
    ) AS master_id,
    ROW_NUMBER() OVER (
        PARTITION BY
            "Make",
            "Model",
            UPPER("FileName"),
            "FileModifyDate",
            "FileSize",
            "ImageWidth",
            "ImageHeight"
        ORDER BY "id"
    ) AS rn,
    COUNT(*) OVER (
        PARTITION BY
            "Make",
            "Model",
            UPPER("FileName"),
            "FileModifyDate",
            "FileSize",
            "ImageWidth",
            "ImageHeight"
    ) AS group_size
FROM public.image_remaining;

CREATE TABLE public.remaining_duplicates AS
SELECT
    source.*,
    CASE
        WHEN ranking.rn = 1 THEN NULL
        ELSE ranking.master_id
    END AS copy_of
FROM public.image_remaining AS source
JOIN remaining_duplicate_ranking AS ranking
    ON source."id" = ranking."id"
WHERE ranking.group_size > 1;

CREATE TABLE public.remaining_no_duplicates AS
SELECT source.*
FROM public.image_remaining AS source
WHERE NOT EXISTS (
    SELECT 1
    FROM public.remaining_duplicates AS duplicate
    WHERE duplicate."id" = source."id"
);

DROP TABLE remaining_duplicate_ranking;

COMMIT;

SELECT 'image_remaining' AS category, COUNT(*) AS total
FROM public.image_remaining

UNION ALL

SELECT 'remaining_duplicates', COUNT(*)
FROM public.remaining_duplicates

UNION ALL

SELECT 'remaining_no_duplicates', COUNT(*)
FROM public.remaining_no_duplicates;