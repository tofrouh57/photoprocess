
#!/usr/bin/env python3

import csv
import io
import json
import os
import subprocess
import sys
from pathlib import Path

# ---------- Configuration ----------

BASE = Path(__file__).resolve().parent
MANIFEST = BASE / "metadata_folders.json"
OUTPUT = BASE / "metadata_export.csv"
PROGRESS = BASE / "metadata_progress.json"
ERROR_LOG = BASE / "metadata_errors.log"

FIELDS = [
    "FilePath",
    "FileName",
    "Directory",
    "FileType",
    "MIMEType",
    "FileSize",

    # macOS filesystem dates
    "FileCreateDate",
    "FileModifyDate",
    "FileAccessDate",
    "FileInodeChangeDate",

    # Embedded photo/document metadata dates
    "DateTimeOriginal",
    "CreateDate",
    "ModifyDate",
    "MetadataDate",
    "DateAcquired",

    # Video/audio dates
    "MediaCreateDate",
    "MediaModifyDate",
    "TrackCreateDate",
    "TrackModifyDate",

    # Camera/image metadata
    "Make",
    "Model",
    "ImageWidth",
    "ImageHeight",
]

# -----------------------------------


def log_error(message):
    with ERROR_LOG.open("a", encoding="utf-8") as f:
        f.write(message.rstrip() + "\n")


def atomic_json_write(path, data):
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def inventory(root):
    """List folders without following symlinked directories."""
    root = Path(root).expanduser().resolve()

    if not root.is_dir():
        sys.exit(f"Error: folder does not exist: {root}")

    folders = []

    def onerror(error):
        message = f"INVENTORY WARNING: {error}"
        print(message, file=sys.stderr)
        log_error(message)

    for current, dirs, _files in os.walk(
        root, topdown=True, onerror=onerror, followlinks=False
    ):
        dirs[:] = sorted(
            d for d in dirs
            if not (Path(current) / d).is_symlink()
        )
        folders.append(current)

    folders.sort()
    atomic_json_write(MANIFEST, folders)

    # A fresh inventory starts a fresh export.
    PROGRESS.unlink(missing_ok=True)
    OUTPUT.unlink(missing_ok=True)
    ERROR_LOG.write_text("", encoding="utf-8")

    print(f"Inventory complete: {len(folders)} folders")
    print(f"Folder list: {MANIFEST}")
    print("Next: run the export command.")


def save_progress(index, offset):
    """Save the next folder index and committed CSV byte offset."""
    atomic_json_write(
        PROGRESS,
        {"index": index, "offset": offset}
    )


def initialise_csv():
    """Create CSV header and save the initial checkpoint."""
    with OUTPUT.open("wb") as raw:
        text_file = io.TextIOWrapper(
            raw, encoding="utf-8-sig", newline=""
        )
        writer = csv.writer(
            text_file,
            quoting=csv.QUOTE_ALL,
            lineterminator="\n"
        )
        writer.writerow(FIELDS)
        text_file.flush()
        os.fsync(raw.fileno())
        offset = raw.tell()
        text_file.detach()

    save_progress(0, offset)
    return offset


def export():
    """Process folders individually and resume from the last checkpoint."""
    if not MANIFEST.exists():
        sys.exit("Error: no inventory. Run inventory first.")

    try:
        folders = json.loads(
            MANIFEST.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        sys.exit(f"Cannot read inventory: {exc}")

    if not isinstance(folders, list):
        sys.exit("Error: inventory is not a valid folder list.")

    if PROGRESS.exists():
        try:
            state = json.loads(
                PROGRESS.read_text(encoding="utf-8")
            )
            index = int(state["index"])
            offset = int(state["offset"])
        except (OSError, json.JSONDecodeError, KeyError,
                TypeError, ValueError) as exc:
            sys.exit(f"Invalid progress file: {exc}")

        if not (0 <= index <= len(folders)) or offset < 0:
            sys.exit("Invalid progress values.")

        if not OUTPUT.exists():
            if offset != 0:
                sys.exit(
                    "CSV is missing but progress expects existing data."
                )
            index = 0
            offset = initialise_csv()
        else:
            size = OUTPUT.stat().st_size
            if offset > size:
                sys.exit(
                    "Progress offset exceeds CSV size. "
                    "Restore a matching CSV backup or restart safely."
                )

            # Discard any uncommitted partial output.
            with OUTPUT.open("r+b") as raw:
                raw.truncate(offset)
                raw.flush()
                os.fsync(raw.fileno())

    else:
        if OUTPUT.exists():
            sys.exit(
                f"{OUTPUT} exists but the progress file is missing. "
                "Rename the old CSV or restore its matching progress file."
            )

        index = 0
        offset = initialise_csv()

    if index == len(folders):
        print("Export is already complete.")
        print(f"CSV: {OUTPUT}")
        return

    print(f"Resuming at folder {index + 1} of {len(folders)}")
    print(f"CSV: {OUTPUT}")
    print(f"Errors: {ERROR_LOG}")

    try:
        for i in range(index, len(folders)):
            folder = folders[i]
            print(
                f"[{i + 1}/{len(folders)}] {folder}",
                flush=True
            )

            command = [
                "exiftool",
                "-json",
                "-charset",
                "filename=UTF8",
                *[f"-{field}" for field in FIELDS],
                folder,
            ]

            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
            except OSError as exc:
                message = f"ERROR: Cannot run ExifTool for {folder}: {exc}"
                print(message, file=sys.stderr)
                log_error(message)

                # Skip this folder and continue; the error is logged.
                save_progress(i + 1, offset)
                continue

            if result.returncode != 0:
                message = (
                    f"EXIFTOOL WARNING (exit {result.returncode}) "
                    f"for {folder}: "
                    f"{result.stderr.strip() or 'No details reported'}"
                )
                print(message, file=sys.stderr)
                log_error(message)

            try:
                records = json.loads(result.stdout or "[]")
                if not isinstance(records, list):
                    raise ValueError("Expected a JSON list")
            except (json.JSONDecodeError, ValueError) as exc:
                message = f"ERROR: Invalid JSON for {folder}: {exc}"
                print(message, file=sys.stderr)
                log_error(message)
                print("Stopping without advancing the checkpoint.")
                return

            # Append complete folder results. Byte offsets remain exact.
            with OUTPUT.open("ab") as raw:
                text_file = io.TextIOWrapper(
                    raw, encoding="utf-8", newline=""
                )
                writer = csv.writer(
                    text_file,
                    quoting=csv.QUOTE_ALL,
                    lineterminator="\n"
                )

                for record in records:
                    row = []

                    for field in FIELDS:
                        value = record.get(field, "")

                        if value is None:
                            value = ""
                        elif isinstance(value, (list, dict)):
                            value = json.dumps(
                                value, ensure_ascii=False
                            )

                        row.append(value)

                    writer.writerow(row)

                text_file.flush()
                os.fsync(raw.fileno())
                offset = raw.tell()
                text_file.detach()

            # Checkpoint only after CSV data has been flushed.
            save_progress(i + 1, offset)

    except KeyboardInterrupt:
        print("\nInterrupted. The last completed folder is saved.")
        print("Run the export command again to resume.")
        return

    print("Export complete.")
    print(f"CSV: {OUTPUT}")
    print(f"Error log: {ERROR_LOG}")


def main():
    if len(sys.argv) < 2:
        sys.exit(
            "Usage:\n"
            "  python3 ~/scan_metadata.py inventory /path/to/drive\n"
            "  python3 ~/scan_metadata.py export"
        )

    if sys.argv[1] == "inventory":
        if len(sys.argv) != 3:
            sys.exit("Specify the root folder to inventory.")
        inventory(sys.argv[2])

    elif sys.argv[1] == "export":
        if len(sys.argv) != 2:
            sys.exit("The export command takes no extra arguments.")
        export()

    else:
        sys.exit("Unknown command. Use inventory or export.")


if __name__ == "__main__":
    main()
