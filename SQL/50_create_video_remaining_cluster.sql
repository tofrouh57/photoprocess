BEGIN;

DROP TABLE IF EXISTS public.video_remaining_duplicates;
DROP TABLE IF EXISTS public.video_remaining_no_duplicates;

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
FROM public.video_remaining;

CREATE TABLE public.video_remaining_duplicates AS
SELECT
    source.*,
    CASE
        WHEN ranking.rn = 1 THEN NULL
        ELSE ranking.master_id
    END AS copy_of
FROM public.video_remaining AS source
JOIN remaining_duplicate_ranking AS ranking
    ON source."id" = ranking."id"
WHERE ranking.group_size > 1;

CREATE TABLE public.video_remaining_no_duplicates AS
SELECT source.*
FROM public.video_remaining AS source
WHERE NOT EXISTS (
    SELECT 1
    FROM public.video_remaining_duplicates AS duplicate
    WHERE duplicate."id" = source."id"
);

DROP TABLE remaining_duplicate_ranking;

COMMIT;

SELECT 'video_remaining' AS category, COUNT(*) AS total
FROM public.video_remaining

UNION ALL

SELECT 'video_remaining_duplicates', COUNT(*)
FROM public.video_remaining_duplicates

UNION ALL

SELECT 'video_remaining_no_duplicates', COUNT(*)
FROM public.video_remaining_no_duplicates;