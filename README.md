# Photo metadata processor

These command-line tools inventory folders in a photo library, save selected
ExifTool metadata, and hash image files in PostgreSQL. The workflow has three
steps:

1. `inventory` finds folders and records them in PostgreSQL.
2. `export` reads metadata for folders that are not marked as processed.
3. `hash_photos.py` computes SHA-256 hashes for exported image files.

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
subfolders with `os.scandir()` and does not descend into symlinked directories.
It uses the original filesystem name bytes for traversal and decodes valid
UTF-8 paths without NFC/NFD normalization before storing them. Thus the
manifest and PostgreSQL keep the actual spelling returned by the mounted
filesystem. It then:

- Creates the database tables and index if they do not already exist.
- Inserts each discovered folder into `folders`.
- Streams discovered paths into the existing JSON-array format in
  `metadata_folders.json`, which a later `export` reads incrementally.
- Inserts folders into PostgreSQL in batches rather than retaining the entire
  folder inventory in memory.
- Leaves existing folder rows unchanged, including their processing status
  and processed timestamp. Newly discovered folders are inserted as pending.
- Clears the local `metadata_errors.log` at the start of the inventory run.

If one directory or entry cannot be accessed, inventory logs the path and
continues with other entries. A folder whose name cannot be decoded as UTF-8
is logged and skipped because PostgreSQL `TEXT` cannot store arbitrary
non-UTF-8 filesystem bytes.

Existing database folder rows are not deleted if their paths are absent from
the new inventory. Existing rows keep their IDs and original
`discovered_date` and processing state. A newly encountered path gets a new
folder row with `status = 'pending'` and a sequence-generated ID.

### Browse and manage folders

Launch the desktop folder browser from the project directory:

```sh
python3 folders_ui.py
```

The UI uses the same database settings and `PHOTOPROCESS_DB_PASSWORD`
environment variable as `main.py`. It displays folder ID, path, discovery
timestamp, status, and processed timestamp. Use the filters to search paths,
select a status, or enter inclusive date ranges for discovery and processing
dates. Enter dates as `YYYY-MM-DD`; leave a date field empty to omit that
bound. The path filter treats plain text as a case-insensitive substring; SQL
`%` and `_` wildcards can also be entered explicitly.

Select one or more rows in the folder list with the mouse (use Shift or
Command-click to extend selection on macOS) and choose an action:

- **Reset selected** sets `status` to `pending`, clears `processed_date`, and
  retains existing metadata. The folders become eligible for a later `export`,
  provided they are also present in `metadata_folders.json`. A successful
  export replaces their metadata.
- **Delete selected** permanently deletes the selected folder rows and their
  related metadata and processing-error rows. The UI confirms before deleting.
  Inventoried paths can be added again by running `inventory`, which creates
  new folder IDs for rows that were deleted.

The UI only changes database state; it does not edit the manifest or run
ExifTool.

### 2. Export metadata

```sh
python3 main.py export
```

Export reads `metadata_folders.json` incrementally, looks up paths in bounded
batches, and skips folders already marked `processed`. For each remaining
folder, it enumerates that folder's direct regular files with `os.scandir()`
and passes the exact original filesystem paths to ExifTool as argument-list
items (never through a shell). Large directories are passed in bounded
batches to avoid operating-system command-line length limits. Child folders
are processed as their own folder rows rather than recursively rescanned by
ExifTool. ExifTool returns JSON records, which are inserted into
`file_metadata`.

For each successful folder, the script replaces that folder's metadata rows
and updates its status from `pending` to `processed` with a `processed_date`.
Those database changes happen in one transaction. If no files yield metadata
records, the folder is still marked processed after a successful ExifTool run.

Running `export` repeatedly skips processed folders. Folders left pending due
to ExifTool errors are attempted again. Running `inventory` does not reset
existing folder statuses; only newly discovered folders start pending. Use
**Reset selected** in the folder UI when you want to reprocess selected
folders. Successful re-exports replace their existing metadata rather than
adding duplicate rows.

If a folder fails on a later run after having previously succeeded, its
previous metadata rows are not deleted by the failed run; the folder remains
pending until a successful export replaces those rows.

Each folder is committed independently. If the program is interrupted, already
completed folders remain marked `processed`; the current and remaining
`pending` folders are retried by running `export` again. The database status
and manifest are the resume state; this version does not use a CSV byte-offset
checkpoint or generate a CSV file.

#### Verify accented names on the NFS mount

The following focused check locates the two example directories by NFC
comparison but passes the original byte paths returned by `os.scandir()` to
the commands. It inventories and exports each selected directory separately.
If those folder rows are already `processed`, select them in `folders_ui.py`
and reset them first; inventory deliberately preserves existing status.

```sh
python3 - <<'PY'
import os
import subprocess
import sys
import unicodedata

parent = b"/Volumes/root_cfo211/cfo211_photo/2007"
wanted = {"Géraud", "Noël entre copains 281207"}
found = {}

with os.scandir(parent) as entries:
    for entry in entries:
        key = unicodedata.normalize("NFC", os.fsdecode(entry.name))
        if key in wanted and entry.is_dir(follow_symlinks=False):
            found[key] = entry.path

missing = wanted - found.keys()
if missing:
    raise SystemExit(f"Directories not found: {sorted(missing)}")

for name in sorted(wanted):
    original_path = os.fsdecode(found[name])
    print(f"Testing {name}: {original_path}")
    subprocess.run(
        [sys.executable, "main.py", "inventory", original_path],
        check=True,
    )
    subprocess.run([sys.executable, "main.py", "export"], check=True)
PY
```

The script only reads directory entries and photo metadata from the NFS mount.
Inventory writes its manifest beside `main.py` and folder state to PostgreSQL;
export writes metadata/state to PostgreSQL. Neither command renames, moves,
deletes, or writes photo files on the NAS.

### Hash photos for exact duplicate detection

After exporting metadata, run the hash script from the project directory:

```sh
python3 hash_photos.py
```

The script selects file paths from `file_metadata` whose `MIMEType` starts
with `image/`, reads each file on the machine running the script, and computes
a SHA-256 content hash in chunks. The photo library must therefore be mounted
and accessible at the paths recorded by ExifTool. Each result is upserted into
`file_content_hashes`, keyed by folder and file path. Running the script again
rehashes all currently listed image files, so changed file contents update
their stored hashes.

Files that cannot be read or change while being hashed are reported in
`metadata_errors.log` and recorded in `processing_errors` as `hash_file`.
Their previous hash record is removed so it cannot be mistaken for a current
result. Hash records for files no longer listed as images in `file_metadata`
are also removed. A hash run can be interrupted; run it again to refresh all
image-file hashes.

Use this query to list identical file contents found in more than one folder:

```sql
SELECT
    h.sha256,
    h.file_size,
    COUNT(*) AS copies,
    COUNT(DISTINCT h.folder_id) AS folder_count,
    STRING_AGG(
        f.path || ' / ' || h.file_path,
        E'\n' ORDER BY f.path, h.file_path
    ) AS file_locations
FROM file_content_hashes AS h
JOIN folders AS f ON f.id = h.folder_id
GROUP BY h.sha256, h.file_size
HAVING COUNT(DISTINCT h.folder_id) > 1
ORDER BY copies DESC, h.sha256;
```

Matching SHA-256 hashes identify byte-for-byte identical file contents with
overwhelming confidence. This checks file bytes, not whether two different
encodings depict the same image.

## PostgreSQL schema

The script creates these objects in the configured database:

### `folders`

| Column | Meaning |
|---|---|
| `id` | Sequence-generated primary key for the folder row |
| `path` | Unique folder path |
| `discovered_date` | Timestamp set when that path is first inserted |
| `status` | `pending` before a successful scan; `processed` after one |
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

Errors are retained across retries and inventory runs. The related tables use
foreign keys with `ON DELETE CASCADE`: deleting a folder row deletes its
metadata, processing-error, and hash rows as well. If the path is inventoried
again, it is inserted as a new folder row with a new ID.

### `file_content_hashes`

Created by `hash_photos.py`, this table stores a SHA-256 digest and byte size
for each hashed image path and folder. It references `folders.id` with
`ON DELETE CASCADE` and is uniquely keyed by `(folder_id, file_path)`.

The script uses `CREATE TABLE IF NOT EXISTS`. It also migrates legacy `NULL`
folder statuses to `pending`, updates the status default, and replaces the
previous status constraints. It does not migrate unrelated table structures;
inspect and migrate other schema differences separately.

## Errors and retries

Errors are reported to the terminal and appended to the local
`metadata_errors.log`. Each local log line starts with a UTC timestamp,
followed by a tab and the message. The log is cleared when `inventory` starts;
database error rows are not cleared.

Inventory and export errors are reported in the terminal and local log.
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
WHERE status = 'pending'
ORDER BY path;
```

## Local files

These files are located beside `main.py`:

- `metadata_folders.json` — folder list used by `export`
- `metadata_errors.log` — timestamped local error log

Metadata and processing state are stored in PostgreSQL, not in a local CSV.
