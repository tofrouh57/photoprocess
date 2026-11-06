DROP TABLE IF EXISTS public.bronze_file_metadata;

CREATE TABLE public.bronze_file_metadata AS
SELECT *
FROM public.file_metadata;
--where "MIMEType" like 'image%';

ALTER TABLE public.bronze_file_metadata

ALTER COLUMN "FileCreateDate"
    TYPE timestamp without time zone
    USING to_timestamp(NULLIF(TRIM("FileCreateDate"), ''),
                       'YYYY:MM:DD HH24:MI:SS') AT TIME ZONE 'UTC',

ALTER COLUMN "FileModifyDate"
    TYPE timestamp without time zone
    USING to_timestamp(NULLIF(TRIM("FileModifyDate"), ''),
                       'YYYY:MM:DD HH24:MI:SS') AT TIME ZONE 'UTC',

ALTER COLUMN "FileAccessDate"
    TYPE timestamp without time zone
    USING to_timestamp(NULLIF(TRIM("FileAccessDate"), ''),
                       'YYYY:MM:DD HH24:MI:SS') AT TIME ZONE 'UTC',

ALTER COLUMN "FileInodeChangeDate"
    TYPE timestamp without time zone
    USING to_timestamp(NULLIF(TRIM("FileInodeChangeDate"), ''),
                       'YYYY:MM:DD HH24:MI:SS') AT TIME ZONE 'UTC',

ALTER COLUMN "DateTimeOriginal"
    TYPE timestamp without time zone
    USING to_timestamp(NULLIF(TRIM("DateTimeOriginal"), ''),
                       'YYYY:MM:DD HH24:MI:SS') AT TIME ZONE 'UTC',

ALTER COLUMN "CreateDate"
    TYPE timestamp without time zone
    USING to_timestamp(NULLIF(TRIM("CreateDate"), ''),
                       'YYYY:MM:DD HH24:MI:SS') AT TIME ZONE 'UTC',

ALTER COLUMN "ModifyDate"
    TYPE timestamp without time zone
    USING to_timestamp(NULLIF(TRIM("ModifyDate"), ''),
                       'YYYY:MM:DD HH24:MI:SS') AT TIME ZONE 'UTC',

ALTER COLUMN "MetadataDate"
    TYPE timestamp without time zone
    USING to_timestamp(NULLIF(TRIM("MetadataDate"), ''),
                       'YYYY:MM:DD HH24:MI:SS') AT TIME ZONE 'UTC',

ALTER COLUMN "DateAcquired"
    TYPE timestamp without time zone
    USING to_timestamp(NULLIF(TRIM("DateAcquired"), ''),
                       'YYYY:MM:DD HH24:MI:SS') AT TIME ZONE 'UTC',

ALTER COLUMN "MediaCreateDate"
    TYPE timestamp without time zone
    USING to_timestamp(NULLIF(TRIM("MediaCreateDate"), ''),
                       'YYYY:MM:DD HH24:MI:SS') AT TIME ZONE 'UTC',

ALTER COLUMN "MediaModifyDate"
    TYPE timestamp without time zone
    USING to_timestamp(NULLIF(TRIM("MediaModifyDate"), ''),
                       'YYYY:MM:DD HH24:MI:SS') AT TIME ZONE 'UTC',

ALTER COLUMN "TrackCreateDate"
    TYPE timestamp without time zone
    USING to_timestamp(NULLIF(TRIM("TrackCreateDate"), ''),
                       'YYYY:MM:DD HH24:MI:SS') AT TIME ZONE 'UTC',

ALTER COLUMN "TrackModifyDate"
    TYPE timestamp without time zone
    USING to_timestamp(NULLIF(TRIM("TrackModifyDate"), ''),
                       'YYYY:MM:DD HH24:MI:SS') AT TIME ZONE 'UTC';

