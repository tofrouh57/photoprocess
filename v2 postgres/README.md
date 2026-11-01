# Photo metadata processor

This command-line tool inventories folders in a photo library and saves
selected ExifTool metadata in PostgreSQL. It has two steps:

1. `inventory` finds folders and records them in PostgreSQL.
2. `export` reads metadata for folders that are not marked as processed.

ExifTool must be installed and available on `PATH`. The application uses
Psycopg 3 to connect to PostgreSQL.

## Requirements

- Python 3.8 or later
- PostgreSQL reachable from this computer
- ExifTool installed and available as the `exiftool` command
- The PostgreSQL role must be allowed to connect to the configured database
  and create tables and indexes on the first run

Install the Python dependency from the project directory:

```sh
python3 -m pip install -r requirements.txt
```

## Database configuration

The default connection settings in `main.py` are:

| Setting | Default |
|---|---|
| Host | `192.168.1.160` |
| Port | `5432` |
| Database | `photoprocess` |
| User | `postgres` |
| Password | Read from `PHOTOPROCESS_DB_PASSWORD` |

Set the password in the same Terminal session used to run the commands:

```sh
export PHOTOPROCESS_DB_PASSWORD='your-database-password'
```

Optional environment variables can override the host, port, database, and
user:

```sh
export PHOTOPROCESS_DB_HOST='192.168.1.160'
export PHOTOPROCESS_DB_PORT='5432'
export PHOTOPROCESS_DB_NAME='photoprocess'
export PHOTOPROCESS_DB_USER='postgres'
```

Do not put the database password in `main.py` or commit it to source control.
The password environment variable is only available to programs launched from
that Terminal session. Set it again in a new session, or configure it through
your operating system's secret-management mechanism.

## Running the commands

Run commands from the project directory, or provide the path to `main.py`.
Generated local files are stored next to `main.py`, regardless of the current
working directory.

### 1. Inventory a photo library

```sh
python3 main.py inventory /path/to/photo-library
```

The root folder must exist. Inventory recursively discovers the root and its
subfolders, sorts their paths, and does not descend into symlinked directories.
It then:

- Creates the database tables and index if they do not already exist.
- Inserts each discovered folder into `folders`.
- Writes the discovered paths to `metadata_folders.json`, which is the list
  that a later `export` processes.
- Resets the processing status and processed timestamp of every discovered
  folder to pending, including folders that had already been processed.
- Clears the local `metadata_errors.log` at the start of the inventory run.

Existing database folder rows are not deleted if their paths are absent from
the new inventory. Existing rows keep their IDs and original
`discovered_date`; inventory updates only their processing status. A newly
encountered path gets a new folder row and a sequence-generated ID.

### 2. Export metadata

```sh
python3 main.py export
```

Export reads `metadata_folders.json`, looks up those paths in PostgreSQL, and
runs ExifTool once per folder that is not marked `processed`. ExifTool returns
JSON records, which are inserted into `file_metadata`.

For each successful folder, the script replaces that folder's metadata rows
and updates its status to `processed` with a `processed_date`. Those database
changes happen in one transaction. If no files yield metadata records, the
folder is still marked processed after a successful ExifTool run.

Running `export` repeatedly skips processed folders. Folders left pending due
to ExifTool errors are attempted again. Running `inventory` before `export`
resets all currently inventoried folders to pending, so the next export reads
them again. Successful re-exports replace their existing metadata rather than
adding duplicate rows.

If a folder fails on a later run after having previously succeeded, its
previous metadata rows are not deleted by the failed run; the folder remains
pending until a successful export replaces those rows.

## PostgreSQL schema

The script creates these objects in the configured database:

### `folders`

| Column | Meaning |
|---|---|
| `id` | Sequence-generated primary key for the folder row |
| `path` | Unique folder path |
| `discovered_date` | Timestamp set when that path is first inserted |
| `status` | `NULL` while pending; `processed` after a successful ExifTool run |
| `processed_date` | Timestamp set when processing succeeds; otherwise `NULL` |

Folder IDs are stable across inventory runs as long as the folder row remains
in the database and its path stays the same. Renaming or moving a folder changes
its path and is treated as a different folder. If a folder row is deleted and
later inventoried again, PostgreSQL creates a new row and assigns a new ID.
Sequence values may have gaps; that is normal.

### `file_metadata`

Stores one row per file and folder. Each row references `folders.id` and has a
column for each field requested by the `FIELDS` list in `main.py`. These
ExifTool fields are stored as PostgreSQL `TEXT`; values that ExifTool returns
as lists or objects are serialized as JSON text. A uniqueness constraint
prevents duplicate rows for the same `FilePath` within a folder.

### `processing_errors`

Stores processing errors associated with a folder:

| Column | Meaning |
|---|---|
| `id` | Sequence-generated error ID |
| `folder_id` | The related folder |
| `occurred_at` | Database timestamp for the error |
| `error_type` | For example `exiftool_command`, `exiftool_file`, or `exiftool_json` |
| `exit_code` | ExifTool exit code when available |
| `error_message` | Diagnostic text |

Errors are retained across retries and inventory runs. Both child tables use
foreign keys with `ON DELETE CASCADE`: deleting a folder row deletes its
metadata and processing-error rows as well. If the path is inventoried again,
it is inserted as a new folder row with a new ID.

The script uses `CREATE TABLE IF NOT EXISTS`. It creates these definitions for
a new database, but does not migrate or repair tables that already exist with
an older or different structure. Inspect and migrate an existing schema
separately before relying on new constraints.

## Errors and retries

Errors are reported to the terminal and appended to the local
`metadata_errors.log`. Each local log line starts with a UTC timestamp,
followed by a tab and the message. The log is cleared when `inventory` starts;
database error rows are not cleared.

ExifTool errors can be reported in two ways:

- A nonzero process exit code is recorded as an `exiftool_command` error, with
  the exit code and ExifTool's standard-error diagnostic output.
- An `Error` field in an individual ExifTool JSON record is recorded as an
  `exiftool_file` error, including the affected file path.

If ExifTool cannot be launched, the launch error is logged and stored as an
`exiftool_launch` error. Invalid ExifTool JSON is stored as an
`exiftool_json` error and stops the current export run. Failed folders are not
marked processed; export continues to the next folder for ordinary command
and per-file errors. Correct the underlying issue and rerun `export` to retry
pending folders.

Example: query recent processing errors:

```sql
SELECT e.occurred_at,
       f.path AS folder_path,
       e.error_type,
       e.exit_code,
       e.error_message
FROM processing_errors AS e
JOIN folders AS f ON f.id = e.folder_id
ORDER BY e.occurred_at DESC;
```

Example: check folders that still need processing:

```sql
SELECT id, path, discovered_date
FROM folders
WHERE status IS NULL
ORDER BY path;
```

## Local files

These files are located beside `main.py`:

- `metadata_folders.json` — folder list used by `export`
- `metadata_errors.log` — timestamped local error log

Metadata and processing state are stored in PostgreSQL, not in a local CSV.
