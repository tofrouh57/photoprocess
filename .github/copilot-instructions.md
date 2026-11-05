# Copilot Instructions for PhotoProcess

## Build, test, and lint

Project setup:

```sh
python3 -m pip install -r requirements.txt
```

Database-backed commands require a password in the same shell session:

```sh
export PHOTOPROCESS_DB_PASSWORD='your-database-password'
```

Optional overrides:

```sh
export PHOTOPROCESS_DB_HOST='192.168.1.160'
export PHOTOPROCESS_DB_PORT='5432'
export PHOTOPROCESS_DB_NAME='photoprocess_debian'
export PHOTOPROCESS_DB_USER='postgres'
```

Run the workflow from the repository root:

```sh
python3 main.py inventory /path/to/photo-library
python3 main.py export
python3 folders_ui.py
python3 hash_photos.py
```

ExifTool must be installed and available on `PATH` for `export`.

There is no configured lint command, no build step, and no automated test suite in this repository. There is no single-test selector to call; when adding tests, prefer the repository's standard Python test runner if one is introduced later.

## High-level architecture

This repository is a small PostgreSQL-backed photo processing workflow with three main moving parts:

- `main.py` owns the shared schema, database helpers, and CLI entry points. It creates the `folders`, `file_metadata`, `processing_errors`, and `corrupted_files` tables, and it defines `database_settings()`, `ensure_schema()`, and `report_error()` for reuse.
- `inventory` walks the target photo tree with `os.walk()` and stores discovered folder paths in the database. It is responsible for keeping folder state consistent with the UI and export pipeline.
- `export` selects pending folders (`status IS NULL`), runs ExifTool per folder, stores selected metadata in `file_metadata`, and marks the folder as processed in the same transaction as the metadata replacement.
- `folders_ui.py` is the Tkinter browser for inspecting and modifying folder rows. It must stay aligned with the database semantics in `main.py`; the README, UI, and schema all need to agree on what counts as pending/processed.
- `hash_photos.py` queries exported image rows from `file_metadata`, hashes files on disk, and upserts results into `file_content_hashes`.

Local workflow artifacts are anchored to the repository's `BASE` path rather than the current working directory:

- `metadata_folders.json`
- `metadata_errors.log`

## Key conventions

- Reuse the shared helpers from `main.py` instead of duplicating connection or error-handling logic in new scripts.
- Use Psycopg SQL composition (`sql.Identifier`, `sql.SQL`) for dynamic table/column names and parameterized values for user data.
- Keep writes transaction-scoped: a successful export must replace a folder's metadata and mark the folder as processed together; failed exports must leave the folder pending for retry.
- Treat `status IS NULL` as pending and `status = 'processed'` as complete. When changing this behavior, verify the schema, CLI, UI, and README together.
- Invoke ExifTool using a subprocess argument list, not a shell string, and keep filesystem paths as individual argument values.
- Report operational failures to stderr and to the repository's error log; processing failures should also be recorded in `processing_errors` and related table entries when applicable.
- Keep generated manifests and logs anchored to the project base directory in `main.py`, not the process working directory.
- Be careful with `NULL`/`processed` semantics, because the current workflow is sensitive to pending-state mismatches between the database schema, CLI, and UI.

## Repository-specific notes

- This is a small Python CLI project, not a web app or package library.
- Database configuration is environment-driven and intentionally not hard-coded in source control.
- The project assumes PostgreSQL and ExifTool are already installed and reachable in the execution environment.
