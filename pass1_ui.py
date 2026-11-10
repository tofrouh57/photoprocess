#!/usr/bin/env python3

import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import psycopg

from main import database_settings
from pass1 import ensure_schema, ensure_source_schema


SCRIPT_DIR = Path(__file__).resolve().parent
PASS1_SCRIPT = SCRIPT_DIR / "pass1.py"


class Pass1Browser:
    def __init__(self, root):
        self.root = root
        self.root.title("Pass 1 Library Browser")
        self.root.geometry("1100x760")
        self.library_by_name = {}
        self.node_data = {}
        self.output_queue = queue.Queue()
        self.process = None

        self.library_var = tk.StringVar()
        self.source_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Ready")

        self._build_ui()
        self.refresh_libraries()
        self.root.after(100, self._poll_process_output)

    def _build_ui(self):
        toolbar = ttk.Frame(self.root, padding=8)
        toolbar.pack(fill=tk.X)

        ttk.Label(toolbar, text="Library:").pack(side=tk.LEFT)
        self.library_combo = ttk.Combobox(
            toolbar,
            textvariable=self.library_var,
            state="readonly",
            width=28,
        )
        self.library_combo.pack(side=tk.LEFT, padx=(6, 8))
        self.library_combo.bind("<<ComboboxSelected>>", self._library_selected)
        ttk.Button(toolbar, text="Create library...", command=self.create_library).pack(
            side=tk.LEFT
        )
        ttk.Button(toolbar, text="Refresh", command=self.refresh_all).pack(
            side=tk.LEFT, padx=6
        )

        process_frame = ttk.LabelFrame(
            self.root, text="Process a source folder", padding=8
        )
        process_frame.pack(fill=tk.X, padx=8, pady=(0, 8))
        ttk.Label(process_frame, text="Source folder:").grid(
            row=0, column=0, sticky=tk.W
        )
        source_entry = ttk.Entry(process_frame, textvariable=self.source_var)
        source_entry.grid(row=0, column=1, sticky=tk.EW, padx=6)
        ttk.Button(
            process_frame, text="Browse...", command=self.choose_source
        ).grid(row=0, column=2)
        self.index_button = ttk.Button(
            process_frame,
            text="Index library",
            command=self.start_index,
        )
        self.index_button.grid(row=0, column=3, padx=(8, 0))
        self.process_button = ttk.Button(
            process_frame,
            text="Copy into selected library",
            command=self.start_copy,
        )
        self.process_button.grid(row=0, column=4, padx=(8, 0))
        process_frame.columnconfigure(1, weight=1)

        content = ttk.Panedwindow(self.root, orient=tk.VERTICAL)
        content.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        tree_frame = ttk.LabelFrame(
            content, text="Library and source records", padding=6
        )
        self.tree = ttk.Treeview(
            tree_frame,
            columns=("kind", "status", "details"),
            show="tree headings",
            selectmode="browse",
        )
        self.tree.heading("#0", text="Folder / file")
        self.tree.heading("kind", text="Type")
        self.tree.heading("status", text="Status")
        self.tree.heading("details", text="Details")
        self.tree.column("#0", width=420, minwidth=220)
        self.tree.column("kind", width=110, minwidth=90)
        self.tree.column("status", width=150, minwidth=120)
        self.tree.column("details", width=360, minwidth=180)
        tree_scroll = ttk.Scrollbar(
            tree_frame, orient=tk.VERTICAL, command=self.tree.yview
        )
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<<TreeviewOpen>>", self._tree_opened)
        content.add(tree_frame, weight=4)

        log_frame = ttk.LabelFrame(content, text="Process output", padding=6)
        self.log = tk.Text(log_frame, height=10, wrap=tk.WORD, state=tk.DISABLED)
        log_scroll = ttk.Scrollbar(
            log_frame, orient=tk.VERTICAL, command=self.log.yview
        )
        self.log.configure(yscrollcommand=log_scroll.set)
        self.log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        content.add(log_frame, weight=1)

        status = ttk.Label(
            self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W
        )
        status.pack(fill=tk.X, side=tk.BOTTOM)

    def _connect(self):
        return psycopg.connect(**database_settings(), autocommit=True)

    def _prepare_schema(self, conn):
        ensure_schema(conn)
        ensure_source_schema(conn)

    def refresh_libraries(self):
        try:
            with self._connect() as conn:
                self._prepare_schema(conn)
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT id, name, root_path
                        FROM pass1_libraries
                        ORDER BY name
                        """
                    )
                    rows = cursor.fetchall()
        except (psycopg.Error, RuntimeError, ValueError) as exc:
            messagebox.showerror(
                "Could not load libraries", str(exc), parent=self.root
            )
            return

        selected = self.library_var.get()
        self.library_by_name = {
            name: {"id": library_id, "root": root_path}
            for library_id, name, root_path in rows
        }
        names = list(self.library_by_name)
        self.library_combo.configure(values=names)
        if selected in self.library_by_name:
            self.library_var.set(selected)
        elif names:
            self.library_var.set(names[0])
        else:
            self.library_var.set("")
        self.refresh_tree()

    def refresh_all(self):
        self.refresh_libraries()

    def _library_selected(self, _event=None):
        self.refresh_tree()

    def refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        self.node_data.clear()
        name = self.library_var.get()
        library = self.library_by_name.get(name)
        if not library:
            self.status_var.set("Create or select a library.")
            return

        self._add_branch(
            "",
            "library",
            f"Library: {name}",
            "Library",
            library["root"],
            {"node_type": "library_root", "library_id": library["id"]},
        )
        self._add_branch(
            "",
            "sources",
            "Processed source folders",
            "Sources",
            "",
            {"node_type": "sources_root", "library_id": library["id"]},
        )
        self.status_var.set(f"Library root: {library['root']}")

    def _add_branch(self, parent, label, kind, status, details, data):
        iid = self.tree.insert(
            parent,
            tk.END,
            text=label,
            values=(kind, status, details),
            open=False,
        )
        self.node_data[iid] = data
        self.tree.insert(iid, tk.END, text="Loading...", values=("", "", ""))
        return iid

    def _tree_opened(self, _event=None):
        iid = self.tree.focus()
        data = self.node_data.get(iid)
        if not data or data.get("loaded"):
            return
        children = self.tree.get_children(iid)
        if len(children) != 1 or self.tree.item(children[0], "text") != "Loading...":
            return
        self.tree.delete(children[0])
        data["loaded"] = True
        try:
            with self._connect() as conn:
                self._prepare_schema(conn)
                node_type = data["node_type"]
                if node_type in {"library_root", "library_folder"}:
                    self._load_library_children(conn, iid, data)
                elif node_type in {"sources_root", "source_root", "source_folder"}:
                    self._load_source_children(conn, iid, data)
        except (psycopg.Error, RuntimeError, ValueError, OSError) as exc:
            data["loaded"] = False
            messagebox.showerror("Could not load records", str(exc), parent=self.root)

    @staticmethod
    def _direct_child(parent_path, child_path):
        if parent_path == ".":
            return child_path != "." and "/" not in child_path
        prefix = f"{parent_path}/"
        if not child_path.startswith(prefix):
            return False
        return "/" not in child_path[len(prefix) :]

    def _load_library_children(self, conn, parent_iid, data):
        library_id = data["library_id"]
        parent_path = data.get("relative_path", ".")
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, relative_path, status
                FROM pass1_folders
                WHERE library_id = %s
                ORDER BY relative_path
                """,
                (library_id,),
            )
            folders = cursor.fetchall()
            folder_id = data.get("folder_id")
            if folder_id is None:
                cursor.execute(
                    """
                    SELECT id FROM pass1_folders
                    WHERE library_id = %s AND relative_path = '.'
                    """,
                    (library_id,),
                )
                root_folder = cursor.fetchone()
                folder_id = root_folder[0] if root_folder else None
            files = []
            if folder_id is not None:
                cursor.execute(
                    """
                    SELECT relative_path, media_type, status,
                           content_sha256, error_message
                    FROM pass1_files
                    WHERE folder_id = %s
                    ORDER BY relative_path
                    """,
                    (folder_id,),
                )
                files = cursor.fetchall()

        for folder_id, relative_path, status in folders:
            if self._direct_child(parent_path, relative_path):
                label = Path(relative_path).name
                self._add_branch(
                    parent_iid,
                    label,
                    "Folder",
                    status,
                    relative_path,
                    {
                        "node_type": "library_folder",
                        "library_id": library_id,
                        "folder_id": folder_id,
                        "relative_path": relative_path,
                    },
                )

        for relative_path, media, status, sha256, error in files:
            self._add_file(
                parent_iid,
                relative_path,
                media or "file",
                status,
                f"SHA-256: {sha256 or 'unavailable'}"
                if not error
                else error,
            )

    def _load_source_children(self, conn, parent_iid, data):
        with conn.cursor() as cursor:
            if data["node_type"] == "sources_root":
                cursor.execute(
                    """
                    SELECT root_path, COUNT(*) AS folder_count
                    FROM pass1_source_folders
                    WHERE library_id = %s
                    GROUP BY root_path
                    ORDER BY root_path
                    """,
                    (data["library_id"],),
                )
                for root_path, folder_count in cursor.fetchall():
                    self._add_branch(
                        parent_iid,
                        root_path,
                        "Source root",
                        "",
                        f"{folder_count} folder(s)",
                        {
                            "node_type": "source_root",
                            "library_id": data["library_id"],
                            "root_path": root_path,
                        },
                    )
                return

            root_path = data["root_path"]
            parent_path = data.get("relative_path", ".")
            cursor.execute(
                """
                SELECT id, relative_path, status
                FROM pass1_source_folders
                WHERE library_id = %s AND root_path = %s
                ORDER BY relative_path
                """,
                (data["library_id"], root_path),
            )
            folders = cursor.fetchall()
            folder_id = data.get("folder_id")
            if folder_id is None:
                folder_id = next(
                    (row[0] for row in folders if row[1] == "."),
                    None,
                )
            files = []
            if folder_id is not None:
                cursor.execute(
                    """
                    SELECT relative_path, media_type, status, reference_file_id,
                           error_message, content_sha256
                    FROM pass1_source_files
                    WHERE folder_id = %s
                    ORDER BY relative_path
                    """,
                    (folder_id,),
                )
                files = cursor.fetchall()

        for child_id, relative_path, status in folders:
            if self._direct_child(parent_path, relative_path):
                self._add_branch(
                    parent_iid,
                    Path(relative_path).name,
                    "Source folder",
                    status,
                    relative_path,
                    {
                        "node_type": "source_folder",
                        "library_id": data["library_id"],
                        "root_path": root_path,
                        "folder_id": child_id,
                        "relative_path": relative_path,
                    },
                )

        for row in files:
            relative_path, media, status, reference_id, error, sha256 = row
            details = error or (
                f"Reference file #{reference_id}"
                if reference_id is not None
                else f"SHA-256: {sha256 or 'unavailable'}"
            )
            self._add_file(
                parent_iid,
                relative_path,
                media or "file",
                status,
                details,
            )

    def _add_file(self, parent, relative_path, kind, status, details):
        self.tree.insert(
            parent,
            tk.END,
            text=Path(relative_path).name or relative_path,
            values=(kind, status, details),
        )

    def create_library(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("Create library")
        dialog.transient(self.root)
        dialog.grab_set()

        name_var = tk.StringVar()
        root_var = tk.StringVar()
        ttk.Label(dialog, text="Library name:").grid(
            row=0, column=0, sticky=tk.W, padx=8, pady=8
        )
        ttk.Entry(dialog, textvariable=name_var, width=48).grid(
            row=0, column=1, columnspan=2, sticky=tk.EW, padx=8, pady=8
        )
        ttk.Label(dialog, text="Library root:").grid(
            row=1, column=0, sticky=tk.W, padx=8, pady=8
        )
        ttk.Entry(dialog, textvariable=root_var, width=48).grid(
            row=1, column=1, sticky=tk.EW, padx=8, pady=8
        )

        def browse_root():
            path = filedialog.askdirectory(parent=dialog, title="Select library root")
            if path:
                root_var.set(path)

        ttk.Button(
            dialog, text="Browse...", command=browse_root
        ).grid(row=1, column=2, padx=8, pady=8)

        def save():
            name = name_var.get().strip()
            path = Path(root_var.get()).expanduser()
            if not name:
                messagebox.showerror(
                    "Missing name", "Enter a library name.", parent=dialog
                )
                return
            if not path.is_dir():
                messagebox.showerror(
                    "Invalid root",
                    "Select an existing library root directory.",
                    parent=dialog,
                )
                return
            try:
                with self._connect() as conn:
                    self._prepare_schema(conn)
                    with conn.transaction():
                        with conn.cursor() as cursor:
                            cursor.execute(
                                """
                                INSERT INTO pass1_libraries (name, root_path)
                                VALUES (%s, %s)
                                ON CONFLICT DO NOTHING
                                """,
                                (name, str(path.resolve())),
                            )
                            cursor.execute(
                                """
                                SELECT root_path FROM pass1_libraries
                                WHERE name = %s
                                """,
                                (name,),
                            )
                            existing = cursor.fetchone()
                            if existing is None or existing[0] != str(path.resolve()):
                                raise ValueError(
                                    "That library name or root is already registered "
                                    "for a different library."
                                )
            except (psycopg.Error, RuntimeError, ValueError) as exc:
                messagebox.showerror(
                    "Could not create library", str(exc), parent=dialog
                )
                return
            dialog.destroy()
            self.refresh_libraries()
            self.library_var.set(name)
            self.refresh_tree()

        buttons = ttk.Frame(dialog)
        buttons.grid(row=2, column=0, columnspan=3, sticky=tk.E, padx=8, pady=8)
        ttk.Button(buttons, text="Create", command=save).pack(side=tk.LEFT, padx=4)
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side=tk.LEFT)
        dialog.columnconfigure(1, weight=1)

    def choose_source(self):
        path = filedialog.askdirectory(parent=self.root, title="Select source folder")
        if path:
            self.source_var.set(path)

    def start_copy(self):
        library = self.library_var.get()
        source = self.source_var.get().strip()
        if not library:
            messagebox.showerror(
                "No library selected", "Select or create a library first.", parent=self.root
            )
            return
        if not source or not Path(source).is_dir():
            messagebox.showerror(
                "Invalid source folder",
                "Select an existing source directory.",
                parent=self.root,
            )
            return
        self._start_command(
            [
                sys.executable,
                str(PASS1_SCRIPT),
                "copy-source",
                library,
                str(Path(source).expanduser().resolve()),
            ],
            f"Processing source folder into library {library}...",
        )

    def start_index(self):
        library = self.library_var.get()
        if not library:
            messagebox.showerror(
                "No library selected", "Select or create a library first.", parent=self.root
            )
            return
        self._start_command(
            [sys.executable, str(PASS1_SCRIPT), "index", library],
            f"Indexing library {library}...",
        )

    def _start_command(self, command, status):
        if self.process is not None:
            return

        try:
            self.process = subprocess.Popen(
                command,
                cwd=SCRIPT_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            messagebox.showerror("Could not start copy", str(exc), parent=self.root)
            return

        self.index_button.configure(state=tk.DISABLED)
        self.process_button.configure(state=tk.DISABLED)
        self.status_var.set(status)
        self._append_log(f"$ {subprocess.list2cmdline(command)}\n")
        threading.Thread(target=self._read_process_output, daemon=True).start()

    def _read_process_output(self):
        process = self.process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            self.output_queue.put(("line", line))
        self.output_queue.put(("exit", process.wait()))

    def _poll_process_output(self):
        try:
            while True:
                event, value = self.output_queue.get_nowait()
                if event == "line":
                    self._append_log(value)
                else:
                    self.process = None
                    self.index_button.configure(state=tk.NORMAL)
                    self.process_button.configure(state=tk.NORMAL)
                    if value == 0:
                        self.status_var.set("Copy process completed.")
                    else:
                        self.status_var.set(f"Copy process exited with status {value}.")
                        messagebox.showerror(
                            "Copy failed",
                            f"Pass 1 copy-source exited with status {value}. "
                            "See process output for details.",
                            parent=self.root,
                        )
                    self.refresh_libraries()
        except queue.Empty:
            pass
        self.root.after(100, self._poll_process_output)

    def _append_log(self, text):
        self.log.configure(state=tk.NORMAL)
        self.log.insert(tk.END, text)
        self.log.see(tk.END)
        self.log.configure(state=tk.DISABLED)


def main():
    root = tk.Tk()
    Pass1Browser(root)
    root.mainloop()


if __name__ == "__main__":
    main()
