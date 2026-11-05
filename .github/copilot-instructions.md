# Copilot Instructions

## Project architecture

This repository is a small Python photo-library workflow backed by PostgreSQL:

- `main.py` provides the `inventory` and `export` CLI commands. Inventory discovers directories, updates the `folders` table, and writes the export manifest. Export reads that manifest, runs ExifTool for folders not marked processed, and stores selected fields from `FIELDS` in `file_metadata`.
- `folders_ui.py` is a Tkinter browser for searching, resetting, and deleting folder rows. It imports the database helpers from `main.py`.
- `hash_photos.py` reads image paths from `file_metadata`, computes SHA-256 hashes, and upserts results into `file_content_hashes`. It also uses the shared database and error-reporting helpers from `main.py`.
- `main.py` creates the core PostgreSQL tables. Metadata fields are derived from `FIELDS`; values are stored as text, with list/dict values serialized as JSON. Folder-related tables use foreign keys.
- `metadata_folders.json` and `metadata_errors.log` are local workflow files located beside `main.py`, regardless of the command's current working directory.

### Verify inventory and status behavior before changing it

The current `main.py` and `folders_ui.py` do not agree on pending-folder status: the schema allows `NULL` or `processed`, and inventory resets discovered rows to `NULL`, while the UI filters for and writes `pending`. The README also describes inventory/export behavior that differs from the current `main.py`. Before changing this workflow, verify the implementation across all three files and keep schema, CLI, UI, and README behavior aligned.

## Commands

Install the Python dependency:

```sh
python3 -m pip install -r requirements.txt
```

Set `PHOTOPROCESS_DB_PASSWORD` in the environment before running database-backed commands. Optional connection overrides are `PHOTOPROCESS_DB_HOST`, `PHOTOPROCESS_DB_PORT`, `PHOTOPROCESS_DB_NAME`, and `PHOTOPROCESS_DB_USER`.

Run the workflow and utilities from the repository root:

```sh
python3 main.py inventory /path/to/photo-library
python3 main.py export
python3 folders_ui.py
python3 hash_photos.py
```

ExifTool must be installed and available on `PATH` for export.

There is no configured build, lint, or automated test command, and no test suite or single-test selector is present in the repository.

## Codebase conventions

- Reuse `database_settings()`, `ensure_schema()`, and `report_error()` from `main.py` in database utilities rather than duplicating connection or error-handling logic.
- Use Psycopg SQL composition (`sql.Identifier`/`sql.SQL`) for dynamic SQL identifiers and parameter placeholders for values.
- Keep database writes transaction-scoped. Export replaces a folder's metadata and marks it processed together; preserve retry behavior by leaving failed folders unprocessed.
- Invoke ExifTool with a subprocess argument list, not through a shell. Keep filesystem paths as argument values.
- Report operational errors to stderr and the local error log; processing failures should also be recorded in `processing_errors` where the workflow currently does so.
- Keep generated manifests and logs anchored to `BASE` in `main.py`, not to the process working directory.
