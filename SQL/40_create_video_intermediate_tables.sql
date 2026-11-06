
BEGIN;

-- 1. Remove previous results
DROP TABLE IF EXISTS public.video_duplicates;
DROP TABLE IF EXISTS public.video_no_duplicates;
DROP TABLE IF EXISTS public.video_remaining;

-- 2. Identify and rank candidate groups
CREATE TEMP TABLE duplicate_ranking AS
SELECT
    "id",

    FIRST_VALUE("id") OVER (
        PARTITION BY
            "Make",
            "Model",
            UPPER("FileName"),
            "MediaCreateDate",
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
            "MediaCreateDate",
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
            "MediaCreateDate",
            "FileSize",
            "ImageWidth",
            "ImageHeight"
    ) AS group_size

FROM public.bronze_file_metadata
WHERE
    "MediaCreateDate" > TIMESTAMP '2003-12-01'
	AND "MediaCreateDate"::time > TIME '00:00:00' -- remove weird dates as 2003-01-01 0:0:0
    AND "MIMEType" ILIKE 'video%'
    AND "FileName" IS NOT NULL
    AND "FileSize" IS NOT NULL
    AND "ImageWidth" IS NOT NULL
    AND "ImageHeight" IS NOT NULL;


-- 3. Create table containing all members of duplicate groups
CREATE TABLE public.video_duplicates AS
SELECT
    source.*,
    CASE
        WHEN ranking.rn = 1 THEN NULL
        ELSE ranking.master_id
    END AS copy_of
FROM public.bronze_file_metadata AS source
JOIN duplicate_ranking AS ranking
    ON source."id" = ranking."id"
WHERE ranking.group_size > 1;


-- 4. Create table containing eligible files with zero duplicates
CREATE TABLE public.video_no_duplicates AS
SELECT source.*
FROM public.bronze_file_metadata AS source
WHERE
    source."MediaCreateDate" > TIMESTAMP '2003-12-01'
	AND "MediaCreateDate"::time > TIME '00:00:00'
    AND source."MIMEType" ILIKE 'video%'
    AND source."FileName" IS NOT NULL
    AND source."FileSize" IS NOT NULL
    AND source."ImageWidth" IS NOT NULL
    AND source."ImageHeight" IS NOT NULL
    AND NOT EXISTS (
        SELECT 1
        FROM public.video_duplicates AS duplicate
        WHERE duplicate."id" = source."id"
    );


-- 5. Create table containing files not evaluated by this process
CREATE TABLE public.video_remaining AS
SELECT source.*
FROM public.bronze_file_metadata AS source
WHERE "MIMEType" ILIKE 'video%'
AND NOT EXISTS (
    SELECT 1
    FROM public.video_duplicates AS duplicate
    WHERE duplicate."id" = source."id"
)
AND NOT EXISTS (
    SELECT 1
    FROM public.video_no_duplicates AS single_file
    WHERE single_file."id" = source."id"
);

-- 6. Clean up
DROP TABLE duplicate_ranking;

COMMIT;

----------------------------------------------------


SELECT 'Original' AS category, COUNT(*) AS total
FROM public.bronze_file_metadata

UNION ALL

SELECT 'video Duplicates', COUNT(*)
FROM public.video_duplicates

UNION ALL

SELECT 'video No duplicates', COUNT(*)
FROM public.video_no_duplicates

UNION ALL

SELECT 'video Remaining', COUNT(*)
FROM public.video_remaining;